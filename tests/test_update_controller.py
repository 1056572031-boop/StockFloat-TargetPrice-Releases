import threading
from unittest import main
from unittest import mock

import sf_update_controller as controller

try:
    from _base import SFCase
except ImportError:
    from tests._base import SFCase


class TestUpdateController(SFCase):
    def setUp(self):
        super().setUp()
        self.messages = []
        self.ctrl = controller.UpdateController(self.messages.append, lambda: "1.4.85")
        self.addCleanup(self.ctrl.stop)

    def test_check_routes_success_to_existing_message_shape(self):
        fake_update = object()
        with mock.patch("sf_update.check_for_update", return_value=fake_update):
            self.ctrl.check(True)
            self.assertTrue(self._wait_for(lambda: self.messages))
        self.assertEqual(self.messages[0], ("update_check", (True, fake_update, None)))

    def test_check_routes_failure_to_existing_message_shape(self):
        failure = RuntimeError("offline")
        with mock.patch("sf_update.check_for_update", side_effect=failure):
            self.ctrl.check(False)
            self.assertTrue(self._wait_for(lambda: self.messages))
        kind, payload = self.messages[0]
        self.assertEqual(kind, "update_check")
        self.assertFalse(payload[0])
        self.assertIsNone(payload[1])
        self.assertIs(payload[2], failure)

    def test_download_reports_integer_progress_and_terminal_message(self):
        update = object()
        cancel = threading.Event()

        def fake_download(info, cancel_event, progress_callback):
            self.assertIs(info, update)
            self.assertIs(cancel_event, cancel)
            progress_callback(10, 100)
            progress_callback(11, 100)
            progress_callback(99, 100)
            return "/tmp/update.zip"

        with mock.patch("sf_update.download_update", side_effect=fake_download):
            self.ctrl.download(update, cancel)
            self.assertTrue(self._wait_for(lambda: self.messages))

        self.assertEqual(
            self.messages,
            [
                ("update_progress", (10, 10, 100)),
                ("update_progress", (11, 11, 100)),
                ("update_progress", (99, 99, 100)),
                ("update_download", (update, "/tmp/update.zip", None)),
            ],
        )

    def test_check_start_failure_reports_owned_error_and_forgets_worker(self):
        failure = RuntimeError("cannot start")
        token = object()
        with mock.patch.object(threading.Thread, "start", side_effect=failure):
            self.ctrl.check(True, token)
        self.assertEqual(self.messages, [("update_check", (True, None, failure, token))])
        self.assertEqual(self.ctrl.diagnostics()["workers"], 0)
        self.assertEqual(self.ctrl.diagnostics()["checks_failed"], 1)

    def test_download_start_failure_reports_owned_error_and_allows_retry(self):
        failure = RuntimeError("cannot start")
        update, token = object(), object()
        with mock.patch.object(threading.Thread, "start", side_effect=failure):
            self.ctrl.download(update, threading.Event(), token)
        self.assertEqual(self.messages, [("update_download", (update, None, failure, token))])
        self.assertEqual(self.ctrl.diagnostics()["workers"], 0)
        self.assertEqual(self.ctrl.diagnostics()["downloads_failed"], 1)
        self.assertFalse(self.ctrl.cancel_download(update, token=token))
        with mock.patch("sf_update.download_update", return_value="ready"):
            self.ctrl.download(update, threading.Event(), token)
            self.assertTrue(self._wait_for(lambda: len(self.messages) == 2))
        self.assertEqual(self.messages[-1], ("update_download", (update, "ready", None, token)))

    def test_queued_download_start_failure_unblocks_later_download(self):
        first, second, third = object(), object(), object()
        token2, token3 = object(), object()
        started, release = threading.Event(), threading.Event()
        failure = RuntimeError("cannot start queued")
        self.addCleanup(release.set)
        real_start = threading.Thread.start
        def start(thread):
            if threading.current_thread().name == "update-download":
                raise failure
            return real_start(thread)
        def download(update, cancel_event, progress_callback):
            if update is first:
                started.set()
                release.wait(2)
            return "ready"
        with mock.patch.object(threading.Thread, "start", start), \
             mock.patch("sf_update.download_update", side_effect=download):
            self.ctrl.download(first, threading.Event(), object())
            self.assertTrue(started.wait(1))
            self.ctrl.download(second, threading.Event(), token2)
            release.set()
            self.assertTrue(self._wait_for(lambda: any(m[1][0] is second for m in self.messages)))
            self.assertTrue(self._wait_for(lambda: self.ctrl.diagnostics()["workers"] == 0))
            self.ctrl.download(third, threading.Event(), token3)
            self.assertTrue(self._wait_for(lambda: any(m[1][0] is third for m in self.messages)))
        self.assertIn(("update_download", (second, None, failure, token2)), self.messages)
        self.assertIn(("update_download", (third, "ready", None, token3)), self.messages)

    def test_stop_between_download_registration_and_worker_start(self):
        update, token = object(), object()
        cancel = threading.Event()
        real_start = self.ctrl._start  # discipline-ok(R5): force the registration/start interleaving
        start_results = []

        def stop_before_start(target, name):
            # Inspect registration here because diagnostics does not expose active identity.
            self.assertIs(self.ctrl._active_cancel, cancel)  # discipline-ok(R5): active identity is not exposed
            self.assertIs(self.ctrl._active_update, update)  # discipline-ok(R5): active identity is not exposed
            self.assertIs(self.ctrl._active_token, token)  # discipline-ok(R5): active identity is not exposed
            self.assertFalse(cancel.is_set())
            self.ctrl.stop()
            start_results.append(real_start(target, name))
            return start_results[-1]

        with mock.patch.object(self.ctrl, "_start", side_effect=stop_before_start), \
             mock.patch.object(threading, "Thread") as thread, \
             mock.patch("sf_update.download_update") as download:
            self.ctrl.download(update, cancel, token)

        self.assertEqual(start_results, [False])
        self.assertTrue(cancel.is_set())
        self.assertFalse(self.ctrl.cancel_download(update, token=token))
        # Identity references are not exposed by diagnostics and must also be released.
        self.assertIsNone(self.ctrl._active_cancel)  # discipline-ok(R5): verify retained identity is released
        self.assertIsNone(self.ctrl._active_update)  # discipline-ok(R5): verify retained identity is released
        self.assertIsNone(self.ctrl._active_token)  # discipline-ok(R5): verify retained identity is released
        thread.assert_not_called()
        download.assert_not_called()
        self.assertEqual(self.messages, [])
        diagnostics = self.ctrl.diagnostics()
        self.assertTrue(diagnostics["stopped"])
        self.assertEqual(diagnostics["workers"], 0)
        self.assertFalse(diagnostics["pending_download"])
        self.assertEqual(diagnostics["downloads_started"], 1)
        self.assertEqual(diagnostics["downloads_completed"], 0)
        self.assertEqual(diagnostics["downloads_failed"], 0)

    def test_stop_prevents_new_workers(self):
        self.ctrl.stop()
        self.ctrl.check(False)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.ctrl.diagnostics()["workers"], 0)

    def test_cancel_download_targets_active_identity(self):
        update = object()
        started = threading.Event()
        release = threading.Event()
        cancel = threading.Event()

        def fake_download(info, cancel_event, progress_callback):
            self.assertIs(info, update)
            started.set()
            release.wait(1)
            return "/tmp/update.zip"

        with mock.patch("sf_update.download_update", side_effect=fake_download):
            self.ctrl.download(update, cancel)
            self.assertTrue(self._wait_for(started.is_set))
            self.assertFalse(self.ctrl.cancel_download(object()))
            self.assertTrue(self.ctrl.cancel_download(update))
            self.assertTrue(cancel.is_set())
            release.set()
            self.assertTrue(self._wait_for(lambda: self.messages))

    def test_cancel_queued_download_does_not_start_it_after_previous_worker_finishes(self):
        old, queued = object(), object()
        old_cancel, queued_cancel = threading.Event(), threading.Event()
        started, release = threading.Event(), threading.Event()
        calls = []

        def fake_download(info, cancel_event, progress_callback):
            calls.append(info)
            if info is old:
                started.set()
                release.wait(2)
            return "/tmp/update.zip"

        with mock.patch("sf_update.download_update", side_effect=fake_download):
            self.ctrl.download(old, old_cancel)
            self.assertTrue(started.wait(1))
            self.ctrl.download(queued, queued_cancel)
            self.assertTrue(self.ctrl.cancel_download(queued))
            self.assertTrue(queued_cancel.is_set())
            release.set()
            self.assertTrue(self._wait_for(lambda: bool(self.messages)))
            self.assertTrue(self._wait_for(lambda: not self.ctrl.diagnostics()["workers"]))
        self.assertEqual(calls, [old])

    def test_stop_cancels_active_download_without_window_owned_token(self):
        update, cancel = object(), threading.Event()
        started = threading.Event()
        finished = threading.Event()
        def fake_download(info, cancel_event, progress_callback):
            started.set()
            if cancel_event.wait(2):
                finished.set()
            return "/tmp/update.zip"
        with mock.patch("sf_update.download_update", side_effect=fake_download):
            self.ctrl.download(update, cancel)
            self.assertTrue(started.wait(1))
            self.ctrl.stop()
            self.assertTrue(cancel.is_set())
            self.assertTrue(finished.wait(1))

    def test_download_after_stop_does_not_create_a_pending_or_active_task(self):
        self.ctrl.stop()
        cancel = threading.Event()
        self.ctrl.download(object(), cancel)
        self.assertTrue(cancel.is_set())
        self.assertFalse(self.ctrl.diagnostics()["pending_download"])
        self.assertEqual(self.ctrl.diagnostics()["workers"], 0)

    def test_token_cancel_does_not_cancel_another_round_of_same_update(self):
        update = object()
        current, stale = object(), object()
        cancel, started, release = threading.Event(), threading.Event(), threading.Event()
        def fake_download(info, cancel_event, progress_callback):
            started.set()
            release.wait(2)
            return "/tmp/update.zip"
        with mock.patch("sf_update.download_update", side_effect=fake_download):
            self.ctrl.download(update, cancel, current)
            self.assertTrue(started.wait(1))
            try:
                self.assertFalse(self.ctrl.cancel_download(update, token=stale))
                self.assertFalse(cancel.is_set())
                self.assertTrue(self.ctrl.cancel_download(update, token=current))
                self.assertTrue(cancel.is_set())
            finally:
                release.set()

    def test_precancelled_worker_does_not_enter_download_transport(self):
        import sf_update
        cancel = threading.Event()
        cancel.set()
        with mock.patch("sf_update.download_update") as download:
            self.ctrl.download(object(), cancel)
            self.assertTrue(self._wait_for(lambda: bool(self.messages)))
        download.assert_not_called()
        self.assertIsInstance(self.messages[-1][1][2], sf_update.UpdateCancelled)

    def test_replaced_pending_task_emits_its_own_cancelled_terminal(self):
        import threading
        from unittest.mock import patch
        from tests._base import wait_for
        from sf_update_controller import UpdateController
        from sf_update import UpdateCancelled
        started, release = threading.Event(), threading.Event()
        messages = []
        controller = UpdateController(messages.append, lambda: "1.0.0")
        self.addCleanup(controller.stop)
        self.addCleanup(release.set)
        first, second, third = object(), object(), object()
        token1, token2, token3 = object(), object(), object()
        def download(update, cancel_event, progress_callback):
            if update is first:
                started.set()
                self.assertTrue(release.wait(3))
                raise UpdateCancelled("cancelled")
            return "ready"
        with patch("sf_update.download_update", side_effect=download):
            controller.download(first, threading.Event(), token1)
            self.assertTrue(started.wait(2))
            controller.download(second, threading.Event(), token2)
            controller.download(third, threading.Event(), token3)
            self.assertTrue(wait_for(lambda: any(item[0] == "update_download" and item[1][-1] is token2
                                                 for item in messages), timeout=2))
            cancelled = next(item[1] for item in messages if item[0] == "update_download" and item[1][-1] is token2)
            self.assertIsInstance(cancelled[2], UpdateCancelled)
            release.set()

    def test_replaced_pending_task_reports_cancel_even_when_update_import_fails(self):
        import threading
        from unittest.mock import patch
        from sf_update_controller import UpdateController
        started, release = threading.Event(), threading.Event()
        messages = []
        controller = UpdateController(messages.append, lambda: "1.0.0")
        self.addCleanup(controller.stop)
        self.addCleanup(release.set)
        first, second, third = object(), object(), object()
        with patch("sf_update.download_update", side_effect=lambda *a, **k: (started.set(), release.wait(3), "ready")[-1]):
            controller.download(first, threading.Event(), object())
            self.assertTrue(started.wait(2))
            controller.download(second, threading.Event(), object())
            with patch.dict("sys.modules", {"sf_update": None}):
                controller.download(third, threading.Event(), object())
        self.assertTrue(any(item[0] == "update_download" and item[1][0] is second for item in messages))
        release.set()

    def test_displaced_terminal_enqueue_failure_is_visible(self):
        import threading
        from unittest.mock import patch
        from sf_update_controller import UpdateController
        started, release = threading.Event(), threading.Event()
        messages = []
        def enqueue(item):
            if item[0] == "update_download" and item[1][0] is displaced:
                raise RuntimeError("queue unavailable")
            messages.append(item)
        controller = UpdateController(enqueue, lambda: "1.0.0")
        self.addCleanup(controller.stop)
        self.addCleanup(release.set)
        displaced = object()
        with patch("sf_update.download_update", side_effect=lambda *a, **k: (started.set(), release.wait(3), "ready")[-1]):
            controller.download(object(), threading.Event(), object())
            self.assertTrue(started.wait(2))
            controller.download(displaced, threading.Event(), object())
            with self.assertRaisesRegex(RuntimeError, "queue unavailable"):
                controller.download(object(), threading.Event(), object())
        release.set()

    def test_cancel_without_identity_cancels_active_and_removes_pending(self):
        import threading
        from unittest.mock import patch
        from sf_update_controller import UpdateController
        started, release = threading.Event(), threading.Event()
        controller = UpdateController(lambda _: None, lambda: "1.0.0")
        self.addCleanup(controller.stop)
        self.addCleanup(release.set)
        first_cancel, pending_cancel = threading.Event(), threading.Event()
        with patch("sf_update.download_update", side_effect=lambda *a, **k: (started.set(), release.wait(3), "ready")[-1]):
            controller.download(object(), first_cancel, object())
            self.assertTrue(started.wait(2))
            controller.download(object(), pending_cancel, object())
            self.assertTrue(controller.cancel_download())
            self.assertTrue(first_cancel.is_set())
            self.assertTrue(pending_cancel.is_set())
            self.assertFalse(controller.diagnostics()["pending_download"])
        release.set()

    def test_cancel_without_active_download_returns_false(self):
        self.assertFalse(self.ctrl.cancel_download())

    def test_pending_download_is_dropped_when_controller_stops_before_old_finishes(self):
        old, new = object(), object()
        old_cancel, new_cancel = threading.Event(), threading.Event()
        old_started = threading.Event()
        release_old = threading.Event()

        def fake_download(info, cancel_event, progress_callback):
            if info is old:
                old_started.set()
                release_old.wait(1)
            return "/tmp/update.zip"

        with mock.patch("sf_update.download_update", side_effect=fake_download):
            self.ctrl.download(old, old_cancel)
            self.assertTrue(self._wait_for(old_started.is_set))
            self.ctrl.download(new, new_cancel)
            self.ctrl.stop()
            old_cancel.set()
            release_old.set()
            self.assertTrue(self._wait_for(lambda: not self.ctrl.diagnostics()["pending_download"]))
        self.assertTrue(new_cancel.is_set())

    def test_new_download_waits_for_cancelled_worker_before_starting(self):
        old = object()
        new = object()
        old_cancel = threading.Event()
        new_cancel = threading.Event()
        old_started = threading.Event()
        release_old = threading.Event()
        calls = []

        def fake_download(info, cancel_event, progress_callback):
            calls.append((info, cancel_event))
            if info is old:
                old_started.set()
                release_old.wait(1)
            return "/tmp/%s.zip" % ("old" if info is old else "new")

        with mock.patch("sf_update.download_update", side_effect=fake_download):
            self.ctrl.download(old, old_cancel)
            self.assertTrue(self._wait_for(old_started.is_set))
            self.ctrl.download(new, new_cancel)
            self.assertTrue(old_cancel.is_set())
            self.assertEqual([item[0] for item in calls], [old])
            release_old.set()
            self.assertTrue(self._wait_for(lambda: [item[0] for item in calls] == [old, new]))

    def test_three_identity_switches_keep_one_download_worker(self):
        old, middle, newest = object(), object(), object()
        old_cancel, middle_cancel, newest_cancel = (threading.Event(), threading.Event(), threading.Event())
        started = {old: threading.Event(), middle: threading.Event(), newest: threading.Event()}
        releases = {old: threading.Event(), middle: threading.Event(), newest: threading.Event()}
        calls = []

        def fake_download(info, cancel_event, progress_callback):
            calls.append(info)
            started[info].set()
            releases[info].wait(1)
            return "/tmp/update.zip"

        with mock.patch("sf_update.download_update", side_effect=fake_download):
            self.ctrl.download(old, old_cancel)
            self.assertTrue(self._wait_for(started[old].is_set))
            self.ctrl.download(middle, middle_cancel)
            self.ctrl.download(newest, newest_cancel)
            self.assertTrue(middle_cancel.is_set())
            self.assertTrue(old_cancel.is_set())
            self.assertEqual(calls, [old])
            releases[old].set()
            self.assertTrue(self._wait_for(lambda: calls == [old, newest]))
            self.assertFalse(started[middle].is_set())
            releases[newest].set()


    @staticmethod
    def _wait_for(predicate):
        for _ in range(100):
            if predicate():
                return True
            threading.Event().wait(0.01)
        return False


if __name__ == "__main__":
    main()
