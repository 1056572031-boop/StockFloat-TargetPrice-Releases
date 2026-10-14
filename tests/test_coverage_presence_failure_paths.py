"""Camera failure recovery without real devices, authorization or native sinks.

Public start/stop methods own the fake resources exactly as production owns its
AVFoundation objects. Event ordering and late-frame delivery are observed, not
just collaborator call counts.
"""
from __future__ import annotations

import pathlib
import sys
import threading
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from _base import SFCase
except ImportError:  # pragma: no cover
    from tests._base import SFCase

import sf_presence as presence

apply_profile = presence.FacePresenceMonitor._apply_power_profile  # discipline-ok(R5): standalone device configuration contract
camera_devices = presence._camera_devices  # discipline-ok(R5): discovery fallback under test
deliver_frame = presence.FacePresenceMonitor._on_frame  # discipline-ok(R5): native sample callback entry
TIME = 1000.0


def factory(value):
    return types.SimpleNamespace(alloc=lambda: types.SimpleNamespace(init=lambda: value))


class SinkBase:
    @classmethod
    def new(cls):
        return cls()


class Session:
    def __init__(self, events, fail=None):
        self.events, self.fail = events, fail
        self.running = False

    def event(self, name):
        self.events.append(name)
        if name == self.fail:
            raise RuntimeError(name + " failed")

    def canAddInput_(self, port):
        self.event("check-input")
        return self.fail != "reject-input"

    def addInput_(self, port):
        self.event("add-input")

    def canAddOutput_(self, output):
        self.event("check-output")
        return self.fail != "reject-output"

    def addOutput_(self, output):
        self.event("add-output")

    def startRunning(self):
        self.event("start")
        self.running = True

    def stopRunning(self):
        self.event("stop")
        self.running = False


class Output:
    def __init__(self, events, fail=None):
        self.events, self.fail = events, fail
        self.delegate = None

    def setAlwaysDiscardsLateVideoFrames_(self, enabled):
        self.events.append("discard-late")
        if self.fail == "discard-late":
            raise RuntimeError("discard failed")

    def setSampleBufferDelegate_queue_(self, delegate, queue):
        label = "detach" if delegate is None else "attach"
        self.events.append(label)
        if label == self.fail:
            raise RuntimeError(label + " failed")
        self.delegate = delegate


class Face:
    def confidence(self):
        return 0.9

    def boundingBox(self):
        return ((0.2, 0.2), (0.3, 0.3))


