"""目标价状态控制器。"""

from __future__ import annotations

from typing import Any

from sf_window_state import TargetState


class TargetController:
    def __init__(self, state: TargetState | None = None):
        self.state = state or TargetState()

    @property
    def alerts(self) -> list[Any]:
        return self.state.alerts

    @alerts.setter
    def alerts(self, value: list[Any]) -> None:
        self.state.alerts = value

    def clear(self) -> None:
        self.state.alerts = []

    def diagnostics(self) -> dict[str, int]:
        return {"alerts": len(self.state.alerts)}
