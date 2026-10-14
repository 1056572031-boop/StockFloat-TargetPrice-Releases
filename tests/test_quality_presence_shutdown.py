"""A driver that stalls inside stopRunning cannot stall the GUI indefinitely."""
import threading
import time

import sf_presence
from tests._base import SFCase


class PresenceShutdownQualityTests(SFCase):
    def test_stop_wait_has_a_total_bound_and_keeps_live_resources_owned(self):
        entered, release = threading.Event(), threading.Event()

        class Session:
            def stopRunning(self):
                entered.set()
                release.wait(3)

        monitor = sf_presence.FacePresenceMonitor()
        fields = {"_session": Session(), "state": sf_presence.STATE_RUNNING}
        for name, value in fields.items():
            setattr(monitor, name, value)
        started = time.monotonic()
        try:
            self.assertFalse(monitor.stop(wait=True))
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertTrue(entered.is_set())
            self.assertEqual(monitor.stop_timeouts, 1)
            self.assertEqual(monitor.state, sf_presence.STATE_RUNNING)
        finally:
            release.set()
            self.assertTrue(monitor.stop(wait=True))
        self.assertEqual(monitor.state, sf_presence.STATE_OFF)
