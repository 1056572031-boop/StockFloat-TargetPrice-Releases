"""Pointer gestures and native deactivate/input drift regressions."""
from unittest import mock

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

import sf_glass
from sf_ui_render import CHART_TOP, Y_PRICE
from tests.test_power_v1410 import _FakeNSWindow, _WinCase


def pointer(win, kind, local, global_pos):
    release = kind == QEvent.Type.MouseButtonRelease
    button = (Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove
              else Qt.MouseButton.LeftButton)
    buttons = Qt.MouseButton.NoButton if release else Qt.MouseButton.LeftButton
    QApplication.sendEvent(win, QMouseEvent(kind, QPointF(local), QPointF(global_pos),
                                           button, buttons, Qt.KeyboardModifier.NoModifier))


class TestChartDrag(_WinCase):
    def _window(self, collapsed=False):
        win = self.make_win()
        win.show()
        if collapsed:
            win._toggle_chart()  # discipline-ok(R5): create the collapsed public click surface without waiting for animation
        self.track_patch(mock.patch.object(win, "_snap_to_edge", lambda: None))
        return win

    def test_chart_drag_moves_window_without_toggling(self):
        for collapsed in (False, True):
            with self.subTest(collapsed=collapsed):
                win = self._window(collapsed)
                start = win.pos()
                local = QPoint(win.width() // 2, Y_PRICE + 5 if collapsed else CHART_TOP + 10)
                global_pos = win.mapToGlobal(local)
                delta = QPoint(QApplication.startDragDistance() + 30, 20)
                pointer(win, QEvent.Type.MouseButtonPress, local, global_pos)
                pointer(win, QEvent.Type.MouseMove, local + delta, global_pos + delta)
                self.assertEqual(win.pos(), start + delta)
                toggled = mock.Mock()
                self.track_patch(mock.patch.object(win, "_toggle_chart", toggled))
                pointer(win, QEvent.Type.MouseButtonRelease, local, global_pos + delta)
                toggled.assert_not_called()

    def test_small_chart_motion_remains_a_click(self):
        win = self._window()
        start = win.pos()
        local = QPoint(win.width() // 2, CHART_TOP + 10)
        global_pos = win.mapToGlobal(local)
        delta = QPoint(max(0, QApplication.startDragDistance() - 1), 0)
        toggle = mock.Mock()
        self.track_patch(mock.patch.object(win, "_toggle_chart", toggle))
        pointer(win, QEvent.Type.MouseButtonPress, local, global_pos)
        pointer(win, QEvent.Type.MouseMove, local + delta, global_pos + delta)
        self.assertEqual(win.pos(), start)
        pointer(win, QEvent.Type.MouseButtonRelease, local + delta, global_pos + delta)
        toggle.assert_called_once_with()

    def test_release_without_move_does_not_toggle_after_large_motion(self):
        win = self._window()
        local = QPoint(win.width() // 2, CHART_TOP + 10)
        global_pos = win.mapToGlobal(local)
        toggle = mock.Mock()
        self.track_patch(mock.patch.object(win, "_toggle_chart", toggle))
        pointer(win, QEvent.Type.MouseButtonPress, local, global_pos)
        delta = QPoint(QApplication.startDragDistance(), 0)
        pointer(win, QEvent.Type.MouseButtonRelease, local + delta, global_pos + delta)
        toggle.assert_not_called()

    def test_title_drag_still_moves(self):
        win = self._window()
        local = QPoint(10, 10)
        start = win.pos()
        global_pos = win.mapToGlobal(local)
        delta = QPoint(40, 30)
        pointer(win, QEvent.Type.MouseButtonPress, local, global_pos)
        pointer(win, QEvent.Type.MouseMove, local + delta, global_pos + delta)
        self.assertEqual(win.pos(), start + delta)
        pointer(win, QEvent.Type.MouseButtonRelease, local, global_pos + delta)


class TestNativeTopDrift(_WinCase):
    def _native(self):
        win = self.make_win()
        native = _FakeNSWindow(behavior=sf_glass.public_overlay_collection_behavior())
        self.track_patch(mock.patch.object(sf_glass, "content_ns_window", return_value=native))
        self.track_patch(mock.patch.object(sf_glass, "active_space_id", return_value=None))
        return win, native

    def test_qt_always_show_tool_window_is_set(self):
        win = self.make_win()
        self.assertTrue(win.testAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow))

    def test_deactivation_hiding_drift_is_repaired(self):
        win, native = self._native()
        native.setHidesOnDeactivate_(True)
        native.writes.clear()
        sf_glass.GlassMixin._force_top(win)  # discipline-ok(R5): explicitly drive the native watchdog tick while the fixture clock is stopped
        self.assertFalse(native.hidesOnDeactivate())
        self.assertIn(("orderFrontRegardless", None), native.writes)

    def test_mouse_passthrough_drift_is_repaired(self):
        win, native = self._native()
        native.setIgnoresMouseEvents_(True)
        native.writes.clear()
        sf_glass.GlassMixin._force_top(win)  # discipline-ok(R5): drive one watchdog tick to verify native repair output
        self.assertFalse(native.ignoresMouseEvents())
        self.assertIn(("setIgnoresMouseEvents_", False), native.writes)

    def test_healthy_native_window_does_no_writes(self):
        win, native = self._native()
        sf_glass.GlassMixin._force_top(win)  # discipline-ok(R5): drive one watchdog tick and observe all native writes
        self.assertEqual(native.writes, [])

    def test_level_repair_preserves_transparent_pixel_input_mode(self):
        win, native = self._native()
        native._level = 0  # discipline-ok(R5): simulate Qt rebuilding the native window at its default level
        sf_glass.GlassMixin._force_top(win)  # discipline-ok(R5): drive one watchdog tick and observe native writes
        self.assertIn(("setLevel_", sf_glass._TOP_WINDOW_LEVEL), native.writes)  # discipline-ok(R5): compare the native write against the product's sole level definition
        self.assertNotIn(("setIgnoresMouseEvents_", False), native.writes)

    def test_minimized_and_quitting_windows_are_not_resurrected(self):
        for attr in ("_minimized", "_quitting"):
            with self.subTest(state=attr):
                win, native = self._native()
                setattr(win, attr, True)  # discipline-ok(R5): model a delayed watchdog callback arriving after a hide/quit transition
                native.setHidesOnDeactivate_(True)
                native.writes.clear()
                sf_glass.GlassMixin._force_top(win)  # discipline-ok(R5): deliver the delayed watchdog callback explicitly
                self.assertEqual(native.writes, [])

    def test_failed_native_setter_leaves_diagnostics(self):
        win, native = self._native()
        native.setHidesOnDeactivate_(True)
        self.track_patch(mock.patch.object(native, "setHidesOnDeactivate_", side_effect=RuntimeError("bridge")))
        diagnostic = mock.Mock()
        self.track_patch(mock.patch.object(sf_glass, "log_swallow", diagnostic))
        sf_glass.GlassMixin._force_top(win)  # discipline-ok(R5): drive a failed native repair and verify observable diagnostic output
        self.assertTrue(any("hidesOnDeactivate" in c.args[0] for c in diagnostic.call_args_list))
        self.assertIn(("orderFrontRegardless", None), native.writes)

    def test_diagnostics_report_native_input_even_without_glass(self):
        win, native = self._native()
        self.track_patch(mock.patch.object(sf_glass, "native_cocoa_active", return_value=True))
        native.setIgnoresMouseEvents_(True)
        state = win.glass_diagnostics()
        self.assertTrue(state["main_interaction"]["ignores_mouse_events"])
        self.assertFalse(state["main_interaction"]["hides_on_deactivate"])
        self.assertEqual(state["main_interaction"]["level"], 25)
        self.assertIsNone(state["real_state"])


class TestRealNativeTop(_WinCase):
    def _window(self):
        win = self.make_win()
        if not sf_glass.native_cocoa_active():
            self.skipTest("real NSWindow checks require QT_QPA_PLATFORM=cocoa")
        win.show()
        QApplication.instance().processEvents()
        native = sf_glass.content_ns_window(win)
        self.assertIsNotNone(native)
        return win, native

    def test_native_window_is_not_configured_to_hide_on_deactivation(self):
        _, native = self._window()
        self.assertFalse(native.hidesOnDeactivate())

    def test_native_drift_is_repaired(self):
        win, native = self._window()
        for getter, setter in (("hidesOnDeactivate", "setHidesOnDeactivate_"),
                               ("ignoresMouseEvents", "setIgnoresMouseEvents_")):
            with self.subTest(property=getter):
                getattr(native, setter)(True)
                sf_glass.GlassMixin._force_top(win)  # discipline-ok(R5): drive native watchdog repair rather than relying on wall-clock scheduling
                self.assertFalse(getattr(native, getter)())