class PresenceFailurePaths(SFCase):
    def setUp(self):
        super().setUp()
        self.monitors = []
        self.events = []
        self.log, self.swallowed, self.throttled = mock.Mock(), mock.Mock(), mock.Mock()
        self.track_patch(mock.patch.object(presence, "log_line", self.log))
        self.track_patch(mock.patch.object(presence, "log_swallow", self.swallowed))
        self.track_patch(mock.patch.object(presence, "log_line_throttled", self.throttled))
        self.track_patch(mock.patch.object(presence.time, "monotonic", return_value=TIME))
        self.track_patch(mock.patch.object(presence, "_dispatch_queue", return_value="capture-queue"))
        self.track_patch(mock.patch.object(presence, "_SINK_CLASS", None))
        self.owners, self.parked = {}, []
        self.track_patch(mock.patch.object(presence, "_SINK_OWNERS", self.owners))
        self.track_patch(mock.patch.object(presence, "_PARKED_SINKS", self.parked))
        self.track_patch(mock.patch.object(presence, "_ACCESS_ASKED_AT", None))
        self.track_patch(mock.patch.object(presence, "_camera_devices", return_value="camera"))
        self.track_patch(mock.patch.object(presence, "camera_access_status", return_value="authorized"))
        self.track_patch(mock.patch.object(presence.FacePresenceMonitor, "_apply_power_profile"))

    def tearDown(self):
        for monitor in self.monitors:
            monitor.stop()
        super().tearDown()

    def monitor(self, session_fail=None, output_fail=None, no_session=False,
                no_output=False, no_input=False):
        session = Session(self.events, session_fail)
        output = Output(self.events, output_fail)
        request = types.SimpleNamespace(results=lambda: [Face(), Face()])
        handler = types.SimpleNamespace(
            initWithCMSampleBuffer_options_=lambda buffer, options: types.SimpleNamespace(
                performRequests_error_=lambda requests, error: (True, None)))
        table = {
            "AVCaptureSession": factory(None if no_session else session),
            "AVCaptureDeviceInput": types.SimpleNamespace(
                deviceInputWithDevice_error_=lambda device, error: (None if no_input else "port", None)),
            "AVCaptureVideoDataOutput": factory(None if no_output else output),
            "NSObject": SinkBase,
            "VNDetectFaceRectanglesRequest": factory(request),
            "VNImageRequestHandler": types.SimpleNamespace(alloc=lambda: handler),
        }
        monitor = presence.FacePresenceMonitor()
        self.monitors.append(monitor)
        self.track_patch(mock.patch.object(presence, "_load_frameworks", return_value=(table, "")))
        return monitor, session, output, table

    def assert_released(self, monitor):
        self.assertEqual(self.owners, {}, "partial sessions must leave no owner registrations")
        self.assertEqual(monitor.snapshot(), (0, 0.0))
        for name in ("_session", "_input", "_output", "_delegate"):
            self.assertIsNone(vars(monitor)[name], name)

    def test_partial_build_failures_release_resources_and_remain_retryable(self):
        failures = ("check-input", "reject-input", "add-input", "check-output",
                    "reject-output", "add-output", "start")
        for fail in failures:
            with self.subTest(stage=fail):
                monitor, session, output, _table = self.monitor(session_fail=fail)
                self.assertEqual(monitor.start(), presence.STATE_FAILED)
                self.assertFalse(session.running)
                self.assertIn("stop", self.events)
                if "attach" in self.events:
                    self.assertLess(self.events.index("detach"), self.events.index("stop"))
                    self.assertIsNone(output.delegate)
                self.assert_released(monitor)
                session.fail = None
                self.events.clear()
                self.assertEqual(monitor.start(), presence.STATE_RUNNING)
                self.assertTrue(session.running)
                monitor.stop()
                self.events.clear()

    def test_missing_session_input_and_output_are_reported_as_failures(self):
        for option in ("no_session", "no_input", "no_output"):
            with self.subTest(option=option):
                monitor, _session, _output, _table = self.monitor(**{option: True})
                self.assertEqual(monitor.start(), presence.STATE_FAILED)
                self.assertTrue(monitor.last_error)
                self.assert_released(monitor)

    def test_output_setup_failure_still_detaches_before_stopping(self):
        monitor, session, output, _table = self.monitor(output_fail="attach")
        self.assertEqual(monitor.start(), presence.STATE_FAILED)
        self.assertFalse(session.running)
        self.assertIn("detach", self.events)
        self.assertIsNone(output.delegate)
        self.assert_released(monitor)

    def test_start_exception_is_named_in_diagnostics_and_logs(self):
        monitor, _session, _output, _table = self.monitor(session_fail="start")
        self.assertEqual(monitor.start(), presence.STATE_FAILED)
        self.assertIn("start failed", monitor.last_error)
        self.assertTrue(any("session_failed" in call.args[0]
                            for call in self.throttled.call_args_list))

    def test_late_frames_after_failed_detach_cannot_resurrect_stopped_monitor(self):
        monitor, session, output, _table = self.monitor()
        self.assertEqual(monitor.start(), presence.STATE_RUNNING)
        sink = output.delegate
        output.fail = "detach"
        self.assertTrue(monitor.stop())
        self.assertFalse(session.running)
        self.assertIn(sink, self.parked)
        sink.captureOutput_didOutputSampleBuffer_fromConnection_(output, object(), None)
        self.assertEqual(monitor.frames, 0)
        self.assertEqual(monitor.snapshot(), (0, 0.0), "late native callbacks must be inert after stop")
        self.assert_released(monitor)

    def test_parked_sink_limit_is_bounded_and_new_sink_has_no_owner(self):
        monitor, _session, output, _table = self.monitor()
        self.assertEqual(monitor.start(), presence.STATE_RUNNING)
        previous = [object() for _ in range(8)]
        self.parked.extend(previous)
        sink = output.delegate
        output.fail = "detach"
        monitor.stop()
        self.assertEqual(len(self.parked), len(previous))
        self.assertNotIn(previous[0], self.parked)
        self.assertIs(self.parked[-1], sink)
        self.assertIsNone(vars(sink).get("_owner"))

    def test_stop_failure_is_logged_but_other_references_are_released(self):
        monitor, _session, _output, _table = self.monitor()
        self.assertEqual(monitor.start(), presence.STATE_RUNNING)
        vars(monitor)["_session"].fail = "stop"
        self.assertTrue(monitor.stop())
        self.assertEqual(monitor.state, presence.STATE_OFF)
        self.assert_released(monitor)
        self.assertTrue(any("_teardown" in call.args[0] for call in self.swallowed.call_args_list))

    def test_failed_worker_start_unlatches_starting_and_can_retry(self):
        monitor, _session, _output, _table = self.monitor()
        worker = types.SimpleNamespace(start=mock.Mock(side_effect=RuntimeError("cannot create thread")))
        with mock.patch.object(presence.threading, "Thread", return_value=worker):
            self.assertEqual(monitor.request_start(), presence.STATE_FAILED)
        self.assertFalse(monitor.is_starting())
        self.assertIn("thread", monitor.last_error)
        self.assertEqual(monitor.start(), presence.STATE_RUNNING)

    def test_failed_deferred_stop_does_not_drop_live_resources_or_raise_into_ui(self):
        monitor, session, _output, _table = self.monitor()
        self.assertEqual(monitor.start(), presence.STATE_RUNNING)
        lock = vars(monitor)["_api_lock"]
        lock.acquire()
        try:
            worker = types.SimpleNamespace(start=mock.Mock(side_effect=RuntimeError("no stop worker")))
            with mock.patch.object(presence.threading, "Thread", return_value=worker):
                self.assertFalse(monitor.stop(wait=False))
            self.assertTrue(session.running)
        finally:
            lock.release()
        self.assertTrue(monitor.stop(wait=True))
        self.assertFalse(session.running)
        self.assertTrue(any("stop" in call.args[0] for call in self.swallowed.call_args_list))

    def test_interactive_stop_retains_ownership_until_background_stop_completes(self):
        monitor, session, _output, _table = self.monitor()
        self.assertEqual(monitor.start(), presence.STATE_RUNNING)
        monitor.stop(wait=False)
        self.assertTrue(monitor.stop(wait=True))
        self.assertFalse(session.running)
        self.assertEqual(monitor.state, presence.STATE_OFF)
        self.assert_released(monitor)

    def test_first_menu_authorization_on_new_monitor_reaches_system_request(self):
        monitor, _session, _output, table = self.monitor()
        request = mock.Mock()
        table["AVCaptureDevice"] = types.SimpleNamespace(
            requestAccessForMediaType_completionHandler_=request)
        with mock.patch.object(presence, "camera_access_status", return_value="undetermined"):
            self.assertEqual(monitor.request_access_once(), "requested")
        request.assert_called_once()
        self.assertEqual(monitor.state, presence.STATE_STARTING)

    def test_permission_failure_is_not_overwritten_as_waiting_and_retry_is_possible(self):
        monitor, _session, _output, table = self.monitor()
        request = mock.Mock(side_effect=RuntimeError("permission bridge failed"))
        table["AVCaptureDevice"] = types.SimpleNamespace(
            requestAccessForMediaType_completionHandler_=request)
        vars(monitor)["_cls"] = table
        with mock.patch.object(presence, "camera_access_status", return_value="undetermined"):
            self.assertEqual(monitor.request_access_once(), presence.STATE_FAILED)
            self.assertEqual(monitor.state, presence.STATE_FAILED)
            self.assertIn("permission bridge failed", monitor.last_error)
            request.side_effect = None
            self.assertEqual(monitor.request_access_once(), "requested")
        self.assertEqual(request.call_count, 2)

    def test_framework_failure_during_first_request_is_visible_not_waiting(self):
        monitor, _session, _output, _table = self.monitor()
        with mock.patch.object(presence, "camera_access_status", return_value="undetermined"), \
             mock.patch.object(presence, "_load_frameworks", return_value=(None, "classes unavailable")):
            self.assertEqual(monitor.request_access_once(), presence.STATE_UNSUPPORTED)
        self.assertIn("classes unavailable", monitor.last_error)

    def test_frame_constructor_failures_count_errors_without_advancing_snapshot(self):
        for missing in ("request", "handler"):
            monitor, _session, _output, table = self.monitor()
            if missing == "request":
                table["VNDetectFaceRectanglesRequest"] = factory(None)
            else:
                table["VNImageRequestHandler"] = types.SimpleNamespace(alloc=lambda: types.SimpleNamespace(
                    initWithCMSampleBuffer_options_=lambda buffer, opts: None))
            vars(monitor)["_cls"] = table
            monitor.state = presence.STATE_RUNNING
            deliver_frame(monitor, None, object(), None)
            self.assertEqual(monitor.frames, 0)
            self.assertEqual(monitor.detect_errors, 1)
            self.assertEqual(monitor.snapshot(), (0, 0.0))

    def test_failed_frame_does_not_replace_last_good_sample(self):
        monitor, _session, _output, table = self.monitor()
        vars(monitor)["_cls"] = table
        monitor.state = presence.STATE_RUNNING
        deliver_frame(monitor, None, object(), None)
        saved = monitor.snapshot()
        with mock.patch.object(presence.time, "monotonic", return_value=TIME + 10):
            deliver_frame(monitor, None, None, None)
        self.assertEqual(monitor.snapshot(), saved)
        self.assertEqual(monitor.frames, 1)
        self.assertEqual(monitor.detect_errors, 1)

    def test_sample_notification_failure_preserves_count_and_is_named(self):
        monitor, _session, _output, table = self.monitor()
        vars(monitor).update(_cls=table, _on_sample=mock.Mock(side_effect=RuntimeError("signal gone")))
        monitor.state = presence.STATE_RUNNING
        deliver_frame(monitor, None, object(), None)
        self.assertEqual(monitor.snapshot(), (2, TIME))
        self.assertEqual(monitor.frames, 1)
        self.assertEqual(monitor.detect_errors, 0)
        self.assertIn("_on_frame.notify", self.swallowed.call_args.args[0])

    def inflight_frame(self, fail):
        monitor, _session, output, table = self.monitor()
        entered, release = threading.Event(), threading.Event()

        def detect(requests, error):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test callback was not released")
            if fail:
                raise RuntimeError("old detection failed")
            return True

        table["VNImageRequestHandler"] = types.SimpleNamespace(alloc=lambda: types.SimpleNamespace(
            initWithCMSampleBuffer_options_=lambda buffer, options: types.SimpleNamespace(
                performRequests_error_=detect)))
        self.assertEqual(monitor.start(), presence.STATE_RUNNING)
        sink = output.delegate
        worker = threading.Thread(target=sink.captureOutput_didOutputSampleBuffer_fromConnection_,
                                  args=(output, object(), None), daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2), "the old session must really be inside detection")
            self.assertTrue(monitor.stop())
            self.assertEqual(monitor.start(), presence.STATE_RUNNING)
            release.set()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(monitor.snapshot(), (0, 0.0))
            self.assertEqual(monitor.frames, 0, "old session success must not reach the restarted session")
            self.assertEqual(monitor.detect_errors, 0, "old session error must not reach the restarted session")
            if fail:
                self.swallowed.assert_not_called()
        finally:
            release.set()
            worker.join(timeout=2)

    def test_inflight_success_cannot_overwrite_restarted_session(self):
        self.inflight_frame(False)

    def test_inflight_failure_cannot_poison_restarted_session(self):
        self.inflight_frame(True)

    def test_inactive_monitor_drops_frame_without_attempting_detection(self):
        monitor, _session, _output, table = self.monitor()
        allocation = mock.Mock(side_effect=AssertionError("off monitor must not detect"))
        table["VNDetectFaceRectanglesRequest"] = types.SimpleNamespace(alloc=allocation)
        vars(monitor)["_cls"] = table
        deliver_frame(monitor, None, object(), None)
        allocation.assert_not_called()
        self.assertEqual(monitor.frames, 0)
        self.assertEqual(monitor.detect_errors, 0)


