# -*- coding: utf-8 -*-
"""收盘后打开程序，显示的必须是接口给的真实收盘价，不是旧分时缓存里的价格。

起因（2026-10-29 用户报障）：收盘后启动，价格与涨跌幅不是最新，必须手动点一次
「刷新行情」才对。实测根因在 `_quote_zero_fallback`：它的触发条件是
「价格 <= 0 **或** 当前不在交易时段」，于是收盘后接口明明给了有效收盘价，
也会被 `minute_points` 的最后一点覆盖。而启动时 `minute_points` 是从首屏快照
预填的，可能是几天前的数据（快照里分时点属于 10-26，行情属于 10-29），
结果是真实的 51.87（-6.39%）被显示成 57.44（+3.66%）。手动刷新之所以「修好」，
只是因为那时新的分时点已经到了，兜底恰好算出了正确的值。

兜底本来只为一件事存在：接口在休市/盘前把价格返回成 0 时，界面不要显示 `--`。
所以这里钉死两条：价格有效就用价格；价格缺失才用分时兜底。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

import sf_quote
import stock_float_window as m
from _base import SFCase

CLOSED = ("已收盘", None)

# 与 2026-10-29 收盘后真实接口返回一致的行情（长电科技）。
REAL_CLOSE = {
    "name": "长电科技", "code": "600584", "price": "51.87", "pre_close": "55.41",
    "change": "-3.54", "pct": "-6.39", "high": "55.35", "low": "51.85",
    "volume": "642602", "limit_up": "60.95", "limit_down": "49.87",
}
# 首屏快照预填进来的旧分时点：属于几个交易日之前，最后一点 57.44。
STALE_MINUTES = [("0930", 58.58, 58.58), ("0950", 57.33, 57.89), ("0951", 57.44, 57.88)]


def _shown(win):
    """读窗口当前显示的价格与涨跌幅（私有字段没有公开读口，集中在这一处读）。"""
    return win._disp_price, win._disp_pct  # discipline-ok(R5): 显示值只存在于这两个字段


def _deliver(win, quote):
    """把一条行情交给窗口（与 _poll 取队列后的调用同一入口）。"""
    win._apply_quote(quote)  # discipline-ok(R5): 行情应用没有公开入口，真实路径是 _poll 取队列后调它


class ClosedMarketValidQuote(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.ensure_qapp()

    def setUp(self):
        super().setUp()
        blocked = lambda *a, **k: None  # noqa: E731
        for p in (
            patch.object(m, "load_config",
                         return_value=("sh600584", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: True),
            patch.object(sf_quote, "http_get", lambda *a, **k: ""),
            patch.object(m, "start_icon_worker", blocked),
            patch.object(m.FloatWindow, "_force_top", blocked),
            patch.object(m.FloatWindow, "_setup_reopen_handler", blocked),
            patch.object(m.QuoteRuntimeController, "start", blocked),
            patch("PyQt6.QtWidgets.QMenu.popup", blocked),
            patch.object(m, "market_status", return_value=CLOSED),
            patch.object(m, "_is_trading_time", return_value=False),
        ):
            self.track_patch(p)
        self.win = self.track_widget(m.FloatWindow())
        self.win.running = False
        self.win._wake.set()  # discipline-ok(R5): 夹具不让常驻线程醒来
        for timer in (self.win.timer, self.win.top_timer):
            timer.stop()

    def test_valid_close_price_is_not_overridden_by_stale_minute_point(self):
        self.win.minute_points = list(STALE_MINUTES)
        _deliver(self.win, dict(REAL_CLOSE))
        price, pct = _shown(self.win)
        self.assertAlmostEqual(price, 51.87, places=2,
                               msg="收盘后接口给了有效收盘价，必须显示它而不是旧分时点 57.44")
        self.assertAlmostEqual(pct, -6.39, places=2,
                               msg="涨跌幅必须取接口值，不能按旧分时点重算出 +3.66%")
        self.assertEqual(self.win.quote["price"], "51.87",
                         "写进首屏快照的行情价也必须是真实收盘价")

    def test_same_day_minute_point_does_not_replace_the_official_close(self):
        # 14:59 的分时点与最终收盘价（集合竞价撮合）可以不同，官方收盘价才是答案。
        self.win.minute_points = [("1458", 51.95, 53.3), ("1459", 51.90, 53.3)]
        _deliver(self.win, dict(REAL_CLOSE))
        price, _pct = _shown(self.win)
        self.assertAlmostEqual(price, 51.87, places=2)

    def test_missing_price_still_uses_minute_fallback_after_close(self):
        # 兜底的本职不能丢：接口在休市把价格给成 0 时，仍然显示分时最后一笔而不是 `--`。
        self.win.minute_points = [("1458", 51.95, 53.3), ("1500", 51.87, 53.3)]
        _deliver(self.win, dict(REAL_CLOSE, price="0.00", pct="0.00"))
        price, pct = _shown(self.win)
        self.assertAlmostEqual(price, 51.87, places=2)
        self.assertAlmostEqual(pct, (51.87 - 55.41) / 55.41 * 100, places=2)


if __name__ == "__main__":
    unittest.main()
