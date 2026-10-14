"""行情展示状态控制器。

价格转换、动画和绘制回调仍在兼容层；本对象先拥有展示状态，避免网络线程状态
与渲染中间值继续混在 QWidget 字段里。
"""

from __future__ import annotations

from typing import Any

from sf_window_state import QuoteState


class QuotePresentationController:
    def __init__(self, state: QuoteState | None = None):
        self.state = state or QuoteState()

    def reset_for_stock(self) -> None:
        self.state.quote = None
        self.state.minute_points = []
        self.state.minute_day = ""
        self.state.minute_stale = False
        self.state.real_price = 0.0
        self.state.price_is_fallback = False
        self.state.pct_unknown = False
        self.state.disp_price = None
        self.state.disp_pct = None
        self.state.line_ver += 1
        self.state.chart_changed = True
        self.state.need_redraw = True
        self.state.width_dirty = True

    def diagnostics(self) -> dict[str, Any]:
        return {
            "has_quote": self.state.quote is not None,
            "minute_points": len(self.state.minute_points),
            "minute_day": self.state.minute_day,
            "price_is_fallback": self.state.price_is_fallback,
        }
