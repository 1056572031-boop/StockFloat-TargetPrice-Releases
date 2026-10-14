"""System-maintenance scheduling is optional and private Space calls can be disabled."""
from types import SimpleNamespace
from unittest import mock

import sf_glass
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

    def test_private_space_kill_switch_prevents_loading_private_library(self):
        with mock.patch.object(sf_glass, "feature_enabled", return_value=False), \
                mock.patch.object(sf_glass, "_cgs_libs") as private:
            self.assertIsNone(sf_glass.active_space_id())
            with self.assertRaises(RuntimeError):
                sf_glass.cgs_add_window_to_space(123)
        private.assert_not_called()
