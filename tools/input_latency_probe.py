# -*- coding: utf-8 -*-
"""tools/input_latency_probe.py —— 指针/触控板交互延迟画像（跟手性量化）。

为什么需要它
------------
"触控跟不跟手、反应够不够快"不能靠读代码拍脑袋回答。功耗底座
（tools/power_profile.py）量的是**稳态空转**；用户能感觉到的交互延迟发生在
**事件处理链路**上：指针事件进入 → 处理 → 排队等重绘 → 上屏。本工具逐场景量：

  step_gap      注入节拍（模拟触控板 8ms）vs 实际节拍 —— 事件循环跟不跟得上
  self_us       链路上每个函数单次自耗时（均值 / p95 / 最差 / 合计）
  in2paint_ms   输入事件进处理函数 → 之后第一次 paintEvent 画完 的墙钟时间
  trail_px      拉伸期间"鼠标已在的高度"与"窗口实际高度"之差（跟手差）
  moved_px      拖动手势里窗口真的走了多远（防呆：手势没生效时数字全部无意义）

场景
----
  hover        悬停扫过底部（_hit_btn + setCursor + update 链路）
  press        铃铛按下反馈（按下 → 第一帧按下态 → 松开开面板）
  drag         拖动窗口（mouseMove → move() → moveEvent → 玻璃层同步）
  drag_panel   目标价面板开着时拖动主窗（每拍多两次 WindowServer 往返）
  resize       底部边缘拉伸（16ms 合并 + 每帧 mask + _ink_delta 相位缓存未命中）

事件注入方式
------------
用 `app.postEvent(win, QMouseEvent(...))` 而不是 QTest：
  · PyQt6 的 `QTest.mouseMove` 不支持 buttons 形参，拖拽/拉伸分支根本进不去；
  · postEvent 走真实事件队列（含排队），比 QTest 的直投更接近真实输入。
  代价：跳过了 macOS/Qt 的"同一轮次内移动事件合并"，所以本工具的量是**偏保守
  （偏慢）**的上界，真机上触控板通常比这里更宽松。

隔离与代价
----------
配置 / 日志 / 单实例 socket / 自选列表全部指向临时副本，绝不写用户真实数据，
也不会踢掉正在运行的正式版（口径同 tools/power_profile.py）。
`--native`（默认）用真实 NSWindow：玻璃层与工具窗的 objc 调用在 offscreen 下
直接 SIGSEGV，只有真窗口能量到它们的真实开销；代价是桌面上会短暂出现一个
悬浮窗并被真的拖动/缩放。`--no-native` 只量纯 Python 侧（玻璃/置顶全桩掉）。

用法
----
    ./.venv/bin/python tools/input_latency_probe.py                        # 全场景
    ./.venv/bin/python tools/input_latency_probe.py --scenarios drag,resize
    ./.venv/bin/python tools/input_latency_probe.py --mode off --json
"""
import argparse
import datetime
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

ANCHOR = {
    "trading": datetime.datetime(2026, 9, 22, 10, 30, 0,
                                 tzinfo=datetime.timezone(datetime.timedelta(hours=8))),
    "off": datetime.datetime(2026, 9, 22, 16, 30, 0,
                             tzinfo=datetime.timezone(datetime.timedelta(hours=8))),
}

IDLE_MS = 8000       # idle 场景的静默采样时长
STEP_MS = 8.0        # 模拟触控板事件节奏（macOS 约 8~12ms）
STEPS = 36           # 每个手势注入的移动事件数
DRAG_DX = 3          # 拖动：每拍指针走 3px
RESIZE_DY = 2        # 拉伸：每拍指针走 2px

# 交互链路上要包的函数：(宿主类, 方法名, 记录键)
TARGETS = [
    ("FloatWindow", "mousePressEvent", "press"),
    ("FloatWindow", "mouseMoveEvent", "move"),
    ("FloatWindow", "mouseReleaseEvent", "release"),
    ("FloatWindow", "moveEvent", "moveEvent"),
    ("FloatWindow", "resizeEvent", "resizeEvent"),
    ("FloatWindow", "_sync_glass_frame", "sync_glass"),
    ("FloatWindow", "_sync_open_tool_windows", "sync_tools"),
    ("FloatWindow", "_raise_tool_window", "raise_tool"),
    ("FloatWindow", "_place_panel", "place_panel"),
    ("FloatWindow", "_force_top", "force_top"),
    ("FloatWindow", "_set_glass_mask", "set_glass_mask"),
    ("FloatWindow", "_snap_to_edge", "snap_edge"),
    ("FloatWindow", "_anim_tick", "anim_tick"),
    ("RenderMixin", "paintEvent", "paint"),
    ("RenderMixin", "_ink_delta", "ink_delta"),
]
INPUT_KEYS = ("press", "move", "release")


