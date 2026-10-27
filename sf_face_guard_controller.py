"""Own face-guard sessions, pending stops and value state.

The UI host supplies visibility, timers and wakeup callbacks. Authorization and
frame arbitration retain their existing UI-thread entry points.
"""

from __future__ import annotations

import time
from typing import Any

import sf_presence
from sf_logging import log_line, log_swallow

from sf_window_state import FaceGuardValueState


class FaceGuardController:
    def __init__(self, state: FaceGuardValueState | None = None):
        self.state = state or FaceGuardValueState()
        self.monitor = None
        self.arbiter = None
        self.pending_stop: list[Any] = []

    def set_preference(self, enabled: bool) -> None:
        self.state.preference = bool(enabled)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "preference": self.state.preference,
            "paused": self.state.paused,
            "stalled": self.state.stalled,
            "screen_locked": self.state.screen_locked,
            "last_stop": dict(self.state.last_stop),
        }

    def sync(self, host, reason="", stop_wait=True):
        """把"该不该看守"落到现实上。幂等，可以在任何状态变化后调用。

        开摄的两个条件（v1.4.41 复核后定下来的口径，改动前先想清楚再动）：
          ① 窗口可见 —— 有东西需要保护；
          ② 因第二张脸被自动收起、且此刻确实不在屏上 —— 必须继续看着，否则
             "脸变回一张就自动恢复"永远等不到数据来源（用户已确认接受这个取舍：
             自动收起期间摄像头继续运行）。
        **手动**缩小不在其列：那时摄像头立刻停，绿灯灭、不耗电，恢复由用户点 Dock。
        `stop_wait=False` 给交互路径用：主线程绝不等一个正在后台拉起会话的锁。
        """
        if not host._face_guard_available() or not host._face_guard_pref:
            host._stop_face_guard(reason or "off", wait=stop_wait)
            return
        # 退出中的窗口一律不再拉起采集：`_sync_face_guard` 会被延迟排队的 singleShot
        # 与轮询表反复调用，而"进程都要走了还把摄像头打开"既点绿灯，又会在 Qt 析构期
        # 把 ObjC 会话留在一个还在回调的飞线程上（本仓在退出收尾上踩过同类坑）。
        if getattr(host, "_quitting", False):
            host._stop_face_guard("quitting", wait=True)
            return
        a = host._face_guard_arbiter
        # 复核 F1：必须**同时**满足"因第二张脸收起"和"现在不在屏上"。只看前半个标志位
        # 会漏掉一条真实操作序列 —— 自动收起后用户自己点了 Dock 图标恢复，
        # hidden_by_guard 还留着 True，于是下一次他手动缩小窗口时摄像头不停，
        # 约 5 秒后程序还会把窗口替他弹回来，等于推翻他刚做的操作。
        watching = bool(a is not None and a.hidden_by_guard and not host.isVisible())
        if host._face_guard_paused:
            host._stop_face_guard("paused", wait=stop_wait)
            return
        if not (host.isVisible() or watching):
            host._stop_face_guard(reason or "window hidden", wait=stop_wait)
            return
        if a is None:
            a = host._face_guard_arbiter = sf_presence.PresenceArbiter()
        if host._face_guard_monitor is None:
            host._face_guard_monitor = host._face_guard_new_monitor()
        state = host._face_guard_monitor.request_start()
        timer = getattr(host, "_guard_timer", None)
        try:
            if timer is not None and not timer.isActive():
                timer.start()
        except Exception as exc:
            log_swallow("stock_float_window._sync_face_guard", exc)
        # 只在状态**变化**时写一条：本函数会被轮询表按重试节拍反复调用，
        # 每次都写就会让 debug.log 挤满 "start requested"，反而看不出什么时候真起来了。
        if state != host._face_guard_logged_state:
            host._face_guard_logged_state = state
            log_line("face_guard state=%s (%s)" % (state, reason or "-"))

    def stop(self, host, reason="", wait=True, stop_timer=True):
        """停摄像头并清计数；`stop_timer` 决定是否同时停止轮询。绿灯随采集停止而灭，
        所以这条也必须保证被调到。

        `wait` 透传给 monitor.stop()：交互路径（缩小、关功能）传 False，避免主线程
        等后台首次拉起；退出路径传 True，进程走之前必须真停。

        `stop_timer`（v1.4.60，复核 F2）：屏幕锁定/休眠这一支**只停采集、不停轮询表**。
        那张按 `FACE_GUARD_TICK_SEC` 节拍运行的表既是判定循环、也是"解锁后重新武装"的唯一驱动 ——
        一起停掉就等于把报警器交给一个已经被关掉的东西去按：守卫从此不再工作，直到用户手动开关一次菜单
        或重启程序。代价是锁屏期间按该节拍空转两次比较，实测可忽略。

        v1.4.59（S1 第二处）：`stop()` 返回 False 的含义是"**还没停**"，补停跑在 daemon
        线程上，进程一退就被硬杀 → 会话永远没停、绿灯不灭，而诊断包会报
        `camera_held=false`（说谎）。所以这里不许就地丢弃未停完的 monitor：挂进
        `_face_guard_pending_stop`，任何一次 wait=True 的收口先把它兑现。
        """
        # 诊断留痕：停会话的理由（缩小/关功能/中断/锁屏/退出）就地落账，
        # face_guard.last_stop 读取 —— state 归 off 后远程唯一的"为什么停了"。
        host._face_guard_last_stop = {"reason": reason or "", "at": time.time()}
        if wait:
            host._drain_pending_guard_stops(reason)
        mon = host._face_guard_monitor
        if mon is not None:
            try:
                if not mon.stop(wait=wait):
                    host._face_guard_pending_stop.append(mon)
                    log_line("face_guard 停未兑现，挂起等退出收口 (%s)" % (reason or "-"))
            except Exception as exc:
                log_line("face_guard stop failed (%s): %r" % (reason or "-", exc), "WARNING")
            host._face_guard_monitor = None
        a = host._face_guard_arbiter
        if a is not None:
            # 复核 F1：中断（采不到画面）不是用户改了主意，窗口还收在 Dock 里等自动恢复。
            # 这里若一起 reset 掉 hidden_by_guard，30 秒后重试时就会被判成"用户自己收的、
            # 不用看守"而再次停表 —— 自动恢复从此死掉，而菜单上写的还是"等它松开"。
            if reason == "stalled":
                a.clear_streaks()
            else:
                a.reset()
        host._face_guard_logged_state = ""
        host._face_guard_next_retry = 0.0
        host._face_guard_last_fresh = 0.0
        # reason=="stalled" 时**不清** stalled：那正是我们停下来的原因，状态行要留着说。
        if reason != "stalled":
            host._face_guard_stalled = False
        try:
            if (stop_timer and host._guard_timer is not None
                    and host._guard_timer.isActive()):
                host._guard_timer.stop()
        except Exception as exc:
            log_swallow("stock_float_window._stop_face_guard", exc)

    def drain_pending(self, host, reason=""):
        """把之前"没停完"的 monitor 逐个同步停掉（退出收尾、以及任何 wait=True 的收口）。

        每个都单独 try：一个停不掉不影响其余，也不许把退出流程打断 —— 但必须留痕，
        否则"绿灯为什么还亮着"这件事事后无从回答（§6 第 9 条）。
        """
        pending = host._face_guard_pending_stop
        if not pending:
            return
        host._face_guard_pending_stop = []
        deadline = time.monotonic() + 0.25
        for mon in pending:
            if time.monotonic() >= deadline:
                host._face_guard_pending_stop.append(mon)
                continue
            try:
                if not mon.stop(wait=True):
                    host._face_guard_pending_stop.append(mon)
                    log_line("face_guard 退出收口仍未停完 (%s)" % (reason or "-"),
                             "WARNING")
            except Exception as exc:
                log_line("face_guard pending stop error: %r" % (exc,), "WARNING")
