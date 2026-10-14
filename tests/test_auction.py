import datetime
from types import SimpleNamespace
from unittest.mock import patch

import sf_quote as quote
from tests._base import SFCase, fixed_clock


class TestAuction(SFCase):
    def test_session_boundaries_and_labels(self):
        cases = (
            (9, 14, False, "未开盘"),
            (9, 15, True, "集合竞价"),
            (9, 24, True, "集合竞价"),
            (9, 25, True, "竞价结束"),
            (9, 29, True, "竞价结束"),
            (9, 30, True, None),
            (14, 56, True, None),
            (14, 57, True, "集合竞价"),
            (14, 59, True, "集合竞价"),
            (15, 0, True, None),
            (15, 1, False, "已收盘"),
        )
        with patch.object(quote, "is_holiday", return_value=False):
            for hour, minute, active, label in cases:
                with self.subTest(hour=hour, minute=minute), fixed_clock(
                    datetime.datetime(2026, 10, 14, hour, minute)
                ):
                    self.assertEqual(quote._is_trading_time(), active)  # discipline-ok(R5): 轮询时段判定没有公开入口，需直接验证竞价不会降频
                    status = quote.market_status()
                    self.assertEqual(status[0] if status else None, label)

    def test_sleep_stops_at_auction_start(self):
        with patch.object(quote, "is_holiday", return_value=False), fixed_clock(
            datetime.datetime(2026, 10, 14, 9, 14, 50)
        ):
            self.assertEqual(quote.seconds_to_session_change(), 10)

    def test_auction_uses_intraday_poll_interval(self):
        import threading
        import stock_float_window as window
        host = SimpleNamespace(_is_trading_time=quote._is_trading_time,  # discipline-ok(R5): 接入真实时段函数验证行情轮询策略，非伪造 trading=True
                               _poll_lock=threading.Lock(), _poll_state=window.POLL_NORMAL)
        with patch.object(quote, "is_holiday", return_value=False):
            for hour, minute in ((9, 15), (9, 24), (9, 25), (14, 58)):
                with fixed_clock(datetime.datetime(2026, 10, 14, hour, minute)):
                    interval = window.FloatWindow._poll_interval(host)  # discipline-ok(R5): 行情轮询策略无公开入口，断言实际刷新间隔
                    self.assertEqual(interval, window.compute_poll_interval(window.POLL_NORMAL))

    def test_no_trades_during_auction_is_not_suspension(self):
        data = {"price": "10", "pre_close": "10", "volume": "0",
                "limit_up": "11", "limit_down": "9"}
        with patch.object(quote, "is_holiday", return_value=False):
            for hour, minute in ((9, 20), (9, 27), (14, 58)):
                with fixed_clock(datetime.datetime(2026, 10, 14, hour, minute)):
                    self.assertIsNone(quote.quote_status(data))
            with fixed_clock(datetime.datetime(2026, 10, 14, 10, 0)):
                self.assertEqual(quote.quote_status(data), "停牌")

    def test_holidays_and_weekends_have_no_auction(self):
        with patch.object(quote, "is_holiday", return_value=True), fixed_clock(
            datetime.datetime(2026, 10, 14, 9, 20)
        ):
            self.assertIsNone(quote.auction_status())
            self.assertFalse(quote._is_trading_time())  # discipline-ok(R5): 轮询时段判定没有公开入口，需验证休市不启用高频刷新
        with patch.object(quote, "is_holiday", return_value=False), fixed_clock(
            datetime.datetime(2026, 10, 17, 9, 20)
        ):
            self.assertIsNone(quote.auction_status())

    def test_auction_label_is_painted(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QFont, QFontMetricsF, QImage, QPainter
        import sf_ui_render as render

        app = self.ensure_qapp()
        render.load_bundled_fonts()
        host = SimpleNamespace(
            quote={"name": "贵州茅台"},
            _market_status=quote.market_status,
            _font=lambda family, size, weight: QFont(family, size, weight),
            _fm=QFontMetricsF,
        )
        image = QImage(300, 80, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        drawn = []
        original = QPainter.drawText

        def record(p, rect, alignment, text):
            drawn.append((rect, text))
            return original(p, rect, alignment, text)

        try:
            with patch.object(quote, "is_holiday", return_value=False), fixed_clock(
                datetime.datetime(2026, 10, 14, 9, 20)
            ), patch.object(QPainter, "drawText", record):
                render.RenderMixin._paint_market_status(host, painter, image.width(), "贵州茅台")  # discipline-ok(R5): 直调绘制入口避免 paintEvent 吞异常，断言实际画出的文字
        finally:
            painter.end()
        self.assertEqual([text for _, text in drawn], ["集合竞价"])
        self.assertTrue(all(image.rect().contains(rect.toRect()) for rect, _ in drawn))
        self.assertTrue(any(image.pixelColor(x, y).alpha() for x in range(image.width())
                            for y in range(image.height())))
        self.assertIsNotNone(app)
