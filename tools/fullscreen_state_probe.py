#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/fullscreen_state_probe.py —— 第三方全屏 Space 下的"位置跳变/只剩背景"取证。

用户 2026-09-24 报了两件事，都发生在"浏览器全屏"这个前置条件下：
  ① 全屏期间拖动主窗，再点「收起分时图」，主窗坐标突然跳到别处；
  ② 全屏后回到桌面，主窗只剩毛玻璃背景，元素看不见但点得动。

两类都不是绘制问题，而是**几何与合成状态**问题，只能同帧对比：

    model_*   应用侧模型（产品做判据的依据）
    ws_*      WindowServer 里的真实 bounds（用户眼睛看到的）
    截图       唯一的"用户此刻看到什么" ground truth

判据：
  ① 跳变落点是不是**拖动之前的旧位置** —— 是，则根因为"写几何用了过时模型"；
     不是，则要另找搬走窗口的那一次 move()。所以本工具专门记 `jump_target_is_old`。
  ② `glass_on_top` 连续区间有多长、两窗各自 isVisible / isOnActiveSpace / 在不在
     屏列表 —— 用来区分"被背景层压住"与"内容窗根本没合成"。

两种观察档位：
  --external  旁观**正在运行的正式版**（默认推荐：现场最真实）。全程不 import 产品
              模块，只读 WindowServer + 发 HID 事件 + 截图。窗口号取自产品自己写的
              `main=… glass=…` 日志行（仓库规矩：只认产品侧报的真实窗口号）。
  默认        另起一个源码窗口，能额外看到 Qt 侧缓存几何与产品的手势标志位。

用法（需要辅助功能权限；会在用户屏幕上真的动鼠标）：
    ./.venv/bin/python tools/fullscreen_state_probe.py --external --jump
    ./.venv/bin/python tools/fullscreen_state_probe.py --external --occlusion
    ./.venv/bin/python tools/fullscreen_state_probe.py --jump --inprocess
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
REAL_LOG = os.path.expanduser("~/Library/Application Support/StockFloatTargetPrice/debug.log")
HOME = tempfile.mkdtemp(prefix="sf_fs_probe_")

# 分时图点击热区（展开态）：y ∈ [CHART_TOP, h-BOTTOM_SECTION_HIDDEN]，取其中点。
CHART_TOP = 108
CHART_BOTTOM_KEEP = 46
# 顶部按下点：避开右上四个按钮与中下热区。
DRAG_GRAB_X, DRAG_GRAB_Y = 20.0, 8.0


# ============================ WindowServer 侧 ============================

def ws_snapshot() -> list:
    """当前在屏窗口 [(号, 属主, x, y, w, h, 顺序下标)]，越靠前越在上。"""
    import Quartz
    out = []
    lst = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly,
                                            Quartz.kCGNullWindowID)
    for i, info in enumerate(lst):
        b = info.get("kCGWindowBounds")
        if not b:
            continue
        out.append((int(info.get(Quartz.kCGWindowNumber, -1)),
                    str(info.get("kCGWindowOwnerName") or ""),
                    float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"]),
                    i))
    return out


def pair_numbers(log_path: str = REAL_LOG) -> tuple | None:
    """从产品自己的日志里取**本轮进程**的 (main, glass) 窗口号。

    仓库已记录过一次教训：两窗 kCGWindowLayer 都是 25，用 layer/亮度猜哪个是背景层
    会误报。唯一可信来源是产品侧打印的 `main=… glass=…`；取不到时调用方自行降级。
    """
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
    except OSError:
        return None
    runs = re.findall(r"STARTUP v\S+ run=(\w+)", txt)
    if not runs:
        return None
    tail = txt.split("run=" + runs[-1], 1)[1]
    m = re.findall(r"\(main=(\d+) glass=(\d+)\)", tail)
    return (int(m[-1][0]), int(m[-1][1])) if m else None


def guess_pair(snap: list, owner: str) -> tuple | None:
    """退路：按属主找出两条同尺寸窗口，号小者当内容窗（玻璃窗晚于它创建）。

    只在日志里查不到 `main=… glass=…` 时使用，调用方必须在结论里注明是猜的。
    """
    mine = [e for e in snap if owner in e[1]]
    by_num = sorted(mine, key=lambda e: e[0])
    if len(by_num) < 2:
        return None
    return (by_num[0][0], by_num[1][0])


# ============================ 采样器 ============================

