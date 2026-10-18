"""Observable rejection controls for dynamic and compositor measurements."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

from PyQt6.QtGui import QColor, QImage, QPainter

from tests._base import SFCase
from tools import power_profile
from tools.visual_sequence_guard import drag_problems, first_paint_problems, sequence_problems, text_match


def workload():
    return {"mode": "trading", "native": False, "net": "mock", "vary": True, "seconds": 3, "has_quote": True,
            "minute_points": 241, "poll_fail": 0, "poll_state": 0,
            "window_visible": True, "first_frame_painted": True, "probe_valid": True,
            "measurement_counts": {"apply_quote": 4, "anim_tick": 60, "paint": 50}}


def sequence():
    return [{"time": time, "content_visible": True, "size": [184, 224],
             "pair": {"glass_on_top": False, "desync_px": 0}}
            for time in (0, 0.2, 0.4, 0.6, 0.8, 1.0)]


def text_frame(ratio=1):
    image = QImage(184 * ratio, 224 * ratio, QImage.Format.Format_ARGB32)
    image.setDevicePixelRatio(ratio)
    image.fill(QColor(25, 28, 30))
    painter = QPainter(image)
    for x in (20, 35, 50, 65):
        painter.fillRect(x, 80, 7, 16, QColor(235, 235, 235))
    painter.end()
    return image


class DynamicWorkloadTests(SFCase):
    def test_healthy_dynamic_and_idle_workloads_pass_separately(self):
        sample = workload()
        self.assertEqual(power_profile.workload_problems(sample), [])
        sample.update(vary=False, measurement_counts={})
        self.assertEqual(power_profile.workload_problems(sample), [])

    def test_each_missing_dynamic_increment_is_rejected_despite_lifetime_counts(self):
        for name in ("apply_quote", "anim_tick", "paint"):
            sample = workload()
            sample["measurement_counts"][name] = 0
            sample["call_counts"] = {name: 10000}
            self.assertTrue(power_profile.workload_problems(sample), name)

    def test_short_or_unhealthy_workload_cannot_be_called_low_cpu_success(self):
        for field, value in (("seconds", 1), ("seconds", float("nan")), ("has_quote", False), ("minute_points", 0),
                             ("poll_fail", 1), ("poll_state", 1), ("window_visible", False),
                             ("first_frame_painted", False)):
            sample = workload()
            sample[field] = value
            self.assertTrue(power_profile.workload_problems(sample), field)

    def test_real_network_workload_still_requires_a_visible_painted_window(self):
        # G05 复核定：本地健康判据不许只锁 mock 档。真实网络档窗口没显示/首帧没画时，
        # 任何“稳态 CPU”结论都是空转冒充的 —— 这两条就是“改坏必红”的锚。
        for field, value in (("window_visible", False), ("first_frame_painted", False)):
            sample = workload()
            sample.update(net="real", vary=False, measurement_counts={})
            sample[field] = value
            problems = power_profile.workload_problems(sample)
            self.assertTrue(problems, field)
            self.assertTrue(any("real workload has no visible window" in p for p in problems), field)

    def test_real_network_workload_with_local_health_keeps_degraded_network_legal(self):
        # 反向红线：真实网络档的行情失败是合法现场，不许为了“看起来更严”把
        # 报价/分时判据也抬到 real 档 —— 那会把真实降级测量全部错杀。
        sample = workload()
        sample.update(net="real", vary=False, measurement_counts={},
                      has_quote=False, minute_points=0, poll_fail=3, poll_state=1)
        self.assertEqual(power_profile.workload_problems(sample), [])

    def test_parent_rejects_success_exit_with_invalid_or_unqualified_payload(self):
        args = SimpleNamespace(mode="trading", seconds=3, net="mock", vary=True,
                               profile=False, native=False)
        for invalid in (dict(workload(), measurement_counts={}), dict(workload(), probe_valid=False),
                        dict(workload(), vary=False), dict(workload(), net="real")):
            child = SimpleNamespace(returncode=0, stdout=json.dumps(invalid), stderr="")
            with mock.patch.object(power_profile.subprocess, "run", return_value=child), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertIsNone(power_profile.run_child(args))

    def test_parent_accepts_independently_valid_payload(self):
        args = SimpleNamespace(mode="trading", seconds=3, net="mock", vary=True,
                               profile=False, native=False)
        child = SimpleNamespace(returncode=0, stdout=json.dumps(workload()), stderr="")
        with mock.patch.object(power_profile.subprocess, "run", return_value=child):
            self.assertEqual(power_profile.run_child(args), workload())

    def test_parent_accepts_locally_healthy_real_network_payload(self):
        # real 档正向对照：场景绑定一致 + 本地健康成立时必须放行（反向是上一条的 net 篡改拒绝）。
        args = SimpleNamespace(mode="trading", seconds=30, net="real", vary=False,
                               profile=False, native=False)
        payload = workload()
        payload.update(net="real", vary=False, measurement_counts={})
        child = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        with mock.patch.object(power_profile.subprocess, "run", return_value=child):
            self.assertEqual(power_profile.run_child(args), payload)


class MeasurementEntryTests(SFCase):
    def test_cli_rejects_invalid_duration_before_starting_any_probe(self):
        cases = ((["--seconds", value], "finite and positive")
                 for value in ("0", "-1", "nan", "inf"))
        cases = list(cases) + [
            (["--vary", "--seconds", "1", "--net", "mock"], "at least 2 seconds"),
            (["--vary", "--seconds", "3", "--net", "real"], "--net mock"),
        ]
        for argv, message in cases:
            with self.subTest(argv=argv), mock.patch.object(sys, "argv", ["power_profile", *argv]), \
                    mock.patch.object(power_profile, "measure") as measure, \
                    mock.patch.object(power_profile, "run_child") as child, \
                    contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    power_profile.main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(message, error.getvalue())
                measure.assert_not_called()
                child.assert_not_called()

    def test_measurement_counts_exclude_startup_and_control_probe_exit_status(self):
        # Isolate collaborators, not measure()/finish(): no QWidget, network, or real config.
        for moving in (False, True):
            with self.subTest(moving=moving), tempfile.TemporaryDirectory() as directory:
                callbacks = []

                class Timer:
                    def __init__(self):
                        self.timeout = SimpleNamespace(connect=lambda callback: None)

                    def start(self, *args):
                        pass

                    def isActive(self):
                        return False

                    @staticmethod
                    def singleShot(milliseconds, callback):
                        callbacks.append(callback)

                class Render:
                    def paintEvent(self, event):
                        pass

                class Window(Render):
                    def __init__(self):
                        # Internal fields are the actual measurement protocol's inputs.
                        vars(self).update(_anim_timer=Timer(), _poll_state=0, _poll_fail=0,
                                          _sf_first_frame_logged=True, minute_points=[1], quote={"price": 20})

                    def _sleep(self, seconds):
                        pass

                    def _animations_suspended(self):
                        return False

                    def _apply_quote(self):
                        pass

                    def _anim_tick(self):
                        pass

                    def update(self, *args):
                        pass

                    def isVisible(self):
                        return True

                    def show(self):
                        # Startup alone must never qualify as steady-state activity.
                        self.exercise()
                        windows.append(self)

                    def exercise(self):
                        for _ in range(12):
                            self._apply_quote()  # discipline-ok(R5): exercise wrapped measurement collaborators
                            self._anim_tick()  # discipline-ok(R5): exercise wrapped measurement collaborators
                            self.paintEvent(SimpleNamespace(rect=lambda: SimpleNamespace(
                                x=lambda: 0, y=lambda: 0, width=lambda: 10, height=lambda: 10)))

                windows = []

                class App:
                    @staticmethod
                    def instance():
                        return App()

                    def exec(self):
                        if moving:
                            windows[0].exercise()
                        callbacks[0]()

                module = SimpleNamespace(FloatWindow=Window, RenderMixin=Render,
                                         load_bundled_fonts=lambda: None, is_tiny_move=lambda *args: False)
                modules = {"stock_float_window": module, "sf_quote": SimpleNamespace(),
                           "sf_ui_render": SimpleNamespace(is_price_partial_redraw=lambda *args: False),
                           "sf_network": SimpleNamespace(_NET_MONITOR=SimpleNamespace(snapshot=lambda: {})),
                           "PyQt6.QtCore": SimpleNamespace(QTimer=Timer),
                           "PyQt6.QtWidgets": SimpleNamespace(QApplication=App)}
                with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ), \
                        mock.patch.object(sys, "path", list(sys.path)), \
                        mock.patch.object(power_profile.os, "chdir"), \
                        mock.patch.object(power_profile.os.path, "expanduser", return_value=directory + "/absent.json"), \
                        mock.patch.object(power_profile.tempfile, "mkdtemp", return_value=directory), \
                        mock.patch.object(power_profile.time, "monotonic", side_effect=[1000.0, 1003.0]), \
                        mock.patch.object(power_profile.os, "_exit", side_effect=SystemExit) as exit_process, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises(SystemExit):
                        power_profile.measure("trading", 3, "mock", False, vary=True)
                result = json.loads(output.getvalue())
                self.assertEqual(result["probe_valid"], moving)
                self.assertEqual(bool(result["workload_problems"]), not moving)
                for key in ("apply_quote", "anim_tick", "paint"):
                    self.assertEqual(result["measurement_counts"][key], 12 if moving else 0)
                    self.assertEqual(result["call_counts"][key], 24 if moving else 12)
                exit_process.assert_called_once_with(0 if moving else 2)


class VisualSequenceTests(SFCase):
    def test_percent_only_cannot_be_a_valid_price_template(self):
        for scale in (1, 2):
            frame = text_frame(scale)
            frame.fill(QColor(25, 28, 30))
            painter = QPainter(frame)
            for x in (125, 137, 149):
                painter.fillRect(x, 84, 7, 12, QColor(235, 235, 235))
            painter.end()
            self.assertFalse(text_match(frame, frame)["passed"], "percent text is not price digits")

    def test_drag_needs_real_motion_known_pair_and_dense_samples(self):
        clean = [{"time": i * 0.02, "position": [400 + i * 5, 180],
                  "pair": {"glass_on_top": False, "desync_px": 0}} for i in range(16)]
        self.assertEqual(drag_problems(clean), [])
        self.assertTrue(drag_problems([]))
        for field, value in (("pair", None), ("position", [400, 180]), ("time", 100)):
            samples = copy.deepcopy(clean)
            for sample in samples:
                sample[field] = value
            self.assertTrue(drag_problems(samples), field)
        samples = copy.deepcopy(clean)
        samples[3]["pair"]["desync_px"] = 55
        self.assertTrue(drag_problems(samples))

    def test_real_text_template_matches_at_both_scales(self):
        for scale in (1, 2):
            frame = text_frame(scale)
            self.assertTrue(text_match(frame, frame.copy())["passed"])

    def test_blank_white_transparent_and_no_template_are_rejected(self):
        frame = text_frame()
        for color in (QColor(25, 28, 30), QColor("white"), QColor(0, 0, 0, 0)):
            blank = frame.copy()
            blank.fill(color)
            self.assertFalse(text_match(frame, blank)["passed"])
            self.assertFalse(text_match(blank, frame)["passed"])
        self.assertFalse(text_match(frame, QImage())["passed"])
        self.assertFalse(text_match(frame, text_frame(2))["passed"])

    def test_healthy_sequence_and_first_paint_have_positive_controls(self):
        self.assertEqual(sequence_problems(sequence(), startup=True), [])
        self.assertEqual(first_paint_problems([{"size": [184, 224], "has_price": True}]), [])

    def test_one_invisible_inverted_desynced_or_unknown_sample_refuses(self):
        for field, value in (("content_visible", False), ("pair", None),
                             ("pair", {"glass_on_top": True, "desync_px": 0}),
                             ("pair", {"glass_on_top": False, "desync_px": 3}),
                             ("pair", {"glass_on_top": False, "desync_px": float("nan")}),
                             ("size", None)):
            samples = sequence()
            samples[2][field] = value
            self.assertTrue(sequence_problems(samples), (field, value))

    def test_sparse_empty_reversed_or_short_evidence_refuses(self):
        self.assertTrue(sequence_problems([]))
        self.assertTrue(sequence_problems(sequence()[:2]))
        for times in ((0, 0.2, 0.9, 1.1, 1.3, 1.5), (0, 0.2, 0.1, 0.6, 0.8, 1),
                      (0, 0.1, 0.2, 0.3, 0.4, 0.5), (0, 0.2, float("nan"), 0.6, 0.8, 1)):
            samples = sequence()
            for sample, stamp in zip(samples, times):
                sample["time"] = stamp
            self.assertTrue(sequence_problems(samples), times)
        late = sequence()
        for sample in late:
            sample["time"] += 1
        self.assertTrue(sequence_problems(late))
        long_capture = sequence()
        long_capture[2]["capture_seconds"] = 0.9
        self.assertTrue(sequence_problems(long_capture))

    def test_startup_resize_or_placeholder_is_rejected(self):
        samples = sequence()
        samples[0]["size"] = [184, 420]
        self.assertTrue(sequence_problems(samples, startup=True))
        paints = [{"size": [184, 224], "has_price": True}]
        self.assertTrue(first_paint_problems([]))
        for changed in ({"size": [184, 420], "has_price": True},
                        {"size": [184, 224], "has_price": False}):
            self.assertTrue(first_paint_problems(copy.deepcopy(paints) + [changed]))
