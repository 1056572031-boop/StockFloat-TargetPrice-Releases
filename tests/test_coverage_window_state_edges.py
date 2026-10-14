"""Window state edges exercised on hosts without QWidget construction.

Public diagnostic text, native settings navigation and target transactions can
be observed without creating a real window, starting workers or writing user
configuration. Collaborators are explicit; production methods run unchanged.
"""
from __future__ import annotations

import pathlib
import sys
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from _base import SFCase
except ImportError:  # pragma: no cover
    from tests._base import SFCase

import sf_presence as presence
import stock_float_window as window

rainbow_tick = window.FloatWindow._rainbow_tick  # discipline-ok(R5): deterministic timer delivery
clear_targets = window.FloatWindow._clear_targets  # discipline-ok(R5): persistence transaction entry
open_camera_settings = window.FloatWindow._open_camera_privacy_settings  # discipline-ok(R5): native URL route


def host(**state):
    return types.SimpleNamespace(**state)


class WindowStateEdges(SFCase):
    def setUp(self):
        super().setUp()
        self.log = mock.Mock()
        self.track_patch(mock.patch.object(window, "log_line", self.log))
        self.track_patch(mock.patch.object(window.time, "monotonic", return_value=1000.0))

    def rainbow_host(self, active=True):
        driver = types.SimpleNamespace(rainbow_active=mock.Mock(return_value=active))
        timer = types.SimpleNamespace(stop=mock.Mock())
        repaint = mock.Mock()
        return host(_anim_driver=driver, _rainbow_timer=timer, _repaint_now=repaint), driver, timer, repaint

    def test_active_rainbow_repaints_without_stopping_timer(self):
        target, driver, timer, repaint = self.rainbow_host()
        rainbow_tick(target)
        driver.rainbow_active.assert_called_once_with(1000.0)
        timer.stop.assert_not_called()
        repaint.assert_called_once_with()
        self.log.assert_not_called()

    def test_expired_rainbow_stops_timer_and_repaints_normal_color(self):
        target, _driver, timer, repaint = self.rainbow_host(active=False)
        rainbow_tick(target)
        timer.stop.assert_called_once_with()
        repaint.assert_called_once_with()

    def test_rainbow_error_is_logged_and_timer_stops(self):
        target, driver, timer, repaint = self.rainbow_host()
        driver.rainbow_active.side_effect = RuntimeError("driver lost")
        rainbow_tick(target)
        repaint.assert_not_called()
        timer.stop.assert_called_once_with()
        self.assertIn("rainbow tick error", self.log.call_args.args[0])

    def test_rainbow_timer_cleanup_error_never_escapes(self):
        target, driver, timer, _repaint = self.rainbow_host()
        driver.rainbow_active.side_effect = RuntimeError("driver lost")
        timer.stop.side_effect = RuntimeError("timer destroyed")
        rainbow_tick(target)
        timer.stop.assert_called_once_with()
        self.log.assert_called_once()

    def clear_host(self, *, enabled=True, saved=True, live=None):
        live = live if live is not None else [{"target": 10, "state": "pending"}]
        save, feedback, redraw, stop = mock.Mock(return_value=saved), mock.Mock(), mock.Mock(), mock.Mock()
        target = host(_targets_enabled=lambda: enabled, _safe_alerts=lambda: live,
                      _persist_alerts=save, _after_save_config=feedback, update=redraw,
                      _clear_timer=host(stop=stop), _alerts=live)
        return target, save, feedback, redraw, stop

    def test_clear_targets_persists_before_memory_and_visible_update(self):
        target, save, feedback, redraw, stop = self.clear_host()
        self.assertTrue(clear_targets(target))
        save.assert_called_once_with([])
        feedback.assert_called_once_with(True)
        self.assertEqual(vars(target)["_alerts"], [])
        stop.assert_called_once_with()
        redraw.assert_called_once_with()

    def test_failed_clear_keeps_targets_and_timer_untouched(self):
        target, save, feedback, redraw, stop = self.clear_host(saved=False)
        live = vars(target)["_alerts"]
        self.assertFalse(clear_targets(target))
        self.assertIs(vars(target)["_alerts"], live)
        save.assert_called_once_with([])
        feedback.assert_called_once_with(False)
        stop.assert_not_called()
        redraw.assert_not_called()

    def test_empty_clear_needs_no_disk_write(self):
        target, save, feedback, redraw, stop = self.clear_host(live=[])
        self.assertTrue(clear_targets(target))
        save.assert_not_called()
        feedback.assert_called_once_with(True)
        redraw.assert_called_once_with()
        stop.assert_called_once_with()

    def test_disabled_targets_never_attempt_clear(self):
        target, save, feedback, redraw, stop = self.clear_host(enabled=False)
        self.assertFalse(clear_targets(target))
        for callback in (save, feedback, redraw, stop):
            callback.assert_not_called()

    def test_clear_timer_failure_does_not_undo_successful_clear(self):
        target, _save, _feedback, redraw, stop = self.clear_host()
        stop.side_effect = RuntimeError("deleted timer")
        self.assertTrue(clear_targets(target))
        self.assertEqual(vars(target)["_alerts"], [])
        redraw.assert_called_once_with()

    def test_clear_persistence_exception_is_red_and_logged(self):
        target, save, _feedback, redraw, stop = self.clear_host()
        save.side_effect = OSError("disk gone")
        self.assertFalse(clear_targets(target))
        redraw.assert_not_called()
        stop.assert_not_called()
        self.assertIn("clear targets error", self.log.call_args.args[0])

    def camera_route(self, url="camera-url", opened=True, error=None):
        open_url = mock.Mock(return_value=opened, side_effect=error)
        workspace = host(sharedWorkspace=lambda: host(openURL_=open_url))
        nsurl = host(URLWithString_=mock.Mock(return_value=url))
        with mock.patch.dict(sys.modules, {"AppKit": host(NSWorkspace=workspace),
                                          "Foundation": host(NSURL=nsurl)}):
            result = open_camera_settings(host(CAMERA_PRIVACY_URL=window.FloatWindow.CAMERA_PRIVACY_URL))
        nsurl.URLWithString_.assert_called_once_with(window.FloatWindow.CAMERA_PRIVACY_URL)
        return result, open_url

    def test_camera_settings_url_is_opened_and_success_is_reported(self):
        result, open_url = self.camera_route()
        self.assertTrue(result)
        open_url.assert_called_once_with("camera-url")
        self.assertIn("ok=True", self.log.call_args.args[0])

    def test_invalid_camera_url_is_logged_without_opening(self):
        result, open_url = self.camera_route(url=None)
        self.assertFalse(result)
        open_url.assert_not_called()
        self.assertEqual(self.log.call_args.args[1], "WARNING")

    def test_rejected_camera_settings_open_reports_false(self):
        result, _open_url = self.camera_route(opened=False)
        self.assertFalse(result)
        self.assertIn("ok=False", self.log.call_args.args[0])

    def test_camera_workspace_failure_is_logged_and_does_not_escape(self):
        result, _open_url = self.camera_route(error=RuntimeError("workspace unavailable"))
        self.assertFalse(result)
        self.assertIn("workspace unavailable", self.log.call_args.args[0])


