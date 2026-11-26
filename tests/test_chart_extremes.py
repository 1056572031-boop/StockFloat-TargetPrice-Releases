"""日内高低价必须使用行情快照，而不是分钟采样极值。"""
from tests._base import SFCase

from sf_ui_render import bottom_rows
from tests.test_power_v1410 import _WinCase
from tests.test_coverage_render_logging_failures import RecordingPainter
from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QImage, QPainterPath


class ChartPainter(RecordingPainter):
    def __init__(self, image):
        super().__init__(image)
        self.lines = []
        self.fills = []

    def drawPath(self, path):
        self.lines.append(QPainterPath(path))
        return super().drawPath(path)

    def fillPath(self, path, brush):
        self.fills.append(QPainterPath(path))
        return super().fillPath(path, brush)


class TestChartExtremes(SFCase):
    def test_daily_extremes_do_not_depend_on_sampled_minute_prices(self):
        quote = {"pre_close": "10.00", "high": "10.80", "low": "9.90"}
        rows = bottom_rows(quote)
        self.assertEqual(rows[0][1][1], "10.8")
        self.assertEqual(rows[1][1][1], "9.9")


class TestRealWindowChart(_WinCase):
    def test_quote_only_updates_keep_line_and_dot_connected(self):
        """Changing the live price must invalidate the tail even inside a fixed y range."""
        win = self.make_win()
        self.addCleanup(win.close)
        win._exit_persisted = True  # discipline-ok(R5): prevent fixture configuration writes
        points = [("1000", 10.0, 11.0), ("1001", 20.0, 12.0), ("1002", 12.0, 13.0)]
        win.minute_points = list(points)
        rect = QRectF(10, 10, 164, 80)
        for price, fallback in ((15.0, False), (16.0, False), (0.0, False), (19.0, True)):
            with self.subTest(price=price, fallback=fallback):
                win.quote = {"price": str(price), "pre_close": "11", "change": "1"}
                win._price_is_fallback = fallback  # discipline-ok(R5): cached-price boundary
                metrics = win._chart_metrics(points, rect)  # discipline-ok(R5): no public chart geometry API
                image = QImage(184, 100, QImage.Format.Format_ARGB32)
                image.fill(0)
                painter = ChartPainter(image)
                try:
                    win._draw_chart_fill(painter, rect, metrics)  # discipline-ok(R5): exercise shared fill cache first
                    win._draw_chart_line(painter, rect, metrics)  # discipline-ok(R5): real line geometry
                    win._draw_chart_dot(painter, rect, metrics)  # discipline-ok(R5): actual circle center
                finally:
                    painter.end()
                endpoint = painter.lines[-1].currentPosition()
                tail = painter.lines[-1].elementAt(len(points) - 1)
                self.assertAlmostEqual(tail.y, 70.0, msg="last minute observation must remain in the drawn line")
                fill_endpoint = painter.fills[-1].elementAt(painter.lines[-1].elementCount() - 1)
                self.assertAlmostEqual(fill_endpoint.x, endpoint.x())
                self.assertAlmostEqual(fill_endpoint.y, endpoint.y())
                center = painter.ellipses[-1][0]
                expected_y = 70.0 if fallback or price <= 0 else (50.0 if price == 15.0 else 130.0 / 3.0)
                self.assertAlmostEqual(endpoint.y(), expected_y)
                self.assertLessEqual(abs(endpoint.y() - center.y()), 1.0)
                self.assertLessEqual(abs(endpoint.x() - center.x()), 1.0)
                self.assertAlmostEqual(painter.lines[-1].elementAt(0).y, 250.0 / 3.0)
                self.assertEqual(win.minute_points, points, "rendering must not overwrite source minute prices")

    def test_single_minute_point_and_stale_chart_keep_live_endpoint(self):
        win = self.make_win()
        self.addCleanup(win.close)
        win._exit_persisted = True  # discipline-ok(R5): prevent fixture persistence
        win._minute_stale = True  # discipline-ok(R5): stale curve keeps existing live-quote semantics
        win.minute_points = [("1000", 12.0, 13.0)]
        win.quote = {"price": "15", "pre_close": "10", "change": "1"}
        rect = QRectF(10, 10, 164, 80)
        metrics = win._chart_metrics(win.minute_points, rect)  # discipline-ok(R5): no public geometry API
        image = QImage(184, 100, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = ChartPainter(image)
        try:
            win._draw_chart_line(painter, rect, metrics)  # discipline-ok(R5): actual single-point path
            win._draw_chart_dot(painter, rect, metrics)  # discipline-ok(R5): actual stale dot
        finally:
            painter.end()
        self.assertAlmostEqual(painter.lines[-1].currentPosition().y(), 50.0 / 3.0)
        self.assertLessEqual(abs(painter.lines[-1].currentPosition().y() - painter.ellipses[-1][0].y()), 1.0)

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
