"""Minute recovery audit: dates, empty responses, redraws and first retry timing."""
import datetime
import json
import os
import queue
from unittest import mock

import sf_config
import sf_quote
import stock_float_window as window
import sf_ui_render
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPainter
from sf_storage_tasks import SnapshotWriter
from sf_window_message_router import WindowMessageRouter
from tests._base import SFCase, fixed_clock
from tests.test_minute_recovery_latency import _Host, _VirtualClock


NOW = datetime.datetime(2026, 10, 15, 10, 0)
TODAY = "2026-10-15"
YESTERDAY = "2026-10-14"
CODE = "sh600183"
POINTS = [("0931", 116.0, 116.0)]


def internal(host, name):
    return getattr(host, name)


def set_internal(host, **values):
    # QWidget state properties must run their setters, not bypass them via __dict__.
    for name, value in values.items():
        setattr(host, name, value)


class DatedPoints(list):
    def __init__(self, values, day):
        super().__init__(values)
        self.day = day


class MinuteRecoveryStateAudit(_Host):
    def setUp(self):
        super().setUp()
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=NOW))
        self.track_patch(mock.patch.object(sf_quote, "_beijing_now", return_value=NOW))
        self.win.minute_points = list(POINTS)
        set_internal(self.win, _minute_day=TODAY, _minute_stale=True, _need_redraw=False)

    def test_equal_points_recovery_requests_a_redraw(self):
        internal(self.win, "_apply_minute_points")(list(POINTS))
        self.assertFalse(internal(self.win, "_minute_stale"))
        self.assertTrue(internal(self.win, "_need_redraw"),
                        "A recovered label cannot wait for price changes or dot animation")

    def test_equal_points_recovery_through_the_real_poll_repaints_a_collapsed_window(self):
        set_internal(self.win, _chart_expanded=False, _minimized=False)
        self.win.q.put(("minute", (CODE, sf_quote.MinutePoints(POINTS, day=TODAY))))
        self.win.running = True
        try:
            with mock.patch.object(self.win, "_repaint_now") as repaint:
                internal(self.win, "_poll")()
            self.assertFalse(internal(self.win, "_minute_stale"))
            self.assertTrue(repaint.called)
        finally:
            self.win.running = False

    def test_fetch_poll_and_label_pixels_recover_in_visible_and_minimized_modes(self):
        sf_ui_render.load_bundled_fonts()

        def label_pixels():
            image = QImage(self.win.width(), self.win.height(), QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            try:
                internal(self.win, "_paint_stale_label")(painter, self.win.width())
            finally:
                painter.end()
            return any(image.pixelColor(x, y).alpha() for y in range(image.height())
                       for x in range(image.width()))

        for minimized in (False, True):
            with self.subTest(minimized=minimized):
                set_internal(self.win, _minute_stale=False, _minimized=minimized)
                self.win.running = True
                try:
                    with mock.patch.object(window, "fetch_minute", side_effect=[
                            [], sf_quote.MinutePoints(POINTS, day=TODAY)]):
                        self.assertFalse(self.I.fetch_minute())
                        internal(self.win, "_poll")()
                        self.assertTrue(label_pixels())
                        self.assertEqual(self.win.minute_points, POINTS)
                        self.assertTrue(self.I.fetch_minute())
                        internal(self.win, "_poll")()
                        self.assertFalse(label_pixels())
                        self.assertEqual(self.win.minute_points, POINTS)
                finally:
                    self.win.running = False

    def test_old_points_arriving_after_open_cannot_be_relabelled_today(self):
        internal(self.win, "_apply_minute_points")(DatedPoints(POINTS, YESTERDAY))
        self.assertEqual(internal(self.win, "_minute_day"), TODAY)
        self.assertTrue(internal(self.win, "_minute_stale"))

    def test_off_market_points_keep_the_actual_day(self):
        with mock.patch.object(self.win, "_is_trading_time", return_value=False):
            internal(self.win, "_apply_minute_points")(DatedPoints(POINTS, YESTERDAY))
        self.assertEqual(internal(self.win, "_minute_day"), YESTERDAY)

    def test_empty_fetch_is_counted_as_a_failure(self):
        vars(self.win).update(_minute_fail=0)
        with mock.patch.object(window, "fetch_minute", return_value=[]):
            self.assertFalse(self.I.fetch_minute())
        self.assertEqual(internal(self.win, "_minute_fail"), 1)

    def test_session_boundary_clears_a_reconnecting_label_after_close(self):
        with mock.patch.object(self.win, "_is_trading_time", return_value=False), \
                mock.patch.object(self.win, "_update_nap_policy"), \
                mock.patch.object(self.win, "_sync_dot_timer"), \
                mock.patch.object(self.win, "_arm_session_timer"), \
                mock.patch.object(self.win, "_repaint_now") as repaint:
            internal(self.win, "_on_session_boundary")()
        self.assertFalse(internal(self.win, "_minute_stale"))
        self.assertTrue(repaint.called)

    def test_late_failure_after_close_does_not_reintroduce_the_label(self):
        set_internal(self.win, _minute_stale=False)
        with mock.patch.object(self.win, "_is_trading_time", return_value=False):
            internal(self.win, "_handle_minute_stale")(self.win.code)
        self.assertFalse(internal(self.win, "_minute_stale"))
        self.assertEqual(self.win.minute_points, POINTS)

    def test_old_stock_success_cannot_reset_new_stock_failure_count(self):
        set_internal(self.win, _minute_fail=3)

        def completed_old_fetch(code):
            self.win.code = "sz000001"
            return POINTS

        with mock.patch.object(window, "fetch_minute", side_effect=completed_old_fetch):
            self.assertIsNone(self.I.fetch_minute())
        self.assertEqual(internal(self.win, "_minute_fail"), 3)
        self.assertTrue(self.win.q.empty())

    def test_cache_receipt_day_cannot_overwrite_source_day(self):
        set_internal(self.win, _minute_day=YESTERDAY, _last_quote_saved_at=0)
        with mock.patch.object(internal(self.win, "_snapshot_writer"), "submit") as submit:
            self.I.maybe_cache()
        points, saved_day = submit.call_args.args[2:]
        self.assertEqual(saved_day, TODAY)
        self.assertEqual(points.day, YESTERDAY)

    def test_old_cache_curve_is_not_seeded_during_trading(self):
        self.win.minute_points = []
        snapshot = {"code": CODE, "quote": {"price": "116", "pct": "0"},
                    "minute": POINTS, "day": TODAY, "minute_day": YESTERDAY}
        with mock.patch.dict(os.environ, {"SF_SEED_FIRST_SCREEN": "1"}), \
                mock.patch.object(window, "load_last_quote", return_value=snapshot):
            self.I.seed_first_screen()
        self.assertEqual(self.win.minute_points, [])

    def test_same_day_cache_is_marked_unconfirmed_until_live_success(self):
        snapshot = {"code": CODE, "quote": {"price": "116", "pct": "0"},
                    "minute": POINTS, "day": TODAY, "minute_day": TODAY}
        with mock.patch.dict(os.environ, {"SF_SEED_FIRST_SCREEN": "1"}), \
                mock.patch.object(window, "load_last_quote", return_value=snapshot):
            self.I.seed_first_screen()
        self.assertEqual(self.win.minute_points, POINTS)
        self.assertTrue(internal(self.win, "_minute_stale"))
        internal(self.win, "_apply_minute_points")(sf_quote.MinutePoints(POINTS, day=TODAY))
        self.assertFalse(internal(self.win, "_minute_stale"))

    def test_first_retry_uses_the_new_interval_immediately(self):
        clock = _VirtualClock()

        def fetch():
            clock.fetches.append(clock.t)
            if len(clock.fetches) == 2:
                clock.stopped = True
            return False

        vars(self.win).update(_minute_interval=window.CHART_COLLAPSED_MINUTE_INTERVAL)
        with mock.patch.object(window.time, "monotonic", side_effect=clock.monotonic), \
                mock.patch.object(window, "seconds_to_session_change", return_value=0), \
                mock.patch.object(self.win, "_is_trading_time", return_value=True), \
                mock.patch.object(self.win, "_take_force", return_value=False), \
                mock.patch.object(self.win, "_fetch_minute", side_effect=fetch), \
                mock.patch.object(self.win, "_sleep", side_effect=clock.sleep):
            self.win.running = True
            try:
                self.I.minute_loop()
            finally:
                self.win.running = False
        self.assertEqual(len(clock.fetches), 2)
        self.assertAlmostEqual(clock.fetches[1] - clock.fetches[0], window.MINUTE_RETRY_INTERVAL)


class MinuteSourceAudit(SFCase):
    def primary(self, rows, day=TODAY):
        body = {"data": {CODE: {"data": {"date": day, "data": rows}}}}
        with fixed_clock(NOW), mock.patch.object(sf_quote, "http_get", return_value=json.dumps(body)):
            return internal(sf_quote, "_fetch_minute_primary")(CODE)

    def fallback(self, rows):
        body = {"data": {"trends": rows}}
        with fixed_clock(NOW), mock.patch.object(sf_quote, "http_get", return_value=json.dumps(body)):
            return sf_quote.fetch_minute_fallback(CODE)

    def test_primary_keeps_its_source_day(self):
        points = self.primary(["0931 116.0 100 1160000"])
        self.assertEqual(getattr(points, "day", None), TODAY)

    def test_fallback_keeps_its_source_day(self):
        points = self.fallback([TODAY + " 09:31,116,116,116,116,1,11600,116"])
        self.assertEqual(getattr(points, "day", None), TODAY)

    def test_zero_and_negative_prices_cannot_end_a_recovery(self):
        self.assertEqual(self.primary(["0931 0", "0932 -1"]), [])
        rows = [TODAY + " 09:31,0,%s,0,0,1,1,1" % value for value in (0, -1)]
        self.assertEqual(self.fallback(rows), [])

    def test_invalid_clock_minutes_cannot_become_valid_points(self):
        self.assertEqual(self.primary(["0960 116"]), [])
        self.assertEqual(self.fallback([TODAY + " 09:60,116,116,116,116,1,1,116"]), [])

    def test_empty_fallback_cannot_claim_a_successful_source(self):
        with mock.patch.object(sf_quote, "_fetch_minute_primary", return_value=[]), \
                mock.patch.object(sf_quote, "fetch_minute_fallback", return_value=[]), \
                mock.patch.object(sf_quote, "_is_trading_time", return_value=True), \
                mock.patch.object(sf_quote, "_LAST_MINUTE_SOURCE", "primary"), \
                mock.patch.object(sf_quote, "log_line") as log:
            self.assertEqual(sf_quote.fetch_minute(CODE), [])
            self.assertEqual(sf_quote.get_last_minute_source(), "primary")
            self.assertFalse(log.called)

    def test_duplicate_and_unsorted_minutes_are_normalized(self):
        self.assertEqual(self.primary(["0932 117", "0931 115", "0931 116"]),
                         [("0931", 116.0, None), ("0932", 117.0, None)])

    def test_future_day_and_invalid_day_are_rejected(self):
        for day in ("2026-10-16", "2026-99-99", "not-a-date"):
            with self.subTest(day=day):
                self.assertEqual(self.primary(["0931 116"], day=day), [])

    def test_missing_date_is_not_invented(self):
        self.assertEqual(self.primary(["0931 116"], day="").day, "")
        self.assertEqual(self.fallback(["09:31,116,116,116,116,1,1,116"]).day, "")

    def test_non_trading_fallback_uses_only_the_latest_source_day(self):
        rows = [YESTERDAY + " 09:31,116,115,116,116,1,1,116",
                TODAY + " 09:32,116,117,116,116,1,1,116",
                YESTERDAY + " 09:33,116,114,116,116,1,1,116"]
        with mock.patch.object(sf_quote, "_is_trading_time", return_value=False):
            points = self.fallback(rows)
        self.assertEqual(points, [("09:32", 117.0, 116.0)])
        self.assertEqual(points.day, TODAY)


class MinuteCacheAndQueueAudit(SFCase):
    def test_async_snapshot_keeps_the_day_without_aliasing_the_points(self):
        saved = []
        points = sf_quote.MinutePoints(POINTS, day=YESTERDAY)
        writer = SnapshotWriter(lambda *args: saved.append(args))
        try:
            writer.submit(CODE, {}, points, TODAY)
        finally:
            self.assertTrue(writer.close(timeout=2))
        self.assertEqual(saved[0][2].day, YESTERDAY)
        self.assertIsNot(saved[0][2], points)
        with mock.patch.object(sf_config, "_atomic_write_json") as write:
            self.assertTrue(sf_config.save_last_quote(*saved[0]))
        payload = write.call_args.args[1]
        self.assertEqual(payload["minute_day"], YESTERDAY)
        self.assertEqual(payload["day"], TODAY)

    def test_old_stock_failure_cannot_swallow_current_stock_failure(self):
        messages = queue.Queue()
        messages.put(("minute_stale", CODE))
        messages.put(("minute_stale", "sz000001"))
        batch = WindowMessageRouter.drain(messages, CODE)
        self.assertEqual(batch.minute_stale_after, CODE)
