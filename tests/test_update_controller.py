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

    def test_stop_prevents_new_workers(self):
        self.ctrl.stop()
        self.ctrl.check(False)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.ctrl.diagnostics()["workers"], 0)

    @staticmethod
    def _wait_for(predicate):
        for _ in range(100):
            if predicate():
                return True
            threading.Event().wait(0.01)
        return False


if __name__ == "__main__":
    main()
