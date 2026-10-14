"""Coalesced background writes for the disposable first-screen cache."""
import threading
import time


class SnapshotWriter:
    def __init__(self, write):
        self._write = write
        self._condition = threading.Condition()
        self._pending: tuple | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self.write_count = 0
        self.failure_count = 0
        self.last_write_ms = 0.0

    def submit(self, code, quote, points, day):
        payload = (code, dict(quote or {}), list(points), day)
        with self._condition:
            if self._closed:
                return
            self._pending = payload
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="quote-snapshot", daemon=True)
                self._thread.start()
            self._condition.notify()

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._pending is None:
                    return
                payload, self._pending = self._pending, None
            started = time.monotonic()
            try:
                if self._write(*payload) is False:
                    self.failure_count += 1
            except Exception:
                self.failure_count += 1
            finally:
                self.write_count += 1
                self.last_write_ms = (time.monotonic() - started) * 1000

    def close(self, timeout=0.25):
        with self._condition:
            self._closed = True
            self._condition.notify()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return thread is None or not thread.is_alive()
