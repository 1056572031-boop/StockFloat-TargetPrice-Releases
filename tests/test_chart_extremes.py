"""日内高低价必须使用行情快照，而不是分钟采样极值。"""
from tests._base import SFCase

from sf_ui_render import bottom_rows
from tests.test_power_v1410 import _WinCase
from tests.test_coverage_render_logging_failures import RecordingPainter
from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QImage


class TestChartExtremes(SFCase):
    def test_daily_extremes_do_not_depend_on_sampled_minute_prices(self):
        quote = {"pre_close": "10.00", "high": "10.80", "low": "9.90"}
        rows = bottom_rows(quote)
        self.assertEqual(rows[0][1][1], "10.8")
        self.assertEqual(rows[1][1][1], "9.9")


class TestRealWindowChart(_WinCase):
    def test_property_quote_drives_chart_and_fallback_returns_to_minute_tail(self):
        win = self.make_win()
        win.quote = {"pre_close": "56.01", "price": "59.85", "change": "3.84",
                     "high": "59.85", "low": "56.00"}
        win.minute_points = [("1000", 59.20, 58.386), ("1001", 59.77, 58.478)]
        rect = QRectF(10, 10, 164, 80)
        metrics = win._chart_metrics(win.minute_points, rect)  # discipline-ok(R5): real chart scale has no public geometry API
        self.assertGreater(metrics["hi"], 59.85)
        expected = int(win._chart_y(rect, metrics, 59.85))  # discipline-ok(R5): compare actual dot to production coordinate mapping
        image = QImage(184, 100, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = RecordingPainter(image)
        try:
            win._draw_chart_dot(painter, rect, metrics)  # discipline-ok(R5): inspect the real drawing entry on a real window
        finally:
            painter.end()
        self.assertEqual(painter.ellipses[-1][0].y(), expected)
        self.assertEqual(bottom_rows(win.quote)[0][1][1], "59.85")
        win._price_is_fallback = True  # discipline-ok(R5): exercise the real cached-quote fallback state
        metrics = win._chart_metrics(win.minute_points, rect)  # discipline-ok(R5): real chart scale is internal
        expected_tail = int(win._chart_y(rect, metrics, 59.77))  # discipline-ok(R5): chart-coordinate oracle
        painter = RecordingPainter(image)
        try:
            win._draw_chart_dot(painter, rect, metrics)  # discipline-ok(R5): render the actual dot
        finally:
            painter.end()
        self.assertEqual(painter.ellipses[-1][0].y(), expected_tail)
        win._exit_persisted = True  # discipline-ok(R5): prevent test window persisting user configuration
        win.close()