class Sampler:
    """8ms 一拍记三方几何 + 合成顺序 + 截图锚点。

    两种模式共用：`win` 为 None 时是纯外部旁观（不碰任何产品对象），
    非 None 时额外记 Qt 侧缓存几何与产品的手势标志位。
    """

    def __init__(self, main_num: int, glass_num: int, win=None,
                 interval: float = 0.008) -> None:
        self.win = win
        self.rows: list = []
        self.phase = "setup"
        self.t0 = time.perf_counter()
        self.main_num, self.glass_num = int(main_num), int(glass_num)
        self._interval = interval
        # 这两个只在 in-process 档有值（真 NSWindow / objc 桥拿回来的对象），
        # 静态侧没有存根可推，标成 Any：本文件要量的是运行时行为，不是类型。
        self._nsw: Any = None
        self._gw: Any = None
        self._thread: threading.Thread | None = None
        self._stop_flag: threading.Event | None = None
        if win is not None:
            # in-process 档要挂原生 run loop：系统拖拽/全屏切换期间 AppKit 跑在
            # NSEventTrackingRunLoopMode 模态循环里，QTimer 整段不被唤醒，
            # 而那正是要看的时间窗（sf_glass._glass_follow_begin 同一个道理）。
            from CoreFoundation import (CFAbsoluteTimeGetCurrent, CFRunLoopAddTimer,
                                        CFRunLoopGetMain, CFRunLoopTimerCreate,
                                        kCFRunLoopCommonModes)
            import CoreFoundation
            import objc
            self._nsw = objc.objc_object(c_void_p=int(win.winId())).window()
            self._gw = win._glass_window

            def _cb(_timer, _info) -> None:
                try:
                    self.tick()
                except Exception as exc:                # 采样器绝不能弄死被测对象
                    self.rows.append({"err": repr(exc), "t": 0.0, "phase": self.phase})

            self._keep = _cb
            self.timer = CFRunLoopTimerCreate(None, CFAbsoluteTimeGetCurrent() + interval,
                                              interval, 0, 0, _cb, None)
            self._add, self._get, self._modes = (CFRunLoopAddTimer, CFRunLoopGetMain,
                                                 kCFRunLoopCommonModes)
            self._invalidate = CoreFoundation.CFRunLoopTimerInvalidate
        else:
            # 纯外部旁观：本进程没有 run loop 可挂（主线程在挂钟 sleep），
            # 用后台线程轮询 WindowServer 即可。
            self._stop_flag = threading.Event()
            self.timer = None

    def start(self) -> None:
        if self.timer is not None:
            self._add(self._get(), self.timer, self._modes)
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        assert self._stop_flag is not None
        while not self._stop_flag.wait(self._interval):
            try:
                self.tick()
            except Exception as exc:
                self.rows.append({"err": repr(exc), "t": 0.0, "phase": self.phase})

    def stop(self) -> None:
        if self.timer is not None:
            self._invalidate(self.timer)
            return
        if self._stop_flag is not None:
            self._stop_flag.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def tick(self) -> None:
        nsw, gw = self._nsw, self._gw
        snap = ws_snapshot()
        by = {e[0]: e for e in snap}
        m, g = by.get(self.main_num), by.get(self.glass_num)
        row = {"t": round((time.perf_counter() - self.t0) * 1000, 1),
               "phase": self.phase,
               "ws_main": (m[2], m[3], m[4], m[5]) if m else None,
               "ws_glass": (g[2], g[3], g[4], g[5]) if g else None,
               "i_main": m[6] if m else None,
               "i_glass": g[6] if g else None,
               "glass_on_top": bool(g and m and g[6] < m[6]),
               "main_on": m is not None, "glass_on": g is not None}
        if self.win is not None and nsw is not None and gw is not None:
            q = self.win
            mf, gf = nsw.frame(), gw.frame()
            row.update({
                "qt": (int(q.x()), int(q.y()), int(q.width()), int(q.height())),
                "exp": bool(getattr(q, "_chart_expanded", False)),
                "model_main": (round(mf.origin.x, 1), round(mf.origin.y, 1),
                               round(mf.size.width, 1), round(mf.size.height, 1)),
                "model_glass": (round(gf.origin.x, 1), round(gf.origin.y, 1),
                                round(gf.size.width, 1), round(gf.size.height, 1)),
                "nsw_vis": bool(nsw.isVisible()),
                "gw_vis": bool(gw.isVisible()),
                "nsw_space": bool(nsw.isOnActiveSpace()),
                "gw_space": bool(gw.isOnActiveSpace()),
                "sys_drag": bool(getattr(q, "_sys_drag", False)),
                "drag": bool(getattr(q, "_drag", False)),
                "press_chart": bool(getattr(q, "_press_chart", False)),
                "follow": getattr(q, "_glass_follow_timer", None) is not None,
            })
        self.rows.append(row)


