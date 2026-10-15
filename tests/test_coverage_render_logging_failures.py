"""Real diagnostic files and offscreen pixels for failure evidence paths."""
from __future__ import annotations

import datetime
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QPainter

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from _base import SFCase
except ImportError:  # pragma: no cover
    from tests._base import SFCase

import sf_logging as logs
import sf_ui_render as render

draw_stale = render.RenderMixin._draw_chart_stale  # discipline-ok(R5): stale-data rendering entry
draw_dot = render.RenderMixin._draw_chart_dot  # discipline-ok(R5): live/stale visual contract
draw_chart = render.RenderMixin._draw_chart  # discipline-ok(R5): minimum-height and empty-data branches
IMAGE_WIDTH, IMAGE_HEIGHT = 184, 100
FIXED_TIME = 1000.0


class Clock:
    instant = datetime.datetime(2030, 1, 8, 10)

    @staticmethod
    def now():
        return Clock.instant


class LoggingFailureEvidence(SFCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.track_patch(mock.patch.object(logs, "LOG_PATH", str(self.root / "debug.log")))
        for name, value in (("_LOG_MUTED", False), ("_LAST_FAILURES", {}),
                            ("_INTERNAL_FAULTS", {}), ("_THROTTLE_STATE", {}),
                            ("_last_failures_written_at", 0.0)):
            self.track_patch(mock.patch.object(logs, name, value))
        self.track_patch(mock.patch.object(logs, "_maybe_rotate_log"))
        self.track_patch(mock.patch.object(logs, "_nslog_line"))
        self.track_patch(mock.patch.object(logs, "datetime", types.SimpleNamespace(datetime=Clock)))
        self.track_patch(mock.patch.object(logs.time, "time", return_value=FIXED_TIME))
        Clock.instant = datetime.datetime(2030, 1, 8, 10)

    def test_summary_write_failure_does_not_lose_main_exception_log(self):
        blocked = self.root / "blocked"
        blocked.write_bytes(b"not a directory")
        with mock.patch.object(logs, "last_failures_path", return_value=str(blocked / "last.json")):
            logs.log_swallow("camera.start", RuntimeError("native failure"))
        self.assertEqual(logs.internal_faults()["write_last_failures"], 1)
        text = (self.root / "debug.log").read_text(encoding="utf-8")
        self.assertIn("SWALLOWED camera.start", text)
        self.assertIn("native failure", text)

    def test_summary_record_failure_is_counted_without_recursive_logging(self):
        with mock.patch.object(logs, "_record_swallow", side_effect=RuntimeError("summary unavailable")):
            logs.log_swallow("glass.follow", RuntimeError("timer failure"))
        self.assertEqual(logs.internal_faults()["record_swallow"], 1)
        self.assertIn("glass.follow", (self.root / "debug.log").read_text(encoding="utf-8"))

    def test_summary_keeps_newest_sites_and_truncates_exception_text(self):
        with mock.patch.object(logs, "LAST_FAILURES_MAX_KEYS", 2):
            for name in ("old", "middle", "new"):
                Clock.instant += datetime.timedelta(seconds=1)
                logs.log_swallow(name, RuntimeError("x" * 400))
        path = pathlib.Path(logs.last_failures_path())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(data["failures"]), {"middle", "new"})
        self.assertTrue(all(len(item["exc"]) <= 200 for item in data["failures"].values()))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_system_log_failure_does_not_erase_file_evidence(self):
        with mock.patch.object(logs, "_nslog_line", side_effect=RuntimeError("NSLog unavailable")):
            logs.log_line("exit cleanup failure", "WARNING")
        self.assertIn("exit cleanup failure", (self.root / "debug.log").read_text(encoding="utf-8"))


class RecordingPainter(QPainter):
    def __init__(self, image):
        super().__init__(image)
        self.labels = []

    def drawText(self, *args):
        self.labels.append(args[-1])
        return super().drawText(*args)


