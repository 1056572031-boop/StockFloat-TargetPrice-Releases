"""Native material selection, rollback and painting without real Cocoa windows."""

import types
from unittest import mock

from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication, QWidget

import sf_glass
from sf_constants import BG_LIQUID_GLASS_DIM, LIQUID_GLASS_OPACITY, R
from sf_glass import GlassMixin, create_glass_surface
from sf_ui_render import RenderMixin
from tests._base import SFCase

_setup = GlassMixin._setup_glass  # discipline-ok(R5): Exercise native setup via its private lifecycle entry.
_mask = GlassMixin._set_glass_mask  # discipline-ok(R5): Native rounding has no independent public entry.
_paint = RenderMixin._paint_background  # discipline-ok(R5): Inspect rendered background pixels without paintEvent swallowing errors.


class View:
    def __init__(self, kind, failure=None):
        self.kind = kind
        self.failure = failure
        self.settings = {}

    def initWithFrame_(self, frame):
        if self.failure == "init":
            raise RuntimeError("view init failed")
        self.settings["frame"] = frame
        return self

    def __getattr__(self, name):
        if not name.startswith("set"):
            raise AttributeError(name)

        def setter(value):
            if self.failure == name:
                raise RuntimeError("view configuration failed")
            self.settings[name] = value
        return setter


class Factory:
    def __init__(self, kind, failure=None):
        self.kind = kind
        self.failure = failure
        self.made = []

    def alloc(self):
        view = View(self.kind, self.failure)
        self.made.append(view)
        return view


def toolkit(liquid=True, failure=None):
    api = types.SimpleNamespace(
        NSVisualEffectView=Factory("legacy"),
        NSGlassEffectViewStyleRegular=0,
        NSAppearanceNameDarkAqua="dark-appearance",
        NSAppearance=types.SimpleNamespace(appearanceNamed_=lambda name: name))
    if liquid:
        api.NSGlassEffectView = Factory("liquid", failure)
    return api


class MaterialSelectionTests(SFCase):
    def test_regular_surface_has_approved_configuration(self):
        api = toolkit()
        view, backend, reason = create_glass_surface(api, "frame", True)
        self.assertEqual(backend, "liquid")
        self.assertEqual(reason, "")
        self.assertEqual(view.settings["setStyle_"], api.NSGlassEffectViewStyleRegular)
        self.assertEqual(view.settings["setAlphaValue_"], LIQUID_GLASS_OPACITY)
        self.assertEqual(view.settings["setCornerRadius_"], float(R))
        self.assertEqual(view.settings["setAppearance_"], api.NSAppearanceNameDarkAqua)
        self.assertFalse(view.settings["setEffectIsInteractive_"])
        self.assertFalse(api.NSVisualEffectView.made)

    def test_missing_new_api_uses_existing_material(self):
        view, backend, reason = create_glass_surface(toolkit(liquid=False), "frame", True)
        self.assertEqual(backend, "legacy")
        self.assertEqual(reason, "liquid_api_unavailable")
        self.assertEqual(view.settings["setMaterial_"], 21)
        self.assertEqual(view.settings["setBlendingMode_"], 0)
        self.assertEqual(view.settings["setState_"], 1)

    def test_each_failed_native_configuration_falls_back_and_logs(self):
        for failure in ("init", "setStyle_", "setCornerRadius_", "setEffectIsInteractive_", "setAppearance_", "setAlphaValue_"):
            with self.subTest(failure), mock.patch.object(sf_glass, "log_swallow") as log:
                view, backend, reason = create_glass_surface(toolkit(failure=failure), "frame", True)
                self.assertEqual(view.kind, "legacy")
                self.assertEqual(backend, "legacy")
                self.assertEqual(reason, "liquid_create_failed:RuntimeError")
                log.assert_called_once()

    def test_legacy_hosts_never_allocate_liquid_surface(self):
        api = toolkit()
        view, backend, reason = create_glass_surface(api, "frame")
        self.assertEqual((view.kind, backend, reason), ("legacy", "legacy", ""))
        self.assertFalse(api.NSGlassEffectView.made)

    def test_failure_of_both_materials_is_not_a_fake_success(self):
        api = toolkit(failure="init")
        api.NSVisualEffectView.failure = "init"
        with mock.patch.object(sf_glass, "log_swallow"), self.assertRaises(RuntimeError):
            create_glass_surface(api, "frame", True)


class SetupFallbackTests(SFCase):
    class Host(GlassMixin):
        def width(self):
            return 184

        def height(self):
            return 224

    def test_no_native_window_concludes_unavailable(self):
        host = self.Host()
        with mock.patch.object(sf_glass, "content_ns_window", return_value=None), \
                mock.patch.object(sf_glass, "create_glass_surface") as create:
            self.assertFalse(_setup(host))
        create.assert_not_called()
        self.assertEqual(host.glass_diagnostics()["surface"]["fallback_reason"], "native_window_unavailable")

    def test_failed_liquid_attachment_retries_legacy_once(self):
        host = self.Host()
        window = types.SimpleNamespace(frame=lambda: "frame")
        results = [(View("liquid"), "liquid", ""), (View("legacy"), "legacy", "")]
        with mock.patch.object(sf_glass, "content_ns_window", return_value=window), \
                mock.patch.object(sf_glass, "create_glass_surface", side_effect=results) as create, \
                mock.patch.object(host, "_install_glass_window", side_effect=(False, True)) as attach:
            self.assertTrue(_setup(host))
        self.assertEqual(create.call_count, 2)
        self.assertEqual(attach.call_count, 2)
        self.assertEqual(host.glass_diagnostics()["surface"]["backend"], "legacy")
        self.assertEqual(host.glass_diagnostics()["surface"]["fallback_reason"], "liquid_window_setup_failed")

    def test_failed_legacy_attachment_does_not_loop(self):
        host = self.Host()
        with mock.patch.object(sf_glass, "content_ns_window", return_value=object()), \
                mock.patch.object(sf_glass, "create_glass_surface", return_value=(View("legacy"), "legacy", "")) as create, \
                mock.patch.object(host, "_install_glass_window", return_value=False):
            self.assertFalse(_setup(host))
        create.assert_called_once()


class LiquidPaintTests(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    class Host(GlassMixin, RenderMixin, QWidget):
        def __init__(self):
            QWidget.__init__(self)
            self.resize(184, 224)
            self.__dict__.update(_bg_effect=True, _glass_backend="liquid", _bg_path=None,
                                 _bg_path_wh=None, _bg_path_ins=None, _bg_path_ins_wh=None)

    def test_background_is_black_dimming_with_transparent_edges(self):
        host = self.Host()
        self.addCleanup(host.close)
        image = QImage(host.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            _paint(host, painter, host.width())
        finally:
            painter.end()
        center = image.pixelColor(image.width() // 2, image.height() // 2)
        self.assertEqual(center.alpha(), BG_LIQUID_GLASS_DIM)
        self.assertEqual(center.getRgb()[:3], (0, 0, 0))
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)

    def test_liquid_rounding_uses_native_corner_radius(self):
        host = self.Host()
        self.addCleanup(host.close)
        view = View("liquid")
        _mask(host, view, host.width(), host.height())
        self.assertEqual(view.settings["setCornerRadius_"], float(R))

    def test_dimming_does_not_follow_light_system_appearance(self):
        host = self.Host()
        self.addCleanup(host.close)
        with mock.patch.object(sf_glass, "detect_system_appearance", return_value="light"):
            self.assertEqual(host.glass_diagnostics()["surface"]["dim_alpha"], BG_LIQUID_GLASS_DIM)
