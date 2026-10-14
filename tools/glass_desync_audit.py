#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/glass_desync_audit.py —— 背景层错位时，**产品自己的稳态复核在看什么**。

要回答的问题
------------
用户现象：浏览器全屏期间拖动主窗，松手后毛玻璃背景永久停在别处（约半个屏宽），
而 debug.log 里 `re-top: glass above main` 只出现过一次，之后不再自愈。

主窗是两个同级 NSWindow（内容窗 + 独立玻璃背景窗）。松手后 `mouseReleaseEvent`
与每 250ms 的 `_force_top` 都会把两窗对齐，唯一的判据是
`sf_glass._top_state_ok()` 里的 `gw.frame() != window.frame()`。
但 v1.4.31 已经量过：**系统接管拖动期间 AppKit 自己的 `window().frame()` 是过时的**
（服务端拖走 234px，应用侧模型还在起点）。如果这个"过时"在松手后没有立刻收敛，
那么 `gw.frame()` 与 `window.frame()` 会**同时停在旧位置而彼此相等** ——
判据读到"一切正常"，于是没有任何人会去纠正，而 WindowServer 画的是另一个位置。

本工具就是来证伪/证实这句话的。它同帧记录四件事：

    model_main   AppKit 模型里内容窗的 frame          （产品读的东西）
    model_glass  AppKit 模型里玻璃窗的 frame          （产品读的东西）
    ws_main      WindowServer 里内容窗的真实 bounds    （用户看的东西）
    ws_glass     WindowServer 里玻璃窗的真实 bounds    （用户看的东西）
    top_ok       _top_state_ok() 这一轮的结论          （产品以为要不要修）

判据（关键是最后一条，前两条只是描述现象）：
    real_desync_max/final   WindowServer 眼里的错位峰值/终值 → 用户看得见的错位
    model_desync_max        两个应用侧模型之间的错位         → 产品"看得见"的错位
    blind_samples           **同时满足四条**的采样数：
                              真实错位 >2px、`_top_state_ok` 判"已经对了"、
                              `_steady_state_fault` 没报故障、且不在系统拖拽期间
                            大于 0 才叫"没有任何人看得见"。拖拽期间单独不算：
                            那段时间背景层归 8ms 跟随定时器管，稳态复核按设计让位。

v1.4.33 的本机实测（同一台 MacBook Air / macOS 27.0）：
    修前  real 峰值 46px、blind_samples 3
    修后  real 峰值 10~11px、blind_samples 0、松手终值 0px
    —— 峰值下降来自 `_force_top` 不再把背景层往过时模型上拽；
       blind 归零来自 `_steady_state_fault` 补上了"问 WindowServer"这一问。

用法
----
    # ① 旁观你正在用的那个正式版（全程只读：不建窗、不发鼠标事件、不碰配置）。
    #    要证实"浏览器全屏期间拖动会永久脱节"，最真实的现场就是你自己机器上
    #    正在用的程序，而不是本工具另建的窗口 —— 复现请走这条：
    ./.venv/bin/python tools/glass_desync_audit.py --watch 40
    #    然后按屏幕提示：进浏览器全屏 → 拖动悬浮窗 → 松手 → 退出全屏。
    #    Ctrl-C 可随时提前结束。
    # ② 或者让本工具自己起一个源码窗口、发一次真实 HID 拖动（需辅助功能权限）：
    ./.venv/bin/python tools/glass_desync_audit.py                      # 拖一次，不碰全屏
    ./.venv/bin/python tools/glass_desync_audit.py --fullscreen         # 先进 Chrome 全屏再拖
    ./.venv/bin/python tools/glass_desync_audit.py --json               # 机器可读

安全边界
--------
· `--watch` 只调 CGWindowListCopyWindowInfo 读，不写任何东西；
· ② 的所有落盘路径（config/log/universe/last_quote/单实例 socket）在 import 产品模块
  **之前**重定向到临时目录，绝不碰 ~/Library/Application Support 的真配置；
· ② 只发一次真实 HID 鼠标事件（多次连拖会让本进程对合成鼠标事件失响应，已记录）；
· ② 退出时把窗口位置写回临时 config，不写用户配置。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)

