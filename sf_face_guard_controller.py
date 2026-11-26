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


class FaceGuardPort:
    """公开宿主边界；控制器不直接读取窗口私有状态。"""

    def snapshot(self) -> dict[str, Any]:
        raise NotImplementedError

    def ensure_arbiter(self):
        raise NotImplementedError

    def new_monitor(self):
        raise NotImplementedError

    def start_timer(self) -> None:
        raise NotImplementedError

    def stop_timer(self) -> None:
        raise NotImplementedError

    def set_state(self, name: str, value: Any) -> None:
        raise NotImplementedError


class FaceGuardController:
    def __init__(self, state: FaceGuardValueState | None = None):
        self.state = state or FaceGuardValueState()
        self.monitor: Any = None
        self.arbiter: sf_presence.PresenceArbiter | None = None
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

    def sync(self, port: FaceGuardPort, reason="", stop_wait=True):
        """把"该不该看守"落到现实上。幂等，可以在任何状态变化后调用。"""
        snap = port.snapshot()
        if not snap["available"] or not snap["preference"]:
            self.stop(port, reason or "off", wait=stop_wait)
            return
        if snap["quitting"]:
            self.stop(port, "quitting", wait=True)
            return
        a = self.arbiter
        watching = bool(a is not None and a.hidden_by_guard and not snap["visible"])
        if snap["paused"]:
            self.stop(port, "paused", wait=stop_wait)
            return
        if not (snap["visible"] or watching):
            self.stop(port, reason or "window hidden", wait=stop_wait)
            return
        if a is None:
            self.arbiter = sf_presence.PresenceArbiter()
        if self.pending_stop:
            pending = self.pending_stop
            self.pending_stop = []
            for mon in pending:
                try:
                    if not mon.stop(wait=False):
                        self.pending_stop.append(mon)
                except Exception as exc:
                    self.pending_stop.append(mon)
                    log_line("face_guard pending stop retry failed: %r" % (exc,), "WARNING")
            if self.pending_stop:
                try:
                    port.start_timer()
                except Exception as exc:
                    log_swallow("face_guard pending stop timer", exc)
                return
        if self.monitor is None:
            self.monitor = port.new_monitor()
        state = self.monitor.request_start()
        try:
            port.start_timer()
        except Exception as exc:
            log_swallow("stock_float_window._sync_face_guard", exc)
        if state != self.state.logged_state:
            self.state.logged_state = state
            log_line("face_guard state=%s (%s)" % (state, reason or "-"))

    def stop(self, port: FaceGuardPort, reason="", wait=True, stop_timer=True):
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
        self.state.last_stop = {"reason": reason or "", "at": time.time()}
        try:
            port.set_state("last_stop", dict(self.state.last_stop))
        except Exception as exc:
            log_swallow("sf_face_guard_controller.stop(last_stop)", exc)
        if wait:
            self.drain_pending(port, reason)
        elif self.pending_stop:
            pending = self.pending_stop
            self.pending_stop = []
            for old in pending:
                try:
                    if not old.stop(wait=False):
                        self.pending_stop.append(old)
                except Exception as exc:
                    self.pending_stop.append(old)
                    log_line("face_guard pending stop retry failed: %r" % (exc,), "WARNING")
        mon = self.monitor
        if mon is not None:
            try:
                if not mon.stop(wait=wait):
                    self.pending_stop.append(mon)
                    log_line("face_guard 停未兑现，挂起等退出收口 (%s)" % (reason or "-"))
            except Exception as exc:
                self.pending_stop.append(mon)
                log_line("face_guard stop failed (%s): %r" % (reason or "-", exc), "WARNING")
            self.monitor = None
        if wait and self.pending_stop:
            log_line("face_guard 同步停止未停完，保留 %d 个会话待收口 (%s)"
                     % (len(self.pending_stop), reason or "-"), "WARNING")
        a = self.arbiter
        if a is not None:
            if reason in ("stalled", "screen off"):
                a.clear_streaks()
            else:
                a.reset()
        self.state.logged_state = ""
        self.state.next_retry = 0.0
        self.state.last_fresh = 0.0
        if reason != "stalled":
            self.state.stalled = False
        if stop_timer:
            try:
                if self.pending_stop and reason not in ("quitting", "exit"):
                    port.start_timer()
                else:
                    port.stop_timer()
            except Exception as exc:
                log_swallow("stock_float_window._stop_face_guard", exc)

    def drain_pending(self, port: FaceGuardPort, reason=""):
        """同步收口控制器持有的 pending monitor。"""
        pending = self.pending_stop
        if not pending:
            return
        self.pending_stop = []
        deadline = time.monotonic() + 0.25
        for mon in pending:
            if time.monotonic() >= deadline:
                self.pending_stop.append(mon)
                continue
            try:
                if not mon.stop(wait=True):
                    self.pending_stop.append(mon)
                    log_line("face_guard 退出收口仍未停完 (%s)" % (reason or "-"), "WARNING")
            except Exception as exc:
                self.pending_stop.append(mon)
                log_line("face_guard pending stop error: %r" % (exc,), "WARNING")
