"""Permanent corner gate must reject the original defect and stale artifacts."""

import importlib
from pathlib import Path
import sys
import types
from unittest import mock

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath

from renderer import smooth_round_rect_path
from sf_constants import GLASS_INSET, R
from tests._base import SFCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import verify_card_corners as gate


def capture(backend="liquid", scale=2):
    width, height = 6 * R, 8 * R
    image = QImage(width * scale, height * scale, QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(scale)
    image.fill(QColor(245, 245, 245))
    if backend == "liquid":
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, width, height), R, R)
    else:
        path = smooth_round_rect_path(GLASS_INSET, GLASS_INSET,
                                      width - 2 * GLASS_INSET, height - 2 * GLASS_INSET, R)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillPath(path, QColor(44, 44, 44))
    finally:
        painter.end()
    return image


class NativeCornerJudgeTests(SFCase):
    def test_both_materials_and_scales_pass_with_matched_geometry(self):
        for backend in ("liquid", "legacy"):
            for scale in (1, 2):
                with self.subTest(backend=backend, scale=scale):
                    self.assertFalse(gate.native_corner_problems(
                        capture(backend, scale), backend, scale, R,
                        GLASS_INSET if backend == "legacy" else 0))

    def test_original_squircle_over_circular_material_is_rejected(self):
        self.assertTrue(gate.native_corner_problems(capture("legacy"), "liquid", 2, R))

    def test_one_protruding_corner_is_rejected(self):
        image = capture()
        painter = QPainter(image)
        painter.fillRect(QRectF(1, 1, 4, 4), QColor(44, 44, 44))
        painter.end()
        self.assertTrue(gate.native_corner_problems(image, "liquid", 2, R))

    def test_blank_capture_cannot_pass_as_transparent_corners(self):
        image = capture()
        image.fill(QColor(245, 245, 245))
        self.assertTrue(gate.native_corner_problems(image, "liquid", 2, R))

    def test_missing_or_invalid_evidence_fails_closed(self):
        for image, backend, scale, radius in ((QImage(), "liquid", 2, R),
                                              (capture(), "liquid", 0, R),
                                              (capture(), "unknown", 2, R),
                                              (capture(), "liquid", 2, 0)):
            self.assertTrue(gate.native_corner_problems(image, backend, scale, radius))


class CornerRenderGateTests(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()

    def test_actual_production_background_passes_the_matrix(self):
        self.assertFalse(gate.render_problems())

    def test_restoring_the_original_path_fails_every_size_and_scale(self):
        def previous(w, h, radius, inset):
            return smooth_round_rect_path(inset, inset, w - 2 * inset, h - 2 * inset, radius - inset)
        with mock.patch("sf_ui_render.liquid_glass_dim_path", previous):
            self.assertEqual(len(gate.render_problems()), 6)


class BundleGeometryTests(SFCase):
    class Bundle:
        def __init__(self):
            self.modules = {}
            for name in ("renderer", "sf_ui_render", "sf_glass", "sf_constants"):
                module = importlib.import_module(name)
                self.modules[name] = compile(Path(module.__file__).read_text(), name, "exec")

        def code(self, name):
            return self.modules.get(name)

    def test_validated_packaged_geometry_is_accepted(self):
        self.assertFalse(gate.bundle_problems(self.Bundle()))

    def test_old_packaged_path_is_rejected_even_when_source_passes(self):
        bundle = self.Bundle()
        bundle.modules["renderer"] = compile("def liquid_glass_dim_path(w,h,r,inset): return None", "old", "exec")
        self.assertTrue(gate.bundle_problems(bundle))

    def test_missing_packaged_geometry_is_rejected(self):
        bundle = self.Bundle()
        bundle.modules.pop("sf_glass")
        self.assertTrue(gate.bundle_problems(bundle))

    def test_packaged_constant_drift_is_rejected(self):
        bundle = self.Bundle()
        bundle.modules["sf_constants"] = compile("R = 99", "old", "exec")
        self.assertTrue(gate.bundle_problems(bundle))

    def test_code_shape_ignores_only_location_not_behavior(self):
        first = gate.find_code(compile("def f(): return 1", "one", "exec"), "f")
        moved = gate.find_code(compile("\n\ndef f(): return 1", "two", "exec"), "f")
        changed = gate.find_code(compile("def f(): return 2", "one", "exec"), "f")
        self.assertEqual(gate.code_shape(first), gate.code_shape(moved))
        self.assertNotEqual(gate.code_shape(first), gate.code_shape(changed))

    def test_missing_or_ambiguous_functions_are_rejected(self):
        for source in ("x = 1", "def f(): pass\ndef f(): pass"):
            with self.assertRaises(ValueError):
                gate.find_code(compile(source, "sample", "exec"), "f")


class CornerGateCommandTests(SFCase):
    def test_self_test_catches_an_oracle_that_accepts_everything(self):
        self.assertFalse(gate.self_test())
        with mock.patch.object(gate, "native_corner_problems", return_value=[]):
            self.assertTrue(gate.self_test())

    def test_self_test_catches_an_oracle_that_rejects_everything(self):
        with mock.patch.object(gate, "native_corner_problems", return_value=["always red"]):
            self.assertTrue(gate.self_test())

    def test_self_test_cli_does_not_claim_source_or_artifact_validation(self):
        with mock.patch.object(gate, "render_problems") as render:
            self.assertEqual(gate.main(["--self-test"]), 0)
        render.assert_not_called()

    def test_cli_returns_nonzero_on_pixel_failure(self):
        with mock.patch.object(gate, "render_problems", return_value=["mismatch"]):
            self.assertEqual(gate.main([]), 1)

    def test_cli_disposes_bundle_and_cannot_ignore_native_failure(self):
        bundle = mock.Mock()
        with mock.patch.object(gate, "render_problems", return_value=[]), \
                mock.patch("verify_dmg_pyz.Bundle", return_value=bundle), \
                mock.patch.object(gate, "bundle_problems", return_value=[]), \
                mock.patch.object(gate, "native_problems", return_value=["failed"]):
            self.assertEqual(gate.main(["--app", "example.app", "--native"]), 1)
        bundle.dispose.assert_called_once()

    def test_native_gate_runs_each_available_material_and_propagates_failure(self):
        api = types.SimpleNamespace(NSGlassEffectView=object())
        result = types.SimpleNamespace(returncode=1, stdout="probe output", stderr="probe failure")
        with mock.patch.dict(sys.modules, AppKit=api), \
                mock.patch.object(gate.subprocess, "run", return_value=result) as run:
            self.assertEqual(len(gate.native_problems(Path("evidence"))), 2)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all("--corners" in call.args[0] for call in run.call_args_list))

    def test_older_hosts_must_still_run_the_legacy_probe(self):
        result = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.dict(sys.modules, AppKit=types.SimpleNamespace()), \
                mock.patch.object(gate.subprocess, "run", return_value=result) as run:
            self.assertFalse(gate.native_problems(Path("evidence")))
        run.assert_called_once()
        self.assertIn("--legacy", run.call_args.args[0])
