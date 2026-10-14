# -*- coding: utf-8 -*-
"""Native boundary failures must not become valid frames or retained camera sessions."""
import ctypes
from types import SimpleNamespace
from unittest import mock

from tests._base import SFCase
import sf_glass
import sf_presence as presence

_frame = presence.FacePresenceMonitor._on_frame  # discipline-ok(R5): frames enter only through AVFoundation callbacks
_join = sf_glass.GlassMixin._force_join_active_space  # discipline-ok(R5): test the native failure backoff boundary


class CameraSessionFailure(SFCase):
    def test_failed_start_detaches_output_before_stopping_partial_session(self):
        calls = []
        session = SimpleNamespace(
            canAddInput_=lambda port: True, addInput_=lambda port: None,
            canAddOutput_=lambda output: True, addOutput_=lambda output: None,
            startRunning=mock.Mock(side_effect=RuntimeError("start failed")),
            stopRunning=lambda: calls.append("stop"))
        output = SimpleNamespace(
            setAlwaysDiscardsLateVideoFrames_=lambda value: None,
            setSampleBufferDelegate_queue_=lambda sink, queue: calls.append(
                "detach" if sink is None else "attach"))
        factory = lambda obj: SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: obj))
        classes = {
            "AVCaptureSession": factory(session),
            "AVCaptureDeviceInput": SimpleNamespace(
                deviceInputWithDevice_error_=lambda device, error: (object(), None)),
            "AVCaptureVideoDataOutput": factory(output),
        }
        monitor = presence.FacePresenceMonitor()
        with mock.patch.object(presence, "_load_frameworks", return_value=(classes, "")), \
             mock.patch.object(presence, "_camera_devices", return_value=object()), \
             mock.patch.object(presence, "camera_access_status", return_value="authorized"), \
             mock.patch.object(presence, "_dispatch_queue", return_value=object()), \
             mock.patch.object(presence.FacePresenceMonitor, "_apply_power_profile"), \
             mock.patch.object(presence.FacePresenceMonitor, "_make_sink", return_value=object()):
            self.assertEqual(monitor.start(), presence.STATE_FAILED)
        self.assertEqual(calls, ["attach", "detach", "stop"])
        self.assertEqual(monitor.snapshot(), (0, 0.0))


class VisionReturnShape(SFCase):
    def test_failed_tuple_does_not_publish_a_successful_frame(self):
        for result in [False, (False, "NSError"), (False, None)]:
            with self.subTest(result=result):
                monitor = presence.FacePresenceMonitor(tick_sec=0)
                # This case tests detection errors, not the stopped-session gate.
                monitor.state = presence.STATE_RUNNING
                monitor._session_gen = 0  # discipline-ok(R5): pin the synthetic session generation
                monitor._last_detect = 0.0  # discipline-ok(R5): make the zero-tick probe independent of prior clock patches
                request = SimpleNamespace(results=lambda: [])
                handler = SimpleNamespace(performRequests_error_=lambda requests, error: result)
                vars(monitor)["_cls"] = {
                    "VNDetectFaceRectanglesRequest": SimpleNamespace(
                        alloc=lambda: SimpleNamespace(init=lambda: request)),
                    "VNImageRequestHandler": SimpleNamespace(
                        alloc=lambda: SimpleNamespace(
                            initWithCMSampleBuffer_options_=lambda buffer, options: handler)),
                }
                with mock.patch.object(presence.time, "monotonic", return_value=1000.0), \
                     mock.patch.object(presence, "log_line_throttled") as logged:
                    _frame(monitor, None, object(), None)
                self.assertEqual(monitor.frames, 0)
                self.assertEqual(monitor.detect_errors, 1)
                self.assertEqual(monitor.snapshot(), (0, 0.0))
                logged.assert_called_once()


class SpaceAllocationFailure(SFCase):
    def test_unavailable_private_api_is_logged_without_breaking_public_window(self):
        host = SimpleNamespace()
        window = SimpleNamespace(windowNumber=lambda: 7)
        with mock.patch.object(sf_glass, "cgs_add_window_to_space", side_effect=RuntimeError("API removed")), \
             mock.patch.object(sf_glass, "log_swallow") as logged:
            _join(host, window)
        logged.assert_called_once()

    def test_partial_cf_allocation_is_released_and_never_passed_to_cgs(self):
        released = []
        add = mock.Mock()
        cg = SimpleNamespace(CGSMainConnectionID=mock.Mock(return_value=7),
                             CGSGetActiveSpace=mock.Mock(return_value=9),
                             CGSAddWindowsToSpaces=add)
        cf = SimpleNamespace(CFNumberCreate=mock.Mock(side_effect=[1, 2]),
                             CFArrayCreate=mock.Mock(side_effect=[3, None]),
                             CFRelease=mock.Mock(side_effect=released.append))
        with mock.patch.object(sf_glass, "_cgs_libs", return_value=(ctypes, cg, cf)):
            with self.assertRaises(MemoryError):
                sf_glass.cgs_add_window_to_space(11)
        add.assert_not_called()
        self.assertCountEqual(released, [1, 2, 3])
