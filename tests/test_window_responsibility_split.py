"""The extracted controllers work without constructing a QWidget."""

import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sf_face_guard_controller import FaceGuardController
from sf_update_flow import UpdateFlowController
from tests._base import SFCase
from tests.test_power_v1410 import _WinCase
from tools.diag_coverage_check import curated_modules


def _peek(host, name):
    """Read the legacy host port in one reviewable fixture boundary."""
    return getattr(host, name)


class LegacyPromptDisposalTests(_WinCase):
    def setUp(self):
        super().setUp()
        root = self.enterContext(tempfile.TemporaryDirectory(prefix="sf-update-home-"))
        self.track_patch(mock.patch.dict(os.environ, {"HOME": root}))

    def test_modern_prompt_uses_real_window_screen_anchor(self):
        import sf_system_alert
        import sf_update
        import stock_float_window as window_module

        with tempfile.TemporaryDirectory(prefix="sf-update-window-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root), \
                mock.patch.object(sf_system_alert, "show_alert", return_value=mock.Mock()) as alert, \
                mock.patch.object(sf_system_alert, "set_screen_anchor") as anchor:
            win = self.make_win()
            update = sf_update.UpdateInfo("2.0.0", "notes", "https://github.com/x/y", 8, "a" * 64)
            # The port has no public prompt entry on the window; exercise its real adapter.
            _peek(win, "_update_port").prompt_update(update, kind="offer", generation=1, identity=("test",))
            alert.assert_called_once()
            anchor.assert_called_once()
            win._exit_persisted = True  # discipline-ok(R5): close while isolation patches remain active
            win.close()

    def test_stale_modern_callback_does_not_close_current_legacy_prompt(self):
        import sf_system_alert
        import sf_update
        import stock_float_window as window_module

        with tempfile.TemporaryDirectory(prefix="sf-update-window-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root), \
                mock.patch.object(sf_system_alert, "show_alert") as alert:
            win = self.make_win()
            update = sf_update.UpdateInfo("2.0.0", "notes", "https://github.com/x/y", 8, "a" * 64)
            port = _peek(win, "_update_port")  # discipline-ok(R5): queued callback lifecycle boundary
            modern, legacy = mock.Mock(), mock.Mock()
            alert.side_effect = [modern, legacy]
            port.prompt_update(update, kind="offer", generation=1, identity=("test",))
            callback = alert.call_args.kwargs["on_result"]
            port.flow.clear_verified_session()
            with mock.patch.object(win, "_ready_update_archive", return_value=None), \
                    mock.patch.object(win, "_q_put") as queue:
                win._prompt_update(update)  # discipline-ok(R5): legacy prompt entry
                callback("立即更新")
                _, answer = queue.call_args.args[0]
                win._handle_update_prompt_answer(answer)  # discipline-ok(R5): deliver queued callback
            self.assertIs(win.current_update_prompt_dialog(), legacy)
            legacy.close.assert_not_called()
            win._exit_persisted = True  # discipline-ok(R5): close while isolation patches remain active
            win.close()

    def test_current_forced_exit_and_legacy_button_remain_actionable(self):
        import sf_system_alert
        import sf_update
        import stock_float_window as window_module

        with tempfile.TemporaryDirectory(prefix="sf-update-window-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root), \
                mock.patch.object(sf_system_alert, "show_alert", return_value=mock.Mock()) as alert:
            win = self.make_win()
            port = _peek(win, "_update_port")  # discipline-ok(R5): real update session
            update = sf_update.UpdateInfo(
                "99.0.0", "notes", "https://github.com/x/y", 8, "a" * 64,
                release_sequence=port.get_local_sequence() + 8,
                published_at="2026-11-19T00:00:00Z")
            with mock.patch.object(win, "_q_put") as queue, mock.patch.object(win, "quit") as quit_app:
                port.flow.on_manifest_verified(update, automatic=False)
                callback = alert.call_args.kwargs["on_result"]
                callback("退出程序")
                _, answer = queue.call_args.args[0]
                win._handle_update_prompt_answer(answer)  # discipline-ok(R5): deliver queued response
                quit_app.assert_called_once_with()
            port.flow.clear_verified_session()
            with mock.patch.object(win, "_ready_update_archive", return_value=None), \
                    mock.patch.object(win, "_q_put") as queue, \
                    mock.patch.object(win, "_accept_update_prompt") as accept:
                win._update_pending = update  # discipline-ok(R5): legacy session state has no public setter
                win._prompt_update(update)  # discipline-ok(R5): legacy prompt entry
                alert.call_args.kwargs["on_result"]("立即更新")
                _, answer = queue.call_args.args[0]
                win._handle_update_prompt_answer(answer)  # discipline-ok(R5): deliver queued response
                accept.assert_called_once_with(None, update)
            win._exit_persisted = True  # discipline-ok(R5): close while isolation patches remain active
            win.close()

    def test_disposed_prompt_invalidates_token_and_ignores_queued_answer(self):
        import sf_system_alert
        import sf_update

        win = self.make_win()
        update = sf_update.UpdateInfo("2.0.0", "notes", "https://github.com/x/y", 8, "a" * 64)
        dialog = mock.Mock()
        # The legacy prompt and queue handlers have no public entry points;
        # exercise them on the isolated real window, not a private host double.
        with mock.patch.object(sf_system_alert, "show_alert", return_value=dialog) as alert, \
                mock.patch.object(win, "_ready_update_archive", return_value=None), \
                mock.patch.object(win, "_q_put") as queue, \
                mock.patch.object(win, "_accept_update_prompt") as accept:
            win._prompt_update(update)  # discipline-ok(R5): legacy prompt entry
            callback = alert.call_args.kwargs["on_result"]
            self.assertIsNotNone(_peek(win, "_legacy_update_prompt_token"))
            self.assertIs(win.current_update_prompt_dialog(), dialog)
            win.dispose_update_dialog(dialog)
            dialog.close.assert_called_once_with()
            self.assertIsNone(win.current_update_prompt_dialog())
            self.assertIsNone(_peek(win, "_legacy_update_prompt_token"))
            callback("立即更新")
            self.assertEqual(queue.call_count, 1)
            kind, answer = queue.call_args.args[0]
            self.assertEqual(kind, "update_prompt_answer")
            self.assertFalse(win._handle_update_prompt_answer(answer))  # discipline-ok(R5): deliver queued answer
            accept.assert_not_called()


class UpdateFlowPortTests(SFCase):
    def setUp(self):
        super().setUp()
        self.backend = mock.Mock()
        self.flow = UpdateFlowController(lambda host: self.backend, mock.Mock(), mock.Mock())

    def test_update_controls_do_not_retry_internal_typeerror(self):
        import stock_float_window as window_module
        for operation in ("begin_update_download", "finish_update_download"):
            calls = []

            def failing(*args):
                calls.append(args[-1])
                raise TypeError("internal failure")

            controls = window_module._UpdateWindowControls(  # discipline-ok(R5): adapter is the behavior under test
                SimpleNamespace(**{operation: failing}))
            with self.subTest(operation=operation), self.assertRaisesRegex(TypeError, "internal failure"):
                if operation == "begin_update_download":
                    controls.download_started(threading.Event(), True, False, "task")
                else:
                    controls.download_finished(object(), "archive", None, mock.Mock(), "task")
            self.assertEqual(calls, ["task"])

    def test_update_port_does_not_retry_internal_typeerror(self):
        import stock_float_window as window_module
        from tests.test_force_update_flow import FakeCache
        for operation in ("download", "check"):
            calls = []

            def failing(*args):
                calls.append(args[-1])
                raise TypeError("internal failure")

            controller = SimpleNamespace(**{operation: failing})
            target = mock.Mock()
            target.update_controller.return_value = controller
            with mock.patch.object(window_module, "UpdateCache", return_value=FakeCache()):
                port = window_module.make_update_flow_port(target)
                with self.subTest(operation=operation), self.assertRaisesRegex(TypeError, "internal failure"):
                    if operation == "download":
                        port.start_download(object(), prompt_when_ready=False, show_progress=False, token="task")
                    else:
                        port.request_check(True, token="task")
            self.assertEqual(calls, ["task"])

    def test_legacy_internal_typeerror_is_not_retried(self):
        import stock_float_window as window_module
        calls = []

        def legacy(event, prompt, progress):
            calls.append(event)
            raise TypeError("legacy internal failure")

        controls = window_module._UpdateWindowControls(  # discipline-ok(R5): adapter is the behavior under test
            SimpleNamespace(begin_update_download=legacy))
        event = threading.Event()
        with self.assertRaisesRegex(TypeError, "legacy internal failure"):
            controls.download_started(event, True, False, "task")
        self.assertEqual(calls, [event])

    def test_uninspectable_callable_is_invoked_only_once_with_token(self):
        import stock_float_window as window_module
        calls = []

        def operation(*args):
            calls.append(args)
            raise TypeError("operation failure")

        controls = window_module._UpdateWindowControls(  # discipline-ok(R5): adapter is the behavior under test
            SimpleNamespace(begin_update_download=operation))
        for error in (TypeError("unknown"), ValueError("unknown")):
            calls.clear()
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(window_module.inspect, "signature", side_effect=error), \
                    self.assertRaisesRegex(TypeError, "operation failure"):
                controls.download_started(threading.Event(), True, False, "task")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][-1], "task")

    def test_incompatible_signature_does_not_execute(self):
        import stock_float_window as window_module
        calls = []

        def incompatible():
            calls.append(True)

        controls = window_module._UpdateWindowControls(  # discipline-ok(R5): adapter is the behavior under test
            SimpleNamespace(begin_update_download=incompatible))
        with self.assertRaises(TypeError):
            controls.download_started(threading.Event(), True, False, "task")
        self.assertEqual(calls, [])

    def test_window_controls_forward_public_operations_and_legacy_signatures(self):
        import stock_float_window as window_module
        target = mock.Mock()
        controls = window_module._UpdateWindowControls(target)  # discipline-ok(R5): actual adapter is the behavior under test
        cancel, update, record = threading.Event(), object(), mock.Mock()
        controls.download_started(cancel, True, False, "task")
        target.begin_update_download.assert_called_once_with(cancel, True, False, "task")
        controls.download_finished(update, "archive", None, record, "task")
        target.finish_update_download.assert_called_once_with(update, "archive", None, record, "task")
        controls.finish_update_check(True)
        target.finish_update_check.assert_called_once_with(True, consume_abort=True)
        controls.record_download({"outcome": "ok"})
        target.record_update_download.assert_called_once_with({"outcome": "ok"})
        dialog = object()
        controls.dispose_dialog(dialog)
        target.dispose_update_dialog.assert_called_once_with(dialog)
        self.assertIs(controls.current_prompt_dialog(), target.current_update_prompt_dialog.return_value)
        controls.anchor_dialog()
        target.anchor_update_dialog_screen.assert_called_once_with()
        controls.set_prompt_dialog(dialog)
        target.set_update_prompt_dialog.assert_called_once_with(dialog)
        controls.queue(("update", None))
        target.queue_update_message.assert_called_once_with(("update", None))
        controls.show_message("title", "body")
        target.show_update_message.assert_called_once_with("title", "body")
        controls.show_checking()
        target.show_checking_dialog.assert_called_once_with()
        controls.install(update, "archive")
        target.install_update.assert_called_once_with(update, "archive")
        controls.quit()
        target.quit.assert_called_once_with()
        started, finished = [], []
        legacy = SimpleNamespace(
            begin_update_download=lambda event, prompt, progress: started.append((event, prompt, progress)),
            finish_update_download=lambda item, archive, error, callback: finished.append((item, archive, error, callback)))
        legacy_controls = window_module._UpdateWindowControls(legacy)  # discipline-ok(R5): verify compatibility at real adapter boundary
        legacy_controls.download_started(cancel, True, False, "task")
        legacy_controls.download_finished(update, "archive", None, record, "task")
        self.assertEqual(started, [(cancel, True, False)])
        self.assertEqual(finished, [(update, "archive", None, record)])

    def test_real_window_public_lifecycle_operations_preserve_state_and_route_actions(self):
        import stock_float_window as window_module
        controller, dialog, cancel = object(), object(), threading.Event()
        reset, reveal, dispose, queue, install, checking = (mock.Mock() for _ in range(6))
        target = SimpleNamespace(_update_controller=controller, reset_update_progress=reset,
                                 _open_download_progress_box=reveal,
                                 _dispose_update_dialog=dispose, _q_put=queue,
                                 _launch_downloaded_update=install, _show_checking_dialog=checking)
        api = window_module.FloatWindow
        self.assertIs(api.update_controller(target), controller)
        api.begin_update_download(target, cancel, True, False, "one")
        self.assertTrue(_peek(target, "_update_downloading"))
        self.assertIs(_peek(target, "_update_cancel"), cancel)
        self.assertEqual(_peek(target, "_update_download_token"), "one")
        self.assertTrue(_peek(target, "_update_prompt_when_ready"))
        reveal.assert_not_called()
        api.begin_update_download(target, cancel, False, True, "two")
        self.assertEqual(reset.call_count, 2)
        reveal.assert_called_once_with()
        self.assertFalse(_peek(target, "_update_prompt_when_ready"))
        details = {"outcome": "cancelled"}
        api.record_update_download(target, details)
        self.assertIs(_peek(target, "_update_last_download"), details)
        self.assertIsNone(api.current_update_prompt_dialog(target))
        api.set_update_prompt_dialog(target, dialog)
        self.assertIs(api.current_update_prompt_dialog(target), dialog)
        api.dispose_update_dialog(target, dialog)
        dispose.assert_called_once_with(dialog)
        message = ("update_check", None)
        api.queue_update_message(target, message)
        queue.assert_called_once_with(message)
        api.install_update(target, "update", "archive")
        install.assert_called_once_with("update", "archive")
        api.show_checking_dialog(target)
        checking.assert_called_once_with()

    def test_legacy_download_cancel_reaches_its_worker_with_modern_port_present(self):
        import stock_float_window as window_module
        cancel = threading.Event()
        port = mock.Mock()
        vars(port)["_flow"] = SimpleNamespace(download_token=None)
        host = SimpleNamespace(_update_port=port, _update_cancel=cancel,
                               _update_downloading=True, _update_download_token=None)
        window_module.FloatWindow._cancel_update_download(host)  # discipline-ok(R5): exercise real window cancellation entry
        self.assertTrue(cancel.is_set())

    def test_retired_modern_token_cannot_update_progress(self):
        import stock_float_window as window_module
        owner = mock.Mock()
        with tempfile.TemporaryDirectory(prefix="sf-token-progress-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root):
            port = window_module.make_update_flow_port(owner)
            self.assertFalse(port.accepts_progress(object()))

    def test_legacy_terminal_cannot_close_replacement_progress(self):
        import stock_float_window as window_module
        old = SimpleNamespace(release_sequence=None)
        host = SimpleNamespace(_update_port=mock.Mock(), _update_download_token=object(),
                               _update_downloading=True, close_update_flow=mock.Mock(),
                               _launch_downloaded_update=mock.Mock())
        result = window_module.FloatWindow._handle_update_download(host, old, None, None, object())  # discipline-ok(R5): real queued terminal entry
        self.assertFalse(result)
        host.close_update_flow.assert_not_called()

    def test_legacy_start_internal_typeerror_is_not_retried_and_releases_state(self):
        import stock_float_window as window_module
        calls = []

        def download(update, cancel_event, token=None):
            calls.append((cancel_event, token))
            raise TypeError("backend failed")

        host = SimpleNamespace(_update_downloading=False, _quitting=False,
                               reset_update_progress=mock.Mock(),
                               _update_controller=SimpleNamespace(download=download),
                               close_update_progress=mock.Mock(), _show_update_message=mock.Mock())
        host.set_legacy_update_prompt_when_ready = lambda value: setattr(host, "_update_prompt_when_ready", value)
        host.open_update_download_progress = mock.Mock()
        host.begin_legacy_update_download = lambda event, token: window_module.FloatWindow.begin_legacy_update_download(host, event, token)
        host.clear_legacy_update_token = lambda: window_module.FloatWindow.clear_legacy_update_token(host)
        with self.assertRaisesRegex(TypeError, "backend failed"):
            window_module.FloatWindow._download_update_background(host, SimpleNamespace(version="fixture", release_sequence=None), False, False)  # discipline-ok(R5): real legacy entry
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].is_set())
        self.assertFalse(_peek(host, "_update_downloading"))  # discipline-ok(R5): legacy state has no public snapshot
        self.assertIsNone(_peek(host, "_update_download_token"))  # discipline-ok(R5): assert task ownership released
        host.close_update_progress.assert_called_once()

    def test_legacy_downloads_receive_distinct_tokens(self):
        import stock_float_window as window_module
        backend = mock.Mock()
        host = SimpleNamespace(_update_downloading=False, _quitting=False,
                               reset_update_progress=mock.Mock(), _update_controller=backend)
        host.set_legacy_update_prompt_when_ready = lambda value: setattr(host, "_update_prompt_when_ready", value)
        host.open_update_download_progress = mock.Mock()
        host.begin_legacy_update_download = lambda event, token: window_module.FloatWindow.begin_legacy_update_download(host, event, token)
        update = SimpleNamespace(release_sequence=None)
        window_module.FloatWindow._download_update_background(host, update, False, False)  # discipline-ok(R5): real legacy entry
        self.assertIsNotNone(_peek(host, "_update_download_token"))
        self.assertIs(backend.download.call_args.args[2], _peek(host, "_update_download_token"))

    def test_legacy_cancelled_progress_is_ignored_before_worker_returns(self):
        import stock_float_window as window_module
        cancel = threading.Event()
        owner = SimpleNamespace(_update_download_token=object(), _update_cancel=cancel)
        with tempfile.TemporaryDirectory(prefix="sf-cancelled-progress-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root):
            owner.update_controller = mock.Mock(return_value=mock.Mock())
            port = window_module.make_update_flow_port(owner)
            token = _peek(owner, "_update_download_token")  # discipline-ok(R5): fixture task token
            self.assertTrue(port.accepts_progress(token))
            window_module.FloatWindow._cancel_update_download(SimpleNamespace(  # discipline-ok(R5): real window cancellation
                _update_port=port, _update_cancel=cancel))
            self.assertFalse(port.accepts_progress(token))

    def test_pending_manifest_retry_is_bounded_and_stops_after_shutdown(self):
        import stock_float_window as window_module
        owner = mock.Mock()
        vars(owner)["_quitting"] = False  # discipline-ok(R5): lifecycle fixture
        with tempfile.TemporaryDirectory(prefix="sf-retry-timer-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root), \
                mock.patch.object(window_module.QTimer, "singleShot") as timer:
            port = window_module.make_update_flow_port(owner)
            for _ in range(20):
                port.schedule_manifest_retry()
            self.assertLessEqual(timer.call_count, 1)
            vars(owner)["_quitting"] = True  # discipline-ok(R5): lifecycle fixture
            callback = timer.call_args.args[1]
            with mock.patch.object(port.flow, "retry_pending_manifest") as retry:
                callback()
                retry.assert_not_called()

    def test_manifest_retry_stops_after_bounded_busy_attempts(self):
        import stock_float_window as window_module
        owner = mock.Mock()
        vars(owner)["_quitting"] = False  # discipline-ok(R5): lifecycle fixture
        callbacks = []
        with tempfile.TemporaryDirectory(prefix="sf-retry-limit-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root), \
                mock.patch.object(window_module.QTimer, "singleShot", side_effect=lambda delay, cb: callbacks.append(cb)):
            port = window_module.make_update_flow_port(owner)
            with mock.patch.object(port.flow, "retry_pending_manifest", side_effect=port.schedule_manifest_retry):
                port.schedule_manifest_retry()
                for _ in range(100):
                    if not callbacks:
                        break
                    callbacks.pop(0)()
            self.assertFalse(callbacks, "busy cache retries must eventually stop")
            owner.show_update_message.assert_called()

    def test_new_manual_check_resets_exhausted_retry_budget(self):
        import stock_float_window as window_module
        owner = mock.Mock()
        vars(owner)["_quitting"] = False  # discipline-ok(R5): lifecycle fixture
        with tempfile.TemporaryDirectory(prefix="sf-retry-renew-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root), \
                mock.patch.object(window_module.QTimer, "singleShot") as timer:
            port = window_module.make_update_flow_port(owner)
            for _ in range(10):
                port.schedule_manifest_retry()
                callback = timer.call_args.args[1]
                callback()
            port.request_check(True)
            port.schedule_manifest_retry()
            self.assertEqual(timer.call_count, 11)

    def test_retry_callback_does_not_run_after_controller_stop(self):
        import stock_float_window as window_module
        owner = mock.Mock()
        vars(owner)["_quitting"] = False  # discipline-ok(R5): lifecycle fixture
        with tempfile.TemporaryDirectory(prefix="sf-retry-stop-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root), \
                mock.patch.object(window_module.QTimer, "singleShot") as timer:
            port = window_module.make_update_flow_port(owner)
            port.schedule_manifest_retry()
            port.stop()
            with mock.patch.object(port.flow, "retry_pending_manifest") as retry:
                timer.call_args.args[1]()
                retry.assert_not_called()

    def test_window_keeps_pending_verified_manifest_when_legacy_check_arrives(self):
        import stock_float_window as window_module
        import sf_update
        from tests.test_force_update_flow import FakeCache
        from sf_update_cache import UpdateCacheBusyError
        owner = mock.Mock()
        cache = FakeCache()
        with mock.patch.object(window_module, "UpdateCache", return_value=cache):
            port = window_module.make_update_flow_port(owner)
            initial = sf_update.UpdateInfo("2.0.0", "notes", "https://github.com/x/y", 8, "a" * 64,
                                           release_sequence=4, published_at="2026-11-19T00:00:00Z")
            pending = sf_update.UpdateInfo("3.0.0", "notes", "https://github.com/x/y", 8, "b" * 64,
                                           release_sequence=5, published_at="2026-11-20T00:00:00Z")
            legacy = sf_update.UpdateInfo("1.1.0", "notes", "https://github.com/x/y", 8, "c" * 64)
            port.flow.on_manifest_verified(initial, automatic=True)
            port.flow.start_download(prompt_when_ready=False, show_progress=True)
            with mock.patch.object(cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
                port.flow.on_manifest_verified(pending, automatic=True)
            host = SimpleNamespace(_update_port=port, finish_update_check=mock.Mock(return_value=False),
                                   _update_last_check_at=0, _quitting=False, _update_check_abort=False,
                                   _update_prompted_version=None, _update_ready_version=None,
                                   _update_pending=None, _update_downloading=False,
                                   _prompt_update=mock.Mock(), _show_update_message=mock.Mock(),
                                   _ready_update_archive=mock.Mock(return_value=None),
                                   _download_update_background=mock.Mock())
            window_module.FloatWindow._handle_update_check(host, False, legacy, None)  # discipline-ok(R5): real check result entry
            self.assertTrue(port.flow.is_current(identity=port.flow._identity(initial)))  # discipline-ok(R5): compare current identity at boundary

    def test_legacy_manifest_after_modern_acceptance_is_rejected_after_restart(self):
        import sf_update
        import stock_float_window as window_module
        from sf_update_cache import UpdateCache

        modern = sf_update.UpdateInfo("2.0.0", "notes", "https://github.com/x/y", 8, "a" * 64,
                                      release_sequence=4, published_at="2026-11-19T00:00:00Z")
        legacy = sf_update.UpdateInfo("3.0.0", "notes", "https://github.com/x/y", 8, "b" * 64)
        with tempfile.TemporaryDirectory(prefix="sf-protocol-downgrade-") as root:
            cache = UpdateCache(root)
            with cache.write_lock():
                self.assertTrue(cache.accept_manifest_identity(modern))
            owner = mock.Mock()
            with mock.patch.object(window_module, "UPDATE_STAGING_DIR", root):
                port = window_module.make_update_flow_port(owner)
                host = SimpleNamespace(_update_port=port, finish_update_check=mock.Mock(return_value=True),
                                       _update_check_aborted=False, _update_last_check_at=0.0,
                                       _update_failure_count=0, _update_retry_timer=None, _quitting=False,
                                       _prompt_update=mock.Mock(), _show_update_message=mock.Mock())
                with mock.patch.object(window_module._UPDATE_FLOW, "checked") as legacy_handler:  # discipline-ok(R5): isolate legacy handler at real window boundary
                    window_module.FloatWindow._handle_update_check(host, True, legacy, None)  # discipline-ok(R5): actual queued response
                legacy_handler.assert_not_called()

    def test_legacy_manifest_before_modern_acceptance_remains_compatible(self):
        import sf_update
        import stock_float_window as window_module
        legacy = sf_update.UpdateInfo("3.0.0", "notes", "https://github.com/x/y", 8, "b" * 64)
        with tempfile.TemporaryDirectory(prefix="sf-legacy-compatible-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root):
            port = window_module.make_update_flow_port(mock.Mock())
            host = SimpleNamespace(_update_port=port)
            with mock.patch.object(window_module._UPDATE_FLOW, "checked") as handler:  # discipline-ok(R5): isolate legacy dispatch for compatibility assertion
                window_module.FloatWindow._handle_update_check(host, True, legacy, None)  # discipline-ok(R5): actual queued response
            handler.assert_called_once_with(host, True, legacy, None)

    def test_busy_cache_rejects_legacy_check_with_visible_feedback(self):
        import sf_update
        import stock_float_window as window_module
        from sf_update_cache import UpdateCache
        legacy = sf_update.UpdateInfo("3.0.0", "notes", "https://github.com/x/y", 8, "b" * 64)
        with tempfile.TemporaryDirectory(prefix="sf-legacy-busy-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root):
            port = window_module.make_update_flow_port(mock.Mock())
            host = SimpleNamespace(_update_port=port, _show_update_message=mock.Mock())
            with UpdateCache(root).write_lock(), mock.patch.object(window_module._UPDATE_FLOW, "checked") as handler:  # discipline-ok(R5): prove no legacy dispatch while locked
                result = window_module.FloatWindow._handle_update_check(host, True, legacy, None)  # discipline-ok(R5): actual queued response
            self.assertFalse(result)
            handler.assert_not_called()
            _peek(host, "_show_update_message").assert_called_once()  # discipline-ok(R5): verify user-visible feedback

    def test_rejected_legacy_response_invalidates_modern_session_and_reports_reused_click(self):
        import sf_update
        import stock_float_window as window_module
        from sf_update_cache import UpdateCache
        modern = sf_update.UpdateInfo("2.0.0", "notes", "https://github.com/x/y", 8, "a" * 64,
                                      release_sequence=4, published_at="2026-11-19T00:00:00Z")
        legacy = sf_update.UpdateInfo("3.0.0", "notes", "https://github.com/x/y", 8, "b" * 64)
        with tempfile.TemporaryDirectory(prefix="sf-legacy-reject-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root):
            port = window_module.make_update_flow_port(mock.Mock())
            with UpdateCache(root).write_lock() as cache:
                self.assertTrue(cache.accept_manifest_identity(modern))
            port.flow.on_manifest_verified(modern, automatic=False)
            self.assertTrue(port.flow.is_current())
            port.finish_update_check = mock.Mock(return_value=True)
            host = SimpleNamespace(_update_port=port, _show_update_message=mock.Mock())
            window_module.FloatWindow._handle_update_check(host, False, legacy, None)  # discipline-ok(R5): actual queued response
            self.assertFalse(port.flow.is_current(), "协议降级必须使旧提示失效")
            _peek(host, "_show_update_message").assert_called_once()

    def test_legacy_check_without_cache_fails_closed_with_feedback(self):
        import sf_update
        import stock_float_window as window_module
        legacy = sf_update.UpdateInfo("3.0.0", "notes", "https://github.com/x/y", 8, "b" * 64)
        with mock.patch.object(window_module, "UpdateCache", side_effect=OSError("unavailable")):
            port = window_module.make_update_flow_port(mock.Mock())
        port.finish_update_check = mock.Mock(return_value=True)
        host = SimpleNamespace(_update_port=port, _show_update_message=mock.Mock())
        result = window_module.FloatWindow._handle_update_check(host, False, legacy, None)  # discipline-ok(R5): actual queued response
        self.assertFalse(result)
        _peek(host, "_show_update_message").assert_called_once()

    def test_legacy_equal_local_version_reaches_latest_result(self):
        import sf_update
        import stock_float_window as window_module
        legacy = sf_update.UpdateInfo("1.0.0", "notes", "https://github.com/x/y", 8, "b" * 64)
        with tempfile.TemporaryDirectory(prefix="sf-legacy-equal-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root), \
                mock.patch.object(window_module, "_bundle_version", return_value="1.0.0"):
            port = window_module.make_update_flow_port(mock.Mock())
            host = SimpleNamespace(_update_port=port)
            with mock.patch.object(window_module._UPDATE_FLOW, "checked") as handler:  # discipline-ok(R5): inspect actual dispatch boundary
                window_module.FloatWindow._handle_update_check(host, True, legacy, None)  # discipline-ok(R5): actual queued response
            handler.assert_called_once_with(host, True, None, None)

    def test_revealing_background_progress_marks_fatal_failure_visible(self):
        import sf_update
        import stock_float_window as window_module
        from tests.test_force_update_flow import FakeCache
        owner = mock.Mock()
        cache = FakeCache()
        with mock.patch.object(window_module, "UpdateCache", return_value=cache):
            port = window_module.make_update_flow_port(owner)
            update = sf_update.UpdateInfo("9.9.9", "notes", "https://github.com/x/y", 8, "a" * 64,
                                          release_sequence=999, published_at="2026-11-20T00:00:00Z")
            port.flow.on_manifest_verified(update, automatic=True)
            token = port.flow.download_token
            port.show_download_progress()
            port.flow.on_download_finished(update, None, sf_update.UpdateFatal("disk"), token)
            owner.show_update_message.assert_called()

    def test_legacy_completion_keeps_unrelated_update_prompt_open(self):
        import stock_float_window as window_module
        import sf_update
        token = object()
        prompt = mock.Mock()
        update = sf_update.UpdateInfo("1.0.1", "notes", "https://github.com/x/y", 8, "a" * 64)
        host = SimpleNamespace(_update_port=None, _update_download_token=token,
                               _update_downloading=True, _update_prompt_when_ready=False,
                               _update_cancel=threading.Event(), _update_prompt_dialog=prompt,
                               close_update_flow=mock.Mock(), close_update_progress=mock.Mock(),
                               _show_update_message=mock.Mock())
        window_module.FloatWindow._handle_update_download(host, update, None, sf_update.UpdateCancelled("cancel"), token)  # discipline-ok(R5): actual legacy terminal boundary
        prompt.close.assert_not_called()
        host.close_update_flow.assert_not_called()
        host.close_update_progress.assert_called_once_with()

    def test_cancelled_legacy_download_rejects_late_success(self):
        import stock_float_window as window_module
        token = object()
        cancel = threading.Event()
        cancel.set()
        update = SimpleNamespace(release_sequence=None)
        launch = mock.Mock()
        host = SimpleNamespace(_update_port=None, _update_download_token=token,
                               _update_cancel=cancel, _update_downloading=True,
                               _update_prompt_when_ready=False, close_update_progress=mock.Mock(),
                               _launch_downloaded_update=launch)
        window_module.FloatWindow._handle_update_download(host, update, "old.zip", None, token)  # discipline-ok(R5): real queued terminal entry
        launch.assert_not_called()  # discipline-ok(R5): verify no installation

    def test_legacy_two_argument_backend_remains_supported(self):
        host = SimpleNamespace(_update_downloading=False, _quitting=False,
                               reset_update_progress=mock.Mock())
        host.set_legacy_update_prompt_when_ready = lambda value: setattr(host, "_update_prompt_when_ready", value)
        host.open_update_download_progress = mock.Mock()
        host.begin_legacy_update_download = lambda event, token: vars(host).update(
            _update_cancel=event, _update_download_token=token)
        calls = []
        backend = SimpleNamespace(download=lambda update, event: calls.append((update, event)))
        flow = UpdateFlowController(lambda _: backend, mock.Mock(), mock.Mock())
        update = object()
        flow.download(host, update, show_progress=False)
        self.assertEqual(calls, [(update, _peek(host, "_update_cancel"))])

    def test_successful_retry_resets_retry_budget(self):
        import stock_float_window as window_module
        owner = mock.Mock()
        vars(owner)["_quitting"] = False
        callbacks = []
        with tempfile.TemporaryDirectory(prefix="sf-retry-success-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root), \
                mock.patch.object(window_module.QTimer, "singleShot", side_effect=lambda delay, cb: callbacks.append(cb)):
            port = window_module.make_update_flow_port(owner)
            with mock.patch.object(port.flow, "retry_pending_manifest", return_value=object()):
                for _ in range(11):
                    port.schedule_manifest_retry()
                    callbacks.pop(0)()
            self.assertEqual(len(callbacks), 0)
            owner.show_update_message.assert_not_called()

    def test_close_update_flow_forwards_to_lifecycle_closer(self):
        import stock_float_window as window_module
        closer = mock.Mock()
        host = SimpleNamespace(_close_update_flow_box=closer)
        window_module.FloatWindow.close_update_flow(host)
        closer.assert_called_once_with()

    def test_background_progress_window_notifies_real_flow(self):
        import stock_float_window as window_module
        flow = mock.Mock()
        dialog = mock.Mock()
        dialog.present.return_value = True
        host = SimpleNamespace(_update_port=SimpleNamespace(flow=flow), _update_flow_box=dialog)
        window_module.FloatWindow._show_background_download_progress(host)  # discipline-ok(R5): exercise real window boundary
        flow.progress_revealed.assert_called_once_with()
        dialog.present.assert_called_once_with()

    def test_legacy_uninspectable_backend_uses_token_once(self):
        import sf_update_flow

        for error_type in (TypeError, ValueError):
            with self.subTest(error_type=error_type):
                backend = mock.Mock()
                swallowed = mock.Mock()
                flow = UpdateFlowController(lambda _: backend, mock.Mock(), swallowed)
                host = SimpleNamespace(
                    _update_downloading=False, _quitting=False,
                    reset_update_progress=mock.Mock(),
                )
                host.set_legacy_update_prompt_when_ready = lambda value: setattr(host, "_update_prompt_when_ready", value)
                host.open_update_download_progress = mock.Mock()
                host.begin_legacy_update_download = lambda event, token: vars(host).update(
                    _update_cancel=event, _update_download_token=token)
                update = object()
                error = error_type("signature unavailable")
                with mock.patch.object(sf_update_flow.inspect, "signature", side_effect=error):
                    flow.download(host, update, prompt_when_ready=False, show_progress=False)
                swallowed.assert_called_once_with("sf_update_flow.inspect_signature", error)
                backend.download.assert_called_once_with(
                    update, _peek(host, "_update_cancel"), _peek(host, "_update_download_token"))  # discipline-ok(R5): assert legacy task ownership
                self.assertFalse(_peek(host, "_update_cancel").is_set())  # discipline-ok(R5): assert cancellation state
                self.assertTrue(_peek(host, "_update_downloading"))  # discipline-ok(R5): assert legacy lifecycle state

    def test_legacy_false_start_preserves_error_when_cleanup_raises(self):
        cleanup_error = RuntimeError("progress cleanup failed")
        self.backend.download.return_value = False
        host = SimpleNamespace(
            _update_downloading=False, _quitting=False,
            reset_update_progress=mock.Mock(),
            close_update_progress=mock.Mock(side_effect=cleanup_error),
        )
        host.set_legacy_update_prompt_when_ready = lambda value: setattr(host, "_update_prompt_when_ready", value)
        host.open_update_download_progress = mock.Mock()
        host.begin_legacy_update_download = lambda event, token: vars(host).update(
            _update_cancel=event, _update_download_token=token)
        host.clear_legacy_update_token = lambda: setattr(host, "_update_download_token", None)
        update = SimpleNamespace(version="fixture")
        with self.assertRaisesRegex(RuntimeError, "更新下载未能启动") as raised:
            self.flow.download(host, update, prompt_when_ready=False, show_progress=False)
        self.backend.download.assert_called_once()
        called_update, event, token = self.backend.download.call_args.args
        self.assertIs(called_update, update)
        self.assertTrue(event.is_set())
        self.assertIsNotNone(token)
        host.close_update_progress.assert_called_once_with()
        self.flow.swallow.assert_called_once_with("sf_update_flow.legacy_start_cleanup", cleanup_error)
        self.assertIsNot(raised.exception, cleanup_error)
        self.assertFalse(_peek(host, "_update_downloading"))  # discipline-ok(R5): assert legacy state cleanup
        self.assertIsNone(_peek(host, "_update_cancel"))  # discipline-ok(R5): assert owned event cleanup
        self.assertIsNone(_peek(host, "_update_download_token"))  # discipline-ok(R5): assert token cleanup
        self.assertEqual(_peek(host, "_update_last_download")["outcome"], "failed")  # discipline-ok(R5): assert recorded failure outcome

    def test_port_start_cleanup_failure_preserves_original_error(self):
        import stock_float_window as window_module
        from tests.test_force_update_flow import FakeCache

        target = mock.Mock()
        controller = target.update_controller.return_value
        start_error = RuntimeError("backend start failed")
        cleanup_error = RuntimeError("port cleanup failed")
        controller.download.side_effect = start_error
        target.finish_update_download.side_effect = cleanup_error
        update, token = object(), object()
        with mock.patch.object(window_module, "UpdateCache", return_value=FakeCache()), \
                mock.patch.object(window_module, "log_swallow") as swallowed:
            port = window_module.make_update_flow_port(target)
            with self.assertRaises(RuntimeError) as raised:
                port.start_download(update, prompt_when_ready=True, show_progress=True, token=token)
        self.assertIs(raised.exception, start_error)
        controller.download.assert_called_once()
        called_update, event, called_token = controller.download.call_args.args
        self.assertIs(called_update, update)
        self.assertIs(called_token, token)
        self.assertTrue(event.is_set())
        controller.cancel_download.assert_called_once_with(update, token=token)
        target.finish_update_download.assert_called_once()
        item, archive, error, record, finish_token = target.finish_update_download.call_args.args
        self.assertIs(item, update)
        self.assertIsNone(archive)
        self.assertIs(error, start_error)
        self.assertTrue(callable(record))
        self.assertIs(finish_token, token)
        swallowed.assert_called_once_with("stock_float_window.update_start_cleanup", cleanup_error)
        self.assertIsNone(port._cancel_event)  # discipline-ok(R5): cleanup ownership invariant

    def test_start_failure_cleans_owned_window_and_cancels_owned_task(self):
        import stock_float_window as window_module
        from tests.test_force_update_flow import FakeCache
        target = mock.Mock()
        controller = target.update_controller.return_value
        controller.download.side_effect = TypeError("start failed")
        update, token = object(), object()
        with mock.patch.object(window_module, "UpdateCache", return_value=FakeCache()):
            port = window_module.make_update_flow_port(target)
            with self.assertRaisesRegex(TypeError, "start failed"):
                port.start_download(update, prompt_when_ready=True, show_progress=True, token=token)
        controller.cancel_download.assert_called_once_with(update, token=token)
        self.assertIs(target.finish_update_download.call_args.args[4], token)

    def test_terminal_window_cleanup_failure_does_not_leave_flow_active(self):
        import stock_float_window as window_module
        from tests.test_force_update_flow import FakeCache
        import sf_update
        target = mock.Mock()
        cache = FakeCache()
        update = sf_update.UpdateInfo("9.9.9", "notes", "https://github.com/x/y", 8, "a" * 64,
                                      release_sequence=2, published_at="2026-11-25T00:00:00Z")
        with mock.patch.object(window_module, "UpdateCache", return_value=cache):
            port = window_module.make_update_flow_port(target)
            port.flow.on_manifest_verified(update, automatic=False)
            port.flow.start_download(prompt_when_ready=True, show_progress=True)
            token = port.flow.download_token
            target.finish_update_download.side_effect = TypeError("cleanup failed")
            port.on_download_finished(update, None, RuntimeError("download failed"), token)
            self.assertFalse(port.flow.diagnostics()["download_active"])
            self.assertIsNone(port.flow.download_token)

    def test_stopped_controller_does_not_leave_download_active(self):
        import stock_float_window as window_module
        from tests.test_force_update_flow import FakeCache
        from sf_update_controller import UpdateController
        import sf_update
        controller = UpdateController(lambda message: None, lambda: "1.0.0")
        controller.stop()
        target = mock.Mock()
        target.update_controller.return_value = controller
        update = sf_update.UpdateInfo("9.9.9", "notes", "https://github.com/x/y", 8, "a" * 64,
                                      release_sequence=2, published_at="2026-11-25T00:00:00Z")
        with mock.patch.object(window_module, "UpdateCache", return_value=FakeCache()):
            port = window_module.make_update_flow_port(target)
            port.flow.on_manifest_verified(update, automatic=False)
            with self.assertRaises(RuntimeError):
                port.flow.start_download(prompt_when_ready=True, show_progress=True)
            self.assertFalse(port.flow.diagnostics()["download_active"])
            target.finish_update_download.assert_called_once()

    def test_real_update_port_download_outcomes_and_task_identity(self):
        import sf_update
        import stock_float_window as window_module
        controller = mock.Mock()
        target = mock.Mock()
        target.update_controller.return_value = controller
        update = SimpleNamespace(version="1.2.3", archive_sha256="a" * 64)
        token = object()
        with tempfile.TemporaryDirectory(prefix="sf-real-port-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root):
            port = window_module.make_update_flow_port(target)
            self.assertIsNotNone(port.cache)
            self.assertIsNotNone(port.flow)
            port.start_download(update, prompt_when_ready=True, show_progress=False, token=token)
            event = controller.download.call_args.args[1]
            target.begin_update_download.assert_called_once_with(event, True, False, token)
            port.cancel_download(update, token=token)
            controller.cancel_download.assert_called_once_with(update, token=token)
            port.show_update_message("title", "body")
            target.show_update_message.assert_called_once_with("title", "body")
            port.install_update(update, "archive")
            target.install_update.assert_called_once_with(update, "archive")
            port.finish_update_check(True)
            target.finish_update_check.assert_called_once_with(True, consume_abort=True)
            for error, outcome, fatal in ((None, "ok", False),
                                           (sf_update.UpdateCancelled("cancel"), "cancelled", False),
                                           (sf_update.UpdateFatal("disk"), "failed", True)):
                target.finish_update_download.side_effect = lambda item, archive, exc, record, task: record(item, archive, exc, True)
                self.assertFalse(port.on_download_finished(update, "archive", error, token))
                details = target.record_update_download.call_args.args[0]
                self.assertEqual(details["outcome"], outcome)
                self.assertEqual(details["fatal"], fatal)
            port.stop()
            controller.stop.assert_called_once_with()

    def test_real_port_local_identity_reads_bundle_and_falls_back_safely(self):
        import plistlib
        import stock_float_window as window_module
        with tempfile.TemporaryDirectory(prefix="sf-bundle-port-") as root:
            folder = Path(root)
            executable = folder / "Contents" / "MacOS" / "Skyrocket"
            executable.parent.mkdir(parents=True)
            plist = executable.parent.parent / "Info.plist"
            target = mock.Mock()
            with mock.patch.object(window_module, "UPDATE_STAGING_DIR", str(folder / "cache")), \
                    mock.patch.object(window_module.sys, "executable", str(executable)), \
                    mock.patch.object(window_module, "_bundle_version", return_value="bundle-fixture"):
                port = window_module.make_update_flow_port(target)
                self.assertEqual(port.get_local_version(), "bundle-fixture")
                self.assertEqual(port.get_local_sequence(), window_module.RELEASE_SEQUENCE)
                for value in (True, 0, "invalid"):
                    plist.write_bytes(plistlib.dumps({"SkyrocketReleaseSequence": value}))
                    self.assertEqual(port.get_local_sequence(), window_module.RELEASE_SEQUENCE)
                plist.write_bytes(plistlib.dumps({"SkyrocketReleaseSequence": 42}))
                self.assertEqual(port.get_local_sequence(), 42)
                plist.write_bytes(b"not a plist")
                self.assertEqual(port.get_local_sequence(), window_module.RELEASE_SEQUENCE)

    def test_real_port_prompt_actions_remain_bound_to_latest_generation(self):
        import sf_system_alert
        import sf_update
        import stock_float_window as window_module
        from tests.test_force_update_flow import FakeCache
        target = mock.Mock()
        update = sf_update.UpdateInfo("9.9.9", "1. 更新", "https://github.com/o/r/a.zip", 3, "a" * 64,
                                      release_sequence=999, published_at="2026-11-19T00:00:00Z")
        cache = FakeCache()
        with mock.patch.object(window_module, "UpdateCache", return_value=cache), \
                mock.patch.object(sf_system_alert, "show_alert") as alert, \
                mock.patch.object(window_module, "_bundle_version", return_value="1.0.0"):
            port = window_module.make_update_flow_port(target)
            port.flow.on_manifest_verified(update, automatic=False)
            options = alert.call_args.kwargs
            self.assertEqual(options["buttons"], ("开始下载", "退出程序"))
            self.assertFalse(options["dismissible"])
            options["on_result"]("开始下载")
            queued = target.queue_update_message.call_args.args[0]
            self.assertTrue(port.handle_prompt_answer(queued[1]))
            task = target.update_controller.return_value.download.call_args.args[2]
            self.assertTrue(port.accepts_progress(task))
            self.assertFalse(port.accepts_progress(object()))
            self.assertFalse(port.handle_prompt_answer(("退出程序", -1, None)))
            self.assertFalse(port.handle_prompt_answer(("退出程序", port.flow.generation, ("stale", 1, "bad"))))
            cache.ready[update.archive_sha256] = "verified.zip"
            self.assertTrue(port.on_download_finished(update, "verified.zip", None, task))
            ready = alert.call_args.kwargs
            self.assertEqual(ready["buttons"], ("更新并重启应用", "退出程序"))
            self.assertTrue(port.handle_prompt_answer("更新并重启应用"))
            target.install_update.assert_called_once_with(update, "verified.zip")
            self.assertTrue(port.handle_prompt_answer("退出程序"))
            target.quit.assert_called_once_with()
            self.assertFalse(port.handle_prompt_answer("unknown"))
            port.show_download_progress()
            target.show_background_download_progress.assert_called_once_with()
            port.prompt_update(update, kind="offer")
            self.assertEqual(alert.call_args.kwargs["buttons"], ("暂不更新", "立即更新"))
            self.assertTrue(port.handle_prompt_answer("立即更新"))
            port.prompt_update(update, kind="ready")
            self.assertEqual(alert.call_args.kwargs["buttons"], ("暂不更新", "更新并重启应用"))

    def test_real_port_dependency_failures_are_bounded_and_legacy_controller_works(self):
        import sys
        import stock_float_window as window_module
        from tests.test_force_update_flow import FakeCache
        update = SimpleNamespace(version="fixture", archive_sha256="a" * 64)
        target = mock.Mock()
        with mock.patch.object(window_module, "UpdateCache", side_effect=RuntimeError("unavailable")), \
                mock.patch.object(window_module, "log_swallow") as swallowed:
            port = window_module.make_update_flow_port(target)
            self.assertIsNone(port.cache)
            swallowed.assert_called_once()
        downloads, checks = [], []
        legacy = SimpleNamespace(download=lambda item, event: downloads.append((item, event)),
                                 check=lambda manual: checks.append(manual))
        target.update_controller.return_value = legacy
        with mock.patch.object(window_module, "UpdateCache", return_value=FakeCache()), \
                mock.patch.object(window_module, "log_swallow") as swallowed:
            port = window_module.make_update_flow_port(target)
            port.start_download(update, prompt_when_ready=False, show_progress=True)
            self.assertEqual(downloads[0][0], update)
            port.request_check(True)
            self.assertEqual(checks, [True])
            target.finish_update_download.side_effect = lambda item, archive, error, record, token: record(item, archive, error, False)
            with mock.patch.dict(sys.modules, {"sf_update": None}):
                port.on_download_finished(update, None, OSError("offline"))
            self.assertEqual(target.record_update_download.call_args.args[0]["outcome"], "failed")
            self.assertTrue(swallowed.called)
            target.record_update_download.side_effect = RuntimeError("diagnostic sink")
            self.assertFalse(port.on_download_finished(update, None, OSError("offline")))
            self.assertGreaterEqual(swallowed.call_count, 2)

    def test_cancelled_manual_check_does_not_start_update_or_prompt(self):
        import sf_update
        import stock_float_window as window_module
        from tests.test_force_update_flow import FakeCache
        target = mock.Mock()
        target.finish_update_check.side_effect = lambda manual, consume_abort=True: window_module.FloatWindow.finish_update_check(target, manual, consume_abort=consume_abort)
        vars(target).update(_update_check_aborted=True, _update_manual_wait=False,
                            _update_checking=True, _update_transition_pending=True,
                            _update_check_dialog=None, _update_last_check_at=0.0,
                            _update_retry_timer=None, _quitting=False)
        update = sf_update.UpdateInfo("9.9.9", "notes", "https://github.com/x/y", 8, "a" * 64,
                                      release_sequence=999, published_at="2026-11-20T00:00:00Z")
        with mock.patch.object(window_module, "UpdateCache", return_value=FakeCache()):
            port = window_module.make_update_flow_port(target)
            vars(target)["_update_port"] = port
            result = window_module.FloatWindow._handle_update_check(target, True, update, None)  # discipline-ok(R5): actual queued response
        self.assertFalse(result)
        target.set_update_prompt_dialog.assert_not_called()
        target.begin_update_download.assert_not_called()

    def test_modern_success_clears_retry_and_records_latest_result(self):
        import stock_float_window as window_module
        flow = mock.Mock()
        port = SimpleNamespace(accepts_check=mock.Mock(return_value=True),
                               finish_update_check=mock.Mock(return_value=False), _flow=flow)
        timer = mock.Mock()
        target = SimpleNamespace(_update_port=port, _update_check_aborted=False,
                                 _update_failure_count=2, _update_retry_timer=timer,
                                 _update_last_check_at=0.0, _quitting=False)
        update = SimpleNamespace(release_sequence=3, version="fixture", stage="beta")
        window_module.FloatWindow._handle_update_check(target, False, update, None)  # discipline-ok(R5): actual queued response
        self.assertEqual(target._update_failure_count, 0)  # discipline-ok(R5): diagnostic state
        timer.stop.assert_called_once_with()
        self.assertEqual(target._update_last_result["outcome"], "new")  # discipline-ok(R5): diagnostic state
        self.assertEqual(target._update_last_result["remote_version"], "fixture")  # discipline-ok(R5): diagnostic state

    def test_beta_stage_is_visible_in_all_modern_prompts(self):
        import sf_update
        import sf_system_alert
        import stock_float_window as window_module
        from tests.test_force_update_flow import FakeCache
        update = sf_update.UpdateInfo("9.9.9", "notes", "https://github.com/x/y", 8, "a" * 64,
                                      stage="beta", release_sequence=999, published_at="2026-11-20T00:00:00Z")
        owner = mock.Mock()
        with mock.patch.object(window_module, "UpdateCache", return_value=FakeCache()), \
                mock.patch.object(sf_system_alert, "show_alert") as alert:
            port = window_module.make_update_flow_port(owner)
            for kind in ("offer", "ready", "force_pending", "force_ready"):
                with self.subTest(kind=kind):
                    port.prompt_update(update, kind=kind)
                    self.assertIn("Beta", alert.call_args.kwargs["message"])

    def test_legacy_prompt_answers_cannot_control_later_modern_session(self):
        import sf_system_alert
        import sf_update
        import stock_float_window as window
        from tests.test_force_update_flow import FakeCache

        api = window.FloatWindow
        for button in ("立即更新", "更新并重启应用"):
            with self.subTest(button=button):
                queued, dialogs = [], []
                owner = mock.Mock()
                vars(owner).update(_quitting=False, _update_prompt_dialog=None)
                _peek(owner, "_q_put").side_effect = queued.append
                owner.current_update_prompt_dialog.side_effect = lambda: api.current_update_prompt_dialog(owner)
                owner.set_update_prompt_dialog.side_effect = lambda dialog: api.set_update_prompt_dialog(owner, dialog)
                _peek(owner, "_ready_update_archive").return_value = None
                legacy = sf_update.UpdateInfo("2.0.0", "notes", "https://github.com/x/y", 8, "a" * 64)
                modern = sf_update.UpdateInfo("3.0.0", "notes", "https://github.com/x/y", 8, "b" * 64,
                                              release_sequence=999, published_at="2026-11-20T00:00:00Z")
                def show_alert(**kwargs):
                    dialog = mock.Mock()
                    dialogs.append((dialog, kwargs["on_result"]))
                    return dialog

                with mock.patch.object(window, "UpdateCache", return_value=FakeCache()), \
                        mock.patch.object(sf_system_alert, "show_alert", side_effect=show_alert):
                    port = window.make_update_flow_port(owner)
                    vars(owner)["_update_port"] = port
                    api._prompt_update(owner, legacy)  # discipline-ok(R5): exercise the real legacy callback
                    old_callback = dialogs[0][1]
                    port.flow.on_manifest_verified(modern, automatic=False)
                    modern_dialog = dialogs[-1][0]
                    with mock.patch.object(port.flow, "start_download") as download, \
                            mock.patch.object(port.flow, "install_ready") as install:
                        old_callback(button)
                        api._handle_update_prompt_answer(owner, queued.pop()[1])  # discipline-ok(R5): queued UI entry
                        download.assert_not_called()
                        install.assert_not_called()
                    self.assertIs(owner.current_update_prompt_dialog(), modern_dialog)

    def test_current_legacy_prompt_callback_still_starts_legacy_update(self):
        import sf_system_alert
        import sf_update
        import stock_float_window as window

        api = window.FloatWindow
        queued = []
        update = sf_update.UpdateInfo("2.0.0", "notes", "https://github.com/x/y", 8, "a" * 64)
        owner = mock.Mock()
        vars(owner).update(_quitting=False, _update_port=None, _update_pending=update,
                           _update_prompt_dialog=None)
        _peek(owner, "_q_put").side_effect = queued.append
        _peek(owner, "_ready_update_archive").return_value = None
        with mock.patch.object(sf_system_alert, "show_alert") as alert:
            api._prompt_update(owner, update)  # discipline-ok(R5): real legacy prompt callback
            callback = alert.call_args.kwargs["on_result"]
            callback("立即更新")
            api._handle_update_prompt_answer(owner, queued.pop()[1])  # discipline-ok(R5): real queue entry
        _peek(owner, "_accept_update_prompt").assert_called_once_with(None, update)

    def test_real_window_update_queue_entries_route_only_owned_messages(self):
        import stock_float_window as window_module
        from sf_update_policy import PolicyState
        api = window_module.FloatWindow
        flow = mock.Mock()
        decision = SimpleNamespace(state=PolicyState.FORCE_READY)
        flow.on_first_frame_ready.return_value = decision
        port = SimpleNamespace(accepts_check=mock.Mock(return_value=False),
                               finish_update_check=mock.Mock(return_value=True),
                               handle_prompt_answer=mock.Mock(),
                               accepts_progress=mock.Mock(return_value=False),
                               on_download_finished=mock.Mock(return_value=True), _flow=flow)
        target = SimpleNamespace(_update_port=port)
        update = SimpleNamespace(release_sequence=3)
        self.assertIs(api._notify_update_first_frame(target), decision)  # discipline-ok(R5): actual queued entry under test
        self.assertFalse(api._handle_update_check(target, True, update, None, "old"))  # discipline-ok(R5): reject stale check
        flow.on_manifest_verified.assert_not_called()
        port.accepts_check.return_value = True
        api._handle_update_check(target, False, update, None, "current")  # discipline-ok(R5): modern check entry
        flow.on_manifest_verified.assert_called_once_with(update, automatic=False)
        api._handle_update_prompt_answer(target, "立即更新")  # discipline-ok(R5): actual button queue entry
        port.handle_prompt_answer.assert_called_once_with("立即更新")
        api._cancel_update_download(target)  # discipline-ok(R5): cancellation queue entry
        flow.cancel_download.assert_called_once_with()
        self.assertFalse(api._handle_update_progress(target, 80, 8, 10, "old"))  # discipline-ok(R5): reject stale progress
        self.assertTrue(api._handle_update_download(target, update, "archive", None, "current"))  # discipline-ok(R5): terminal queue entry
        port.on_download_finished.assert_called_once_with(update, "archive", None, "current")

    def test_download_creates_one_cancellation_token_and_one_worker(self):
        host = SimpleNamespace(_update_downloading=False, _quitting=False,
                               _update_progress_pending=(99, 1, 1),
                               reset_update_progress=lambda: setattr(host, "_update_progress_pending", None),
                               _open_download_progress_box=mock.Mock())
        host.set_legacy_update_prompt_when_ready = lambda value: setattr(host, "_update_prompt_when_ready", value)
        host.open_update_download_progress = mock.Mock()
        host.begin_legacy_update_download = lambda event, token: vars(host).update(
            _update_cancel=event, _update_download_token=token)
        update = object()
        self.flow.download(host, update, prompt_when_ready=True, show_progress=False)
        self.flow.download(host, update)
        self.backend.download.assert_called_once_with(update, _peek(host, "_update_cancel"),
                                                      _peek(host, "_update_download_token"))
        _peek(host, "_open_download_progress_box").assert_not_called()
        self.assertTrue(_peek(host, "_update_prompt_when_ready"))
        self.flow.cancel_download(host)
        self.assertTrue(_peek(host, "_update_cancel").is_set())

    def test_completed_download_closes_ui_before_prompting(self):
        order = []
        host = SimpleNamespace(
            _update_prompt_when_ready=True, _update_downloading=True,
            _update_cancel=threading.Event(), _quitting=False,
            close_update_flow=lambda: order.append("closed"),
            _prompt_update=lambda update: order.append("prompt"),
        )
        update = SimpleNamespace(version="future")
        self.flow.downloaded(host, update, "verified.zip", None)
        self.assertEqual(order, ["closed", "prompt"])
        self.assertFalse(_peek(host, "_update_downloading"))
        self.assertIsNone(_peek(host, "_update_cancel"))
        self.assertEqual(_peek(host, "_update_ready_archive"), "verified.zip")

    def test_window_reuses_inflight_check_when_user_requests_manual_result(self):
        import stock_float_window as window_module
        backend = mock.Mock()
        owner = SimpleNamespace(
            _update_port=SimpleNamespace(request_check=mock.Mock()),
            _update_controller=backend, _update_pending=None,
            _threads_stopped=False, _update_checking=True,
            _update_transition_pending=True, _update_manual_wait=False,
            _update_check_aborted=True, _update_downloading=False,
            _update_check_stale=lambda: False, _show_checking_dialog=mock.Mock(),
        )
        window_module.FloatWindow._check_updates_background(owner, manual=True)  # discipline-ok(R5): exercise actual window entry
        self.assertTrue(_peek(owner, "_update_manual_wait"))
        self.assertFalse(_peek(owner, "_update_check_aborted"))
        _peek(owner, "_show_checking_dialog").assert_called_once_with()
        backend.check.assert_not_called()

    def test_legacy_manifest_and_check_errors_use_legacy_result_handler(self):
        import stock_float_window as window_module
        port = SimpleNamespace(accepts_check=mock.Mock(return_value=True),
                               accept_legacy_manifest=mock.Mock(return_value=True),
                               get_local_version=mock.Mock(return_value="1.0.0"),
                               finish_update_check=mock.Mock(), _flow=mock.Mock())
        owner = SimpleNamespace(_update_port=port)
        legacy = SimpleNamespace(version="2.0.0", release_sequence=None)
        error = OSError("unavailable")
        with mock.patch.object(window_module._UPDATE_FLOW, "checked") as checked:  # discipline-ok(R5): verify legacy dispatch at window boundary
            window_module.FloatWindow._handle_update_check(owner, True, legacy, None)  # discipline-ok(R5): exercise queue callback
            checked.assert_called_with(owner, True, legacy, None)
            window_module.FloatWindow._handle_update_check(owner, True, None, error)  # discipline-ok(R5): exercise queue callback
            checked.assert_called_with(owner, True, None, error)
        port.finish_update_check.assert_not_called()
        self.assertEqual(port._flow.clear_verified_session.call_count, 2)  # discipline-ok(R5): verify flow ownership boundary

    def test_stale_download_result_closes_only_its_own_progress(self):
        import stock_float_window as window_module
        old, new = object(), object()
        token_old, token_new = object(), object()
        recorded = []
        closed = []
        owner = SimpleNamespace(_update_download_token=token_old,
                                _update_prompt_when_ready=True,
                                _update_downloading=True, _update_cancel=object(),
                                close_update_progress=lambda: closed.append(True))
        finish = lambda update, archive, error, record, token: window_module.FloatWindow.finish_update_download(  # discipline-ok(R5): exercise real lifecycle boundary
            owner, update, archive, error, record, token)
        port = object.__new__(window_module._UpdateFlowPort)  # discipline-ok(R5): instantiate actual adapter without side effects
        port._flow = SimpleNamespace(accepts_download_terminal=lambda update, token: False)  # discipline-ok(R5): simulate invalidated task
        port._controls = SimpleNamespace(download_finished=finish)  # discipline-ok(R5): route to real window lifecycle
        port._record_download = lambda *args: recorded.append(args)  # discipline-ok(R5): capture diagnostic effect
        self.assertFalse(port.on_download_finished(old, None, RuntimeError("cancelled"), token_old))
        self.assertFalse(_peek(owner, "_update_downloading"))
        self.assertEqual(len(closed), 1)
        vars(owner).update(_update_download_token=token_new, _update_downloading=True)
        self.assertFalse(port.on_download_finished(old, None, RuntimeError("late"), token_old))
        self.assertTrue(_peek(owner, "_update_downloading"))
        self.assertIs(_peek(owner, "_update_download_token"), token_new)
        self.assertEqual(len(closed), 1)

    def test_check_requests_keep_token_and_discard_superseded_reply(self):
        import stock_float_window as window_module
        controller = mock.Mock()
        with tempfile.TemporaryDirectory(prefix="sf-update-port-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root):
            owner = SimpleNamespace(update_controller=lambda: controller)
            port = window_module.make_update_flow_port(owner)
            port.request_check(False)
            first = controller.check.call_args.args[1]
            port.request_check(False)
            second = controller.check.call_args.args[1]
            self.assertIsNot(first, second)
            self.assertFalse(port.accepts_check(first))
            self.assertTrue(port.accepts_check(second))

    def test_window_stale_retry_ignores_the_earlier_check_result(self):
        import stock_float_window as window_module
        controller = mock.Mock()
        with tempfile.TemporaryDirectory(prefix="sf-update-port-") as root, \
                mock.patch.object(window_module, "UPDATE_STAGING_DIR", root):
            owner = SimpleNamespace(update_controller=lambda: controller,
                                    _update_controller=controller, _update_pending=None,
                                    _threads_stopped=False, _update_checking=False,
                                    _update_transition_pending=False, _update_manual_wait=False,
                                    _update_check_aborted=False, _update_downloading=False,
                                    _update_check_stale=lambda: True,
                                    _dispose_update_dialog=mock.Mock(), _update_check_dialog=None)
            port = window_module.make_update_flow_port(owner)
            vars(owner)["_update_port"] = port
            window_module.FloatWindow._check_updates_background(owner)  # discipline-ok(R5): exercise actual window request routing
            first_call = controller.check.call_args.args
            self.assertEqual(len(first_call), 2, "window check must carry a request token")
            first = first_call[1]
            window_module.FloatWindow._check_updates_background(owner)  # discipline-ok(R5): force stale-check retry
            second = controller.check.call_args.args[1]
            self.assertIsNot(first, second)
            self.assertFalse(port.accepts_check(first))
            self.assertTrue(port.accepts_check(second))

    def test_check_completion_does_not_close_existing_update_prompt(self):
        import stock_float_window as window_module
        prompt, checking = mock.Mock(), mock.Mock()
        host = SimpleNamespace(_update_checking=True, _update_transition_pending=True,
                               _update_manual_wait=False, _update_check_aborted=False,
                               _update_check_dialog=checking, _update_prompt_dialog=prompt,
                               _update_check_dialog_token=object(),
                               _dispose_update_dialog=lambda dialog: dialog.close(),
                               close_update_flow=mock.Mock())
        window_module.FloatWindow.finish_update_check(host, False)
        checking.close.assert_called_once_with()
        prompt.close.assert_not_called()
        host.close_update_flow.assert_not_called()

    def test_download_completion_preserves_pending_force_prompt(self):
        import stock_float_window as window_module
        prompt = mock.Mock()
        progress = mock.Mock()
        host = SimpleNamespace(_update_download_token=None, _update_prompt_when_ready=False,
                               _update_downloading=True, _update_cancel=object(),
                               _update_prompt_dialog=prompt, _update_flow_box=progress,
                               _update_flow_progress=None, _update_flow_cancel=None,
                               close_update_progress=None,
                               _close_update_flow_box=mock.Mock())
        host.close_update_progress = lambda: window_module.FloatWindow.close_update_progress(host)
        record = mock.Mock()
        window_module.FloatWindow.finish_update_download(host, object(), None, RuntimeError("offline"), record)
        progress.close.assert_called_once_with()
        prompt.close.assert_not_called()
        _peek(host, "_close_update_flow_box").assert_not_called()  # discipline-ok(R5): ensure broad close is avoided

    def test_legacy_response_clears_previous_forced_identity_before_routing(self):
        import stock_float_window as window_module
        flow = mock.Mock()
        port = SimpleNamespace(accepts_check=mock.Mock(return_value=True),
                               accept_legacy_manifest=mock.Mock(return_value=True),
                               get_local_version=mock.Mock(return_value="1.0.0"), _flow=flow)
        owner = SimpleNamespace(_update_port=port)
        legacy = SimpleNamespace(version="2.0.0", release_sequence=None)
        with mock.patch.object(window_module._UPDATE_FLOW, "checked"):  # discipline-ok(R5): observe legacy dispatch boundary
            window_module.FloatWindow._handle_update_check(owner, True, legacy, None)  # discipline-ok(R5): exercise actual queued callback
        flow.clear_verified_session.assert_called_once_with()

    def test_prompt_rejects_malformed_or_stopped_callbacks(self):
        import stock_float_window as window_module
        port = object.__new__(window_module._UpdateFlowPort)  # discipline-ok(R5): exercise actual port without UI
        port._controls = SimpleNamespace(quit=mock.Mock())  # discipline-ok(R5): isolate quit
        port._flow = SimpleNamespace(generation=1, is_current=lambda **kw: True)  # discipline-ok(R5): isolate session
        self.assertFalse(port.handle_prompt_answer(("退出程序",)))
        port._stopped = True  # discipline-ok(R5): exercise stopped lifecycle
        self.assertFalse(port.handle_prompt_answer(("退出程序", 1, "same")))
        port._controls.quit.assert_not_called()  # discipline-ok(R5): verify no quit

    def test_replaced_force_prompt_discards_queued_exit(self):
        import sf_system_alert
        from sf_update import UpdateInfo
        import stock_float_window as window_module
        update = UpdateInfo("2.0.0", "1. 更新", "https://github.com/o/r/a.zip", 1, "a" * 64)
        queued = []
        controls = SimpleNamespace(dispose_dialog=mock.Mock(), current_prompt_dialog=mock.Mock(return_value=None),
                                   anchor_dialog=mock.Mock(), set_prompt_dialog=mock.Mock(),
                                   queue=queued.append, quit=mock.Mock())
        port = object.__new__(window_module._UpdateFlowPort)  # discipline-ok(R5): exercise actual port without application side effects
        port._controls = controls  # discipline-ok(R5): isolate native UI and quit
        port._flow = SimpleNamespace(generation=1, is_current=lambda **kw: True)  # discipline-ok(R5): same session across replacement
        with mock.patch.object(sf_system_alert, "show_alert") as alert:
            port.prompt_update(update, kind="force_pending", generation=1, identity="same")
            old = alert.call_args.kwargs["on_result"]
            old("退出程序")
            port.prompt_update(update, kind="force_ready", generation=1, identity="same")
        self.assertFalse(port.handle_prompt_answer(queued.pop()[1]))
        controls.quit.assert_not_called()

    def test_prompt_answers_are_single_use_and_close_invalidates_them(self):
        import sf_system_alert
        from sf_update import UpdateInfo
        import stock_float_window as window_module
        update = UpdateInfo("2.0.0", "1. 更新", "https://github.com/o/r/a.zip", 1, "a" * 64)
        queued = []
        controls = SimpleNamespace(dispose_dialog=mock.Mock(), current_prompt_dialog=mock.Mock(return_value=None),
                                   anchor_dialog=mock.Mock(), set_prompt_dialog=mock.Mock(),
                                   queue=queued.append, quit=mock.Mock())
        port = object.__new__(window_module._UpdateFlowPort)  # discipline-ok(R5): actual port, isolated UI
        port._controls = controls  # discipline-ok(R5): inject UI boundary
        flow = SimpleNamespace(generation=1, is_current=lambda **kw: True,
                               start_download=mock.Mock(return_value=True), install_ready=mock.Mock(return_value=True),
                               cancel_download=mock.Mock())
        port._flow = flow  # discipline-ok(R5): exercise port's delivery contract
        with mock.patch.object(sf_system_alert, "show_alert") as alert:
            port.prompt_update(update, kind="pending", generation=1, identity="same")
            callback = alert.call_args.kwargs["on_result"]
            callback("立即更新")
            callback("立即更新")
            self.assertTrue(port.handle_prompt_answer(queued.pop(0)[1]))
            self.assertFalse(port.handle_prompt_answer(queued.pop(0)[1]))
            flow.start_download.assert_called_once()
            port.prompt_update(update, kind="ready", generation=1, identity="same")
            alert.call_args.kwargs["on_result"]("暂不更新")
            self.assertFalse(port.handle_prompt_answer(queued.pop()[1]))
            port.prompt_update(update, kind="force_ready", generation=1, identity="same")
            alert.call_args.kwargs["on_result"]("更新并重启应用")
            self.assertTrue(port.handle_prompt_answer(queued.pop()[1]))
            flow.install_ready.assert_called_once()
            port.prompt_update(update, kind="force_pending", generation=1, identity="same")
            alert.call_args.kwargs["on_result"]("退出程序")
            host = SimpleNamespace(_update_prompt_dialog=object(), _update_port=port,
                                   _update_check_dialog=None, _update_message_dialog=None,
                                   close_update_progress=mock.Mock())
            window_module.FloatWindow._close_update_flow_box(host)  # discipline-ok(R5): real lifecycle close
            self.assertFalse(port.handle_prompt_answer(queued.pop()[1]))
            controls.quit.assert_not_called()
        self.assertTrue(port.handle_prompt_answer(("退出程序", 1, "same")))
        controls.quit.assert_called_once()

    def test_rollback_prompt_describes_downgrade_and_requires_confirmation(self):
        import sf_system_alert
        from sf_update import UpdateInfo
        import stock_float_window as window_module
        update = UpdateInfo("1.0.0", "1. 回退", "https://github.com/o/r/a.zip", 1, "a" * 64,
                            release_sequence=1, published_at="2026-11-19T00:00:00Z")
        controls = SimpleNamespace(dispose_dialog=mock.Mock(), current_prompt_dialog=mock.Mock(return_value=None),
                                   anchor_dialog=mock.Mock(), set_prompt_dialog=mock.Mock(), queue=mock.Mock())
        port = object.__new__(window_module._UpdateFlowPort)  # discipline-ok(R5): construct real adapter without app side effects
        port._controls = controls  # discipline-ok(R5): inject UI boundary
        with mock.patch.object(sf_system_alert, "show_alert") as alert:
            port.prompt_update(update, kind="rollback")
        options = alert.call_args.kwargs
        self.assertIn("回退", options["message"])
        self.assertNotIn("新版本", options["message"])
        self.assertEqual(options["buttons"], ("暂不回退", "回退到此版本"))

    def test_shutdown_result_closes_ui_without_prompt_or_install(self):
        host = SimpleNamespace(_update_prompt_when_ready=True, _quitting=True,
                               close_update_flow=mock.Mock(),
                               _prompt_update=mock.Mock(), _launch_downloaded_update=mock.Mock())
        self.flow.downloaded(host, object(), "verified.zip", None)
        host.close_update_flow.assert_called_once_with()
        _peek(host, "_prompt_update").assert_not_called()
        _peek(host, "_launch_downloaded_update").assert_not_called()


class FaceGuardPortTests(SFCase):
    def test_quitting_host_never_creates_or_starts_a_session(self):
        monitor = mock.Mock(stop=mock.Mock(return_value=True))
        controller = FaceGuardController()
        controller.monitor = monitor
        port = SimpleNamespace(snapshot=lambda: {"available": True, "preference": True,
                                                 "quitting": True, "visible": True,
                                                 "paused": False},
                               set_state=mock.Mock(), stop_timer=mock.Mock(), new_monitor=mock.Mock())
        controller.sync(port, "resume", stop_wait=False)
        monitor.stop.assert_called_once_with(wait=True)
        port.new_monitor.assert_not_called()

    def test_pending_stops_are_isolated_between_controllers(self):
        first, second = FaceGuardController(), FaceGuardController()
        monitor = mock.Mock()
        first.pending_stop.append(monitor)
        self.assertEqual(second.pending_stop, [])
        first.drain_pending(SimpleNamespace(), "quit")
        monitor.stop.assert_called_once_with(wait=True)
        self.assertEqual(first.pending_stop, [])


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