# ============================ 真实 HID 事件 ============================

def post(kind: int, x: float, y: float, button) -> None:
    import Quartz
    ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), button)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def post_move(x: float, y: float) -> None:
    import Quartz
    post(Quartz.kCGEventMouseMoved, x, y, Quartz.kCGMouseButtonLeft)


def drag_thread(x: float, y: float, dx: float, dy: float, steps: int,
                hold_ms: float, down_at: float) -> None:
    """在**另一个线程**发事件：主线程此刻可能正卡在 AppKit 的模态拖拽循环里。"""
    import Quartz
    DOWN = Quartz.kCGMouseButtonLeft
    time.sleep(down_at)
    post_move(x, y)
    time.sleep(0.05)
    post(Quartz.kCGEventLeftMouseDown, x, y, DOWN)
    time.sleep(0.05)
    for i in range(1, steps + 1):
        post(Quartz.kCGEventLeftMouseDragged, x + dx * i / steps,
             y + dy * i / steps, DOWN)
        time.sleep(hold_ms / 1000.0)
    time.sleep(0.25)
    post(Quartz.kCGEventLeftMouseUp, x + dx, y + dy, DOWN)


def click_thread(x: float, y: float, down_at: float) -> None:
    import Quartz
    DOWN = Quartz.kCGMouseButtonLeft
    time.sleep(down_at)
    post_move(x, y)
    time.sleep(0.12)
    post(Quartz.kCGEventLeftMouseDown, x, y, DOWN)
    time.sleep(0.06)
    post(Quartz.kCGEventLeftMouseUp, x, y, DOWN)


_SPACE: dict = {}


def active_space_id():
    """当前活动 Space 的 id（私有 CGS，与产品 sf_glass.active_space_id 同一口径）。

    为什么用它当"进/出全屏"的唯一判据：读 Chrome 菜单项这条路**会说谎** ——
    Chrome 不在前台时 AX 给的是陈旧菜单，实测桌面状态下仍数出 2 条"退出全屏幕"，
    于是连续六轮"没进全屏"的数据被标成 entered=True，还据此得出"新版不再复现"的
    错误结论。Space id 由 WindowServer 给：进全屏必然换 id、退回来必然换回去，
    与任何 app 的可访问性实现无关。
    """
    try:
        import ctypes
        if "cg" not in _SPACE:
            cg = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGSMainConnectionID.restype = ctypes.c_uint32
            cg.CGSGetActiveSpace.restype = ctypes.c_uint64
            cg.CGSGetActiveSpace.argtypes = [ctypes.c_uint32]
            _SPACE["cg"] = cg
        cg = _SPACE["cg"]
        return int(cg.CGSGetActiveSpace(cg.CGSMainConnectionID())) or None
    except Exception:
        return None


def chrome_activate() -> None:
    subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'],
                   check=False, timeout=15)
    time.sleep(1.2)


def chrome_frontmost() -> bool:
    cp = subprocess.run(["osascript", "-e",
                         'tell application "System Events" to '
                         'return name of first process whose frontmost is true'],
                        capture_output=True, text=True, timeout=20)
    return "Chrome" in cp.stdout


def _fs_click() -> bool:
    """点 Chrome 的全屏菜单项 —— 两条名字哪条在点哪条（状态读不准，就别依赖它）。"""
    for title in ("进入全屏幕", "退出全屏幕"):
        script = ('tell application "System Events" to tell process "Google Chrome" to '
                  'click (first menu item of menu "显示" of menu bar item "显示" '
                  'of menu bar 1 whose name is "%s")' % title)
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                           timeout=25)
        if r.returncode == 0:
            return True
    return False


