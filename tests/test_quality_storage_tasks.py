"""Slow cache storage never blocks submit; shutdown still drains the latest copy."""
import threading
import time

from sf_storage_tasks import SnapshotWriter
from tests._base import SFCase


class SnapshotWriterQualityTests(SFCase):
    def test_slow_writer_does_not_block_gui_and_pending_states_are_coalesced(self):
        entered, release = threading.Event(), threading.Event()
        written = []

        def write(code, quote, points, day):
            entered.set()
            release.wait(3)
            written.append(quote["price"])
            return True

        writer = SnapshotWriter(write)
        try:
            writer.submit("A", {"price": "1"}, [], "day")
            self.assertTrue(entered.wait(2))
            started = time.monotonic()
            for number in range(2, 100):
                writer.submit("A", {"price": str(number)}, [], "day")
            self.assertLess(time.monotonic() - started, 0.2)
        finally:
            release.set()
            self.assertTrue(writer.close(timeout=3))
        self.assertEqual(written, ["1", "99"])

    def test_failures_are_observable_and_close_is_idempotent(self):
        writer = SnapshotWriter(lambda *args: False)
        writer.submit("A", {}, [], "day")
        self.assertTrue(writer.close(timeout=2))
        self.assertEqual(writer.failure_count, 1)
        self.assertTrue(writer.close())
        writer.submit("A", {}, [], "day")
        self.assertEqual(writer.write_count, 1)