# ---- 必须在 import sf_constants 之前完成：路径常量在导入时就定型 ----
HOME = tempfile.mkdtemp(prefix="sf_glass_audit_")
os.environ["QT_QPA_PLATFORM"] = "cocoa"
os.environ["SF_CONFIG_PATH"] = os.path.join(HOME, "config.json")
os.environ["SF_LOG_PATH"] = os.path.join(HOME, "debug.log")
os.environ["SF_UNIVERSE_PATH"] = os.path.join(HOME, "securities.json")
os.environ["SF_LAST_QUOTE_PATH"] = os.path.join(HOME, "last_quote.json")
os.environ["SF_SINGLE_SOCK"] = os.path.join(HOME, "single.sock")
os.environ["SF_SEED_FIRST_SCREEN"] = "0"

sys.path.insert(0, PROJ)


# ============================ WindowServer 侧取证 ============================

def ws_state(main_num, glass_num):
    """一次 OnScreenOnly 查询同时拿到：两窗真实 bounds + 谁压住谁。

    刻意只用一次调用（而不是两次 IncludingWindow）：既省，又能顺带拿到合成顺序
    —— 列表越靠前越在上，与 sf_glass.glass_covers_main 同一口径。
    """
    import Quartz
    want: dict = {int(main_num): None, int(glass_num): None}
    order: dict = {}
    lst = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly,
                                            Quartz.kCGNullWindowID)
    for i, info in enumerate(lst):
        n = int(info.get(Quartz.kCGWindowNumber, -1))
        if n in want:
            b = info.get("kCGWindowBounds")
            want[n] = (float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"]))
            order[n] = i
    return want, order


# ============================ AppKit 模型侧取证 ============================

class Sampler:
    """在 AppKit 的模态跟踪循环里也要活着的采样器。

    不能用 QTimer：`performWindowDragWithEvent:` 进入 NSEventTrackingRunLoopMode，
    Qt 定时器整段不被唤醒（sf_glass._glass_follow_begin 同一个道理）。挂到
    common modes 上的 CFRunLoopTimer 才会照常触发 —— 这正是本产品自己的跟随定时器
    用的机制，所以本工具看到的节拍和产品自己看到的节拍是一致的。
    """

    def __init__(self, win, interval=0.008):
        from CoreFoundation import (CFAbsoluteTimeGetCurrent, CFRunLoopAddTimer,
                                    CFRunLoopGetMain, CFRunLoopTimerCreate,
                                    kCFRunLoopCommonModes)
        import objc
        from ctypes import c_void_p
        self.win = win
        self.rows = []
        self.t0 = time.perf_counter()
        nsw = objc.objc_object(c_void_p=int(win.winId())).window()
        self.main_num = int(nsw.windowNumber())
        self.glass_num = int(win._glass_window.windowNumber())
        self._nsw = nsw
        self._gw = win._glass_window

        def _cb(_timer, _info):
            try:
                self.tick()
            except Exception as exc:                      # 采样器绝不能弄死被测对象
                self.rows.append({"err": repr(exc)})

        self._cb = _cb
        self.timer = CFRunLoopTimerCreate(None, CFAbsoluteTimeGetCurrent() + interval,
                                          interval, 0, 0, _cb, None)
        self._add = CFRunLoopAddTimer
        self._get = CFRunLoopGetMain
        self._modes = kCFRunLoopCommonModes
        self._invalidate = __import__("CoreFoundation").CFRunLoopTimerInvalidate

    def start(self):
        self._add(self._get(), self.timer, self._modes)

    def stop(self):
        self._invalidate(self.timer)

    def tick(self):
        top_ok = bool(self.win._top_state_ok(self._nsw, self._gw))
        mf = self._nsw.frame().origin
        gf = self._gw.frame().origin
        ws, order = ws_state(self.main_num, self.glass_num)
        m, g = ws.get(self.main_num), ws.get(self.glass_num)
        if m is None or g is None:
            # 任一窗不在屏（全屏切换动画中会短暂查不到）：记一行但不下结论
            self.rows.append({"t": round((time.perf_counter() - self.t0) * 1000, 1),
                              "offscreen": True, "top_ok": top_ok})
            return
        # v1.4.33 新增的那一问：把 5s 节流临时打开，逐拍问"用户实际看到的顺序/位置
        # 对不对"。探测本身多花一次 WindowServer 往返（实测单次 0.5~0.8ms），
        # 产品侧不会这样跑；这里要的是"错位的那一拍有没有被判定成故障"这个事实。
        self.win._last_z_order_check = 0.0
        fault = self.win._steady_state_fault(self._nsw, self._gw)
        self.rows.append({
            "t": round((time.perf_counter() - self.t0) * 1000, 1),
            "model_main": (round(mf.x, 1), round(mf.y, 1)),
            "model_glass": (round(gf.x, 1), round(gf.y, 1)),
            "ws_main": (round(m[0], 1), round(m[1], 1)),
            "ws_glass": (round(g[0], 1), round(g[1], 1)),
            "model_desync": round(max(abs(mf.x - gf.x), abs(mf.y - gf.y)), 1),
            "real_desync": round(max(abs(m[0] - g[0]), abs(m[1] - g[1])), 1),
            "glass_on_top": order.get(self.glass_num, 99) < order.get(self.main_num, 99),
            "top_ok": top_ok,
            "fault": fault[0] if fault else None,
            # 拖拽期间背景层归 8ms 跟随定时器管，`_steady_state_fault` 按设计不问错位。
            # 不记这一位就会把"设计上的让位"统计成"判据看不见"，虚报缺陷。
            "follow_active": getattr(self.win, "_glass_follow_timer", None) is not None,
        })


# ============================ 真实 HID 拖动 ============================

def post(kind, x, y, button):
    import Quartz
    ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), button)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def drag_thread(x, y, dx, dy, steps, hold_ms, down_at):
    """在**另一个线程**发事件：主线程此刻正卡在 AppKit 的模态拖拽循环里。"""
    import Quartz
    DOWN = Quartz.kCGMouseButtonLeft
    time.sleep(down_at)
    post(Quartz.kCGEventLeftMouseDown, x, y, DOWN)
    time.sleep(0.05)
    for i in range(1, steps + 1):
        post(Quartz.kCGEventLeftMouseDragged, x + dx * i / steps,
             y + dy * i / steps, DOWN)
        time.sleep(hold_ms / 1000.0)
    time.sleep(0.25)
    post(Quartz.kCGEventLeftMouseUp, x + dx, y + dy, DOWN)


