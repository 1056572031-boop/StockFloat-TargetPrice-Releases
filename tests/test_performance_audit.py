"""Cache reuse, invalidation, search work bounds, and benchmark isolation contracts."""
import datetime
import contextlib
import io
import json
from types import SimpleNamespace
import tempfile
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QImage, QPainter, QPainterPath

import sf_quote
import sf_ui_render as render
import sf_universe as universe
from tests._base import SFCase, fixed_clock
from tools import performance_audit, power_profile

# Direct render entry points are required: no QWidget, native window, or worker is created.
draw_fill = render.RenderMixin._draw_chart_fill  # discipline-ok(R5): actual fill painter under test
draw_line = render.RenderMixin._draw_chart_line  # discipline-ok(R5): shared line geometry entry
draw_bottom = render.RenderMixin._paint_bottom  # discipline-ok(R5): actual bottom text painter
draw_background = render.RenderMixin._paint_background  # discipline-ok(R5): actual material painter
chart_metrics = render.RenderMixin._chart_metrics  # discipline-ok(R5): actual chart scale calculation
build_index = universe._index  # discipline-ok(R5): swap a synthetic dictionary without file or network IO


class RenderHost(render.RenderMixin):
    """Pure render host; the production QWidget lifecycle is outside these tests."""

    def __init__(self):
        vars(self).update(
            quote={"change": "0.2", "pre_close": "20", "high": "20.4", "low": "19.8"},
            minute_points=[("0930", 20.1, 20.0), ("1000", 20.4, 20.1), ("1030", 19.8, 20.05)],
            _line_ver=1, _line_path=None, _line_path_key=None, _chart_cache_key=None,
            _fonts={}, _fms={}, _bg_path=None, _bg_path_wh=None, _bg_path_ins=None,
            _bg_path_ins_wh=None, _bg_effect=True, _bg_mode="glass", _glass_backend="legacy",
            _glass_dim=lambda: 140, _glass_surface_rim=lambda: (171, 190, 219, 16))

    def height(self):
        return 300