def chrome_fullscreen_set(want: bool, say=print, desktop_space=None, timeout: float = 10.0) -> bool:
    """把 Chrome 摆成 want 全屏；**只认活动 Space id 变没变**。

    `desktop_space` 由调用方在进入全屏之前、Chrome 已在前台时记下（那是"回桌面"要
    回到的锚点）。不传就地补一次。
    """
    for _ in range(4):
        if desktop_space is None:
            chrome_activate()
            desktop_space = active_space_id()
        now = active_space_id()
        if now is None or desktop_space is None:
            say("!! 拿不到活动 Space id，本轮没有可信的全屏前置")
            return False
        if (now != desktop_space) == want:
            return True
        chrome_activate()
        if not chrome_frontmost():
            say("!! Chrome 没到前台，菜单点击不会生效")
        _fs_click()
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(0.4)
            cur = active_space_id()
            if cur is not None and (cur != desktop_space) == want:
                return True
        say("   Space 没变成 want=%s（现在=%s 锚点=%s），再试一次" % (want, now, desktop_space))
    return False




def shot(tag: str, region=None) -> str:
    """整屏（或指定区域）截图：唯一的"用户看到什么" ground truth。"""
    p = os.path.join(HOME, "%s.png" % tag)
    cmd = ["/usr/sbin/screencapture", "-x"]
    if region:
        cmd += ["-R%d,%d,%d,%d" % tuple(int(v) for v in region)]
    cmd.append(p)
    subprocess.run(cmd, check=False, timeout=20)
    return p


# ============================ 分析 ============================

def _runs(rows: list, pred) -> list:
    """把满足 pred 的拍合并成区间：'空壳持续多久'才是用户关心的量。"""
    out = []
    start = None
    for r in rows:
        if pred(r):
            if start is None:
                start = r
        elif start is not None:
            out.append({"from_ms": start["t"], "to_ms": r["t"],
                        "dur_s": round((r["t"] - start["t"]) / 1000.0, 2),
                        "phase": start["phase"]})
            start = None
    if start is not None:
        out.append({"from_ms": start["t"], "to_ms": rows[-1]["t"],
                    "dur_s": round((rows[-1]["t"] - start["t"]) / 1000.0, 2),
                    "phase": start["phase"]})
    return out


def big_moves(rows: list, key: str, min_px: float = 8.0) -> list:
    """相邻两拍之间该键原点位移超过 min_px 的每一拍（按时间序，不按大小）。"""
    out = []
    prev = None
    for r in rows:
        cur = r.get(key)
        if not cur:
            prev = None
            continue
        if prev is not None:
            d = max(abs(cur[0] - prev[0]), abs(cur[1] - prev[1]))
            if d >= min_px:
                out.append({"t": r["t"], "phase": r["phase"], "d_px": round(d, 1),
                            "from": [round(prev[0], 1), round(prev[1], 1)],
                            "to": [round(cur[0], 1), round(cur[1], 1)]})
        prev = cur
    return out


def settled_positions(rows: list, min_s: float = 0.4) -> list:
    """卡片"真正停在哪儿"的时间线：连续同位置且停够 0.4 秒才算一站。

    为什么需要它：`ws_main_moves` 只给"哪一拍动得最狠"，回答不了用户那句
    "坐标位置突然跳变到别的地方去" —— 要看的是**停下来之后的落点序列**。
    """
    out = []
    cur = None
    start = None
    for r in rows:
        p = r.get("ws_main")
        key = (round(p[0]), round(p[1]), round(p[2]), round(p[3])) if p else None
        if key != cur:
            if cur and start is not None and r["t"] - start >= min_s * 1000.0:
                out.append({"t_ms": start, "stay_s": round((r["t"] - start) / 1000.0, 2),
                            "at": cur})
            cur, start = key, r["t"]
    if cur and start is not None:
        out.append({"t_ms": start, "stay_s": round((rows[-1]["t"] - start) / 1000.0, 2),
                    "at": cur})
    return [e for e in out if e["at"]]


def analyze(rows: list) -> dict:
    for r in rows:                                   # qt 原点转成可与 CG 相减的浮点元组
        q = r.get("qt")
        if q:
            r["qt_key"] = (float(q[0]), float(q[1]))
    good = [r for r in rows if r.get("ws_main")]
    res = {
        "samples": len(rows),
        "rows_missing_main": len([r for r in rows if not r.get("ws_main")]),
        "rows_missing_pair": len([r for r in rows
                                  if not (r.get("ws_main") and r.get("ws_glass"))]),
        "ws_main_moves": [e for e in big_moves(good, "ws_main") if e["d_px"] >= 20.0],
        "glass_on_top_runs": _runs(rows, lambda r: bool(r.get("glass_on_top"))),
        "main_offscreen_runs": _runs(rows, lambda r: not r.get("main_on")),
        "glass_offscreen_runs": _runs(rows, lambda r: not r.get("glass_on")),
        "settled_positions": settled_positions(rows),
        "phases": sorted({str(r["phase"]) for r in rows}),
    }
    if any(r.get("qt") for r in rows):
        res["qt_moves"] = [e for e in big_moves(rows, "qt_key") if e["d_px"] >= 20.0]
        res["lag_before_end"] = _lag(rows, rows[-1]["t"])
    return res


