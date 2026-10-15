"""Cross-window material parity and same-window native view ownership."""

import sys
import types
from unittest import mock

from PyQt6.QtCore import QCoreApplication, QEvent, QPoint
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QWidget

import sf_glass
from sf_constants import BG_ALPHA_DEFAULT, BG_LIQUID_GLASS_DIM
from sf_editor import EditorWindow
from sf_glass import GlassMixin, PopupGlassSurface, paint_popup_surface
from sf_ui_render import RenderMixin
from target_panel import TargetPanel
from tests._base import SFCase
from tools.popup_material_probe import CaptureScene, sample_surface

_paint_main = RenderMixin._paint_background  # discipline-ok(R5): Compare actual background pixels without the paintEvent exception guard.
_init = GlassMixin._init_glass_state  # discipline-ok(R5): The test host must have the same render state as production popups.
_inject = GlassMixin._inject_glass  # discipline-ok(R5): Exercise delayed native creation without relying on timer ordering.
_close = GlassMixin._close_glass  # discipline-ok(R5): Test idempotent native teardown directly.


class SurfaceHost(GlassMixin, QWidget):
    def __init__(self):
        super().__init__()
        _init(self)


def render(widget, paint, scale):
    image = QImage(widget.width() * scale, widget.height() * scale,
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(scale)
    image.fill(0)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint(widget, painter)
    finally:
        painter.end()
    return image


class SharedMaterialPixels(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()

    def test_both_real_popups_match_main_pixels_in_every_surface_mode(self):
        with mock.patch("target_panel.load_alerts", return_value=[]):
            popups = [self.track_widget(EditorWindow("sh600183", lambda code: None, QPoint(), 200)),
                      self.track_widget(TargetPanel("sh600183", "test", lambda alerts: True, lambda: True))]
        main = self.track_widget(SurfaceHost())
        modes = ((True, "liquid", "translucent"), (True, "legacy", "translucent"),
                 (False, "none", "translucent"), (False, "none", "solid"))
        for popup in popups:
            main.resize(popup.size())
            for enabled, backend, mode in modes:
                for appearance in ("dark", "light"):
                    for scale in (1, 2):
                        with self.subTest(popup=type(popup).__name__, backend=backend,
                                          mode=mode, appearance=appearance, scale=scale):
                            for widget in (main, popup):
                                vars(widget).update(_bg_effect=enabled, _glass_backend=backend,
                                                    _bg_mode=mode)
                            with mock.patch.object(sf_glass, "detect_system_appearance", return_value=appearance):
                                actual = render(popup, paint_popup_surface, scale)
                                expected = render(main, lambda w, p: _paint_main(w, p, w.width()), scale)
                            self.assertEqual(actual, expected, "Popup material drifted from the main card")
                            self.assertEqual(actual.pixelColor(0, 0).alpha(), 0)

    def test_native_dimming_has_no_gray_or_blue_tint(self):
        host = self.track_widget(SurfaceHost())
        host.resize(200, 180)
        for enabled, alpha in ((True, BG_LIQUID_GLASS_DIM),
                               (False, int(BG_ALPHA_DEFAULT * 255))):
            vars(host).update(_bg_effect=enabled, _glass_backend="liquid")
            image = render(host, paint_popup_surface, 1)
            self.assertEqual(image.pixelColor(host.width() // 2, host.height() // 2).getRgb(),
                             (0, 0, 0, alpha))

    def test_native_sampler_rejects_blank_input_and_exposes_the_previous_tint(self):
        from PyQt6.QtGui import QColor
        for scale in (1, 2):
            image = QImage(420 * scale, 180 * scale, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor(44, 44, 44))
            painter = QPainter(image)
            try:
                painter.fillRect(220 * scale, 0, 200 * scale, 180 * scale, QColor(67, 68, 70))
            finally:
                painter.end()
            main = sample_surface(image, 0, 0, 200, 180, scale)
            popup = sample_surface(image, 220, 0, 200, 180, scale)
            difference = max(abs(a - b) for a, b in zip(main, popup))
            tolerance = 8
            self.assertGreater(difference, tolerance)
        for image, scale in ((QImage(), 1), (QImage(20, 20, QImage.Format.Format_RGB32), 0)):
            with self.assertRaises(ValueError):
                sample_surface(image, 0, 0, 200, 180, scale)
        with self.assertRaises(ValueError):
            sample_surface(QImage(20, 20, QImage.Format.Format_RGB32), 0, 0, 200, 180, 1)


class EmbeddedViewOwnership(SFCase):
    def test_color_capture_restores_levels_focus_policy_and_watchdog(self):
        main, popup, backdrop = mock.Mock(), mock.Mock(), mock.Mock()
        windows = [mock.Mock() for _ in range(4)]
        for window in windows:
            window.level.return_value = 25
            window.hidesOnDeactivate.return_value = True
        vars(main).update(_glass_window=windows[1])
        mapping = {backdrop: windows[0], main: windows[2], popup: windows[3]}
        with mock.patch.object(sf_glass, "content_ns_window", side_effect=mapping.get):
            scene = CaptureScene(main, popup, backdrop)
        try:
            scene.activate()
            main.top_timer.stop.assert_called_once()
        finally:
            scene.close()
        for window in windows:
            window.setLevel_.assert_called_with(25)
            window.setHidesOnDeactivate_.assert_called_with(True)
        main.top_timer.start.assert_called_once()
        scene.close()
        main.top_timer.start.assert_called_once()

    def make_surface(self):
        window, content, view = mock.Mock(), mock.Mock(), mock.Mock()
        window.appearance.return_value = "original"
        api = types.SimpleNamespace(NSViewWidthSizable=2, NSViewHeightSizable=16,
                                    NSWindowBelow=-1, NSAppearanceNameDarkAqua="dark",
                                    NSAppearance=types.SimpleNamespace(appearanceNamed_=lambda name: name))
        return PopupGlassSurface(window, content, view), window, content, view, api

    def test_view_is_a_sibling_below_qt_not_a_second_window(self):
        surface, window, content, view, api = self.make_surface()
        surface.attach(api, "liquid")
        content.superview().addSubview_positioned_relativeTo_.assert_called_once_with(
            view, api.NSWindowBelow, content)
        view.setFrame_.assert_called_once_with(content.frame())
        view.setAutoresizingMask_.assert_called_once_with(api.NSViewWidthSizable | api.NSViewHeightSizable)
        window.setAppearance_.assert_called_once_with("dark")
        window.setContentView_.assert_not_called()
        window.close.assert_not_called()
        surface.close()

    def test_teardown_detaches_once_and_never_closes_qts_window(self):
        surface, window, content, view, api = self.make_surface()
        surface.attach(api, "legacy")
        surface.sync()
        surface.close()
        surface.close()
        surface.sync()
        view.removeFromSuperview.assert_called_once()
        window.close.assert_not_called()
        window.setAppearance_.assert_called_once_with("original")
        self.assertIsNone(surface.view)
        self.assertIsNone(surface.content)

    def test_missing_container_is_not_reported_as_a_working_surface(self):
        surface, window, content, view, api = self.make_surface()
        content.superview.return_value = None
        with self.assertRaises(RuntimeError):
            surface.attach(api, "liquid")
        surface.close()
        window.setContentView_.assert_not_called()


class PopupNativeSetup(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()

    def make_host(self):
        host = self.track_widget(SurfaceHost())
        host.show()
        return host

    def install(self, host, backend="liquid", failure=None, mask_failure=None):
        window, view = mock.Mock(), mock.Mock()
        window.appearance.return_value = "original"
        api = types.SimpleNamespace(NSViewWidthSizable=2, NSViewHeightSizable=16, NSWindowBelow=-1,
                                    NSAppearanceNameDarkAqua="dark",
                                    NSAppearance=types.SimpleNamespace(appearanceNamed_=lambda name: name))
        with mock.patch.dict(sys.modules, AppKit=api), \
                mock.patch.object(sf_glass, "content_ns_window", return_value=window), \
                mock.patch.object(sf_glass, "create_glass_surface", return_value=(view, backend, ""),
                                  side_effect=failure) as create, \
                mock.patch.object(host, "_set_glass_mask", side_effect=mask_failure), \
                mock.patch.object(sf_glass, "log_line"), mock.patch.object(sf_glass, "log_swallow"):
            _inject(host)
        return window, view, create

    def test_owner_actual_backend_controls_popup_selection(self):
        for backend in ("legacy", "liquid"):
            host, owner = self.make_host(), self.make_host()
            vars(owner).update(_bg_effect=True, _glass_backend=backend)
            host.set_surface_owner(owner)
            window, view, create = self.install(host, backend)
            self.assertEqual(create.call_args.args[2], backend == "liquid")
            self.assertEqual(host.glass_diagnostics()["surface"]["backend"], backend)
            self.assertIsNone(vars(host)["_glass_window"], "Popups must not allocate another NSWindow")
            _close(host)
            view.removeFromSuperview.assert_called_once()
            window.close.assert_not_called()

    def test_owner_without_native_material_keeps_shared_fallback(self):
        host, owner = self.make_host(), self.make_host()
        vars(owner).update(_bg_effect=False, _bg_mode="solid")
        host.set_surface_owner(owner)
        window, view, create = self.install(host)
        create.assert_not_called()
        self.assertEqual(vars(host)["_bg_mode"], "solid")
        self.assertFalse(vars(host)["_bg_effect"])

    def test_failed_creation_leaves_no_native_state_and_records_the_reason(self):
        host = self.make_host()
        window, view, create = self.install(host, failure=RuntimeError("injected"))
        self.assertFalse(vars(host)["_bg_effect"])
        self.assertIsNone(vars(host)["_popup_surface"])
        self.assertEqual(vars(host)["_glass_fallback_reason"], "popup_setup_failed:RuntimeError")

    def test_failure_after_attachment_removes_native_material_before_fallback(self):
        host = self.make_host()
        window, view, create = self.install(host, mask_failure=RuntimeError("injected mask failure"))
        view.removeFromSuperview.assert_called_once()
        window.setAppearance_.assert_any_call("original")
        window.close.assert_not_called()
        self.assertFalse(vars(host)["_bg_effect"])
        self.assertIsNone(vars(host)["_glass_view"])

    def test_delete_later_detaches_native_view_even_without_close_event(self):
        host = self.make_host()
        window, view, create = self.install(host)
        host.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        view.removeFromSuperview.assert_called_once()
        window.close.assert_not_called()

    def test_hidden_popup_does_not_install_a_late_native_view(self):
        host = self.make_host()
        host.hide()
        window, view, create = self.install(host)
        create.assert_not_called()
        self.assertTrue(vars(host)["_glass_pending"])

    def test_popup_diagnostics_identify_embedded_attachment_without_a_fake_window(self):
        host, owner = self.make_host(), self.make_host()
        self.install(host)
        vars(owner).update(_editor=host)
        diagnostic = host.glass_diagnostics()
        self.assertTrue(diagnostic["glass_present"])
        self.assertEqual(diagnostic["surface"]["attachment"], "embedded")
        self.assertEqual(owner.glass_diagnostics()["popup_surfaces"]["editor"],
                         {"backend": "liquid", "effective": True, "embedded": True, "fallback_reason": ""})
        _close(host)

    def test_destroyed_popup_is_removed_from_the_space_watch_without_native_calls(self):
        host = self.make_host()
        sf_glass.clear_popup_space_watch()
        try:
            with mock.patch.object(sf_glass, "native_cocoa_active", return_value=True), \
                    mock.patch.object(sf_glass, "join_active_space", return_value="pending:space"):
                sf_glass.watch_active_space(host)
            host.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            with mock.patch.object(sf_glass, "join_active_space") as join, \
                    mock.patch.object(sf_glass, "log_swallow") as log:
                sf_glass.popup_space_tick()
            join.assert_not_called()
            log.assert_not_called()
            self.assertEqual(sf_glass.popup_space_pending(), [])
            self.assertFalse(sf_glass.popup_space_timer_running())
        finally:
            sf_glass.clear_popup_space_watch()
