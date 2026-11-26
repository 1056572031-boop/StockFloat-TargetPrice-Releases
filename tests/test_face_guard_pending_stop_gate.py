"""Regression coverage for the face-guard pending-stop restart gate."""

from types import SimpleNamespace
from unittest import mock

from sf_face_guard_controller import FaceGuardController
from tests._base import SFCase


def _peek(host, name):
    return getattr(host, name)


class FaceGuardPendingStopGateTests(SFCase):
    def test_public_port_exposes_required_host_operations(self):
        from sf_face_guard_controller import FaceGuardPort

        port = FaceGuardPort()
        for operation in (port.snapshot, port.ensure_arbiter, port.new_monitor,
                          port.start_timer, port.stop_timer,
                          lambda: port.set_state("last_stop", {})):
            with self.subTest(operation=operation), self.assertRaises(NotImplementedError):
                operation()

    def test_window_port_uses_callbacks_for_snapshot_timer_and_monitor(self):
        from stock_float_window import _WindowFaceGuardPort

        snapshot = mock.Mock(return_value={"visible": True})
        new_monitor = mock.Mock()
        start_timer, stop_timer, set_state = mock.Mock(), mock.Mock(), mock.Mock()
        port = _WindowFaceGuardPort(snapshot, new_monitor, start_timer, stop_timer, set_state,
                                   mock.Mock())
        self.assertEqual(port.snapshot(), {"visible": True})
        self.assertIs(port.new_monitor(), new_monitor.return_value)
        port.start_timer()
        port.stop_timer()
        port.set_state("last_stop", {"reason": "test"})
        snapshot.assert_called_once_with()
        new_monitor.assert_called_once_with()
        start_timer.assert_called_once_with()
        stop_timer.assert_called_once_with()
        set_state.assert_called_once_with("last_stop", {"reason": "test"})

    def test_window_port_ensures_one_controller_owned_arbiter(self):
        from stock_float_window import _WindowFaceGuardPort

        controller = FaceGuardController()
        def ensure():
            if controller.arbiter is None:
                controller.arbiter = object()
            return controller.arbiter
        port = _WindowFaceGuardPort(mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(),
                                    mock.Mock(), ensure)
        first = port.ensure_arbiter()
        self.assertIs(first, controller.arbiter)
        self.assertIs(port.ensure_arbiter(), first)

    def test_port_paused_and_quitting_never_start_monitor(self):
        for paused, quitting, expected_wait in ((True, False, False), (False, True, True)):
            with self.subTest(paused=paused, quitting=quitting):
                controller = FaceGuardController()
                monitor = mock.Mock(stop=mock.Mock(return_value=True))
                controller.monitor = monitor
                port = SimpleNamespace(
                    snapshot=lambda: {"available": True, "preference": True,
                                      "quitting": quitting, "visible": True, "paused": paused},
                    set_state=mock.Mock(), stop_timer=mock.Mock(), new_monitor=mock.Mock())
                controller.sync(port, "", stop_wait=False)
                monitor.stop.assert_called_once_with(wait=expected_wait)
                port.new_monitor.assert_not_called()
                port.stop_timer.assert_called_once_with()
                self.assertIsNone(controller.monitor)

    def test_stop_keeps_cleanup_when_port_callbacks_fail(self):
        controller = FaceGuardController()
        monitor = mock.Mock(stop=mock.Mock(return_value=True))
        controller.monitor = monitor
        port = SimpleNamespace(set_state=mock.Mock(side_effect=RuntimeError("state unavailable")),
                               stop_timer=mock.Mock(side_effect=RuntimeError("timer unavailable")))
        with mock.patch("sf_face_guard_controller.log_swallow") as logged:
            controller.stop(port, "off", wait=False)
        monitor.stop.assert_called_once_with(wait=False)
        self.assertIsNone(controller.monitor)
        self.assertEqual(controller.diagnostics()["last_stop"]["reason"], "off")
        self.assertEqual(logged.call_count, 2)

    def test_port_stop_does_not_reenter_window_stop(self):
        controller = FaceGuardController()
        monitor = mock.Mock(stop=mock.Mock(return_value=True))
        controller.monitor = monitor
        port = SimpleNamespace(stop_timer=mock.Mock(), set_state=mock.Mock())
        controller.stop(port, "off", wait=False)
        monitor.stop.assert_called_once_with(wait=False)
        port.stop_timer.assert_called_once_with()
        port.set_state.assert_called_once_with("last_stop", {"reason": "off", "at": mock.ANY})

    def test_nonblocking_stop_retry_keeps_monitor_after_exception(self):
        controller = FaceGuardController()
        monitor = mock.Mock(stop=mock.Mock(side_effect=RuntimeError("retry failed")))
        controller.pending_stop = [monitor]
        port = SimpleNamespace(set_state=mock.Mock(), start_timer=mock.Mock(), stop_timer=mock.Mock())
        controller.stop(port, "off", wait=False)
        monitor.stop.assert_called_once_with(wait=False)
        self.assertEqual(controller.pending_stop, [monitor])
        port.start_timer.assert_called_once_with()

    def test_stop_exception_preserves_old_monitor_and_blocks_restart(self):
        controller = FaceGuardController()
        old_monitor = mock.Mock()
        old_monitor.stop.side_effect = [RuntimeError("stop failed"), False, True]
        replacement = mock.Mock()
        controller.monitor = old_monitor
        port = SimpleNamespace(
            snapshot=lambda: {"available": True, "preference": True, "quitting": False,
                              "visible": True, "paused": False},
            set_state=mock.Mock(), stop_timer=mock.Mock(), start_timer=mock.Mock(),
            new_monitor=mock.Mock(return_value=replacement))

        controller.stop(port, "off", wait=False)
        controller.sync(port, "restore", stop_wait=False)

        port.new_monitor.assert_not_called()
        replacement.request_start.assert_not_called()
        self.assertEqual(controller.pending_stop, [old_monitor])
        controller.sync(port, "retry", stop_wait=False)
        port.new_monitor.assert_called_once_with()
        replacement.request_start.assert_called_once_with()

    def test_pending_stop_error_keeps_monitor_and_survives_timer_error(self):
        old_monitor = mock.Mock()
        old_monitor.stop.side_effect = RuntimeError("stop failed")
        port = SimpleNamespace(
            snapshot=lambda: {"available": True, "preference": True, "quitting": False,
                              "visible": True, "paused": False},
            start_timer=mock.Mock(side_effect=RuntimeError("timer unavailable")),
            new_monitor=mock.Mock())
        controller = FaceGuardController()
        controller.pending_stop = [old_monitor]

        controller.sync(port, "retry", stop_wait=False)

        self.assertEqual(controller.pending_stop, [old_monitor])
        port.new_monitor.assert_not_called()

    def test_drain_pending_retains_timeout_failed_and_exception_monitors(self):
        controller = FaceGuardController()
        timed_out = mock.Mock()
        failed = mock.Mock(stop=mock.Mock(return_value=False))
        errored = mock.Mock(stop=mock.Mock(side_effect=RuntimeError("stop failed")))
        controller.pending_stop = [timed_out, failed, errored]
        with mock.patch("sf_face_guard_controller.time.monotonic",
                        side_effect=(0.0, 1.0, 1.0, 1.0)):
            controller.drain_pending(SimpleNamespace(), "quit")
        self.assertEqual(controller.pending_stop, [timed_out, failed, errored])
        failed.stop.assert_not_called()
        errored.stop.assert_not_called()

    def test_drain_keeps_later_monitors_after_first_stop_exhausts_budget(self):
        controller = FaceGuardController()
        first = mock.Mock(stop=mock.Mock(return_value=True))
        later = [mock.Mock(), mock.Mock()]
        controller.pending_stop = [first, *later]
        with mock.patch("sf_face_guard_controller.time.monotonic",
                        side_effect=(0.0, 0.0, 1.0, 1.0)):
            controller.drain_pending(SimpleNamespace(), "quit")
        first.stop.assert_called_once_with(wait=True)
        for monitor in later:
            monitor.stop.assert_not_called()
        self.assertEqual(controller.pending_stop, later)

    def test_drain_pending_retains_stop_failure(self):
        controller = FaceGuardController()
        monitor = mock.Mock(stop=mock.Mock(return_value=False))
        controller.pending_stop = [monitor]
        with mock.patch("sf_face_guard_controller.time.monotonic", side_effect=(0.0, 0.0)):
            controller.drain_pending(SimpleNamespace(), "quit")
        self.assertEqual(controller.pending_stop, [monitor])
        monitor.stop.assert_called_once_with(wait=True)

    def test_drain_pending_retains_stop_exception(self):
        controller = FaceGuardController()
        monitor = mock.Mock(stop=mock.Mock(side_effect=RuntimeError("stop failed")))
        controller.pending_stop = [monitor]
        with mock.patch("sf_face_guard_controller.time.monotonic", side_effect=(0.0, 0.0)):
            controller.drain_pending(SimpleNamespace(), "quit")
        self.assertEqual(controller.pending_stop, [monitor])
        monitor.stop.assert_called_once_with(wait=True)

    def test_pending_stop_blocks_new_monitor_until_old_stop_finishes(self):
        old_monitor = mock.Mock()
        old_monitor.stop.side_effect = [False, True]
        new_monitor = mock.Mock()
        timer = mock.Mock()
        controller = FaceGuardController()
        controller.pending_stop = [old_monitor]
        port = SimpleNamespace(
            snapshot=lambda: {"available": True, "preference": True, "quitting": False,
                              "visible": True, "paused": False},
            new_monitor=mock.Mock(return_value=new_monitor), start_timer=timer.start)

        controller.sync(port, "restore", stop_wait=False)

        port.new_monitor.assert_not_called()
        new_monitor.request_start.assert_not_called()
        timer.start.assert_called_once_with()
        self.assertEqual(controller.pending_stop, [old_monitor])

        controller.sync(port, "restore retry", stop_wait=False)

        port.new_monitor.assert_called_once_with()
        new_monitor.request_start.assert_called_once_with()
        self.assertEqual(controller.pending_stop, [])
