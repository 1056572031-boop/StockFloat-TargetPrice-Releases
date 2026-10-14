# -*- coding: utf-8 -*-
"""tools/frame_rate_audit.py —— 全局帧率审计：逐个动效量真实刷新率。

为什么需要它
------------
`power_profile.py` 报的是**汇总**绘制次数，会掩盖局部退化：v1.4.12 那次
"绘制从 68 降到 56 帧/秒"看着是省了电，实际是把分时图末端呼吸灯从约 10 帧/秒
压到约 0.6 帧/秒 —— 汇总数字完全看不出来。所以帧率必须**逐个动效**量。

本工具把窗口里每一条独立的动画时钟分开测：

  price    价格滚动（`_anim_timer` 16ms，由行情变化驱动）
  dot      分时图末端呼吸灯（v1.4.14 起有 16ms 专属时钟；此前没有，实测 0.2 帧/秒）
  rainbow  目标价命中的流动彩虹（`_rainbow_timer` 33ms）
  flash    跌破整数关口的白闪（`_flash_timer` 120ms，相位交替）
  drop     跌幅档位红闪（`_flash_drop_timer` 350ms，相位交替）
  layout   分时图收展布局动画（复用 `_anim_timer`，180ms）

每个场景单独起一个子进程：动画状态在同一个 Qt 进程里会互相串（`_anim_timer`
被价格滚动和布局动画共用），分开才量得准。

怎么读结果
----------
  理论上限 = 1000 / 定时器间隔；实测 tick/s 低于它，说明定时器被 CPU 或
  事件循环拖住了；实测 paint/s 高于屏幕刷新率，说明有帧落在同一个 vsync 周期里、
  根本没被显示出来（白画）。
  `dot` 场景刻意让价格**完全不变**，此时唯一的绘制来源就是整窗帧 ——
  它就是呼吸灯的实际刷新率，也是 v1.4.12 那类"少画一帧"优化的检测器。

用法
----
    # 全部场景（真实窗口，能拿到屏幕真实刷新率）
    ./.venv/bin/python tools/frame_rate_audit.py --native

    # 只测呼吸灯与彩虹
    ./.venv/bin/python tools/frame_rate_audit.py --native --scenario dot,rainbow

    # 离屏（没有真实屏幕，refreshRate 取虚拟屏的 60Hz）
    QT_QPA_PLATFORM=offscreen ./.venv/bin/python tools/frame_rate_audit.py
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parent
PY = PROJ / ".venv" / "bin" / "python"

SCENARIOS = ["price", "dot", "rainbow", "flash", "drop", "layout"]

# 每个动效的"驱动定时器"与规格间隔（毫秒）。间隔值与代码同源，写在这里
# 是为了让报告能直接指出"配置上限是多少"。
SPEC = {
    "price":   ("_anim_timer", 16),
    "dot":     ("_dot_timer", 16),   # v1.4.14 之前没有专属定时器，实测只有 0.2 帧/秒
    "rainbow": ("_rainbow_timer", 33),
    "flash":   ("_flash_timer", 120),
    "drop":    ("_flash_drop_timer", 350),
    "layout":  ("_anim_timer", 16),
}


# ---------------------------------------------------------------- 子进程侧
def audit(scenario, seconds, native):
    """在当前进程里跑一个场景的帧率测量，打印 JSON 后硬退出。"""
    if not native:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import faulthandler
    faulthandler.enable()
    sys.path.insert(0, str(PROJ))
    os.chdir(str(PROJ))

    # 落盘物隔离：配置/日志/快照/socket/字典全部指向临时副本，
    # 绝不写用户真实数据（口径同 sf_constants 的测试隔离）。
    tmp = tempfile.mkdtemp(prefix="sf_fps_")
    real_cfg = os.path.expanduser(
        "~/Library/Application Support/StockFloatTargetPrice/config.json")
    if os.path.exists(real_cfg):
        import shutil
        shutil.copy2(real_cfg, os.path.join(tmp, "config.json"))
    for key, name in (("SF_CONFIG_PATH", "config.json"),
                      ("SF_LAST_QUOTE_PATH", "last_quote.json"),
                      ("SF_LOG_PATH", "debug.log"),
                      ("SF_SINGLE_SOCK", "stock_float.sock"),
                      ("SF_UNIVERSE_PATH", "securities.json")):
        os.environ[key] = os.path.join(tmp, name)

    import datetime
    from unittest import mock

    import stock_float_window as M
    import sf_quote
    import sf_ui_render as R
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    M.load_bundled_fonts()

    # ---- 屏幕真实刷新率：离屏插件给的是虚拟屏，只有 --native 才是真值 ----
    screens = [{"name": s.name(), "refresh_hz": float(s.refreshRate()),
                "dpr": float(s.devicePixelRatio())} for s in QGuiApplication.screens()]

    # ---- 时钟注入成盘中：price 场景需要行情线程持续拉取 ----
    trading = datetime.datetime(
        2026, 9, 22, 10, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    M._is_trading_time = lambda now=None: True
    M.market_status = lambda now=None: None

    # ---- 打桩回包：代码必须回显请求里的代码，否则会被"旧股票迟到回包"
    #      校验在动画分支之前静默丢弃（power_profile 曾因此漏测整个动画）。----
    import re as _re
    state = {"tick": 0}

    def _code_of(url):
        m = _re.search(r"[a-z]{2}\d{6}", url)
        return m.group(0) if m else "sh600519"

    def fake_get(url, **kw):
        code = _code_of(url)
        digits = code[2:]
        moving = (scenario == "price")
        i = state["tick"] % 40
        state["tick"] += 1
        p = 20.00 + (i * 0.01 if moving else 0.0)
        pre = 19.80
        chg, pct = p - pre, (p - pre) / pre * 100.0
        if "hq.sinajs.cn" in url:
            return 'var hq_str_%s="贵州茅台,%.2f,%.2f,%.2f,%.2f,%.2f";' % (code, pre, p, p, p, p)
        if "web.ifzq.gtimg.cn" in url:
            return ('{"data":{"%s":{"data":{"date":"20260922",'
                    '"data":["0930 %.2f 100 1000"]}}}}' % (code, p))
        return ('v_%s="1~贵州茅台~%s~%.2f~%.2f~%.2f~%.2f~%.2f~%.2f";'
                % (code, digits, p, pre, chg, pct, p, pre))

    patches = [
        mock.patch.object(sf_quote, "http_get", fake_get),
        mock.patch.object(M, "save_config", lambda *a, **k: True),
        mock.patch.object(M, "start_icon_worker", lambda: None),
        mock.patch.object(M.FloatWindow, "_setup_reopen_handler", lambda self: None),
    ]
    if not native:
        # 离屏没有真实 NSWindow：objc 调用会直接 SIGSEGV（见 power_profile 同注释）
        patches += [
            mock.patch.object(M.FloatWindow, "_force_top", lambda self: None),
            mock.patch.object(M.FloatWindow, "_inject_glass", lambda self: None),
            mock.patch.object(M.FloatWindow, "raise_", lambda self: None),
            mock.patch.object(M.FloatWindow, "activateWindow", lambda self: None),
        ]
    for p in patches:
        p.start()

    # ---- 计数器 ----
    # 注意：所有对 FloatWindow 方法的包装都必须发生在 `FloatWindow()` 之前 ——
    # `QTimer.timeout.connect(self._anim_tick)` 在 __init__ 里就把**绑定方法**存进
    # 连接表，构造之后再改类属性拦不到那些调用（第一版就踩了这个坑，
    # 结果 tick/s 全报 0，只有 paint 是准的，因为 paintEvent 由 Qt 动态查找）。
    counts = {}

    def bump(key, n=1):
        counts[key] = counts.get(key, 0) + n

    def wrap(owner, name, key):
        orig = getattr(owner, name, None)
        if orig is None:
            return
        def wrapper(*a, **k):
            bump(key)
            return orig(*a, **k)
        setattr(owner, name, wrapper)

    wrap(M.FloatWindow, "_anim_tick", "anim_tick")
    wrap(M.FloatWindow, "_flash_tick", "flash_tick")
    wrap(M.FloatWindow, "_drop_flash_tick", "drop_tick")
    wrap(M.FloatWindow, "_rainbow_tick", "rainbow_tick")
    wrap(M.FloatWindow, "_poll", "ui_poll")
    wrap(M.FloatWindow, "_fetch_quote", "fetch_quote")

    paint = {"n": 0, "full": 0, "partial": 0}
    orig_paint = M.RenderMixin.paintEvent
    from sf_ui_render import is_price_partial_redraw

    def paint_probe(self, e):
        paint["n"] += 1
        # 局部帧判据必须与 paintEvent 生产路由完全一致：按本次绘制事件的真实
        # 损坏区比对脏区，而不是只看 _partial_price 是否置位。v1.4.15 合并同频
        # 重复帧后动画帧可能被跳过，脏区会悬挂到下一次整窗重绘上，按标志位
        # 分类会把整窗帧误记成局部帧（实测把 60/秒 误报成 7.2/秒）。
        partial = getattr(self, "_partial_price", None)
        r = e.rect()
        hit = bool(partial) and is_price_partial_redraw(
            (r.x(), r.y(), r.width(), r.height()), partial)
        bump("paint")
        paint["partial" if hit else "full"] += 1
        return orig_paint(self, e)

    M.RenderMixin.paintEvent = paint_probe

    # QWidget.update() 调用次数（整窗 vs 带矩形）
    ups = {"full": 0, "partial": 0}
    orig_update = M.FloatWindow.update

    def update_probe(self, *a):
        ups["partial" if a else "full"] += 1
        return orig_update(self, *a)

    M.FloatWindow.update = update_probe

    win = M.FloatWindow()
    win.show()
    app.processEvents()

    # ---- 占空比采样 ----
    # 只看"整个场景平均每秒画几帧"会把短动画误判成低帧率：布局动画只有 180ms，
    # 每 400ms 触发一次，平均 30 帧/秒其实**动画进行中是 62 帧/秒**。
    # 所以每 10ms 采一次"此刻这条动画是否在跑"，最后按"运行期间"折算真实帧率。
    SAMPLE_MS = 10
    active_ms = {"_anim_timer": 0.0, "_flash_timer": 0.0,
                 "_flash_drop_timer": 0.0, "_rainbow_timer": 0.0,
                 "_dot_timer": 0.0, "chart_visible": 0.0}

    def sample():
        for name in active_ms:
            t = getattr(win, name, None)
            try:
                if t is not None and t.isActive():
                    active_ms[name] += SAMPLE_MS
            except Exception:
                pass

    sampler = QTimer()
    sampler.timeout.connect(sample)
    sampler.start(SAMPLE_MS)

    # ---- 场景驱动：把对应动画持续"续命"到整个测量窗口 ----
    def drive():
        try:
            if scenario == "rainbow":
                win._start_rainbow()
            elif scenario == "flash":
                win._start_price_flash()
            elif scenario == "drop":
                win._start_drop_flash()
            elif scenario == "layout":
                win._toggle_chart()          # 180ms 收展动画，每 0.4s 反向一次
            elif scenario == "dot":
                # 呼吸灯不需要触发：价格恒定不变，唯一的绘制来源就是整窗帧
                pass
        except Exception as exc:
            bump("drive_error")
            counts.setdefault("errors", []).append(repr(exc))

    interval_ms = {"rainbow": 5000, "flash": 700, "drop": 800,
                   "layout": 400, "price": 1000, "dot": 1000}[scenario]
    driver = QTimer()
    driver.timeout.connect(drive)
    driver.start(interval_ms)
    drive()

    import time as _t
    wall0 = _t.monotonic()

    def finish():
        wall = _t.monotonic() - wall0
        out = {
            "scenario": scenario,
            "native": native,
            "seconds": round(wall, 2),
            "screens": screens,
            "counts": {k: v for k, v in counts.items() if k != "errors"},
            "errors": counts.get("errors", []),
            "paint": dict(paint),
            "update_calls": dict(ups),
            "timers": {
                name: (owner.interval() if (owner := getattr(win, name, None)) else None)
                for name in ("_anim_timer", "_flash_timer",
                             "_flash_drop_timer", "_rainbow_timer")
            },
            "per_second": {
                k: round(v / wall, 1) for k, v in counts.items() if k != "errors"
            },
            "paint_per_second": round(paint["n"] / wall, 1),
            "full_paint_per_second": round(paint["full"] / wall, 1),
            "active_ms": {k: round(v, 1) for k, v in active_ms.items()},
            "duty_pct": {k: round(v / (wall * 1000.0) * 100.0, 1)
                         for k, v in active_ms.items()},
            "tmp": tmp,
        }
        print(json.dumps(out, ensure_ascii=False))
        sys.stdout.flush()
        os._exit(0)

    QTimer.singleShot(int(seconds * 1000), finish)
    app.exec()
    finish()


# ---------------------------------------------------------------- 父进程侧
def run_one(scenario, args):
    cmd = [str(PY), str(HERE / "frame_rate_audit.py"), "--child",
           "--scenario", scenario, "--seconds", str(args.seconds)]
    if args.native:
        cmd.append("--native")
    env = dict(os.environ)
    if args.native:
        env.pop("QT_QPA_PLATFORM", None)
    else:
        env["QT_QPA_PLATFORM"] = "offscreen"
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(PROJ))
    line = p.stdout.strip().splitlines()
    if not line:
        return {"scenario": scenario, "error": (p.stderr or "无输出")[-400:]}
    try:
        return json.loads(line[-1])
    except Exception:
        return {"scenario": scenario, "error": "JSON 解析失败"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=",".join(SCENARIOS),
                    help="逗号分隔：price,dot,rainbow,flash,drop,layout")
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--native", action="store_true",
                    help="真实窗口模式（能拿到屏幕真实刷新率；桌面会短暂出现悬浮窗）")
    ap.add_argument("--child", action="store_true", help=__doc__)
    args = ap.parse_args()

    if args.child:
        audit(args.scenario, args.seconds, args.native)
        return

    scen = [s.strip() for s in args.scenario.split(",") if s.strip()]
    rows = [run_one(s, args) for s in scen]

    hz = None
    for r in rows:
        for s in (r.get("screens") or []):
            if s.get("refresh_hz"):
                hz = s["refresh_hz"]
                break
        if hz:
            break
    print("\n屏幕刷新率：%s Hz%s" % (
        hz if hz else "未知",
        "" if args.native else "（离屏虚拟屏，加 --native 取真值）"))
    print("场景      驱动定时器            配置    理论    占空比   平均paint  运行期帧率  判定")
    print("-" * 108)
    KEY = {"price": "anim_tick", "layout": "anim_tick", "rainbow": "rainbow_tick",
           "flash": "flash_tick", "drop": "drop_tick", "dot": "paint"}
    for r in rows:
        if "error" in r:
            print("%-10s 子进程失败：%s" % (r["scenario"], r["error"]))
            continue
        timer, spec_ms = SPEC[r["scenario"]]
        cfg = r["timers"].get(timer) if timer in r["timers"] else spec_ms
        cfg = cfg or spec_ms
        ceiling = (1000.0 / cfg) if cfg else None
        duty = (r.get("duty_pct") or {}).get(timer, 0.0)
        paint_avg = r["paint_per_second"]
        # 运行期帧率 = 平均 paint / 占空比（动画在跑的那段时间里每秒画几帧）
        if r["scenario"] == "dot":
            # 呼吸灯的可见刷新只发生在整窗帧里，所以取整窗重绘次数为准
            running = r["full_paint_per_second"]
        elif duty > 1:
            running = paint_avg / (duty / 100.0)
        else:
            running = paint_avg
        target = round(hz) if hz else 60
        if running >= target - 1:
            verdict = "达 %d 帧" % target
        elif r["scenario"] in ("flash", "drop"):
            verdict = "相位交替，规格即 %.0f 次/秒" % (ceiling or running)
        else:
            verdict = "!! 仅 %.1f 帧" % running
        print("%-10s %-20s %-6s %-6s %-7s %-11s %-10s %s" % (
            r["scenario"], timer,
            ("%sms" % cfg) if cfg else "—",
            ("%.0f/s" % ceiling) if ceiling else "—",
            "%.0f%%" % duty, "%.1f/s" % paint_avg, "%.1f/s" % running, verdict))
        if r.get("errors"):
            print("           驱动异常：%s" % r["errors"][:2])
    print("\n注 1：dot（分时末端呼吸灯）的可见刷新只发生在整窗帧里"
          "\n      （价格动画的局部脏区按设计绝不侵入图表区），所以它的实际帧率 ="
          "\n      整窗重绘次数，而不是 `_dot_timer` 的触发次数。"
          "\n      v1.4.13 及以前它没有专属时钟（实测 0.2 帧/秒，价格一停就冻住），"
          "v1.4.14 起补了 16ms 主画钟。")
    print("注 2：flash/drop 是相位交替（亮/灭），其「帧率」就是闪烁频率，"
          "规格 120ms / 350ms 交替，改成 60 帧会变成频闪，不属于本项要达标的对象。")


if __name__ == "__main__":
    main()
