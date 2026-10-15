"""Regression for Tencent WAF failures and independent quote/minute freshness."""
import datetime
import json
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from PyQt6.QtGui import QImage, QPainter

import sf_constants
import sf_quote
import sf_ui_render
import stock_float_window as window
from tests._base import SFCase, fixed_clock
from tests.test_minute_recovery_latency import _Host


NOW = datetime.datetime(2026, 10, 15, 10, 0)
CODE = "sh600183"
POINTS = [("0931", 114.68, 116.15)]


def internal(host, name):
    # The worker and renderer have no public single-step entry points.
    return getattr(host, name)


def wire(host, **values):
    for name, value in values.items():
        setattr(host, name, value)


class MinuteWafRecovery(SFCase):
    def setUp(self):
        super().setUp()
        self.enterContext(fixed_clock(NOW))
        self.track_patch(mock.patch.object(sf_quote, "_format_err_count", {"quote": 0, "minute": 0}))
        self.track_patch(mock.patch.object(sf_quote, "_LAST_MINUTE_SOURCE", ""))

    def test_default_route_returns_dated_points_without_using_backup(self):
        body = {"data": {CODE: {"data": {"date": "20261015", "data": [
            "0931 114.68 100 1161500.00"]}}}}

        def response(url, **kwargs):
            parsed = urlsplit(url)
            self.assertEqual(parsed.scheme, "https")
            self.assertEqual(parsed.hostname, "proxy.finance.qq.com")
            self.assertEqual(parsed.path, "/ifzqgtimg/appstock/app/minute/query")
            self.assertEqual(parse_qs(parsed.query), {"code": [CODE]})
            return json.dumps(body)

        with mock.patch.object(sf_quote, "MINUTE_URL", sf_constants.MINUTE_URL), \
                mock.patch.object(sf_quote, "http_get", side_effect=response) as get, \
                mock.patch.object(sf_quote, "fetch_minute_fallback") as fallback:
            points = sf_quote.fetch_minute(CODE)
        self.assertEqual(points, POINTS)
        self.assertEqual(points.day, NOW.date().isoformat())
        self.assertEqual(sf_quote.get_last_minute_source(), "primary")
        self.assertEqual(get.call_count, 1)
        fallback.assert_not_called()

    def test_waf_and_transport_failures_are_not_format_errors(self):
        for error in (HTTPError("https://example.invalid/", 501, "WAF", {}, None),
                      TimeoutError("timed out"), OSError("disconnected")):
            if isinstance(error, HTTPError):
                self.addCleanup(error.close)
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(sf_quote, "_fetch_minute_primary", side_effect=error), \
                    mock.patch.object(sf_quote, "fetch_minute_fallback", return_value=POINTS), \
                    mock.patch.object(sf_quote, "_bump_format_error") as bump, \
                    mock.patch.object(sf_quote, "log_line"):
                self.assertEqual(sf_quote.fetch_minute(CODE), POINTS)
                self.assertEqual(sf_quote.get_last_minute_source(), "fallback")
                bump.assert_not_called()

    def test_malformed_response_still_counts_as_a_format_error(self):
        for error in (ValueError("invalid JSON"), TypeError("invalid shape"),
                      KeyError("data"), sf_quote.QuoteDataError("invalid data")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(sf_quote, "_fetch_minute_primary", side_effect=error), \
                    mock.patch.object(sf_quote, "fetch_minute_fallback", return_value=POINTS), \
                    mock.patch.object(sf_quote, "_bump_format_error") as bump, \
                    mock.patch.object(sf_quote, "log_line"):
                self.assertEqual(sf_quote.fetch_minute(CODE), POINTS)
                bump.assert_called_once_with("minute", "MINUTE", CODE, error)

    def test_both_failures_are_logged_and_chained_without_inventing_success(self):
        primary_error = HTTPError("https://example.invalid/", 501, "WAF", {}, None)
        self.addCleanup(primary_error.close)
        backup_error = OSError("backup disconnected")
        with mock.patch.object(sf_quote, "_fetch_minute_primary", side_effect=primary_error), \
                mock.patch.object(sf_quote, "fetch_minute_fallback", side_effect=backup_error), \
                mock.patch.object(sf_quote, "log_line_throttled") as log:
            with self.assertRaises(HTTPError) as caught:
                sf_quote.fetch_minute(CODE)
        self.assertIs(caught.exception, primary_error)
        self.assertIs(caught.exception.__cause__, backup_error)
        self.assertEqual(sf_quote.get_last_minute_source(), "")
        message = log.call_args.args[1]
        self.assertIn(CODE, message)
        self.assertIn("501", message)
        self.assertIn("backup disconnected", message)

    def test_custom_https_minute_url_is_preserved(self):
        body = {"data": {CODE: {"data": {"date": "20261015", "data": ["0931 114.68"]}}}}
        custom = "https://example.invalid/minute?code=%s"
        with mock.patch.object(sf_quote, "MINUTE_URL", custom), \
                mock.patch.object(sf_quote, "http_get", return_value=json.dumps(body)) as get:
            self.assertTrue(sf_quote.fetch_minute(CODE))
        self.assertEqual(get.call_args.args[0], custom % CODE)


class MinuteQuotePresentation(_Host):
    def test_stale_curve_does_not_freeze_quotes_and_recovery_removes_the_label(self):
        self.enterContext(fixed_clock(NOW, ("sf_quote", "stock_float_window")))
        sf_ui_render.load_bundled_fonts()
        self.track_patch(mock.patch.object(self.win, "_animations_suspended", return_value=True))
        self.win.minute_points = sf_quote.MinutePoints(POINTS, day=NOW.date().isoformat())
        wire(self.win, _minute_day=NOW.date().isoformat())
        quote = {"name": "Test", "code": "600183", "price": "114.68", "pct": "-4.14",
                 "pre_close": "119.63", "high": "119.08", "low": "113.20", "volume": "100"}

        def price_pixels():
            image = QImage(self.win.width(), self.win.height(), QImage.Format.Format_ARGB32)
            image.fill(0)
            painter = QPainter(image)
            try:
                internal(self.win, "_paint_price")(painter, self.win.width())
            finally:
                painter.end()
            return bytes(image.constBits().asstring(image.sizeInBytes()))

        def label_texts():
            image = QImage(self.win.width(), self.win.height(), QImage.Format.Format_ARGB32)
            image.fill(0)
            painter = QPainter(image)
            labels = []
            draw = QPainter.drawText

            def record(p, *args):
                labels.append(args[-1])
                return draw(p, *args)

            try:
                with mock.patch.object(QPainter, "drawText", record):
                    internal(self.win, "_paint_stale_label")(painter, self.win.width())
            finally:
                painter.end()
            return labels

        self.win.running = True
        try:
            self.win.q.put(("quote", (CODE, quote)))
            self.win.q.put(("minute_stale", CODE))
            internal(self.win, "_poll")()
            before = price_pixels()
            self.assertEqual(label_texts(), ["分时滞后·重试中"])
            self.win.q.put(("quote", (CODE, dict(quote, price="114.70", pct="-4.12"))))
            internal(self.win, "_poll")()
            self.assertEqual(self.win.quote["price"], "114.70")
            self.assertNotEqual(before, price_pixels())
            self.assertEqual(self.win.minute_points, POINTS)
            self.assertEqual(label_texts(), ["分时滞后·重试中"])
            self.win.q.put(("minute", (CODE, sf_quote.MinutePoints(POINTS, day=NOW.date().isoformat()))))
            internal(self.win, "_poll")()
            self.assertEqual(label_texts(), [])
        finally:
            self.win.running = False
