"""Manual refresh scheduling with real worker threads and no user-data access."""
import threading
from types import SimpleNamespace
from unittest import main
from unittest.mock import patch

import stock_float_window as m

try:
    from _base import SFCase
except ImportError:
    from tests._base import SFCase


def _wire(host, **values):
    for name, value in values.items():
        setattr(host, name, value)


def _peek(host, name):
    return getattr(host, name)


def _host():
    # Bind the real private lifecycle methods: no public non-Qt scheduling API exists.
    host = SimpleNamespace(running=True)
    for name in ("_sleep", "_resched_loops", "_force_fetch", "_take_force",
                 "_quote_loop", "_minute_loop", "_shutdown_threads"):
        setattr(host, name, getattr(m.FloatWindow, name).__get__(host))
    for name in ("_wake", "_resched", "_quote_resched", "_minute_resched"):
        setattr(host, name, threading.Event())
    for name, value in (("_force_lock", threading.Lock()),
                        ("_force_quote", False), ("_force_minute", False),
                        ("_threads_stopped", False)):
        setattr(host, name, value)
    return host


class ManualRefreshWake(SFCase):
    def test_shutdown_wakes_and_joins_both_long_sleeping_workers(self):
        host = _host()
        entered = [threading.Event(), threading.Event()]

        class ObservedEvent(threading.Event):
            def __init__(self, seen):
                super().__init__()
                self.seen = seen

            def wait(self, timeout=None):
                self.seen.set()
                return super().wait(timeout)

        workers = [threading.Thread(target=lambda: _peek(host, "_sleep")(30.0))
                   for _ in range(2)]
        for name, event in zip(("_quote_resched", "_minute_resched"), entered):
            setattr(host, name, ObservedEvent(event))
        for name, worker in zip(("_quote_thread", "_minute_thread"), workers):
            setattr(host, name, worker)
            worker.start()
        try:
            self.assertTrue(all(event.wait(1.0) for event in entered))
            with patch.object(m._NET_POOL, "close_all"):  # discipline-ok(R5): do not cancel unrelated requests in the shared test process
                _peek(host, "_shutdown_threads")()
            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertFalse(host.running)
        finally:
            host.running = False
            _peek(host, "_resched_loops")()
            for worker in workers:
                worker.join(2.0)

    def test_in_flight_quote_does_not_delay_minute_or_lose_next_quote(self):
        host = _host()
        quote_started, quote_release = threading.Event(), threading.Event()
        quote_refreshed, minute_started, minute_refreshed = (
            threading.Event(), threading.Event(), threading.Event())
        counts = {"quote": 0, "minute": 0}

        def quote_fetch():
            counts["quote"] += 1
            if counts["quote"] == 1:
                quote_started.set()
                quote_release.wait(3.0)
            else:
                quote_refreshed.set()

        def minute_fetch():
            counts["minute"] += 1
            (minute_started if counts["minute"] == 1 else minute_refreshed).set()
            return True

        for name, value in (("_poll_interval", lambda: 3600.0),
                            ("_is_trading_time", lambda: False),
                            ("_minimized", False), ("_minute_interval", 30.0),
                            ("_nap", lambda *args: 5.0),
                            ("_fetch_quote", quote_fetch), ("_fetch_minute", minute_fetch)):
            setattr(host, name, value)
        workers = [threading.Thread(target=getattr(host, name))
                   for name in ("_quote_loop", "_minute_loop")]
        for name, worker in zip(("_quote_thread", "_minute_thread"), workers):
            setattr(host, name, worker)
            worker.start()
        try:
            self.assertTrue(quote_started.wait(1.0) and minute_started.wait(1.0))
            _peek(host, "_force_fetch")()
            self.assertTrue(minute_refreshed.wait(1.0), "Minute refresh blocked on quote")
            quote_release.set()
            self.assertTrue(quote_refreshed.wait(1.0), "In-flight quote lost pending refresh")
            self.assertEqual(counts, {"quote": 2, "minute": 2})
        finally:
            host.running = False
            quote_release.set()
            _peek(host, "_wake").set()
            _peek(host, "_resched_loops")()
            for worker in workers:
                worker.join(2.0)
            self.assertFalse(any(worker.is_alive() for worker in workers))

    def test_one_worker_cannot_consume_the_other_workers_wakeup(self):
        host = _host()
        first_done, second_done = threading.Event(), threading.Event()

        def sleep_then_signal(done):
            _peek(host, "_sleep")(5.0)
            done.set()

        quote = threading.Thread(target=sleep_then_signal, args=(first_done,))
        minute = threading.Thread(target=sleep_then_signal, args=(second_done,))
        _wire(host, _quote_thread=quote, _minute_thread=minute)
        try:
            # Deliberately let quote clear its event before minute starts waiting.
            _peek(host, "_resched_loops")()
            quote.start()
            self.assertTrue(first_done.wait(1.0))
            minute.start()
            self.assertTrue(second_done.wait(1.0),
                            "The second worker lost the refresh and slept until its cap")
        finally:
            host.running = False
            _peek(host, "_wake").set()
            _peek(host, "_resched_loops")()
            for worker in (quote, minute):
                if worker.ident is not None:
                    worker.join(2.0)
            self.assertFalse(quote.is_alive() or minute.is_alive())

    def test_repeated_requests_merge_without_starting_more_workers(self):
        host = _host()
        ready = threading.Barrier(3)
        release = threading.Event()

        def in_flight():
            ready.wait(2.0)
            release.wait(2.0)

        workers = [threading.Thread(target=in_flight) for _ in range(2)]
        for name, worker in zip(("_quote_thread", "_minute_thread"), workers):
            setattr(host, name, worker)
            worker.start()
        try:
            ready.wait(2.0)
            for _ in range(100):
                _peek(host, "_force_fetch")()
            for name in ("_force_quote", "_force_minute"):
                self.assertTrue(_peek(host, "_take_force")(name))
                self.assertFalse(_peek(host, "_take_force")(name))
            self.assertEqual(workers, [getattr(host, name) for name in
                                      ("_quote_thread", "_minute_thread")])
        finally:
            release.set()
            for worker in workers:
                worker.join(2.0)

    def test_refresh_after_shutdown_does_not_recreate_workers(self):
        host = _host()
        _wire(host, _threads_stopped=True)
        _peek(host, "_force_fetch")()
        self.assertFalse(hasattr(host, "_quote_thread"))
        self.assertFalse(_peek(host, "_resched").is_set())
        self.assertFalse(_peek(host, "_take_force")("_force_quote"))

    def test_dead_workers_are_recreated_and_registered_once(self):
        host = _host()
        ready = threading.Barrier(3)
        release = threading.Event()

        def recovered():
            ready.wait(2.0)
            release.wait(2.0)

        for name in ("_quote_loop_wrapper", "_minute_loop_wrapper"):
            setattr(host, name, recovered)
        try:
            _peek(host, "_force_fetch")()
            ready.wait(2.0)
            workers = [getattr(host, name) for name in ("_quote_thread", "_minute_thread")]
            with patch.object(m.threading, "Thread", side_effect=AssertionError("extra worker")):
                _peek(host, "_force_fetch")()
            self.assertTrue(all(worker.is_alive() for worker in workers))
        finally:
            release.set()
            for name in ("_quote_thread", "_minute_thread"):
                worker = getattr(host, name, None)
                if worker is not None:
                    worker.join(2.0)


if __name__ == "__main__":
    main()