def _lag(rows: list, t_target: float) -> dict:
    """t_target 前 0.5 秒内，Qt 模型与 WindowServer 真实值各差多少。

    Qt 全局坐标与 CG 全局坐标同为"主屏左上、y 向下"，单屏下可直接相减。
    """
    seg = [r for r in rows if t_target - 500.0 <= r["t"] < t_target
           and r["ws_main"] and r.get("qt")]
    if not seg:
        return {}
    r = seg[-1]
    ws, qt = r["ws_main"], r["qt"]
    return {"t": r["t"], "qt_xy": [qt[0], qt[1]],
            "ws_xy": [round(ws[0], 1), round(ws[1], 1)],
            "lag_px": round(max(abs(ws[0] - qt[0]), abs(ws[1] - qt[1])), 1),
            "qt_h": qt[3], "ws_h": round(ws[3], 1), "expanded": r.get("exp")}


def jump_target_is_pre_drag(rows: list, drag_lo: float, drag_hi: float) -> dict:
    """①的核心判据：跳变的落点，是不是"拖动开始之前"的位置。"""
    before = [r["ws_main"] for r in rows if r["t"] < drag_lo and r["ws_main"]]
    after = [r for r in rows if r["t"] > drag_hi and r["ws_main"]]
    if not before or not after:
        return {}
    p0 = before[0]
    out = []
    for r in after:
        d = max(abs(r["ws_main"][0] - p0[0]), abs(r["ws_main"][1] - p0[1]))
        if d < 12.0:
            out.append({"t": r["t"], "phase": r["phase"], "dist_to_pre_drag_px": round(d, 1),
                        "at": [round(r["ws_main"][0], 1), round(r["ws_main"][1], 1)]})
    return {"pre_drag_xy": [round(p0[0], 1), round(p0[1], 1)],
            "back_to_pre_drag": out[:5],
            "n_back": len(out)}


# ============================ 主流程 ============================

def _order(sampler) -> tuple:
    """(内容窗下标, 背景层下标, glass_on_top)；任一不在屏 → (None,None,None)。"""
    snap = {e[0]: e for e in ws_snapshot()}
    mm, gg = snap.get(sampler.main_num), snap.get(sampler.glass_num)
    if not mm or not gg:
        return None, None, None
    return mm[6], gg[6], gg[6] < mm[6]


def cgs_step_probe(sampler, win, nsw, gw, pump, say, label: str) -> list:
    """逐个单调用，看**哪一次调用把合成顺序弄倒**。

    为什么这样设计：正式版日志 16:08:34~16:08:43 连着 9 条 `re-top: glass above main`
    —— 每 1.25 秒判出一次、每 250ms 也在跑完整置顶，顺序却还是倒的。这说明"修"这个
    动作里有一步在跟另一步打架，而不是"没人修"。要抓出是哪一步，必须**一次只做一件事**
    并立刻读顺序；先把产品自己的看门狗停掉，否则每次读数都混进了它的动作。

    全程只动本进程的两个窗，不碰用户配置。
    """
    from sf_glass import public_overlay_collection_behavior
    steps = []

    def rec(name, fn):
        before = _order(sampler)
        try:
            fn()
        except Exception as exc:
            steps.append({"step": name, "error": repr(exc)})
            return
        pump(0.35)
        after = _order(sampler)
        steps.append({"step": name, "env": label,
                      "before": list(before), "after": list(after),
                      "flipped_to_glass_top": bool(after and after[2] and not before[2])})
        say("   [%s] %-34s 顺序 %s → %s%s" %
            (label, name, before, after, "  ← 倒序" if (after and after[2]
                                                        and not before[2]) else ""))

    timer = getattr(win, "top_timer", None)
    if timer is not None:
        timer.stop()
    try:
        rec("baseline（什么都没做）", lambda: None)
        rec("glass.orderFront + main.orderFrontRegardless",
            lambda: (gw.orderFront_(None), nsw.orderFrontRegardless()))
        rec("只 main.orderFrontRegardless", lambda: nsw.orderFrontRegardless())
        rec("CGSAddWindowsToSpaces(主窗)", lambda: win._force_join_active_space(nsw))
        rec("再 main.orderFrontRegardless", lambda: nsw.orderFrontRegardless())
        rec("CGSAddWindowsToSpaces(背景层)", lambda: win._force_join_active_space(gw))
        rec("再 main.orderFrontRegardless", lambda: nsw.orderFrontRegardless())
        rec("setCollectionBehavior_(公开组合)",
            lambda: nsw.setCollectionBehavior_(public_overlay_collection_behavior()))
        rec("再 main.orderFrontRegardless", lambda: nsw.orderFrontRegardless())
        rec("产品完整 _force_top()", lambda: win._force_top())
    finally:
        if timer is not None:
            timer.start()
    return steps


