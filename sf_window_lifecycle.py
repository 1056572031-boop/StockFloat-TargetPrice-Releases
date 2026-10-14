"""主窗生命周期状态控制器。"""

from __future__ import annotations

from typing import Any

from sf_window_state import LifecycleState


class WindowLifecycleController:
    """集中管理运行、退出和线程收口三个幂等状态。"""

    def __init__(self, state: LifecycleState | None = None):
        self.state = state or LifecycleState()

    @property
    def running(self) -> bool:
        return self.state.running

    @running.setter
    def running(self, value: bool) -> None:
        self.state.running = bool(value)

    @property
    def quitting(self) -> bool:
        return self.state.quitting

    @property
    def threads_stopped(self) -> bool:
        return self.state.threads_stopped

    def begin_quit(self) -> bool:
        if self.state.quitting:
            return False
        self.state.quitting = True
        return True

    def mark_threads_stopped(self) -> bool:
        if self.state.threads_stopped:
            return False
        self.state.threads_stopped = True
        return True

    def stop_running(self) -> None:
        self.state.running = False

    def diagnostics(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "quitting": self.quitting,
            "threads_stopped": self.threads_stopped,
        }