def image_for(host, draw, rect=None, ratio=1):
    image = QImage(300 * ratio, 300 * ratio, QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(ratio)
    image.fill(0)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if rect is None:
            draw(host, painter, 300)
        else:
            draw(host, painter, rect, chart_metrics(host, host.minute_points, rect))
    finally:
        painter.end()
    return image


class TestRenderPerformance(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()
        render.load_bundled_fonts()

    def test_fill_and_line_share_geometry_and_warm_frames_build_no_paths(self):
        host, rect = RenderHost(), QRectF(12, 108, 276, 140)
        image_for(host, draw_fill, rect)
        with mock.patch.object(render, "QPainterPath", wraps=QPainterPath) as paths:
            image_for(host, draw_line, rect)
            image_for(host, draw_fill, rect)
        paths.assert_not_called()

    def test_fill_invalidates_for_all_geometry_scale_data_and_color_changes(self):
        for change in ("data", "width", "height", "origin", "scale", "color", "stock"):
            for ratio in (1, 2):
                with self.subTest(change=change, ratio=ratio):
                    host, rect = RenderHost(), QRectF(12, 108, 276, 140)
                    image_for(host, draw_fill, rect, ratio)
                    if change in ("data", "stock"):
                        host.minute_points = [("0930", 30.2, 30), ("1000", 28.9, 29)]
                        vars(host)["_line_ver"] += 1
                    elif change == "width":
                        rect.setWidth(200)
                    elif change == "height":
                        rect.setHeight(90)
                    elif change == "origin":
                        rect.translate(2, 7)
                    elif change == "scale":
                        host.quote["pre_close"] = "24"
                    else:
                        host.quote["change"] = "-0.2"
                    fresh = RenderHost()
                    fresh.quote, fresh.minute_points = dict(host.quote), list(host.minute_points)
                    self.assertEqual(image_for(host, draw_fill, rect, ratio),
                                     image_for(fresh, draw_fill, rect, ratio))

    def test_fill_does_not_close_or_mutate_the_shared_line_path(self):
        host, rect = RenderHost(), QRectF(12, 108, 276, 140)
        expected = image_for(RenderHost(), draw_line, rect)
        for _ in range(3):
            image_for(host, draw_fill, rect)
        self.assertEqual(image_for(host, draw_line, rect), expected)

    def test_fill_alpha_changes_invalidate_the_cached_gradient(self):
        host, rect = RenderHost(), QRectF(12, 108, 276, 140)
        first = image_for(host, draw_fill, rect)
        with mock.patch.object(render, "CHART_FILL_ALPHA", 160):
            changed = image_for(host, draw_fill, rect)
            self.assertEqual(changed, image_for(RenderHost(), draw_fill, rect))
        self.assertNotEqual(first, changed)

    def test_rim_is_cached_but_color_is_live(self):
        host = RenderHost()
        image_for(host, draw_background)
        with mock.patch.object(render, "smooth_round_rect_path", wraps=render.smooth_round_rect_path) as paths:
            first = image_for(host, draw_background)
            vars(host)["_glass_surface_rim"] = lambda: (126, 145, 173, 64)
            second = image_for(host, draw_background)
        paths.assert_not_called()
        self.assertNotEqual(first, second)

    def test_material_switch_and_resize_match_uncached_rendering(self):
        host = RenderHost()
        image_for(host, draw_background)
        for backend in ("liquid", "legacy"):
            vars(host).update(_glass_backend=backend, height=lambda: 260)
            fresh = RenderHost()
            vars(fresh).update(_glass_backend=backend, height=lambda: 260)
            self.assertEqual(image_for(host, draw_background), image_for(fresh, draw_background))

    def test_bottom_reuses_measurements_and_updates_changed_values(self):
        host = RenderHost()
        image_for(host, draw_bottom)
        with mock.patch.object(render, "bottom_rows_fitting", wraps=render.bottom_rows_fitting) as layout:
            image_for(host, draw_bottom)
            image_for(host, draw_bottom)
            self.assertEqual(layout.call_count, 1)
            host.quote["high"] = "21.6"
            changed = image_for(host, draw_bottom)
            self.assertEqual(layout.call_count, 2)
        fresh = RenderHost()
        fresh.quote = dict(host.quote)
        self.assertEqual(changed, image_for(fresh, draw_bottom))

    def test_caches_are_not_shared_between_windows(self):
        first, second = RenderHost(), RenderHost()
        image_for(first, draw_fill, QRectF(12, 108, 276, 140))
        second.quote["change"] = "-0.5"
        second.minute_points = [("0930", 10, None)]
        expected = image_for(second, draw_fill, QRectF(12, 108, 276, 90))
        self.assertEqual(image_for(second, draw_fill, QRectF(12, 108, 276, 90)), expected)


class CountingName(str):
    comparisons = 0

    def __contains__(self, item):
        type(self).comparisons += 1
        return super().__contains__(item)

    def startswith(self, item, *args):
        type(self).comparisons += 1
        return super().startswith(item, *args)


class TestSearchWorkBounds(SFCase):
    def setUp(self):
        super().setUp()
        for name, value in (("_DISABLED", False), ("_rows", []), ("_by_code", {}), ("_by_name", {})):
            self.track_patch(mock.patch.object(universe, name, value))

    def set_rows(self, names):
        # Install counting strings after normal indexing so normalization cannot erase them.
        rows = [("sh%06d" % i, name) for i, name in enumerate(names)]
        build_index(rows)
        vars(universe)["_rows"] = [(code, CountingName(name)) for code, name in rows]
        CountingName.comparisons = 0
        return rows

    def test_ambiguity_is_decided_at_second_hit_not_end_of_dictionary(self):
        self.set_rows(["Bank A", "Bank B"] + ["Other"] * 6000)
        self.assertEqual(universe.match_confident("Bank"), [])
        self.assertEqual(CountingName.comparisons, 2)

    def test_unique_hit_still_checks_late_ambiguity(self):
        rows = self.set_rows(["Other"] * 6000 + ["Bank A"])
        self.assertEqual(universe.match_confident("Bank"), [rows[-1]])
        rows = self.set_rows(["Bank A"] + ["Other"] * 6000 + ["Bank B"])
        self.assertEqual(universe.match_confident("Bank"), [])

    def test_fuzzy_search_stops_after_limit_without_changing_prefix_priority(self):
        rows = self.set_rows(["Bank %d" % i for i in range(6000)])
        self.assertEqual(universe.match_fuzzy("Bank", 8), rows[:8])
        self.assertEqual(CountingName.comparisons, 8)
        rows = self.set_rows(["AnyBank", "Bank A", "Bank B"])
        self.assertEqual(universe.match_fuzzy("Bank", 8), rows[1:])
        self.assertEqual(universe.match_fuzzy("Any", 8), rows[:1])

    def test_fuzzy_slice_compatibility_and_defensive_copy(self):
        rows = self.set_rows(["Bank A", "Bank B", "Bank C"])
        for limit in (0, 1, 50, -1, -10, None):
            with self.subTest(limit=limit):
                self.assertEqual(universe.match_fuzzy("Bank", limit), rows[:limit])
        result = universe.match_fuzzy("Bank", 2)
        result.clear()
        self.assertEqual(universe.match_fuzzy("Bank", 2), rows[:2])
        with self.assertRaises(TypeError):
            universe.match_fuzzy("Bank", 0.0)


class TestPowerProbeValidity(SFCase):
    def test_probe_config_retains_geometry_but_never_secrets_camera_or_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "original.json"
            payload = {"code": "sh600519", "h": 300, "bg": "glass", "face_guard": True,
                       "alerts": [{"target": 20}], "token": "private", "quote_url": "https://private"}
            source.write_text(json.dumps(payload))
            out = Path(tmp) / "isolated"
            out.mkdir()
            saved = json.loads(Path(power_profile.prepare_probe_config(out, source)).read_text())
            self.assertFalse(saved["face_guard"])
            self.assertEqual(saved["alerts"], [])
            self.assertEqual(saved["h"], payload["h"])
            self.assertNotIn("token", saved)
            self.assertNotIn("quote_url", saved)
            self.assertEqual(json.loads(source.read_text()), payload)

    def test_mock_payloads_pass_real_parsers_freshness_and_source_day_checks(self):
        anchor = datetime.datetime(2026, 9, 22, 15, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        with fixed_clock(anchor, module_names=("sf_quote",)), mock.patch.object(
                sf_quote, "http_get", side_effect=lambda url, **kwargs:
                power_profile.mock_response(url, 3, True, anchor)):
            quote = sf_quote.fetch_quote("sh600519")
            self.assertTrue(sf_quote.quote_is_current(quote, anchor))
            self.assertEqual(quote["pct"], "1.1616")
            points = sf_quote.fetch_minute("sh600519")
        self.assertEqual(len(points), 241)
        self.assertEqual(points.day, anchor.date().isoformat())
        self.assertTrue(all(19 < row[2] < 21 for row in points))

    def test_unknown_mock_request_fails_instead_of_faking_network_success(self):
        with self.assertRaises(ValueError):
            power_profile.mock_response("https://unknown.test/", 0, False, power_profile.ANCHOR["off"])

    def test_failed_child_with_json_is_not_reported_as_a_successful_measurement(self):
        args = SimpleNamespace(mode="trading", seconds=1, net="mock", vary=True,
                               profile=False, native=False)
        child = SimpleNamespace(returncode=2, stdout='{"poll_fail": 5}', stderr="invalid workload")
        with mock.patch.object(power_profile.subprocess, "run", return_value=child), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(power_profile.run_child(args))

    def test_module_inventory_contains_unique_content_hashes_for_all_product_files(self):
        inventory = performance_audit.inventory()
        self.assertEqual(len(inventory), len(list(performance_audit.ROOT.glob("*.py"))))
        self.assertEqual(len({row["module"] for row in inventory}), len(inventory))
        self.assertTrue(all(len(row["sha256"]) == 64 and row["lines"] > 0 for row in inventory))

    def test_benchmark_rejects_empty_workload(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            performance_audit.main(["--iterations", "0"])
        self.assertEqual(raised.exception.code, 2)
