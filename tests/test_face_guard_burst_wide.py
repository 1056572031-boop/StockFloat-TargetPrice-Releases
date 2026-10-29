# -*- coding: utf-8 -*-
"""自动隐藏守卫：恢复驻留、逐帧复检与广角取景回归。"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from _base import SFCase
except ImportError:
    from tests._base import SFCase

import sf_constants
import sf_presence
from sf_presence import (EVENT_HIDE, EVENT_NONE, EVENT_RESTORE,
                         FacePresenceMonitor, PresenceArbiter, STATE_RUNNING)


class _FrameBox:
    def __init__(self, x, width, confidence=0.85):
        self.x = x
        self.width = width
        self.confidence_value = confidence

    def confidence(self):
        return self.confidence_value

    def boundingBox(self):
        return ((self.x, 0.25), (self.width, 0.30))


class _VisionRequest:
    def __init__(self, owner):
        self.owner = owner

    def alloc(self):
        return self

    def init(self):
        return self

    def results(self):
        return list(self.owner.observations)


class _VisionHandler:
    def alloc(self):
        return self

    def initWithCMSampleBuffer_options_(self, sample_buffer, options):
        return self

    def performRequests_error_(self, requests, error):
        return True


class _VisionHarness:
    """只提供产品所需的两个 Vision 类，避免夹具隐含人体检测能力。"""

    def __init__(self, observations):
        self.observations = observations

    def cls(self):
        return {
            "VNDetectFaceRectanglesRequest": _VisionRequest(self),
            "VNImageRequestHandler": _VisionHandler(),
        }


class _Clock:
    def __init__(self, value=100.0):
        self.stamp = value

    def monotonic(self):
        return self.stamp


class _ScriptedZoomDevice:
    def __init__(self, before=1.5, set_mode="apply", lock_result=True,
                 read_error=False):
        self.zoom = before
        self.set_mode = set_mode
        self.lock_result = lock_result
        self.read_error = read_error
        self.calls = []

    def videoZoomFactor(self):
        self.calls.append("read")
        if self.read_error:
            raise RuntimeError("zoom read failed")
        return self.zoom

    def lockForConfiguration_(self, error):
        self.calls.append("lock")
        if isinstance(self.lock_result, BaseException):
            raise self.lock_result
        return self.lock_result

    def setVideoZoomFactor_(self, zoom):
        self.calls.append("set")
        if self.set_mode == "raise":
            raise RuntimeError("zoom set failed")
        if self.set_mode == "apply":
            self.zoom = zoom

    def unlockForConfiguration(self):
        self.calls.append("unlock")


class _RecordingSession:
    def __init__(self):
        self.calls = []

    def alloc(self):
        return self

    def init(self):
        return self

    def canSetSessionPreset_(self, preset):
        return True

    def setSessionPreset_(self, preset):
        pass

    def canAddInput_(self, value):
        return True

    def addInput_(self, value):
        self.calls.append("addInput")

    def canAddOutput_(self, value):
        return True

    def addOutput_(self, value):
        self.calls.append("addOutput")

    def startRunning(self):
        self.calls.append("startRunning")


class _RecordingOutput:
    def alloc(self):
        return self

    def init(self):
        return self

    def setAlwaysDiscardsLateVideoFrames_(self, value):
        pass

    def setSampleBufferDelegate_queue_(self, delegate, queue):
        pass


class _FakeNSObject:
    @classmethod
    def new(cls):
        return cls()


class TestRestoreDwell(SFCase):
    def test_restore_dwell_is_independent_from_hide_dwell(self):
        arbiter = PresenceArbiter(dwell_sec=0.0, restore_dwell_sec=0.38,
                                   hide_hits=1, restore_hits=1)
        self.assertEqual(arbiter.feed(2, now=10.0), EVENT_HIDE)
        self.assertEqual(arbiter.feed(1, now=10.0), EVENT_NONE)
        self.assertEqual(arbiter.feed(1, now=10.37), EVENT_NONE)
        self.assertEqual(arbiter.feed(1, now=10.38), EVENT_RESTORE)

    def test_default_dwell_constants_keep_restore_more_conservative(self):
        arbiter = PresenceArbiter()
        self.assertEqual(arbiter.dwell_sec, sf_constants.FACE_GUARD_DWELL_SEC)
        self.assertEqual(arbiter.restore_dwell_sec,
                         sf_constants.FACE_GUARD_RESTORE_DWELL_SEC)
        self.assertLess(arbiter.dwell_sec, arbiter.restore_dwell_sec)


class TestBurstRecheck(SFCase):
    FPS = 15.0

    def _monitor(self, clock, observations):
        harness = _VisionHarness(observations)
        monitor = FacePresenceMonitor(tick_sec=sf_constants.FACE_GUARD_TICK_SEC)
        monitor._cls = harness.cls()  # discipline-ok(R5): Vision 回调唯一入口的最小夹具。
        monitor.state = STATE_RUNNING
        monitor._last_detect = 0.0  # discipline-ok(R5): 显式让第一帧进入检测路径。
        return monitor, harness

    def _frame(self, monitor, harness, clock, observations):
        harness.observations = observations
        before = monitor.frames
        monitor._on_frame(None, object(), None)  # discipline-ok(R5): 直接驱动采集回调。
        return monitor.frames != before

    def test_steady_one_face_does_not_open_burst(self):
        clock = _Clock()
        one = [_FrameBox(0.05, 0.30)]
        monitor, harness = self._monitor(clock, one)
        with patch.object(sf_presence, "time", SimpleNamespace(monotonic=clock.monotonic)):
            for _ in range(90):
                self._frame(monitor, harness, clock, one)
                clock.stamp += 1.0 / self.FPS
        self.assertEqual(monitor.burst_frames, 0)
        expected = 90 * (1.0 / self.FPS) / monitor.tick_sec
        self.assertLessEqual(abs(monitor.frames - expected), 2.0)

    def _transition_delay(self, phase, burst_window):
        clock = _Clock(100.0 + phase)
        one = [_FrameBox(0.05, 0.30)]
        two = [_FrameBox(0.05, 0.30), _FrameBox(0.55, 0.30)]
        monitor, harness = self._monitor(clock, one)
        arbiter = PresenceArbiter()
        transition = None
        hidden = None
        with patch.object(sf_presence, "time", SimpleNamespace(monotonic=clock.monotonic)), \
                patch.object(sf_presence, "FACE_GUARD_BURST_WINDOW_SEC", burst_window):
            for frame in range(100):
                observations = one if frame < 5 else two
                detected = self._frame(monitor, harness, clock, observations)
                if detected:
                    count, _ = monitor.snapshot()
                    arbiter.feed(count, now=clock.stamp)
                    if count >= 2 and transition is None:
                        transition = clock.stamp
                    if arbiter.hidden_by_guard and hidden is None:
                        hidden = clock.stamp
                if hidden is not None:
                    break
                clock.stamp += 1.0 / self.FPS
        self.assertIsNotNone(transition)
        self.assertIsNotNone(hidden)
        return hidden - transition

    def test_two_faces_burst_reduces_hide_delay_across_phases(self):
        phases = [i / self.FPS / self.FPS for i in range(15)]
        burst = [self._transition_delay(p, sf_constants.FACE_GUARD_BURST_WINDOW_SEC)
                 for p in phases]
        control = [self._transition_delay(p, 0.0) for p in phases]
        burst_sorted = sorted(burst)
        control_sorted = sorted(control)
        burst_median = burst_sorted[len(burst_sorted) // 2]
        control_median = control_sorted[len(control_sorted) // 2]
        self.assertGreaterEqual(control_median - burst_median,
                                sf_constants.FACE_GUARD_EXPEDITE_SEC)
        self.assertLessEqual(max(burst), sf_constants.FACE_GUARD_DWELL_SEC + 0.35)

    def test_first_two_face_sample_does_not_open_burst(self):
        clock = _Clock()
        two = [_FrameBox(0.05, 0.30), _FrameBox(0.55, 0.30)]
        monitor, harness = self._monitor(clock, two)
        with patch.object(sf_presence, "time", SimpleNamespace(monotonic=clock.monotonic)):
            self.assertTrue(self._frame(monitor, harness, clock, two))
        self.assertEqual(monitor._burst_until, 0.0)  # discipline-ok(R5): 无公开 burst 状态口。

    def test_falling_below_two_closes_burst_immediately(self):
        clock = _Clock()
        one = [_FrameBox(0.05, 0.30)]
        two = one + [_FrameBox(0.55, 0.30)]
        monitor, harness = self._monitor(clock, one)
        with patch.object(sf_presence, "time", SimpleNamespace(monotonic=clock.monotonic)):
            self._frame(monitor, harness, clock, one)
            clock.stamp += monitor.tick_sec
            self._frame(monitor, harness, clock, two)
            self.assertGreater(monitor._burst_until, clock.stamp)  # discipline-ok(R5): 复检窗口唯一状态。
            clock.stamp += monitor.tick_sec
            self._frame(monitor, harness, clock, one)
        self.assertEqual(monitor._burst_until, 0.0)  # discipline-ok(R5): 复检窗口唯一状态。

    def test_burst_budget_caps_extra_detection(self):
        clock = _Clock()
        one = [_FrameBox(0.05, 0.30)]
        two = one + [_FrameBox(0.55, 0.30)]
        monitor, harness = self._monitor(clock, one)
        with patch.object(sf_presence, "time", SimpleNamespace(monotonic=clock.monotonic)):
            for frame in range(60):
                self._frame(monitor, harness, clock, one if frame == 0 else two)
                clock.stamp += 1.0 / self.FPS
        self.assertLessEqual(monitor.burst_frames, sf_constants.FACE_GUARD_BURST_BUDGET)


class TestWideView(SFCase):
    def _monitor(self):
        return FacePresenceMonitor()

    def test_zoom_is_applied_read_back_and_unlocked(self):
        monitor = self._monitor()
        device = _ScriptedZoomDevice(1.5)
        monitor._apply_wide_view(device)  # discipline-ok(R5): 取景放宽的唯一入口，没有公开口，装配设备后只能直调
        self.assertEqual(device.calls[:3], ["read", "lock", "set"])
        self.assertEqual(device.calls[3:], ["unlock", "read"])
        self.assertEqual(monitor.applied_profile["zoom_before"], 1.5)
        self.assertEqual(monitor.applied_profile["zoom_after"], 1.0)
        self.assertTrue(monitor.applied_profile["zoom_applied"])
        self.assertAlmostEqual(monitor.near_w_min,
                               sf_constants.FACE_GUARD_NEAR_FACE_W / 1.5)

    def test_set_failure_still_unlocks_and_fails_open(self):
        monitor = self._monitor()
        device = _ScriptedZoomDevice(1.5, set_mode="raise")
        monitor._apply_wide_view(device)  # discipline-ok(R5): 取景放宽的唯一入口，没有公开口，装配设备后只能直调
        self.assertEqual(device.calls[:3], ["read", "lock", "set"])
        self.assertIn("unlock", device.calls)
        self.assertFalse(monitor.applied_profile["zoom_applied"])
        self.assertEqual(monitor.near_w_min, sf_constants.FACE_GUARD_NEAR_FACE_W)

    def test_zoom_failure_modes_keep_default_gate(self):
        cases = [
            _ScriptedZoomDevice(1.0),
            _ScriptedZoomDevice(1.5, set_mode="ignore"),
            _ScriptedZoomDevice(1.5, lock_result=False),
            _ScriptedZoomDevice(1.5, lock_result=(False, "denied")),
            _ScriptedZoomDevice(1.5, lock_result=RuntimeError("lock failed")),
            _ScriptedZoomDevice(1.5, read_error=True),
        ]
        for device in cases:
            with self.subTest(device=device):
                monitor = self._monitor()
                monitor._apply_wide_view(device)  # discipline-ok(R5): 取景放宽的唯一入口，没有公开口，装配设备后只能直调
                self.assertEqual(monitor.near_w_min, sf_constants.FACE_GUARD_NEAR_FACE_W)
                self.assertFalse(monitor.applied_profile["zoom_applied"])

    def test_large_zoom_respects_minimum_scale(self):
        monitor = self._monitor()
        device = _ScriptedZoomDevice(4.0)
        monitor._apply_wide_view(device)  # discipline-ok(R5): 取景放宽的唯一入口，没有公开口，装配设备后只能直调
        self.assertEqual(monitor.near_w_min,
                         sf_constants.FACE_GUARD_NEAR_FACE_W
                         * sf_constants.FACE_GUARD_NEAR_FACE_SCALE_MIN)

    def test_near_gate_is_passed_into_frame_counting(self):
        clock = _Clock()
        wide = [_FrameBox(0.10, 0.05)]
        normal = [_FrameBox(0.55, 0.30)]
        harness = _VisionHarness(wide + normal)
        monitor = FacePresenceMonitor()
        monitor._cls = harness.cls()  # discipline-ok(R5): 只装配两个 Vision 类。
        monitor.state = STATE_RUNNING
        monitor._last_detect = 0.0  # discipline-ok(R5): 让验证帧进入检测路径。
        device = _ScriptedZoomDevice(1.5)
        with patch.object(sf_presence, "time", SimpleNamespace(monotonic=clock.monotonic)):
            self._frame_monitor(monitor, harness, clock, [normal[0], wide[0]])
            self.assertEqual(monitor.snapshot()[0], 1)
            monitor._apply_wide_view(device)  # discipline-ok(R5): 取景放宽的唯一入口，没有公开口，装配设备后只能直调
            clock.stamp += monitor.tick_sec
            self._frame_monitor(monitor, harness, clock, [normal[0], wide[0]])
        self.assertEqual(monitor.snapshot()[0], 2)

    def _frame_monitor(self, monitor, harness, clock, observations):
        harness.observations = observations
        monitor._on_frame(None, object(), None)  # discipline-ok(R5): 直接验证 near_w_min 接线。

    def test_build_session_applies_wide_view_after_start(self):
        session = _RecordingSession()
        output = _RecordingOutput()
        monitor = self._monitor()
        events = []
        monitor._apply_power_profile = lambda session, device: None  # discipline-ok(R5): 替换成空实现，只为单独观察 _build_session 的调用顺序
        monitor._apply_wide_view = lambda device: events.append("wide")  # discipline-ok(R5): 取景放宽的唯一入口，没有公开口，装配设备后只能直调

        class _DeviceInput:
            @staticmethod
            def deviceInputWithDevice_error_(device, error):
                return ("input", None)

        classes = {
            "AVCaptureSession": session,
            "AVCaptureDeviceInput": _DeviceInput,
            "AVCaptureVideoDataOutput": output,
            "NSObject": _FakeNSObject,
        }
        with patch.object(sf_presence, "_dispatch_queue", return_value="queue"):
            monitor._build_session(classes, object())  # discipline-ok(R5): 建会话的唯一入口，要证的就是 startRunning 之后才放宽取景
        self.assertEqual(session.calls[-1], "startRunning")
        self.assertEqual(events, ["wide"])

    def test_wide_view_exception_never_fails_the_session(self):
        """取景放宽是附加的：它抛任何异常，会话都必须照常建成，并留 SWALLOWED 痕。"""
        session = _RecordingSession()
        output = _RecordingOutput()
        monitor = self._monitor()
        swallowed = []
        self.track_patch(patch.object(sf_presence, "log_swallow",
                                      lambda *a, **k: swallowed.append(a)))

        def boom(device):
            raise RuntimeError("设备炸了")

        monitor._apply_power_profile = lambda session, device: None  # discipline-ok(R5): 替换成空实现，只为单独观察 _build_session
        monitor._apply_wide_view = boom  # discipline-ok(R5): 要证的就是它抛错时 _build_session 的兜底

        class _DeviceInput:
            @staticmethod
            def deviceInputWithDevice_error_(device, error):
                return ("input", None)

        classes = {
            "AVCaptureSession": session,
            "AVCaptureDeviceInput": _DeviceInput,
            "AVCaptureVideoDataOutput": output,
            "NSObject": _FakeNSObject,
        }
        with patch.object(sf_presence, "_dispatch_queue", return_value="queue"):
            monitor._build_session(classes, object())  # discipline-ok(R5): 建会话的唯一入口
        self.assertEqual(session.calls[-1], "startRunning", "放宽取景失败不能影响会话启动")
        self.assertTrue(swallowed, "兜异常必须留 SWALLOWED 痕")


if __name__ == "__main__":
    import unittest
    unittest.main()