class FaceDiagnosticText(SFCase):
    def data(self, **changes):
        result = dict(available=True, enabled_by_user=True, stalled=False,
                      paused=False, screen_off=False, state=presence.STATE_RUNNING,
                      stale_sec=30, max_idle_sec=120, detect_errors=0,
                      zero_frame_sec=0, hidden_by_guard=False, tick_sec=0.2,
                      hide_hits=3, dwell_sec=0.4, conf_min=0.6, edge_conf_min=0.55,
                      near_face_w=0.1, edge_shrink=0.5, restore_hits=2, reason="")
        result.update(changes)
        return result

    def text(self, **changes):
        return window.FloatWindow.face_guard_status_text(host(), self.data(**changes))

    def test_failed_and_unavailable_states_never_claim_running(self):
        table = ((presence.STATE_STARTING, "等待摄像头授权"),
                 (presence.STATE_DENIED, "权限被拒绝"),
                 (presence.STATE_RESTRICTED, "系统策略限制"),
                 (presence.STATE_NO_CAMERA, "没有可用摄像头"),
                 (presence.STATE_FAILED, "unknown failure"))
        for state, expected in table:
            with self.subTest(state=state):
                text = self.text(state=state, reason="unknown failure")
                self.assertIn(expected, text)
                self.assertNotIn("运行中", text)

    def test_privacy_lock_and_missing_permission_are_distinct(self):
        self.assertIn("配置关闭", self.text(available=False))
        self.assertIn("未开启", self.text(enabled_by_user=False))
        self.assertIn("保护已中断", self.text(stalled=True))
        self.assertIn("已暂停", self.text(paused=True))
        self.assertIn("显示器休眠", self.text(screen_off=True))
        self.assertIn("未授权", self.text(state=presence.STATE_NEEDS_AUTH))
        self.assertIn("未知原因", self.text(state=presence.STATE_FAILED))

    def test_zero_frame_and_detection_failure_are_not_mislabelled_running(self):
        self.assertIn("系统认不出脸", self.text(detect_errors=4))
        self.assertIn("尚未收到画面", self.text(zero_frame_sec=1))
        self.assertIn("已收起", self.text(zero_frame_sec=1, hidden_by_guard=True))

    def test_running_diagnostic_explains_dwell_radius_and_restore_threshold(self):
        text = self.text(hidden_by_guard=True)
        self.assertIn("运行中", text)
        self.assertIn("0.4 秒", text)
        self.assertIn("10.0%", text)
        self.assertIn("连续 2 次", text)


if __name__ == "__main__":
    unittest.main()