class RenderFailureEvidence(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()
        render.load_bundled_fonts()

    def setUp(self):
        super().setUp()
        self.swallowed = mock.Mock()
        self.track_patch(mock.patch.object(render, "log_swallow", self.swallowed))
        self.track_patch(mock.patch.object(render, "_PAINT_DIAG", render.paint_diagnostics()))

    @staticmethod
    def font(family, size, *args):
        font = QFont(family)
        font.setPointSizeF(size)
        return font

    def image(self):
        image = QImage(IMAGE_WIDTH, IMAGE_HEIGHT, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        return image

    def test_baseline_geometry_error_records_degradation_without_covering_chart(self):
        target = types.SimpleNamespace(
            _font=self.font, _chart_y=mock.Mock(side_effect=RuntimeError("bad baseline geometry")))
        image = self.image()
        painter = RecordingPainter(image)
        pen = QColor(17, 34, 51)
        painter.setPen(pen)
        before = render.paint_diagnostics()
        try:
            draw_stale(target, painter, QRectF(10, 10, 164, 80), {"pre_close": 12})
            self.assertEqual(painter.labels, [])
            self.assertEqual(painter.pen().color(), pen, "degraded paint must restore caller state")
        finally:
            painter.end()
        after = render.paint_diagnostics()
        self.assertEqual(after["stale_hint_line_missing"], before["stale_hint_line_missing"] + 1)
        self.assertIsNone(after["stale_hint_last_gap_px"])
        self.assertIn("line_y", self.swallowed.call_args.args[0])
        self.assertTrue(all(image.pixelColor(x, y).alpha() == 0
                            for y in range(IMAGE_HEIGHT) for x in range(IMAGE_WIDTH)))

    def test_collapsed_chart_is_completely_blank_and_never_reads_metrics(self):
        target = types.SimpleNamespace(minute_points=[("0930", 12)],
                                       _chart_metrics=mock.Mock(side_effect=AssertionError("not drawable")))
        image = self.image()
        painter = RecordingPainter(image)
        try:
            draw_chart(target, painter, QRectF(10, 10, 164, 1))
            self.assertEqual(painter.labels, [])
        finally:
            painter.end()
        self.assertTrue(all(image.pixelColor(x, y).alpha() == 0
                            for y in range(IMAGE_HEIGHT) for x in range(IMAGE_WIDTH)))
        vars(target)["_chart_metrics"].assert_not_called()

    def test_no_points_and_unknown_market_status_show_loading_not_a_blank_chart(self):
        target = types.SimpleNamespace(minute_points=[], _font=self.font, _market_status=lambda: None)
        image = self.image()
        painter = RecordingPainter(image)
        try:
            draw_chart(target, painter, QRectF(10, 10, 164, 80))
            self.assertIn("加载中...", painter.labels)
        finally:
            painter.end()
        self.assertTrue(any(image.pixelColor(x, y).alpha() > 0
                            for y in range(IMAGE_HEIGHT) for x in range(IMAGE_WIDTH)))

    def dot_pixels(self, stale, instant):
        image = self.image()
        painter = RecordingPainter(image)
        target = types.SimpleNamespace(minute_points=[("0930", 12)], _minute_stale=stale,
                                       _chart_x=lambda rect, metrics, index: rect.center().x(),
                                       _chart_y=lambda rect, metrics, price: rect.center().y())
        with mock.patch.object(render.time, "monotonic", return_value=instant):
            try:
                draw_dot(target, painter, QRectF(10, 10, 164, 80), {"n": 1, "color": QColor("red")})
            finally:
                painter.end()
        return image.constBits().asstring(image.sizeInBytes())

    def test_stale_dot_is_frozen_while_live_dot_breathes(self):
        period = render.CHART_DOT_BREATH_PERIOD
        first, second = period * 10, period * 10.5
        self.assertEqual(self.dot_pixels(True, first), self.dot_pixels(True, second))
        self.assertNotEqual(self.dot_pixels(False, first), self.dot_pixels(False, second))


if __name__ == "__main__":
    unittest.main()