# ============================ Chrome 全屏 ============================

def post_key(key, control=False, command=False):
    import Quartz
    KC = {"f": 3}
    FLAG = {
        "control": getattr(Quartz, "kCGEventFlagMaskControl", 0x00040000),
        "command": getattr(Quartz, "kCGEventFlagMaskCommand", 0x00100000),
    }
    flags = (FLAG["control"] if control else 0) | (FLAG["command"] if command else 0)
    code = KC[key]
    down = Quartz.CGEventCreateKeyboardEvent(None, code, True)
    Quartz.CGEventSetFlags(down, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    up = Quartz.CGEventCreateKeyboardEvent(None, code, False)
    Quartz.CGEventSetFlags(up, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def chrome_activate():
    subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'],
                   check=False, timeout=10)


def chrome_fullscreen_toggle():
    """Ctrl+Cmd+F 切一次全屏 —— 复现用户场景里"独占新 Space"那一步。

    不依赖 Chrome 的 AppleScript 字典（它的菜单项名字随本地化变）。全屏是
    **触发条件**而不是**被测对象**，所以切不动只打印、不改判定：判定为"窗口没动"
    时自然会说明数据无效，不会给出一个好看的零错位结论。
    """
    chrome_activate()
    time.sleep(0.8)
    post_key("f", control=True, command=True)


# ============================ 旁观模式（不动任何东西） ============================

def _owner_matches(name, needle):
    return bool(needle) and needle in str(name or "")


def watch(seconds, owner, gap_s=0.008):
    """旁观**已经在跑的那个正式版**：只读 WindowServer，不建窗、不发事件、不碰配置。

    为什么需要这一档：要证实"全屏期间拖动会让背景层永久脱节"，最真实的现场就是
    你自己机器上那个正在用的程序 —— 而不是本工具另建的一个测试窗口。
    本模式全程只读，随时 Ctrl-C。

    判据同 in-process 模式：`final_desync_px` 是松手后最后 1 秒的最大真实错位，
    >2px 就是"用户看得见、程序看不见"的永久脱节。
    """
    import Quartz
    t0 = time.perf_counter()
    # 一帧 = (时刻ms, A号, A.x, A.y, B号, B.x, B.y)。刻意摊平成一串数字而不是
    # 嵌套 list：混着 dict/tuple 的嵌套结构会被 mypy 推成 object，后面每一次
    # 取坐标都成了"对 object 取下标"，读的人也无法从类型上看出装的是什么。
    rows: list[tuple[float, float, float, float, float, float, float]] = []
    print("旁观 %r 的两条在屏窗口 %.0fs —— 现在请：进入浏览器全屏 → 拖动悬浮窗 → "
          "松手 → 退出全屏。Ctrl-C 可提前结束。" % (owner, seconds), flush=True)
    while time.perf_counter() - t0 < seconds:
        lst = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly,
                                                Quartz.kCGNullWindowID)
        mine = [e for e in lst if _owner_matches(e.get("kCGWindowOwnerName"), owner)]
        pair = []
        for e in mine:
            b = e.get("kCGWindowBounds")
            if b and float(b["Width"]) > 60 and float(b["Height"]) > 60:
                # 只取主窗与背景层这一对同尺寸的；工具窗/面板尺寸不同，靠
                # kCGWindowOwnerName 认领后会一起进来，用宽高相同这一条排掉。
                pair.append((float(e.get("kCGWindowNumber", -1)),
                             float(b["X"]), float(b["Y"]),
                             float(b["Width"]), float(b["Height"])))
        same = [p for p in pair[1:] if abs(p[3] - pair[0][3]) < 1.0
                and abs(p[4] - pair[0][4]) < 1.0] if len(pair) >= 2 else []
        if same:
            a, c = pair[0], same[0]
            rows.append(((time.perf_counter() - t0) * 1000.0,
                         a[0], a[1], a[2], c[0], c[1], c[2]))
        time.sleep(gap_s)
    if len(rows) < 5:
        print("!! 只采到 %d 帧两窗同时在屏的样本（程序没在跑？owner 不对？"
              "还是只有一扇窗开着？）" % len(rows))
        return 1
    desync = [max(abs(r[2] - r[5]), abs(r[3] - r[6])) for r in rows]
    tail = desync[-125:]                       # 最后约 1 秒
    moved = max(max(abs(r[2] - rows[0][2]), abs(r[3] - rows[0][3])) for r in rows)
    wi = max(range(len(desync)), key=lambda i: desync[i])
    w = rows[wi]
    worst = {"t_ms": round(w[0], 1), "desync_px": round(desync[wi], 1),
             "content": [int(w[1]), w[2], w[3]], "glass": [int(w[4]), w[5], w[6]]}
    # 一次成型：先建一个"全是数字"的 dict、再往里塞 dict/str，会被 mypy 锁成
    # dict[str, float|int|None] 然后每笔后续赋值都报 incompatible —— 与其到处
    # 标注，不如把这份报告一次算完。
    res = {
        "frames": len(rows),
        "moved_px": round(moved, 1),
        "real_desync_max_px": round(max(desync), 1),
        "real_desync_final_px": round(max(tail), 1),
        "self_healed": max(tail) <= 2.0,
        "worst": worst,
        "verdict": None,
    }
    if moved < 20.0:
        res["verdict"] = ("两窗位置几乎没变（最大位移 %.0fpx）→ 这一轮没真的拖动，"
                          "数据无效，请重来一次" % moved)
    elif res["self_healed"]:
        res["verdict"] = ("拖过之后背景层与内容窗**已对齐**（最后 1 秒最大错位 %.1fpx）"
                          % max(tail))
    else:
        res["verdict"] = ("松手最后 1 秒仍差 %.1fpx → 永久脱节复现，把上面这段数字发我"
                          % max(tail))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


