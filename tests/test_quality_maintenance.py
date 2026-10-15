"""System-maintenance scheduling is optional and private Space calls can be disabled."""
from types import SimpleNamespace
import pathlib
import subprocess
import sys
from unittest import mock

import sf_glass
import sf_maintenance
from sf_maintenance import BackgroundMaintenance
from tests._base import SFCase


class MaintenanceQualityTests(SFCase):
    def test_scheduler_configuration_completion_and_invalidation(self):
        scheduler = SimpleNamespace(setInterval_=mock.Mock(), setTolerance_=mock.Mock(),
                                    setRepeats_=mock.Mock(), scheduleWithBlock_=mock.Mock(),
                                    invalidate=mock.Mock())
        calls = []
        maintenance = BackgroundMaintenance(21600, factory=lambda: scheduler)
        self.assertTrue(maintenance.start(lambda: calls.append(True)))
        scheduler.setInterval_.assert_called_once_with(21600)
        completion = mock.Mock()
        scheduler.scheduleWithBlock_.call_args.args[0](completion)
        self.assertEqual(calls, [True])
        completion.assert_called_once_with(1)
        self.assertTrue(maintenance.stop())
        scheduler.invalidate.assert_called_once()
        self.assertFalse(maintenance.diagnostics()["active"])

    def test_scheduler_failure_allows_the_callers_timer_fallback(self):
        def unavailable():
            raise OSError("not available")
        maintenance = BackgroundMaintenance(21600, factory=unavailable)
        self.assertFalse(maintenance.start(lambda: None))
        self.assertEqual(maintenance.failure_count, 1)

    def _scheduled(self, callback):
        scheduler = SimpleNamespace(setInterval_=mock.Mock(), setTolerance_=mock.Mock(),
                                    setRepeats_=mock.Mock(), scheduleWithBlock_=mock.Mock(),
                                    invalidate=mock.Mock())
        maintenance = BackgroundMaintenance(21600, factory=lambda: scheduler)
        self.assertTrue(maintenance.start(callback))
        self.addCleanup(maintenance.stop)
        return maintenance, scheduler.scheduleWithBlock_.call_args.args[0]

    def test_callback_failure_is_logged_and_native_completion_still_runs(self):
        callback = mock.Mock(side_effect=RuntimeError("maintenance failed"))
        maintenance, scheduled = self._scheduled(callback)
        completion = mock.Mock()
        with mock.patch.object(sf_maintenance, "log_swallow") as log:
            scheduled(completion)
        completion.assert_called_once_with(1)
        self.assertEqual(maintenance.diagnostics()["failure_count"], 1)
        self.assertEqual(log.call_args.args[0], "sf_maintenance.callback")

    def test_completion_failure_cannot_escape_into_the_native_thread(self):
        maintenance, scheduled = self._scheduled(lambda: None)
        completion = mock.Mock(side_effect=RuntimeError("completion failed"))
        with mock.patch.object(sf_maintenance, "log_swallow") as log:
            scheduled(completion)
        self.assertEqual(maintenance.diagnostics()["failure_count"], 1)
        self.assertEqual(log.call_args.args[0], "sf_maintenance.completion")

    def test_malformed_completion_does_not_run_work_or_raise(self):
        callback = mock.Mock()
        maintenance, scheduled = self._scheduled(callback)
        with mock.patch.object(sf_maintenance, "log_swallow") as log:
            scheduled(123)
        callback.assert_not_called()
        self.assertEqual(maintenance.diagnostics()["failure_count"], 1)
        self.assertEqual(log.call_args.args[0], "sf_maintenance.completion")

    def test_late_callback_after_stop_only_completes_the_native_activity(self):
        callback = mock.Mock()
        maintenance, scheduled = self._scheduled(callback)
        self.assertTrue(maintenance.stop())
        completion = mock.Mock()
        scheduled(completion)
        callback.assert_not_called()
        completion.assert_called_once_with(1)

    def test_signature_registration_failure_uses_timer_fallback(self):
        with mock.patch.object(sf_maintenance, "_pin_completion_block_signature",
                               side_effect=RuntimeError("metadata unavailable")), \
                mock.patch.object(sf_maintenance, "log_swallow") as log:
            maintenance = BackgroundMaintenance(21600)
            self.assertFalse(maintenance.start(lambda: None))
        self.assertFalse(maintenance.diagnostics()["active"])
        self.assertEqual(log.call_args.args[0], "sf_maintenance.start")

    def test_real_nested_native_block_completes_without_aborting(self):
        child = pathlib.Path(__file__).with_name("_maintenance_child.py")
        result = subprocess.run([sys.executable, str(child)], capture_output=True,
                                text=True, timeout=30, cwd=str(child.parent.parent))
        if result.returncode == 77:
            self.skipTest(result.stdout.strip())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("NATIVE_COMPLETION=1", result.stdout)

    def test_native_probe_rejects_the_original_integer_completion_metadata(self):
        child = pathlib.Path(__file__).with_name("_maintenance_child.py")
        result = subprocess.run([sys.executable, str(child), "--unpinned"],
                                capture_output=True, text=True, timeout=30,
                                cwd=str(child.parent.parent))
        if result.returncode == 77:
            self.skipTest(result.stdout.strip())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("NATIVE_COMPLETION=0", result.stdout)

    def test_private_space_kill_switch_prevents_loading_private_library(self):
        with mock.patch.object(sf_glass, "feature_enabled", return_value=False), \
                mock.patch.object(sf_glass, "_cgs_libs") as private:
            self.assertIsNone(sf_glass.active_space_id())
            with self.assertRaises(RuntimeError):
                sf_glass.cgs_add_window_to_space(123)
        private.assert_not_called()