def _pct(arr, q):
    s = sorted(arr)
    return s[max(1, int(round(len(s) * q))) - 1]


# ---------------------------------------------------------------- 子进程侧
def run(scenarios, mode, native):
    if not native:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import faulthandler
    faulthandler.enable()

    sys.path.insert(0, PROJ)
    os.chdir(PROJ)

    tmp = tempfile.mkdtemp(prefix="sf_input_")
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
    from PyQt6.QtCore import QPoint, QPointF, Qt, QEvent, QTimer
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    M.load_bundled_fonts()

    # ---- 时钟注入：盘中（点阵时钟常驻 60Hz 重绘）/ 盘后（几乎不重绘）----
    trading = ANCHOR[mode].hour in (10, 14)
    M.market_status = lambda now=None: (None if trading else ("已收盘", M.FG_DIM))
    M._is_trading_time = lambda now=None: trading

    samples = {}      # key -> [自耗时 ns]（按场景用 mark 切片）
    mark = {}         # key -> 上一场景结束时的长度
    # --trace-updates：谁排的帧。盘中实测整窗绘制 ~123 次/秒，而 frame_rate_audit
    # 的稳态记录是 62 次/秒，多出来的那 ~60 次/秒必须先归因再谈修。
    upd_src = {}      # (来源函数:文件, 整窗/局部) -> 次数
    paint_src = {}    # 同上，但数的是**真的画了**的 paintEvent
    evt_types = {}    # Qt 事件类型 -> 次数（判"超额绘制"到底是系统投的 Paint/Expose
                      # 还是应用自己排的帧，只有 update() 计数分不出来）
    trace_on = [False]
    ORIGIN_FILES = ("stock_float_window.py", "sf_ui_render.py", "renderer.py",
                    "sf_glass.py", "sf_anim_driver.py", "sf_quote_loop.py",
                    "sf_editor.py", "target_panel.py")
    SKIP = ("update", "_update", "_repaint_now", "wrapper", "make")

    def origin():
        f = sys._getframe(1)
        while f is not None:
            fn = f.f_code.co_filename
            name = f.f_code.co_name
            if (fn.endswith(ORIGIN_FILES) and name not in SKIP) or fn.endswith(
                    ("target_panel.py", "sf_editor.py")):
                return "%s:%s" % (name, os.path.basename(fn))
            f = f.f_back
        return "?"
    state = {"t_in": None, "in2paint": [], "n_in": 0, "post_ns": None, "queue": []}

    def rec(key, ns):
        lst = samples.setdefault(key, [])
        if len(lst) < 20000:
            lst.append(ns)

    def install():
        """所有钩子必须在 FloatWindow() 之前挂：QTimer.timeout 的连接表在 __init__
        里就存了绑定方法，构造之后再改类属性拦不到那些调用。"""
        hosts = {"FloatWindow": M.FloatWindow, "RenderMixin": M.RenderMixin}
        for host, name, key in TARGETS:
            owner = hosts.get(host)
            orig = getattr(owner, name, None) if owner is not None else None
            if orig is None:
                continue

            def make(orig=orig, key=key):
                is_input = key in INPUT_KEYS

                def wrapper(*a, **k):
                    t0 = time.perf_counter_ns()
                    if is_input:
                        now = time.monotonic_ns()
                        t_post = state["post_ns"]
                        if t_post is not None:
                            state["queue"].append(round((now - t_post) / 1e6, 2))
                            state["post_ns"] = None
                        state["t_in"] = now
                        state["n_in"] += 1
                    try:
                        return orig(*a, **k)
                    finally:
                        rec(key, time.perf_counter_ns() - t0)
                return wrapper

            setattr(owner, name, make())

        for name in ("save_config", "log_line"):
            orig = getattr(M, name, None)
            if orig is None:
                continue

            def make_mod(orig=orig, name=name):
                def wrapper(*a, **k):
                    t0 = time.perf_counter_ns()
                    try:
                        return orig(*a, **k)
                    finally:
                        rec("mod:" + name, time.perf_counter_ns() - t0)
                return wrapper

            setattr(M, name, make_mod())

        orig_paint = M.RenderMixin.paintEvent

        def paint(self, e):
            try:
                return orig_paint(self, e)
            finally:
                t_in = state["t_in"]
                if t_in is not None:
                    d = (time.monotonic_ns() - t_in) / 1e6
                    if d <= 250.0:
                        state["in2paint"].append(round(d, 3))
                    state["t_in"] = None

        M.RenderMixin.paintEvent = paint

    def install_update_trace():
        orig_update = M.FloatWindow.update

        def upd(self, *a):
            if trace_on[0]:
                k = (origin(), "局部" if a else "整窗")
                upd_src[k] = upd_src.get(k, 0) + 1
            return orig_update(self, *a)

        M.FloatWindow.update = upd
        orig_paint = M.RenderMixin.paintEvent

        def paint(self, e):
            if trace_on[0]:
                r = e.rect()
                full = (r.width() >= self.width() - 1 and r.height() >= self.height() - 1)
                key = (str(state.get("last_origin", "?")), "整窗" if full else "局部")
                paint_src[key] = paint_src.get(key, 0) + 1
            return orig_paint(self, e)

        M.RenderMixin.paintEvent = paint
        # 每次请求重绘时记下来源，供 paint 侧归因（近似：一个循环轮次内来源唯一）
        orig_upd_any = M.FloatWindow.update

        def upd2(self, *a):
            state["last_origin"] = origin()
            return orig_upd_any(self, *a)

        M.FloatWindow.update = upd2

        # 事件类型计数是**一次性诊断**：就是它把"UpdateRequest 与 Paint 一一对应、
        # 62.5/秒"这个结论量出来的（也顺手证伪了"盘中 123 帧/秒"）。它要在类上覆盖
        # QWidget.event，与工具窗的事件派发撞过一次段错误，所以默认不开：
        # 需要时 SF_PROBE_EVENTS=1 打开。
        if os.environ.get("SF_PROBE_EVENTS") == "1":
            orig_event = M.FloatWindow.event

            def ev(self, e):
                if trace_on[0]:
                    try:
                        t = e.type().name
                    except Exception:
                        t = str(e.type())
                    evt_types[t] = evt_types.get(t, 0) + 1
                return orig_event(self, e)

            M.FloatWindow.event = ev

    stubbed = []
    if not native:
        # offscreen 无真实 NSWindow：玻璃层/置顶/工具窗提升里的 objc 调用会直接
        # SIGSEGV（winId() 是假指针）。这些恰是"拖动/拉伸跟手"的主要成本，所以
        # 退化模式只量纯 Python 侧，结论必须用 --native 复核。
        M.FloatWindow._force_top = lambda self: None
        M.FloatWindow._inject_glass = lambda self: None
        M.FloatWindow._setup_glass = lambda self: False
        # use_server_origin 是 v1.4.33 给"已判出需要纠正"路径加的形参，桩必须一起接住，
        # 否则 _force_top 一调它就 TypeError（被上层 except 吞掉 → 静默少一条测量链路）。
        M.FloatWindow._sync_glass_frame = lambda self, force_mask=False, \
                use_server_origin=False: None
        M.FloatWindow._glass_mask_flush = lambda self: None
        M.FloatWindow._raise_tool_window = lambda self, tool: None
        M.FloatWindow.raise_ = lambda self: None
        M.FloatWindow.activateWindow = lambda self: None
        stubbed += ["_force_top", "_inject_glass", "_setup_glass",
                    "_sync_glass_frame", "_glass_mask_flush",
                    "_raise_tool_window", "raise_", "activateWindow"]

    install()
    install_update_trace()

    win = M.FloatWindow()
    screen = app.primaryScreen().geometry()
    win.move(screen.right() - win.width() - 320, screen.bottom() - win.height() - 60)
    win.show()

    # 泵事件：processEvents + 2ms 让路。不能空转 processEvents（CPU 打满测不准），
    # PyQt6 也没暴露 waitForEvents；2ms 粒度不影响 8ms 节拍 / 16ms 帧的分辨率。
    def pump(ms):
        t0 = time.monotonic()
        while (time.monotonic() - t0) * 1000 < ms:
            app.processEvents()
            time.sleep(0.002)
        app.processEvents()

    pump(800)      # 等玻璃层注入、首屏落定

    BTN = Qt.MouseButton.LeftButton
    NONE = Qt.MouseButton.NoButton
    MOD = Qt.KeyboardModifier.NoModifier

    def post(etype, local, global_pt, button=NONE, buttons=NONE):
        ev = QMouseEvent(etype, QPointF(local), QPointF(global_pt), button, buttons, MOD)
        state["post_ns"] = time.monotonic_ns()
        app.postEvent(win, ev)

    def press_at(local):
        post(QEvent.Type.MouseButtonPress, local, win.mapToGlobal(QPoint(local)),
             button=BTN, buttons=BTN)
        pump(4)

    def move_to(local, buttons=BTN):
        """按"指针的真实全局位置"注入：local 随窗口移动换算，等价于手指在动。"""
        post(QEvent.Type.MouseMove, local, win.mapToGlobal(QPoint(local)), buttons=buttons)

    def release_at(local):
        post(QEvent.Type.MouseButtonRelease, local, win.mapToGlobal(QPoint(local)),
             button=BTN, buttons=NONE)

    def find_point(pred):
        w, h = win.width(), win.height()
        for y in list(range(2, min(h, 70), 2)) + list(range(max(2, h - 40), h, 2)):
            for x in range(4, w, 3):
                p = QPoint(x, y)
                if pred(p):
                    return p
        return None

    btn_point = lambda want: find_point(lambda p: win._btn_at(p) == want)
    drag_point = find_point(lambda p: win._btn_at(p) is None and not win._chart_hit(p))
    chart_point = find_point(lambda p: win._chart_hit(p) and win._btn_at(p) is None)

    results = {}

    def stats():
        out = {}
        for k, arr in samples.items():
            cur = arr[mark.get(k, 0):]
            mark[k] = len(arr)
            if not cur:
                continue
            out[k] = {"n": len(cur),
                      "mean_us": round(sum(cur) / len(cur) / 1000.0, 1),
                      "p95_us": round(_pct(cur, 0.95) / 1000.0, 1),
                      "max_us": round(max(cur) / 1000.0, 1),
                      "total_ms": round(sum(cur) / 1e6, 2)}
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["total_ms"]))

    def gesture(name, on_step, steps=STEPS, post_ms=400):
        """按 STEP_MS 节拍注入手势，量实际节拍 / 处理耗时 / 输入→上屏。"""
        state["in2paint"] = []
        state["queue"] = []
        trace_on[0] = True
        u0 = sum(upd_src.values())
        p0 = sum(paint_src.values())
        e0 = {k: v for k, v in evt_types.items()}
        src0 = {k: v for k, v in paint_src.items()}
        n_in0 = state["n_in"]
        origin0 = QPoint(win.pos())
        stamps = []
        done = [False]
        idx = [0]
        keep = []
        trail = []

        def finish():
            done[0] = True

        def step():
            stamps.append(time.monotonic_ns())
            v = on_step(idx[0])
            if isinstance(v, (int, float)):
                trail.append(v)
            idx[0] += 1
            if idx[0] >= steps:
                t = QTimer()
                t.setSingleShot(True)
                t.timeout.connect(finish)
                t.start(post_ms)
                keep.append(t)
                timer.stop()

        timer = QTimer()
        timer.timeout.connect(step)
        keep.append(timer)
        t_start = time.monotonic()
        timer.start(int(STEP_MS))
        while not done[0]:
            app.processEvents()
            time.sleep(0.002)
            if time.monotonic() - t_start > 20.0:
                break

        r = {"steps": idx[0], "wall_s": round(time.monotonic() - t_start, 2),
             "delivered_input_events": state["n_in"] - n_in0,
             "moved_px": round(abs(win.x() - origin0.x()) + abs(win.y() - origin0.y()), 1)}
        gaps = [(b - a) / 1e6 for a, b in zip(stamps, stamps[1:]) if b > a]
        if gaps:
            r["step_gap_ms"] = {"mean": round(sum(gaps) / len(gaps), 2),
                                "p95": round(_pct(gaps, 0.95), 2),
                                "max": round(max(gaps), 2),
                                "lag_pct": round(
                                    (sum(gaps) / len(gaps) - STEP_MS) / STEP_MS * 100, 1)}
        q = state["queue"]
        if q:
            r["queue_wait_ms"] = {"mean": round(sum(q) / len(q), 2),
                                  "p95": round(_pct(q, 0.95), 2),
                                  "max": round(max(q), 2)}
        i2p = state["in2paint"]
        if i2p:
            r["in2paint_ms"] = {"n": len(i2p),
                                "mean": round(sum(i2p) / len(i2p), 2),
                                "p95": round(_pct(i2p, 0.95), 2),
                                "max": round(max(i2p), 2)}
        if trail:
            r["trail_px"] = {"mean": round(sum(trail) / len(trail), 2),
                             "max": round(max(trail), 1)}
        # 本轮的绘制/事件速率（减去历史累计），避免跨场景串数
        el = max(time.monotonic() - t_start, 0.001)
        d_src = {("%s|%s" % k): v - src0.get(k, 0)
                 for k, v in paint_src.items() if v - src0.get(k, 0) > 0}
        r["paint_per_s"] = {k: round(v / el, 1) for k, v in
                            sorted(d_src.items(), key=lambda kv: -kv[1])}
        r["events_per_s"] = {k: round((v - e0.get(k, 0)) / el, 1)
                             for k, v in sorted(evt_types.items(),
                                                key=lambda kv: -(kv[1] - e0.get(kv[0], 0)))
                             if v - e0.get(k, 0) > 0}
        r["handlers"] = stats()
        trace_on[0] = False
        results[name] = r
        return r

    # 上一轮"盘中 123 次/秒绘制"就是这个坑：`stats()` 的首次切片把**进程启动到
    # 现在**的全部样本算进了第一个场景，于是第一个场景报出 589 次、第二个报 86 次，
    # 看着像"手势导致绘制翻倍"。真实速率由 paint_per_s（本轮增量/本轮墙钟）给出，
    # 实测恒为 62.5 次/秒。这里先建立基线，第一个场景只数自己的量。
    stats()
    # ---------- 场景 ----------
    # 打字链路（工具窗自绘不走 RenderMixin.paintEvent）由
    # tools/typing_latency_probe.py 单独量：事件过滤器与本工具的类级包装同进程
    # 会段错误（同样的打字代码单独跑完全正常），不在这里硬撑。

    if "idle" in scenarios:
        trace_on[0] = True
        t_idle = time.monotonic()
        pump(IDLE_MS)
        wall = time.monotonic() - t_idle
        results["idle"] = {
            "seconds": round(wall, 2),
            "update_per_s": {("%s|%s" % k): round(v / wall, 1)
                             for k, v in sorted(upd_src.items(),
                                                 key=lambda kv: -kv[1])},
            "paint_per_s": {("%s|%s" % k): round(v / wall, 1)
                            for k, v in sorted(paint_src.items(),
                                               key=lambda kv: -kv[1])},
        }

    if "hover" in scenarios:
        y = max(2, win.height() - 6)
        w = win.width()

        def hov(i):
            move_to(QPoint(6 + (i * 5) % max(8, w - 12), y), buttons=NONE)
        gesture("hover", hov)

    if "press" in scenarios:
        p = btn_point("bell") or btn_point("edit") or btn_point("target")
        if p:
            def pr(i):
                if i == 0:
                    press_at(p)
                elif i == 1:
                    release_at(p)
            r = gesture("press", pr, steps=4, post_ms=600)

            # 单独量：按下处理完 → 第一帧画完（"按下态"上屏）
            state["in2paint"] = []
            press_at(p)
            pump(250)
            if state["in2paint"]:
                v = state["in2paint"]
                r["press_feedback_ms"] = {"mean": round(sum(v) / len(v), 2),
                                          "max": round(max(v), 2), "n": len(v)}
            pump(700)                       # 松开 → 面板/编辑器打开
            r["release_then"] = stats()
            release_at(p)
            pump(500)
            if getattr(win, "_target_panel", None) is not None:
                win._close_target_panel()
            ed = getattr(win, "_editor", None)
            if ed is not None:
                try:
                    ed.close()
                except Exception:
                    pass
            pump(300)

    if "drag" in scenarios and drag_point:
        base = QPoint(drag_point)
        st = {"pressed": False}

        def dr(i):
            if not st["pressed"]:
                press_at(base)
                st["pressed"] = True
            # 匀速：局部偏移恒为 DRAG_DX ⇒ 处理函数每拍把窗口推进 DRAG_DX
            move_to(QPoint(base.x() + DRAG_DX, base.y() + (i % 3)))
        r = gesture("drag", dr)
        t_rel = time.monotonic_ns()
        release_at(QPoint(base.x() + STEPS * DRAG_DX, base.y()))
        pump(500)
        r["release_settle_ms"] = round((time.monotonic_ns() - t_rel) / 1e6, 1)
        r["release_then"] = stats()

    if "resize" in scenarios:
        if not win._chart_expanded and chart_point:
            press_at(chart_point)
            release_at(chart_point)
            pump(800)                        # 展开动画跑完
        h0 = win.height()
        edge = QPoint(12, h0 - 3)
        st = {"pressed": False}

        def rz(i):
            if not st["pressed"]:
                press_at(edge)
                st["pressed"] = True
            dy = (i + 1) * RESIZE_DY
            move_to(QPoint(edge.x(), edge.y() + dy))
            tgt = getattr(win, "_resize_target_h", None)
            return None if tgt is None else abs(tgt - win.height())
        r = gesture("resize", rz, steps=24, post_ms=600)
        t_rel = time.monotonic_ns()
        release_at(QPoint(edge.x(), edge.y() + 24 * RESIZE_DY))
        pump(600)
        r["release_settle_ms"] = round((time.monotonic_ns() - t_rel) / 1e6, 1)
        r["release_then"] = stats()
        r["height_before_after"] = [h0, win.height()]
        if win._chart_expanded and chart_point:
            press_at(chart_point)
            release_at(chart_point)
            pump(800)

    if "drag_panel" in scenarios and win._targets_enabled():
        opened = False
        try:
            win._open_target_panel()
            pump(900)
            opened = getattr(win, "_target_panel", None) is not None
        except Exception as exc:
            results["drag_panel"] = {"error": repr(exc)}
        r = None
        if opened and drag_point:
            base2 = QPoint(drag_point)
            st = {"pressed": False}

            def drp(i):
                if not st["pressed"]:
                    press_at(base2)
                    st["pressed"] = True
                move_to(QPoint(base2.x() + DRAG_DX, base2.y() + (i % 3)))
            r = gesture("drag_panel", drp)
            release_at(QPoint(base2.x() + STEPS * DRAG_DX, base2.y()))
            pump(500)
            r["release_then"] = stats()
            r["panel_visible"] = True
        win._close_target_panel() if opened else None
        pump(300)

    out = {
        "mode": mode,
        "native": native,
        "trading_clock": trading,
        "stubbed": stubbed,
        "target_step_ms": STEP_MS,
        "steps_per_gesture": STEPS,
        "window": {"w": win.width(), "h": win.height()},
        "screen_refresh_hint": "60Hz 屏，1 帧 = 16.7ms",
        "scenarios": results,
        "update_sources": {("%s|%s" % k): v for k, v in upd_src.items()},
        "paint_sources": {("%s|%s" % k): v for k, v in paint_src.items()},
        "tmp": tmp,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.stdout.flush()
    # 不能走 app.quit()：FloatWindow 退出路径末尾自己 os._exit(0)，会吞掉输出
    os._exit(0)


# ---------------------------------------------------------------- 父进程侧
def brief(d):
    print("\n== 指针交互延迟画像  mode=%s native=%s（盘中时钟=%s）==" % (
        d["mode"], d["native"], d["trading_clock"]))
    if d["stubbed"]:
        print("   退化模式，以下路径未量到：" + ", ".join(d["stubbed"]))
    print("   窗口 %sx%s  注入节拍 %sms  %s" % (
        d["window"]["w"], d["window"]["h"], d["target_step_ms"],
        d["screen_refresh_hint"]))
    for name, s in d["scenarios"].items():
        print("\n[%s]" % name)
        if "error" in s:
            print("   未完成：", s["error"])
            continue
        print("   手势生效核对: 注入 %s 拍 / 实际送达 %d 个输入事件 / 窗口移动 %s px" % (
            s.get("steps"), s.get("delivered_input_events", 0), s.get("moved_px")))
        if s.get("events_per_s"):
            print("   Qt 事件到达速率（次/秒）: " + "  ".join(
                "%s=%s" % (k, v) for k, v in list(s["events_per_s"].items())[:6]))
        # 复核 F9：打字场景已迁到 tools/typing_latency_probe.py，这里的死分支删掉
        for col in ("update_per_s", "paint_per_s"):
            if s.get(col):
                print("   %s（次/秒）:" % ("update() 请求来源" if "update" in col
                                            else "实际绘制来源"))
                for k, v in list(s[col].items())[:10]:
                    print("      %-46s %6.1f" % (k, v))
        g = s.get("step_gap_ms")
        if g:
            print("   实际节拍: 均值 %.2f ms（比目标 %+.0f%%）p95 %.2f 最差 %.2f ms" % (
                g["mean"], g["lag_pct"], g["p95"], g["max"]))
        q = s.get("queue_wait_ms")
        if q:
            print("   排队等待(投递→进处理函数): 均值 %sms / p95 %sms / 最差 %sms" % (
                q["mean"], q["p95"], q["max"]))
        i = s.get("in2paint_ms")
        if i:
            print("   输入→画完: 均值 %sms / p95 %sms / 最差 %sms（%d 帧）" % (
                i["mean"], i["p95"], i["max"], i["n"]))
        t = s.get("trail_px")
        if t:
            print("   鼠标与窗口的差: 均值 %s px / 最差 %s px" % (t["mean"], t["max"]))
        if s.get("press_feedback_ms"):
            print("   按下→按下态上屏: 均值 %sms 最差 %sms（%d 次）" % (
                s["press_feedback_ms"]["mean"], s["press_feedback_ms"]["max"],
                s["press_feedback_ms"]["n"]))
        if s.get("release_settle_ms") is not None:
            print("   松开→收口完成（吸附/落盘/面板）: %s ms" % s["release_settle_ms"])
        for k, v in (s.get("handlers") or {}).items():
            print("   %-16s %5d 次  均值 %9.1f µs  p95 %9.1f  最差 %9.1f µs  合计 %7.2f ms" % (
                k, v["n"], v["mean_us"], v["p95_us"], v["max_us"], v["total_ms"]))
        for k, v in (s.get("release_then") or {}).items():
            print("   (松开后) %-11s %5d 次  均值 %9.1f µs  最差 %9.1f µs  合计 %7.2f ms" % (
                k, v["n"], v["mean_us"], v["max_us"], v["total_ms"]))


def child_args(argv):
    a = argparse.ArgumentParser(add_help=False)
    a.add_argument("--scenarios", default="hover,press,drag,resize,drag_panel")
    a.add_argument("--mode", choices=["trading", "off"], default="trading")
    a.add_argument("--no-native", dest="native", action="store_false")
    return a.parse_args(argv)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default="hover,press,drag,resize,drag_panel")
    ap.add_argument("--mode", choices=["trading", "off"], default="trading")
    ap.add_argument("--no-native", dest="native", action="store_false")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cmd = [PY, os.path.abspath(__file__), "--child",
           "--scenarios", args.scenarios, "--mode", args.mode]
    if not args.native:
        cmd.append("--no-native")
    env = dict(os.environ)
    if not args.native:
        env["QT_QPA_PLATFORM"] = "offscreen"
    else:
        env.pop("QT_QPA_PLATFORM", None)
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=PROJ)
    if not p.stdout.strip():
        print(p.stderr[-4000:], file=sys.stderr)
        sys.exit(1)
    d = json.loads(p.stdout)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        brief(d)
        with open("/tmp/sf_input_last.json", "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        print("\n   原始 JSON: /tmp/sf_input_last.json")


if __name__ == "__main__":
    if "--child" in sys.argv:
        sys.argv.remove("--child")
        a = child_args(sys.argv[1:])
        run([s for s in a.scenarios.split(",") if s], a.mode, a.native)
    else:
        main()
