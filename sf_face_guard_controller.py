"""人脸守卫值状态控制器。

`sf_presence.FacePresenceMonitor` 的系统会话仍由主窗兼容层管理；这里先集中管理
偏好、暂停/重试和诊断值，避免状态字段继续与窗口几何混杂。
"""

from __future__ import annotations

from typing import Any

from sf_window_state import FaceGuardValueState


class FaceGuardController:
    def __init__(self, state: FaceGuardValueState | None = None):
        self.state = state or FaceGuardValueState()

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