def attach_external(owner: str, say) -> tuple:
    """不建窗、不 import 产品：只从 WindowServer + 产品日志认领正在跑的那个正式版。"""
    num = pair_numbers()
    src = "产品日志 main=/glass="
    snap = ws_snapshot()
    if num is None:
        num = guess_pair(snap, owner)
        src = "退路：按属主找两条同尺寸窗口，号小者当内容窗（猜的）"
    if num is None:
        raise SystemExit("!! 找不到 %r 的两条窗口；正式版在跑吗？" % owner)
    say("窗口号 main=%d glass=%d（来源：%s）" % (num[0], num[1], src))
    by = {e[0]: e for e in snap}
    if num[0] not in by:
        raise SystemExit("!! 内容窗 %d 不在屏列表里" % num[0])
    return num[0], num[1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="全屏 Space 下的位置/可见性跳变取证")
    ap.add_argument("--jump", action="store_true", help="手势：全屏→拖动→点收起→退全屏")
    ap.add_argument("--occlusion", action="store_true", help="手势：全屏→回桌面看合成")
    ap.add_argument("--cgs", action="store_true",
                    help='逐调用读合成顺序，抓是哪一步把背景层抬到内容窗之上')
    ap.add_argument("--external", action="store_true",
                    help="旁观正在运行的正式版（默认档，现场最真实）")
    ap.add_argument("--inprocess", action="store_true",
                    help="另起源码窗口，额外看 Qt 缓存与产品标志位")
    ap.add_argument("--owner", default="实时股价")
    ap.add_argument("--dx", type=float, default=300.0)
    ap.add_argument("--dy", type=float, default=160.0)
    ap.add_argument("--steps", type=int, default=28)
    ap.add_argument("--hold-ms", type=float, default=20.0)
    ap.add_argument("--settle", type=float, default=3.0)
    ap.add_argument("--record", type=float, default=0.0,
                    help="只旁观 N 秒，一个鼠标事件都不发：让用户自己做真实手势")
    ap.add_argument("--no-fs", action="store_true",
                    help="不做全屏（用来对照'全屏到底是不是必要条件'，也用来验证工具）")
    ap.add_argument("--dump", action="store_true", help="把逐拍原始数据写到临时目录")
    args = ap.parse_args(argv)
    if not (args.jump or args.occlusion or args.cgs or args.record):
        ap.print_help()
        return 2

    def say(*args_):
        print(*args_, flush=True)

    app = win = None
    probe_nsw = None
    pump = _make_pump(app)
    if args.inprocess:
        os.environ["QT_QPA_PLATFORM"] = "cocoa"
        os.environ["SF_CONFIG_PATH"] = os.path.join(HOME, "config.json")
        os.environ["SF_LOG_PATH"] = os.path.join(HOME, "debug.log")
        os.environ["SF_UNIVERSE_PATH"] = os.path.join(HOME, "securities.json")
        os.environ["SF_LAST_QUOTE_PATH"] = os.path.join(HOME, "last_quote.json")
        os.environ["SF_SINGLE_SOCK"] = os.path.join(HOME, "single.sock")
        os.environ["SF_SEED_FIRST_SCREEN"] = "0"
        if PROJ not in sys.path:
            sys.path.insert(0, PROJ)
        from PyQt6.QtWidgets import QApplication
        import sf_ui_render as R
        import stock_float_window as m
        app = QApplication.instance() or QApplication([])
        # load_bundled_fonts 必须在 QApplication 之后：它走
        # QFontDatabase.addApplicationFont，没有 QGuiApplication 实例时**直接段错误**
        # （本机实测 rc=139，faulthandler 落在 QtGui 的 addAppFont）。
        R.load_bundled_fonts()
        win = m.FloatWindow()
        win.move(760, 420)
        win.show()
        pump = _make_pump(app)
        deadline = time.time() + 10.0
        while time.time() < deadline:
            pump(0.03)
            if getattr(win, "_glass_window", None) is not None:
                break
        if getattr(win, "_glass_window", None) is None:
            say("!! 玻璃层没建起来，无法核验")
            return 2
        import objc
        nsw = objc.objc_object(c_void_p=int(win.winId())).window()
        probe_nsw = nsw
        main_num = int(nsw.windowNumber())
        glass_num = int(win._glass_window.windowNumber())
        # 必须把本进程变成**活动应用**：`startSystemMove()` 走的是
        # `performWindowDragWithEvent:`，非活动应用的背景窗会被系统当成"先激活"，
        # 该方法当场返回 False → 手势退化成自绘拖拽，而用户那一次走的是系统拖拽。
        # 复现错路径的探针，量到的不是用户的路径。
        import AppKit
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        win.activateWindow()
        pump(0.8)
    else:
        main_num, glass_num = attach_external(args.owner, say)

    sampler = Sampler(main_num, glass_num, win)
    sampler.start()
    # 先喂一轮：CFRunLoopTimer 第一拍要等 interval，没样本时后面的 rows[-1] 会撞空
    pump(0.3)
    say("采样开始（8ms / common modes），临时目录 %s" % HOME)

    def now_t() -> float:
        return sampler.rows[-1]["t"] if sampler.rows else 0.0

    def real_main() -> tuple | None:
        by = {e[0]: e for e in ws_snapshot()}
        e = by.get(sampler.main_num)
        return (e[2], e[3], e[4], e[5]) if e else None

    drag_window = {"lo": 0.0, "hi": 0.0}
    fs = {"entered": False, "exited": False}
    cgs_steps = []
    # 进全屏**之前**先把 Chrome 带到前台并记下它的桌面 Space id —— 后面"进/出全屏
    # 到底有没有发生"只认这个锚点（菜单项口径会说谎，见 active_space_id 的注释）。
    dspace = None
    if not args.no_fs:
        chrome_activate()
        dspace = active_space_id()
        say("锚点：Chrome 桌面 Space id = %s" % dspace)

    if args.record:
        say("→ 只旁观 %.0f 秒，本工具一个鼠标/键盘事件都不发。" % args.record)
        say("   请现在按报障时的顺序做一遍：浏览器全屏 → 拖动主界面 → "
            "点收起分时走势图 → 回到桌面")
        sampler.phase = "user_gesture"
        pump(args.record)

    if args.cgs:
        if win is None:
            raise SystemExit("--cgs 需要 --inprocess（要直接调产品的置顶路径）")
        say("→ 阶段 1：Chrome 进系统全屏，在全屏 Space 内逐调用读顺序")
        fs["entered"] = chrome_fullscreen_set(True, say, dspace)
        pump(3.0)
        cgs_steps += cgs_step_probe(sampler, win, probe_nsw, win._glass_window,
                                    pump, say, "全屏Space")
        say("→ 阶段 2：退出全屏回桌面，再来一遍")
        fs["exited"] = chrome_fullscreen_set(False, say, dspace)
        pump(3.0)
        cgs_steps += cgs_step_probe(sampler, win, probe_nsw, win._glass_window,
                                    pump, say, "回桌面")

    if args.jump:
        shot("p0_base")
        if not args.no_fs:
            say("→ 阶段 1：Chrome 进系统全屏")
            sampler.phase = "enter_fs"
            fs["entered"] = chrome_fullscreen_set(True, say, dspace)
            pump(3.0)
            shot("p1_fullscreen")
        b = real_main()
        if not b:
            say("!! 查不到内容窗，后面没有落点")
            return 2
        say("→ 阶段 2：真实拖动，窗在 (%.0f,%.0f)，按下点 (%.0f,%.0f)" %
            (b[0], b[1], b[0] + DRAG_GRAB_X, b[1] + DRAG_GRAB_Y))
        sampler.phase = "drag"
        drag_window["lo"] = now_t()
        t = threading.Thread(target=drag_thread,
                             args=(b[0] + DRAG_GRAB_X, b[1] + DRAG_GRAB_Y,
                                   args.dx, args.dy, args.steps, args.hold_ms, 0.2),
                             daemon=True)
        t.start()
        pump(0.3 + (args.steps * args.hold_ms / 1000.0) + 0.6)
        drag_window["hi"] = now_t()
        sampler.phase = "after_drag"
        say("→ 阶段 3：松手后静置 %.1fs" % args.settle)
        pump(args.settle)
        shot("p2_after_drag")

        b = real_main()
        if not b:
            say("!! 松手后查不到内容窗，跳过点击阶段")
        else:
            say("   拖动后窗在 (%.0f,%.0f)" % (b[0], b[1]))
            sampler.phase = "click_collapse"
            cy = CHART_TOP + max(6.0, (b[3] - CHART_TOP - CHART_BOTTOM_KEEP) / 2.0)
            say("→ 阶段 4：点击分时图热区 (%.0f,%.0f) —— 用户报跳变就在这一拍" %
                (b[0] + b[2] / 2.0, b[1] + cy))
            t = threading.Thread(target=click_thread,
                                 args=(b[0] + b[2] / 2.0, b[1] + cy, 0.2), daemon=True)
            t.start()
            pump(1.2)
            sampler.phase = "after_click"
            pump(args.settle)
            b2 = real_main()
            if b2:
                say("   点击后窗在 (%.0f,%.0f)" % (b2[0], b2[1]))
        shot("p3_after_click")
        if not args.no_fs:
            say("→ 阶段 5：退出全屏回桌面")
            sampler.phase = "exit_fs"
            fs["exited"] = chrome_fullscreen_set(False, say, dspace)
            pump(args.settle)
            shot("p4_back_desktop")

    if args.occlusion:
        say("→ 进全屏停 %.1fs，回桌面再观察 %.1fs" % (args.settle, args.settle * 2))
        sampler.phase = "enter_fs"
        fs["entered"] = chrome_fullscreen_set(True, say, dspace)
        pump(args.settle)
        shot("o1_fullscreen")
        sampler.phase = "exit_fs"
        fs["exited"] = chrome_fullscreen_set(False, say, dspace)
        end = time.time() + args.settle * 2
        next_shot = 0.0
        marks = []
        while time.time() < end:
            pump(0.25)
            if time.time() >= next_shot:
                next_shot = time.time() + 1.0
                b = real_main()
                last = sampler.rows[-1] if sampler.rows else {}
                p = shot("o2_%05.0fms" % last.get("t", 0.0),
                         (b[0] - 20, b[1] - 20, b[2] + 40, b[3] + 40) if b else None)
                marks.append({"t_ms": last.get("t"), "glass_on_top": last.get("glass_on_top"),
                              "main_on": last.get("main_on"), "file": p})
        sampler.phase = "done"
        pump(0.4)
        say("区域截图（看'用户此刻看到什么'）：")
        for s in marks:
            say("  t=%s ms glass_on_top=%s main_on=%s → %s" %
                (s["t_ms"], s["glass_on_top"], s["main_on"], s["file"]))

    sampler.stop()
    res = analyze(sampler.rows)
    res["fullscreen"] = dict(fs)
    res["gesture_reached_window"] = bool(
        [r for r in sampler.rows if r.get("drag") or r.get("sys_drag")
         or r.get("press_chart")])
    if not args.no_fs and not (fs["entered"] and fs["exited"]):
        res["verdict"] = ("全屏前置没成立（entered=%s exited=%s）→ 本轮数据不能用来"
                          "回答'全屏下会不会跳'" % (fs["entered"], fs["exited"]))
    if drag_window["hi"]:
        res["jump_target_is_old"] = jump_target_is_pre_drag(
            sampler.rows, drag_window["lo"], drag_window["hi"])
    clicks = [r for r in sampler.rows if r["phase"] == "click_collapse"]
    if clicks:
        res["lag_before_click"] = _lag(sampler.rows, clicks[0]["t"])
    res["shots_dir"] = HOME
    if cgs_steps:
        res["cgs_steps"] = cgs_steps
        res["cgs_flipped"] = [e for e in cgs_steps if e.get("flipped_to_glass_top")]
    if args.dump:
        p = os.path.join(HOME, "rows.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(sampler.rows, fh, ensure_ascii=False)
        res["rows_dump"] = p
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def _make_pump(app):
    """有 Qt 应用时喂事件循环；纯外部旁观时用挂钟。"""
    if app is None:
        return lambda seconds: time.sleep(seconds)

    def _pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            # 不用 QTest.qWait：它的存根把 qWait 声明成需要实例的绑定方法，
            # 静态闸门会为此记一条 call-arg；这里要的就是"让事件循环转 20ms"，
            # processEvents + 挂钟 sleep 等价，且少一个依赖。
            time.sleep(0.02)
    return _pump


if __name__ == "__main__":
    raise SystemExit(main())
