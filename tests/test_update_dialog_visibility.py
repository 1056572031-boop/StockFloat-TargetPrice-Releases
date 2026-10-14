"""Exercise real menu clicks and native presentation, not just update-request flags."""
import unittest
from typing import Any
from unittest.mock import Mock, patch

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

import sf_glass
import sf_system_alert as alerts
import stock_float_window as window
from tests._base import SFCase
from tests.test_v1443_update_feedback import _fake, _NEW
from tests.test_v148_nativealert import _FakeAppKit, _FakeTarget, _Window
from tests.test_v1417_alert_chain import _spawn

# These are the handlers under test; a minimal QWidget avoids real quote/config workers.
_check = window.FloatWindow._check_updates_background  # discipline-ok(R5): direct handler under test
_result = window.FloatWindow._handle_update_check  # discipline-ok(R5): direct handler under test
_cancel = window.FloatWindow._handle_update_check_cancel  # discipline-ok(R5): direct handler under test
_close_flow = window.FloatWindow._close_update_flow_box  # discipline-ok(R5): direct lifecycle boundary under test
_retry_space = alerts.NativeAlert._retry_active_space  # discipline-ok(R5): deterministic timer delivery


class _MenuHost(QWidget):
    contextMenuEvent: Any = window.FloatWindow.contextMenuEvent
    _on_ctx_menu_closed: Any = window.FloatWindow._on_ctx_menu_closed
    _check_updates_background: Any = _check
    _show_checking_dialog: Any = window.FloatWindow._show_checking_dialog
    _dispose_update_dialog: Any = window.FloatWindow._dispose_update_dialog
    _update_check_stale: Any = window.FloatWindow._update_check_stale

    def __init__(self):
        super().__init__()
        self._ctx_menu = None
        self._update_pending = None
        self._threads_stopped = False
        self._update_checking = False
        self._update_transition_pending = False
        self._update_downloading = False
        self._update_check_dialog = None
        self.results = []

    def _force_fetch(self):
        pass

    def _export_diagnostics(self):
        pass

    def _stop_face_guard_menu_timer(self):
        pass

    def _anchor_update_dialog_screen(self):
        pass

    def _q_put(self, item):
        self.results.append(item)


class MenuClickVisibility(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.host = self.track_widget(_MenuHost())
        self.track_patch(patch.object(window, "feature_enabled", return_value=False))
        self.worker = Mock()
        self.shown = Mock()
        self.track_patch(patch.object(window.threading, "Thread", self.worker))
        self.track_patch(patch.object(alerts, "show_alert", self.shown))
        self.track_patch(patch.object(alerts, "bundled_icon_path", return_value=None))

    def click_update(self):
        self.host.contextMenuEvent(QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, QPoint(20, 20), QPoint(140, 140)))
        from PyQt6.QtWidgets import QMenu
        menu = next(child for child in self.host.findChildren(QMenu) if child.isVisible())
        action = next(a for a in menu.actions() if a.text() == "检查更新")
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton,
                         pos=menu.actionGeometry(action).center())

    def test_first_real_menu_click_shows_feedback_before_worker_start(self):
        order = []
        self.shown.side_effect = lambda **kw: order.append("visible") or Mock()
        self.worker.return_value.start.side_effect = lambda: order.append("request")
        self.click_update()
        self.assertEqual(order, ["visible", "request"])

    def test_second_click_reveals_existing_dialog_without_another_request(self):
        dialog = Mock()
        dialog.present.return_value = True
        self.shown.return_value = dialog
        self.click_update()
        self.click_update()
        dialog.present.assert_called_once_with()
        self.shown.assert_called_once()
        self.worker.assert_called_once()

    def test_closed_handle_is_replaced_instead_of_swallowing_click(self):
        dialog = Mock()
        dialog.present.return_value = False
        self.shown.return_value = dialog
        self.click_update()
        self.click_update()
        self.assertEqual(self.shown.call_count, 2)
        dialog.close.assert_called_once_with()
        self.worker.assert_called_once()

    def test_cancel_then_recheck_renews_the_inflight_result(self):
        prompts = []
        host = _fake(prompts=prompts, _update_checking=True,
                     _update_transition_pending=True, _dispose_update_dialog=lambda self, box: None)
        _cancel(host, "取消")
        _check(host, manual=True)
        _result(host, False, _NEW, None)
        self.assertEqual(prompts, [_NEW], "a new manual click must revoke the previous cancellation")

    def test_old_queued_cancel_cannot_close_the_replacement_dialog(self):
        first, second = Mock(), Mock()
        first.present.return_value = False
        self.shown.side_effect = [first, second]
        self.click_update()
        self.shown.call_args.kwargs["on_result"]("取消")
        old_result = self.host.results[-1][1]
        self.click_update()
        _cancel(self.host, old_result)
        second.close.assert_not_called()
        self.shown.call_args.kwargs["on_result"]("取消")
        _cancel(self.host, self.host.results[-1][1])
        second.close.assert_called_once_with()
        self.worker.assert_called_once()

    def test_queued_cancel_after_check_completion_is_ignored(self):
        dialog = Mock()
        self.shown.return_value = dialog
        self.click_update()
        self.shown.call_args.kwargs["on_result"]("取消")
        _close_flow(self.host)
        _cancel(self.host, self.host.results[-1][1])
        dialog.close.assert_called_once_with()


