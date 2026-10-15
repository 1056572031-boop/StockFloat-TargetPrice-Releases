# -*- coding: utf-8 -*-
"""冷启动压测：连续多次冷启动，量化「启动耗时 / 首帧稳定时间 / 是否有中间态变形」。

用法
----
    # 测当前工程（连续 7 次冷启动）
    ./.venv/bin/python tools/coldstart_bench.py --src . --runs 7

    # 测某个快照目录（做 before/after 对比时用）
    ./.venv/bin/python tools/coldstart_bench.py --src /tmp/cs_before --runs 7 --label v1.3.44

    # 汇总对比两份结果
    ./.venv/bin/python tools/coldstart_bench.py --compare a.json b.json

为什么必须子进程
----------------
「冷启动」只有在**全新解释器**里才成立：同一进程内跑第二遍，模块已在
sys.modules、字体度量缓存已热、系统页缓存已命中，测出来的是热启动。
所以每一次冷启动都 fork 一个子进程单独跑，父进程只负责计时与汇总。

每个子进程输出一行 JSON，字段含义：
    wall            父进程测到的子进程总墙钟（含解释器启动），ms
    t_import        到 `import stock_float_window` 完成
    t_qapp          到 QApplication 构造完成
    t_fonts         到 load_bundled_fonts 完成
    t_ctor          到 FloatWindow() 构造完成
    t_show          到 win.show() 返回
    t_first_frame   到第一帧真实绘制完成（grab 拿到内容）
    t_stable        到「连续 3 帧内容完全一致」——即界面稳定，不再有任何变化
    sizes_ctor      构造期间的窗口尺寸变化序列（去连续重复）
    sizes_show      show() 之后到稳定的窗口尺寸变化序列
    frames_until_stable  到稳定为止画了几帧
    first_ink       首帧非透明像素占比（用来判断有没有白屏/空屏）
    stable_ink      稳定后非透明像素占比
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time


HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
PY = os.path.join(PROJ, ".venv", "bin", "python")


# ---------------------------------------------------------------- 子进程侧
def run_one(src, out_path, frames=40, stable_need=3):
    """在**本进程**里完成一次完整冷启动并测量。由父进程 fork 出来，只跑一次。

    结果写进 out_path 后直接 os._exit(0)：离屏插件下窗口析构会 SIGSEGV，
    但那发生在测量之后，跳过析构即可拿到完整数据。
    """
    import time as _t

    T0 = _t.perf_counter()
    marks = {}

    def mark(k):
        marks[k] = (_t.perf_counter() - T0) * 1000.0

    src = os.path.abspath(src)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, src)
    os.chdir(src)

    import stock_float_window as M          # noqa: E402
    mark("t_import")

    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
    from PyQt6.QtWidgets import QApplication                 # noqa: E402
    import sf_quote                                          # noqa: E402

    # Keep the application alive through font registration and every captured frame.
    app = QApplication.instance() or QApplication([])
    if not isinstance(app, QApplication):
        raise RuntimeError("Cold-start measurement requires a QApplication")
    app.setQuitOnLastWindowClosed(False)
    mark("t_qapp")

    M.load_bundled_fonts()
    mark("t_fonts")

    # ---- 隔离：不碰网络、不碰磁盘配置、不起图标线程 ----
    def _fake_http_get(url, **kw):
        if "hq.sinajs.cn" in url:
            return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
        if "/appstock/app/minute/query?" in url:
            return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                    '"data":["0930 1510.0 100 1000", "1000 1511.0 200 2000"]}}}}')
        return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'

    from unittest.mock import patch
    patches = [
        patch.object(M, "load_config", return_value=("sh600519", 100, 100, 224, "glass")),
        patch.object(M, "save_config", lambda *a, **k: None),
        patch.object(sf_quote, "http_get", _fake_http_get),
        patch.object(M, "start_icon_worker", lambda: None),
        patch.object(M.FloatWindow, "_force_top", lambda self: None),
        patch.object(M.FloatWindow, "_setup_reopen_handler", lambda self: None),
        # offscreen 插件不支持 raise()/activateWindow，会直接终止进程
        patch.object(M.FloatWindow, "raise_", lambda self: None),
        patch.object(M.FloatWindow, "activateWindow", lambda self: None),
    ]
    for p in patches:
        p.start()

    # ---- 几何变更记录：首帧变形必然留下痕迹 ----
    geo = []

    def _wrap(name, fn):
        def wrapper(self, *a, **k):
            before = (self.width(), self.height())
            r = fn(self, *a, **k)
            geo.append((name, before, (self.width(), self.height())))
            return r
        return wrapper

    for meth in ("resize", "move", "setMinimumHeight", "setMaximumHeight",
                 "setFixedSize", "setGeometry", "setFixedWidth", "setFixedHeight"):
        fn = getattr(M.FloatWindow, meth, None)
        if fn is not None:
            setattr(M.FloatWindow, meth, _wrap(meth, fn))

    win = M.FloatWindow()
    mark("t_ctor")
    # 走一遍真实状态序列（含每次调用**前后**两个尺寸），才能看出有没有被
    # min/max 钳出来的第三个尺寸：
    #   640×480      Qt 给顶层窗口的默认尺寸（构造起点，不可控、也不必控）
    #   184×480      setFixedWidth 之后、还没设高度（此时窗口尚未露出）
    #   184×<final>  最终尺寸
    # 除这三者之外的任何尺寸（典型是 184×420 —— maximumHeight 把 480 钳出来的
    # 产物）都是"多出来的中间态"，一旦被系统画一次就是用户看到的变形。
    seq = []
    for _n, b, a in geo:
        for s in (b, a):
            if not seq or seq[-1] != list(s):
                seq.append(list(s))
    final = (win.width(), win.height())
    allowed = {(640, 480), (184, 480), final}
    odd_sizes = [s for s in seq if tuple(s) not in allowed]
    sizes_ctor = seq

    # 停掉后台循环，避免测量期间不断有行情进来导致"永不稳定"
    try:
        win.running = False
        win._wake.set()
    except Exception:
        pass
    for attr in ("_timer", "_quote_timer", "_anim_timer", "_poll_timer"):
        t = getattr(win, attr, None)
        try:
            if t is not None:
                t.stop()
        except Exception:
            pass

    geo.clear()
    try:
        win.show()
    except Exception as e:                     # pragma: no cover - offscreen 兜底
        print("WARN show(): %r" % (e,), file=sys.stderr)
    mark("t_show")

    def _frame(i):
        # 注意：离屏插件下 app.processEvents() 会 SIGSEGV，抓帧改用 grab() 强制重绘
        img = win.grab().toImage()
        if img.isNull():
            return None, None, 0.0
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        h = hashlib.md5(bytes(ba.data())).hexdigest()
        return (img.width(), img.height()), h, _ink_ratio(img)

    first = None
    prev_h = None
    same = 0
    stable_at = None
    n_frame = 0
    first_ink = 0.0
    stable_ink = 0.0
    sizes_show = []

    for i in range(frames):
        size, h, ink = _frame(i)
        if size is None:
            continue
        n_frame += 1
        if first is None:
            first = size
            first_ink = ink
            mark("t_first_frame")
        if not sizes_show or sizes_show[-1] != size:
            sizes_show.append(size)
        if h == prev_h:
            same += 1
        else:
            same = 1
        prev_h = h
        if same >= stable_need and stable_at is None:
            stable_at = i
            stable_ink = ink
            mark("t_stable")
            break
        _t.sleep(0.016)

    if stable_at is None:
        mark("t_stable")

    out = dict(marks)
    out.update({
        "sizes_ctor": sizes_ctor,
        "odd_sizes": odd_sizes,
        "sizes_show": sizes_show,
        "frames_until_stable": n_frame,
        "first_ink": round(first_ink, 4),
        "stable_ink": round(stable_ink, 4),
        "final": [win.width(), win.height()],
    })
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)          # 跳过窗口析构，规避离屏插件 SIGSEGV


def _dedup(seq):
    out = []
    for s in seq:
        if not out or out[-1] != s:
            out.append(list(s) if isinstance(s, tuple) else s)
    return out


def _ink_ratio(img):
    """非透明像素占比——用来判断有没有白屏/空屏。"""
    from PyQt6.QtGui import QImage
    im = img.convertToFormat(QImage.Format.Format_ARGB32)
    w, hgt = im.width(), im.height()
    if w <= 0 or hgt <= 0:
        return 0.0
    # 采样而非逐像素：横向取约 120 个采样点已足够判断空屏，全量扫描太慢
    step = max(1, w // 120)
    tot = 0
    ink = 0
    for y in range(0, hgt, step):
        for x in range(0, w, step):
            tot += 1
            if (im.pixelColor(x, y).alpha() if hasattr(im, "pixelColor") else 255) > 20:
                ink += 1
    return ink / max(tot, 1)


# ---------------------------------------------------------------- 统计
def _stat(vals):
    vals = sorted(vals)
    n = len(vals)
    if n == 0:
        return {}
    return {
        "min": round(vals[0], 1),
        "mean": round(sum(vals) / n, 1),
        "p95": round(vals[min(n - 1, int(n * 0.95))], 1),
        "max": round(vals[-1], 1),
    }


def _fmt_table(title, rows):
    print("\n" + title)
    print("  %-22s %8s %8s %8s %8s" % ("阶段(ms)", "min", "mean", "p95", "max"))
    for k, name in rows:
        s = _stat([r[k] for r in RUNS if isinstance(r.get(k), (int, float))])
        if s:
            print("  %-22s %8.1f %8.1f %8.1f %8.1f"
                  % (name, s["min"], s["mean"], s["p95"], s["max"]))


RUNS = []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=".")
    ap.add_argument("--runs", type=int, default=7)
    ap.add_argument("--label", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--one-run", action="store_true")
    ap.add_argument("--no-cache", action="store_true",
                    help="每次冷启动前删掉首屏快照 last_quote.json（模拟首次安装）")
    ap.add_argument("--compare", nargs=2, default=None)
    a = ap.parse_args()

    if a.one_run:
        run_one(a.src, a.json or os.path.join("/tmp", "cs_one_%d.json" % os.getpid()))
        return 0

    if a.compare:
        return _compare(a.compare[0], a.compare[1])

    if not os.path.isfile(PY):
        print("找不到工程 venv 解释器: %s" % PY, file=sys.stderr)
        return 2

    src = os.path.abspath(a.src)
    print("冷启动压测：src=%s  runs=%d" % (src, a.runs))
    cache_path = None
    if a.no_cache:
        sys.path.insert(0, src)
        try:
            import sf_constants as _c
            cache_path = _c.LAST_QUOTE_PATH
        except Exception:
            cache_path = None

    runs = []
    for i in range(a.runs):
        if cache_path and os.path.exists(cache_path):
            try:
                os.remove(cache_path)
            except OSError:
                pass
        t0 = time.perf_counter()
        tmp = os.path.join("/tmp", "cs_run_%d_%d.json" % (os.getpid(), i))
        if os.path.exists(tmp):
            os.remove(tmp)
        process = subprocess.run([PY, os.path.abspath(__file__), "--one-run", "--src", src,
                        "--json", tmp],
                       capture_output=True, text=True, timeout=180)
        wall = (time.perf_counter() - t0) * 1000.0
        if process.returncode:
            print("  第 %d 次子进程退出 %s: %s" %
                  (i + 1, process.returncode, process.stderr[-1200:]), file=sys.stderr)
        data = None
        if os.path.exists(tmp):
            try:
                data = json.load(open(tmp, encoding="utf-8"))
            except Exception:
                data = None
            try:
                os.remove(tmp)
            except OSError:
                pass
        if data is None:
            print("  第 %d 次失败（无结果文件）" % (i + 1), file=sys.stderr)
            continue
        data["wall"] = round(wall, 1)
        runs.append(data)
        print("  第 %d 次：首帧 %.1f ms，稳定 %.1f ms，尺寸变化 %s"
              % (i + 1, data.get("t_first_frame", -1), data.get("t_stable", -1),
                 data.get("sizes_show")))

    if not runs:
        print("全部失败", file=sys.stderr)
        return 1

    global RUNS
    RUNS = runs
    print("\n===== %s  共 %d 次 =====" % (a.label or src, len(runs)))
    _fmt_table("【耗时明细】", [
        ("wall", "子进程总墙钟"),
        ("t_import", "→ import 主模块"),
        ("t_qapp", "→ QApplication"),
        ("t_fonts", "→ 字体注册"),
        ("t_ctor", "→ 主窗构造完成"),
        ("t_show", "→ show()"),
        ("t_first_frame", "→ 首帧绘制完成"),
        ("t_stable", "→ 界面稳定"),
    ])

    print("\n【窗口尺寸中间态】")
    bad = 0
    for i, r in enumerate(runs):
        sc, ss = r.get("sizes_ctor", []), r.get("sizes_show", [])
        odd = r.get("odd_sizes", [])
        flag = ""
        if odd or len(ss) > 1:
            flag = "  <== 异常尺寸 %s" % (odd,)
            bad += 1
        print("  第 %d 次：构造期 %s ；显示后 %s%s"
              % (i + 1, sc or "无", ss or "无", flag))
    print("  结论：%d/%d 次出现异常中间尺寸（如 184×420 钳制态）" % (bad, len(runs)))

    print("\n【首屏是否为空】首帧非透明占比 %.3f / 稳定后 %.3f"
          % (sum(r["first_ink"] for r in runs) / len(runs),
             sum(r["stable_ink"] for r in runs) / len(runs)))

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"label": a.label, "src": src, "runs": runs}, f,
                      ensure_ascii=False, indent=2)
        print("\n明细已写入 %s" % a.json)
    return 0


def _compare(pa, pb):
    A = json.load(open(pa, encoding="utf-8"))
    B = json.load(open(pb, encoding="utf-8"))
    ra, rb = A["runs"], B["runs"]
    keys = [("wall", "子进程总墙钟"), ("t_import", "→ import 主模块"),
            ("t_qapp", "→ QApplication"), ("t_fonts", "→ 字体注册"),
            ("t_ctor", "→ 主窗构造完成"), ("t_show", "→ show()"),
            ("t_first_frame", "→ 首帧绘制完成"), ("t_stable", "→ 界面稳定")]

    def mean(rs, k):
        v = [r[k] for r in rs if isinstance(r.get(k), (int, float))]
        return sum(v) / len(v) if v else 0.0

    print("冷启动 before / after 对比（均值，ms）")
    print("  %-22s %10s %10s %10s" % ("阶段", A.get("label", "before"),
                                      B.get("label", "after"), "变化"))
    for k, name in keys:
        ma, mb = mean(ra, k), mean(rb, k)
        print("  %-22s %10.1f %10.1f %+10.1f" % (name, ma, mb, mb - ma))

    def jumps(rs):
        return sum(1 for r in rs
                   if r.get("odd_sizes") or len(r.get("sizes_show", [])) > 1)

    print("\n  异常中间尺寸次数：before %d/%d  →  after %d/%d"
          % (jumps(ra), len(ra), jumps(rb), len(rb)))
    print("  首帧非透明占比：before %.3f  →  after %.3f"
          % (mean(ra, "first_ink"), mean(rb, "first_ink")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
