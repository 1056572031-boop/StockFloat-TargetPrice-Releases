# -*- coding: utf-8 -*-
import unittest
from datetime import datetime, timedelta, timezone

from sf_target_lifecycle import TargetLifecycleCoordinator
from targets import STATE_HIT, TARGET_DIR_ABOVE, make_alert


_BJ = timezone(timedelta(hours=8))
_NOW = datetime(2026, 9, 9, 10, 0, tzinfo=_BJ)


class TestTargetLifecycleCoordinator(unittest.TestCase):
    def setUp(self):
        self.saved = []
        self.coordinator = TargetLifecycleCoordinator(self._save, lambda: _NOW)

    def _save(self, alerts):
        self.saved.append(alerts)
        return True

    def test_save_commits_only_after_persistence(self):
        old = make_alert(TARGET_DIR_ABOVE, 11, now=_NOW)
        desired = make_alert(TARGET_DIR_ABOVE, 12, now=_NOW)
        ok, alerts, changed = self.coordinator.save([desired], [old])
        self.assertTrue(ok)
        self.assertTrue(changed)
        self.assertEqual(alerts[0]["target"], 12.0)
        self.assertEqual(self.saved, [alerts])

    def test_save_failure_keeps_live_alerts(self):
        live = [make_alert(TARGET_DIR_ABOVE, 11, now=_NOW)]
        coordinator = TargetLifecycleCoordinator(lambda _alerts: False, lambda: _NOW)
        ok, alerts, changed = coordinator.save([make_alert(TARGET_DIR_ABOVE, 12, now=_NOW)], live)
        self.assertFalse(ok)
        self.assertFalse(changed)
        self.assertEqual(alerts, live)

    def test_save_preserves_hit_slot_without_rewriting(self):
        live = [make_alert(TARGET_DIR_ABOVE, 11, now=_NOW)]
        live[0]["state"] = STATE_HIT
        ok, alerts, changed = self.coordinator.save([make_alert(TARGET_DIR_ABOVE, 11, now=_NOW)], live)
        self.assertTrue(ok)
        self.assertFalse(changed)
        self.assertEqual(alerts[0]["state"], STATE_HIT)
        self.assertEqual(self.saved, [])

    def test_clear_empty_state_skips_persistence(self):
        ok, alerts, changed = self.coordinator.clear([])
        self.assertTrue(ok)
        self.assertEqual(alerts, [])
        self.assertFalse(changed)
        self.assertEqual(self.saved, [])


if __name__ == "__main__":
    unittest.main()