class NativePresentation(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.track_patch(patch.object(alerts, "AppKit", _FakeAppKit))
        self.track_patch(patch.object(alerts, "_AlertTarget", _FakeTarget))
        self.track_patch(patch.object(alerts, "center_window"))
        self.track_patch(patch.object(alerts.NativeAlert, "_center_content"))
        self.swallowed = Mock()
        self.track_patch(patch.object(alerts, "_swallow", self.swallowed))

    def make_dialog(self):
        dialog = alerts.NativeAlert("checking", "", ("OK",), "OK", None, None, None)
        self.addCleanup(dialog.close)
        return dialog

    def test_window_policy_covers_fullscreen_and_floating_main_window(self):
        dialog = self.make_dialog()
        native = dialog._window  # discipline-ok(R5): NSWindow getters are the observable native boundary
        self.assertTrue(native.behavior & sf_glass.BEHAVIOR_JOIN_ALL_SPACES)
        self.assertTrue(native.behavior & _FakeAppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        self.assertFalse(native.behavior & sf_glass.BEHAVIOR_MOVE_TO_ACTIVE_SPACE)
        self.assertEqual(native.level, sf_glass.TOOL_WINDOW_LEVEL)
        self.assertFalse(native.hides)

    def test_present_reorders_same_window_and_closed_alert_never_reopens(self):
        dialog = self.make_dialog()
        native = dialog._window  # discipline-ok(R5): native ordering calls expose visible presentation
        native.calls.clear()
        self.assertTrue(dialog.present())
        self.assertIn(("front",), native.calls)
        dialog.close()
        native.calls.clear()
        self.assertFalse(dialog.present())
        self.assertEqual(native.calls, [])

    def test_late_space_attachment_retries_then_stops(self):
        active = [False]
        with patch.object(_Window, "isOnActiveSpace", lambda self: active[0]), \
             patch.object(_Window, "windowNumber", return_value=42, create=True), \
             patch.object(_Window, "orderFrontRegardless", create=True), \
             patch.object(sf_glass, "cgs_add_window_to_space") as join:
            dialog = self.make_dialog()
            join.assert_called_once_with(42)
            _retry_space(dialog)
            self.assertEqual(join.call_count, 2)
            active[0] = True
            _retry_space(dialog)
            self.assertFalse(dialog._space_timer.isActive())  # discipline-ok(R5): verify no continuing retry workload
            dialog.close()
            _retry_space(dialog)
            self.assertEqual(join.call_count, 2, "late callbacks after close must not touch WindowServer")

    def test_unavailable_space_bridge_is_bounded_and_logged(self):
        with patch.object(_Window, "isOnActiveSpace", return_value=False), \
             patch.object(_Window, "windowNumber", return_value=42, create=True), \
             patch.object(sf_glass, "cgs_add_window_to_space", side_effect=RuntimeError("unavailable")):
            dialog = self.make_dialog()
            for _ in range(sf_glass.POPUP_SPACE_MAX_TICKS):
                _retry_space(dialog)
            self.assertFalse(dialog._space_timer.isActive())  # discipline-ok(R5): bounded failure must stop the timer
            self.assertIn("active_space_timeout", self.swallowed.call_args.args[0])


class RealNativePresentation(SFCase):
    def test_real_framework_stores_visibility_policy_and_closed_alert_stays_closed(self):
        if alerts.AppKit is None:
            self.skipTest("AppKit unavailable")
        probe, markers = _spawn("probe")
        if markers.get("probe") != "ok":
            self.skipTest("native window service unavailable: %s" % probe.stderr[-200:])
        proc, markers = _spawn("presentation")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(markers.get("joins_all"), "True")
        self.assertEqual(markers.get("fullscreen_aux"), "True")
        self.assertEqual(markers.get("moves_space"), "False")
        self.assertEqual(markers.get("above_main"), "True")
        self.assertEqual(markers.get("hides"), "False")
        self.assertEqual(markers.get("reopens_closed"), "False")


if __name__ == "__main__":
    unittest.main()
