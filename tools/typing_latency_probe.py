# -*- coding: utf-8 -*-
"""tools/typing_latency_probe.py —— 工具窗（编辑面板/目标价面板）打字链路延迟。

为什么单独一个工具
------------------
指针跟手性用 tools/input_latency_probe.py（主窗走 RenderMixin.paintEvent）。
但**打字发生在工具窗上**：工具窗自绘不走主窗那条路径，主窗那套包装对它是瞎的；
而把事件过滤器与指针探针的类级包装放进同一个进程实测会段错误（同一段打字代码
单独跑完全正常 → 是两者组合的问题，不是产品缺陷）。与其在指针探针里硬撑，
这里独立一份，只干一件事。

量什么
------
  key_to_paint_ms  按键进入输入框 → 该输入框真的重绘（QEvent.Paint）
  keys_delivered   防呆：投递 24 键，过滤器至少看到 24 次按下；
                   并且断言输入框内容真的变了 —— 否则全部数字都是空转
  force_top        打字期间置顶复核被调了几次、合计占住主线程多久
                   （面板开着时这条每 250ms 一次，是"打字黏"的头号嫌疑）

隔离与代价：配置/日志/socket 全部指向临时副本，不动用户真实数据；会真在桌面上
打开编辑面板（原生窗口必须真窗口才量得准）。

用法
----
    ./.venv/bin/python tools/typing_latency_probe.py                 # 编辑面板
    ./.venv/bin/python tools/typing_latency_probe.py --target-panel  # 目标价面板
    ./.venv/bin/python tools/typing_latency_probe.py --keys 40 --json
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
PY = os.path.join(PROJ, ".venv", "bin", "python")
KEYMAP = [("6", "Key_6"), ("0", "Key_0"), ("7", "Key_7"),
          ("5", "Key_5"), ("1", "Key_1"), ("9", "Key_9")]


def measure(keys, use_panel, mode):
    import faulthandler
    faulthandler.enable()
    sys.path.insert(0, PROJ)
    os.chdir(PROJ)

    tmp = tempfile.mkdtemp(prefix="sf_type_")
    real_cfg = os.path.expanduser(
        "~/Library/Application Support/StockFloatTargetPrice/config.json")
    if os.path.exists(real_cfg):
        shutil.copy2(real_cfg, os.path.join(tmp, "config.json"))
    os.environ["SF_CONFIG_PATH"] = os.path.join(tmp, "config.json")
    os.environ["SF_LAST_QUOTE_PATH"] = os.path.join(tmp, "last_quote.json")
    os.environ["SF_LOG_PATH"] = os.path.join(tmp, "debug.log")
    os.environ["SF_SINGLE_SOCK"] = os.path.join(tmp, "stock_float.sock")
    os.environ["SF_UNIVERSE_PATH"] = os.path.join(tmp, "securities.json")

    import stock_float_window as M
    from PyQt6.QtCore import QEvent, QObject, Qt
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtWidgets import QApplication, QLineEdit

    app = QApplication.instance() or QApplication([])
    M.load_bundled_fonts()
    import datetime
    anchor = datetime.datetime(2026, 9, 22, 10, 30 if mode == "trading" else 16, 30,
                               tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    trading = anchor.hour in (10, 14)
    M.market_status = lambda now=None: (None if trading else ("已收盘", M.FG_DIM))
    M._is_trading_time = lambda now=None: trading

    ft = {"n": 0, "ns": 0, "max": 0}
    orig_ft = M.FloatWindow._force_top

    def force_top(self):
        r0 = time.perf_counter_ns()
        try:
            return orig_ft(self)
        finally:
            dt = time.perf_counter_ns() - r0
            ft["n"] += 1
            ft["ns"] += dt
            # 复核 F5：以前这里把**累计总耗时**当"单次最差"报（我据此说过
            # "最差一次 12.5ms"，其实是 20 次的合计）。单次分布必须单独记。
            if dt > ft["max"]:
                ft["max"] = dt

    M.FloatWindow._force_top = force_top

    def pump(ms):
        t0 = time.monotonic()
        while (time.monotonic() - t0) * 1000 < ms:
            app.processEvents()
            time.sleep(0.002)
        app.processEvents()

    win = M.FloatWindow()
    screen = app.primaryScreen().geometry()
    win.move(screen.right() - win.width() - 320, screen.bottom() - win.height() - 60)
    win.show()
    pump(900)

    if use_panel:
        win._open_target_panel()
    else:
        win.open_editor()
    pump(1200)
    tool = getattr(win, "_editor", None) if not use_panel else getattr(win, "_target_panel", None)
    out = {"target": "target_panel" if use_panel else "editor",
           "mode": mode, "trading_clock": trading}
    if tool is None or not tool.isVisible():
        out["error"] = "工具窗没打开"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.stdout.flush()
        os._exit(1)

    le = tool.findChild(QLineEdit)
    if le is None:
        out["error"] = "工具窗里没有 QLineEdit（该窗不需要打字，本工具不适用）"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.stdout.flush()
        os._exit(1)

    st = {"keys": 0, "text_keys": 0, "paints": 0, "k2p": [], "t_key": None}

    class Rec(QObject):
        def eventFilter(self, obj, e):
            if obj is le:
                ty = e.type()
                if ty == QEvent.Type.KeyPress:
                    st["keys"] += 1
                    if e.text():
                        st["text_keys"] += 1
                    st["t_key"] = time.monotonic_ns()
                elif ty == QEvent.Type.Paint:
                    st["paints"] += 1
                    if st["t_key"] is not None:
                        d = (time.monotonic_ns() - st["t_key"]) / 1e6
                        if d <= 250.0:
                            st["k2p"].append(round(d, 2))
                        st["t_key"] = None
            return False

    rec = Rec()          # 必须持有引用：过滤器没有引用计数，被 GC 后控件仍会去调它
    le.installEventFilter(rec)
    le.setFocus(Qt.FocusReason.OtherFocusReason)
    le.selectAll()
    pump(250)
    before = le.text()
    mod = Qt.KeyboardModifier.NoModifier
    t0 = time.monotonic()
    for i in range(keys):
        ch, kn = KEYMAP[i % len(KEYMAP)]
        app.postEvent(le, QKeyEvent(QEvent.Type.KeyPress, getattr(Qt.Key, kn), mod, ch))
        app.postEvent(le, QKeyEvent(QEvent.Type.KeyRelease, getattr(Qt.Key, kn), mod, ch))
        pump(90)         # ≈11 键/秒
    wall = time.monotonic() - t0
    k2p = st["k2p"]
    typed = le.text()
    out.update({
        "seconds": round(wall, 2),
        "keys_typed": keys,
        "keys_delivered": st["keys"],
        "keys_with_text": st["text_keys"],
        "field_repaints": st["paints"],
        "text_before_after": [before[-12:], typed[-12:]],
        "key_to_paint_ms": {
            "n": len(k2p),
            "mean": round(sum(k2p) / len(k2p), 2) if k2p else None,
            "p95": round(sorted(k2p)[max(1, int(round(len(k2p) * 0.95))) - 1], 2)
                    if len(k2p) > 3 else None,
            "max": round(max(k2p), 2) if k2p else None},
        "top_state": {"checks": getattr(win, "_top_checks", 0),
                      "fixes": getattr(win, "_top_fixes", 0)},
        "force_top_during_typing": {
            "calls": ft["n"],
            "per_s": round(ft["n"] / wall, 1),
            "mean_us": round(ft["ns"] / max(ft["n"], 1) / 1000.0, 1),
            "max_us": round(ft["max"] / 1000.0, 1),
            "occupied_pct_of_wall": round(ft["ns"] / 1e6 / (wall * 1000.0) * 100.0, 2)},
        # 防呆断言：内容真的变了 + 过滤器真的看到了按键
        "instrumentation_ok": bool(st["k2p"]) and st["keys"] >= keys and typed != before,
    })
    try:
        le.removeEventFilter(rec)
        tool.close()
    except Exception:
        pass
    pump(300)
    out["tmp"] = tmp
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.stdout.flush()
    os._exit(0)


def brief(d):
    if "error" in d:
        print("未完成：", d["error"])
        return
    print("\n== 打字链路  %s（%s 时钟，盘中=%s）==" % (d["target"], d["mode"], d["trading_clock"]))
    k = d["key_to_paint_ms"]
    print("   按键→输入框重绘: 均值 %sms / p95 %sms / 最差 %sms（%d 次，重绘 %d 次）" % (
        k["mean"], k["p95"], k["max"], k["n"], d["field_repaints"]))
    f = d["force_top_during_typing"]
    print("   打字期间置顶复核: %d 次（%.1f 次/秒）单次均值 %s µs / 最差 %s µs，占主线程 %.2f%%" % (
        f["calls"], f["per_s"], f["mean_us"], f["max_us"], f["occupied_pct_of_wall"]))
    print("   防呆: 送达 %d/%d 键，内容 '%s' → '%s'  instrumentation_ok=%s" % (
        d["keys_delivered"], d["keys_typed"],
        d["text_before_after"][0], d["text_before_after"][1], d["instrumentation_ok"]))


def main():
    if "--child" in sys.argv:
        sys.argv.remove("--child")
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", type=int, default=24)
    ap.add_argument("--mode", choices=["trading", "off"], default="trading")
    ap.add_argument("--target-panel", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    cmd = [PY, os.path.abspath(__file__), "--child", "--keys", str(args.keys),
           "--mode", args.mode]
    if args.target_panel:
        cmd.append("--target-panel")
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJ)
    if not p.stdout.strip():
        print(p.stderr[-3000:], file=sys.stderr)
        sys.exit(1)
    d = json.loads(p.stdout)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        brief(d)
        with open("/tmp/sf_typing_last.json", "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    if "--child" in sys.argv:
        ap = argparse.ArgumentParser(add_help=False)
        ap.add_argument("--keys", type=int, default=24)
        ap.add_argument("--mode", choices=["trading", "off"], default="trading")
        ap.add_argument("--target-panel", action="store_true")
        argv = [x for x in sys.argv[1:] if x != "--child"]
        a = ap.parse_args(argv)
        measure(a.keys, a.target_panel, a.mode)
    else:
        main()