class DeviceFallbackFailurePaths(SFCase):
    def setUp(self):
        super().setUp()
        self.log, self.swallowed = mock.Mock(), mock.Mock()
        self.track_patch(mock.patch.object(presence, "log_line", self.log))
        self.track_patch(mock.patch.object(presence, "log_swallow", self.swallowed))

    def test_default_device_failure_falls_back_to_typed_enumeration(self):
        device = object()
        api = types.SimpleNamespace(defaultDeviceWithMediaType_=mock.Mock(side_effect=RuntimeError("no default")),
                                    devicesWithMediaType_=mock.Mock(return_value=[device]))
        self.assertIs(camera_devices({"AVCaptureDevice": api}), device)
        self.assertIn("default device", self.log.call_args.args[0])

    def test_typed_enumeration_failure_uses_all_devices(self):
        device = object()
        api = types.SimpleNamespace(defaultDeviceWithMediaType_=lambda media: None,
                                    devicesWithMediaType_=mock.Mock(side_effect=RuntimeError("media unsupported")),
                                    devices=lambda: [device])
        self.assertIs(camera_devices({"AVCaptureDevice": api}), device)

    def test_all_device_routes_failing_returns_no_camera_with_reason(self):
        api = types.SimpleNamespace(defaultDeviceWithMediaType_=lambda media: None,
                                    devicesWithMediaType_=mock.Mock(side_effect=RuntimeError("typed failed")),
                                    devices=mock.Mock(side_effect=RuntimeError("all failed")))
        self.assertIsNone(camera_devices({"AVCaptureDevice": api}))
        self.assertIn("all failed", self.log.call_args.args[0])

    def test_device_lock_exception_is_fail_open_and_does_not_unlock_unowned_lock(self):
        monitor = presence.FacePresenceMonitor()
        session = types.SimpleNamespace(canSetSessionPreset_=lambda preset: False)
        device = types.SimpleNamespace(lockForConfiguration_=mock.Mock(side_effect=RuntimeError("lock failed")),
                                       unlockForConfiguration=mock.Mock())
        apply_profile(monitor, session, device)
        device.unlockForConfiguration.assert_not_called()
        self.assertFalse(monitor.applied_profile["fps_applied"])
        self.assertFalse(monitor.applied_profile["preset_applied"])

    def test_unlock_exception_is_named_after_successful_frame_rate_configuration(self):
        monitor = presence.FacePresenceMonitor()
        session = types.SimpleNamespace(canSetSessionPreset_=lambda preset: True, setSessionPreset_=mock.Mock())
        device = types.SimpleNamespace(lockForConfiguration_=lambda err: (True, None),
                                       setActiveVideoMinFrameDuration_=mock.Mock(),
                                       setActiveVideoMaxFrameDuration_=mock.Mock(),
                                       unlockForConfiguration=mock.Mock(side_effect=RuntimeError("unlock failed")))
        apply_profile(monitor, session, device)
        self.assertTrue(monitor.applied_profile["fps_applied"])
        self.assertIn(".unlock", self.swallowed.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
