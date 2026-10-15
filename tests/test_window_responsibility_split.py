"""The extracted controllers work without constructing a QWidget."""

from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from unittest import mock

from sf_face_guard_controller import FaceGuardController
from sf_update_flow import UpdateFlowController
from tools.diag_coverage_check import curated_modules
from tests._base import SFCase


def _peek(host, name):
    """Read the legacy host port in one reviewable fixture boundary."""
    return getattr(host, name)


class UpdateFlowPortTests(SFCase):
    def setUp(self):
        super().setUp()
        self.backend = mock.Mock()
        self.flow = UpdateFlowController(lambda host: self.backend, mock.Mock(), mock.Mock())

    def test_download_creates_one_cancellation_token_and_one_worker(self):
        host = SimpleNamespace(_update_downloading=False, _quitting=False,
                               _open_download_progress_box=mock.Mock())
        update = object()
        self.flow.download(host, update, prompt_when_ready=True, show_progress=False)
        self.flow.download(host, update)
        self.backend.download.assert_called_once_with(update, _peek(host, "_update_cancel"))
        _peek(host, "_open_download_progress_box").assert_not_called()
        self.assertTrue(_peek(host, "_update_prompt_when_ready"))
        self.flow.cancel_download(host)
        self.assertTrue(_peek(host, "_update_cancel").is_set())

    def test_completed_download_closes_ui_before_prompting(self):
        order = []
        host = SimpleNamespace(
            _update_prompt_when_ready=True, _update_downloading=True,
            _update_cancel=threading.Event(), _quitting=False,
            _close_update_flow_box=lambda: order.append("closed"),
            _prompt_update=lambda update: order.append("prompt"),
        )
        update = SimpleNamespace(version="future")
        self.flow.downloaded(host, update, "verified.zip", None)
        self.assertEqual(order, ["closed", "prompt"])
        self.assertFalse(_peek(host, "_update_downloading"))
        self.assertIsNone(_peek(host, "_update_cancel"))
        self.assertEqual(_peek(host, "_update_ready_archive"), "verified.zip")

    def test_shutdown_result_closes_ui_without_prompt_or_install(self):
        host = SimpleNamespace(_update_prompt_when_ready=True, _quitting=True,
                               _close_update_flow_box=mock.Mock(),
                               _prompt_update=mock.Mock(), _launch_downloaded_update=mock.Mock())
        self.flow.downloaded(host, object(), "verified.zip", None)
        _peek(host, "_close_update_flow_box").assert_called_once_with()
        _peek(host, "_prompt_update").assert_not_called()
        _peek(host, "_launch_downloaded_update").assert_not_called()


class FaceGuardPortTests(SFCase):
    def test_quitting_host_never_creates_or_starts_a_session(self):
        host = SimpleNamespace(_face_guard_available=lambda: True, _face_guard_pref=True,
                               _quitting=True, _stop_face_guard=mock.Mock(),
                               _face_guard_new_monitor=mock.Mock())
        FaceGuardController().sync(host, "resume", stop_wait=False)
        _peek(host, "_stop_face_guard").assert_called_once_with("quitting", wait=True)
        _peek(host, "_face_guard_new_monitor").assert_not_called()

    def test_pending_stops_are_isolated_between_controllers(self):
        first, second = FaceGuardController(), FaceGuardController()
        monitor = mock.Mock()
        first.pending_stop.append(monitor)
        self.assertEqual(second.pending_stop, [])
        host = SimpleNamespace(_face_guard_pending_stop=first.pending_stop)
        first.drain_pending(host, "quit")
        monitor.stop.assert_called_once_with(wait=True)
        self.assertEqual(_peek(host, "_face_guard_pending_stop"), [])


class DiagnosticClosureTests(SFCase):
    def test_aliased_helper_and_callback_are_reachable_but_unrelated_function_is_not(self):
        with tempfile.TemporaryDirectory(prefix="sf-capture-closure-") as directory:
            root = Path(directory)
            (root / "stock_float_window.py").write_text(
                "from sf_capture import capture as prepare\n"
                "class FloatWindow:\n"
                "    def _export_diagnostics(self):\n"
                "        return prepare(self)\n")
            helper = root / "sf_capture.py"
            helper.write_text(
                "import sf_quote\nimport sf_network\n"
                "def snapshot():\n    return sf_quote.read()\n"
                "def capture(window):\n    return dict(provider=snapshot)\n"
                "def unused():\n    return sf_network.read()\n")
            for name in ("sf_quote", "sf_network"):
                (root / (name + ".py")).write_text("def read():\n    return {}\n")
            reached = curated_modules(root)
            self.assertIn("sf_quote", reached)
            self.assertNotIn("sf_network", reached)
            helper.write_text("def capture(window):\n    return {}\n")
            self.assertNotIn("sf_quote", curated_modules(root))