# ============================ 主流程 ============================

def main(argv=None):
    ap = argparse.ArgumentParser(description="背景层错位取证：产品模型 vs WindowServer 真实")
    ap.add_argument("--watch", type=float, default=0.0,
                    help="旁观已在跑的正式版若干秒（只读，不建窗、不发事件）")
    ap.add_argument("--owner", default="实时股价",
                    help="--watch 时按 kCGWindowOwnerName 子串认领窗口")
    ap.add_argument("--dx", type=float, default=320.0)
    ap.add_argument("--dy", type=float, default=140.0)
    ap.add_argument("--steps", type=int, default=28)
    ap.add_argument("--hold-ms", type=float, default=20.0)
    ap.add_argument("--settle", type=float, default=3.0, help="松手后观察秒数（跨过 250ms 复核）")
    ap.add_argument("--fullscreen", action="store_true", help="拖之前先让 Chrome 进系统全屏")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.watch > 0:
        return watch(a.watch, a.owner)

    def say(*args):
        if not a.json:
            print(*args, flush=True)

    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtTest import QTest

    import sf_ui_render as R
    import stock_float_window as m

    app = QApplication.instance() or QApplication([])
    R.load_bundled_fonts()
    win = m.FloatWindow()
    win.move(500, 300)
    win.show()
    # 等玻璃层建好（_inject_glass 是 singleShot(0) 排到首帧之后）
    deadline = time.time() + 8.0
    while time.time() < deadline:
        app.processEvents()
        QTest.qWait(30)
        if getattr(win, "_glass_window", None) is not None:
            break
    if getattr(win, "_glass_window", None) is None:
        print("!! 玻璃层没建起来（_glass_window is None），无法核验", file=sys.stderr)
        return 2
    say("玻璃层已建立；窗口 %dx%d @ %s" % (win.width(), win.height(), win.pos()))

    # 先让 AppKit/WindowServer 把两窗都落定，再开始采样
    app.processEvents()
    QTest.qWait(400)

    sampler = Sampler(win)
    sampler.start()
    say("采样开始（8ms / common modes）…")

    def pump(seconds):
        """跑 Qt 事件循环若干秒（期间采样器靠原生 run loop 自己活着）。"""
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            QTest.qWait(20)

    if a.fullscreen:
        say("→ 阶段 1：让 Chrome 进入系统全屏")
        chrome_fullscreen_toggle()
        pump(3.0)
        say("→ 阶段 2：在全屏 Space 内做真实拖动")

    # 按下点：内容窗左上角空白（避开右上按钮与中下分时图热区）
    ws, _ = ws_state(sampler.main_num, sampler.glass_num)
    b = ws.get(sampler.main_num)
    if not b:
        print("!! 采样前就查不到内容窗（全屏切换后本窗不在屏？）", file=sys.stderr)
        return 2
    sx, sy = b[0] + 20.0, b[1] + 8.0
    say("按下点 (%.0f, %.0f)，位移 (%+.0f, %+.0f)" % (sx, sy, a.dx, a.dy))
    t = threading.Thread(target=drag_thread,
                         args=(sx, sy, a.dx, a.dy, a.steps, a.hold_ms, 0.3),
                         daemon=True)
    drag_start = len(sampler.rows)
    t.start()
    pump(0.3 + (a.steps * a.hold_ms / 1000.0) + 0.5)
    drag_end = len(sampler.rows)
    say("→ 阶段 3：松手后在**全屏里**静置 %.1fs（跨过 250ms 置顶复核）" % a.settle)
    pump(a.settle)
    if a.fullscreen:
        say("→ 阶段 4：退出全屏，回到桌面再观察 %.1fs（用户截图就是这一刻）" % a.settle)
        chrome_fullscreen_toggle()
        pump(a.settle)
    sampler.stop()

    rows = [r for r in sampler.rows if "ws_main" in r]
    bad = [r for r in sampler.rows if "err" in r]
    if not rows:
        print("!! 一条有效采样都没有", file=sys.stderr)
        return 2

    def seg(lo, hi, label):
        """按采样下标区间报一段的错位统计；区间内无有效样本时如实说 0。

        `blind` 与总口径用**同一条定义**（错位 >2px、旧体检说对、新复核没判、
        且不在拖拽期间），否则分段数字与总数会互相打脸。
        """
        rs = [r for r in rows if lo <= r["_i"] < hi]
        if not rs:
            return {"label": label, "samples": 0}
        return {
            "label": label,
            "samples": len(rs),
            "real_max": round(max(r["real_desync"] for r in rs), 1),
            "real_end": round(max(r["real_desync"] for r in rs[-25:]), 1),
            "model_max": round(max(r["model_desync"] for r in rs), 1),
            "blind": len([r for r in rs if r["real_desync"] > 2.0 and r["top_ok"]
                          and not r["fault"] and not r["follow_active"]]),
            "misaligned": len([r for r in rs if r["real_desync"] > 2.0 and r["fault"]]),
            "glass_on_top": sum(1 for r in rs if r["glass_on_top"]),
        }

    for i, r in enumerate(sampler.rows):
        if "_i" not in r:
            r["_i"] = i
    real = [r["real_desync"] for r in rows]
    tail = rows[-25:]
    # "瞎"的定义（v1.4.33 起）：真实错位 >2px、旧的 `_top_state_ok` 说"已经对了"、
    # **且**新的 `_steady_state_fault` 也没判出故障、**且**不在系统拖拽期间
    # （那段时间背景层归 8ms 跟随定时器管，`_steady_state_fault` 按设计让位）。
    # 四个条件同时成立才叫"没有任何人看得见"。
    blind = [r for r in rows
             if r["real_desync"] > 2.0 and r["top_ok"] and not r["fault"]
             and not r["follow_active"]]
    caught = [r for r in rows if r["real_desync"] > 2.0 and r["fault"]]
    moved = abs(rows[-1]["ws_main"][0] - rows[0]["ws_main"][0])
    final_real = max(r["real_desync"] for r in tail)

    res = {
        "samples": len(rows),
        "dropped": len([r for r in sampler.rows if r.get("offscreen")]),
        "errors": bad[:3],
        "moved_px": round(moved, 1),
        "real_desync_max_px": round(max(real), 1),
        "real_desync_final_px": round(final_real, 1),
        "blind_samples": len(blind),
        "blind_first": blind[0] if blind else None,
        "misaligned_samples": len(caught),
        "faults_caught": sorted({r["fault"] for r in caught}),
        "glass_on_top_at_end": rows[-1]["glass_on_top"],
        "phases": [seg(0, drag_start, "拖动前"),
                   seg(drag_start, drag_end, "拖动中"),
                   seg(drag_end, drag_end + 999999, "松手后")],
        "window": {"main_num": sampler.main_num, "glass_num": sampler.glass_num},
        "verdict": None,
    }
    if moved < max(20.0, abs(a.dx) * 0.3):
        res["verdict"] = ("窗口没被拖走 %.0fpx（实测 %.0fpx）→ 真实鼠标事件未被接受"
                          "（多半缺辅助功能/输入监控权限），本次数据无效" % (a.dx, moved))
    elif final_real > 1.0:
        res["verdict"] = ("松手并静置 %.0fs 后背景仍与内容真实错位 %.1fpx → 永久错位，必须修"
                          % (a.settle, final_real))
    elif res["blind_samples"]:
        res["verdict"] = ("仍有 %d 拍：真实错位 >2px 而旧体检说对、新复核也没判出故障 "
                          "→ 判据还有一处看不见" % res["blind_samples"])
    elif max(real) > 8.0:
        res["verdict"] = ("跟得上且松手归零：真实峰值 %.1fpx / 终值 %.1fpx；"
                          "其中 %d 拍错位被新复核抓到（%s）"
                          % (max(real), final_real, len(caught),
                             "/".join(res["faults_caught"]) or "无"))
    else:
        res["verdict"] = ("跟得上且松手归零：真实峰值 %.1fpx / 终值 %.1fpx；"
                          "旧体检漏掉的 %d 拍错位新复核全部抓到" % (max(real), final_real,
                                                              len(caught)))

    print(json.dumps(res, ensure_ascii=False, indent=2))
    say("\n最后 12 个采样：")
    say("   %8s %10s %10s %10s %10s %8s %6s" % (
        "t(ms)", "model_main", "model_glass", "ws_main", "ws_glass", "real_dsc", "top_ok"))
    for r in rows[-12:]:
        say("   %8.1f %10s %10s %10s %10s %8.1f %6s" % (
            r["t"], r["model_main"], r["model_glass"], r["ws_main"],
            r["ws_glass"], r["real_desync"], r["top_ok"]))
    try:
        win.quit()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
