"""Capture, encoding and pixel qualification must fail independently."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from tests._base import SFCase
from tools import continuous_frames as cf


def fixture():
    source = {"ready": True, "errors": [], "frames": []}
    decoded = {"decoder_completed": True, "frames": []}
    for i in range(145):
        stamp = 100 + i / 40
        source["frames"].append({"status": 0, "encoded": True, "pts": stamp,
                                 "display_host": stamp, "delivered_host": stamp + 0.01})
        decoded["frames"].append({"movie_pts": i / 40, "visible": True,
                                  "window_x": 40, "window_y": 40})
    phases = [{"name": name, "start": start, "end": end} for name, start, end in (
        ("startup", 100.2, 100.9), ("fullscreen_transition", 100.9, 100.95),
        ("fullscreen", 100.95, 101.65), ("desktop_transition", 101.65, 101.7),
        ("desktop_return", 101.7, 102.4), ("drag", 102.45, 103.2))]
    return source, decoded, phases


class ContinuousFrameRules(SFCase):
    def test_complete_healthy_chain_has_positive_control(self):
        source, decoded, phases = fixture()
        result = cf.analyze(source, decoded, phases)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["decoded_frames"], len(decoded["frames"]))
        self.assertLess(result["phases"]["startup"]["max_source_gap"], 0.03)

    def test_transition_exclusion_cannot_hide_post_transition_blackout(self):
        source, decoded, phases = fixture()
        decoded["frames"][37]["visible"] = False  # Inside Space animation only.
        result = cf.analyze(source, decoded, phases)
        self.assertTrue(result["passed"], result)
        self.assertFalse(result["unqualified_transitions"]["fullscreen_transition"]["qualified"])
        for i in range(40, 45):
            decoded["frames"][i]["visible"] = False
        result = cf.analyze(source, decoded, phases)
        self.assertFalse(result["passed"])
        self.assertFalse(result["phases"]["fullscreen"]["passed"])

    def test_known_idle_retains_only_previously_decoded_pixels(self):
        source, decoded, phases = fixture()
        source["frames"][50].update(status=1, encoded=False)
        decoded["frames"].pop(50)
        self.assertTrue(cf.analyze(source, decoded, phases)["passed"])
        source["frames"][0].update(status=1, encoded=False)
        decoded["frames"].pop(0)
        self.assertFalse(cf.analyze(source, decoded, phases)["passed"])

    def test_idle_does_not_invent_good_pixels_after_a_bad_frame(self):
        source, decoded, _ = fixture()
        decoded["frames"][49]["visible"] = False
        source["frames"][50].update(status=1, encoded=False)
        decoded["frames"].pop(50)
        frames, errors = cf.join_frames(source, decoded)
        self.assertEqual(errors, [])
        self.assertFalse(frames[49]["visible"])
        self.assertFalse(frames[50]["visible"])
        self.assertTrue(frames[51]["visible"])

    def test_missing_video_or_any_decode_loss_refuses(self):
        for field in ("empty", "one_frame", "decoder", "writer", "not_encoded", "ready"):
            source, decoded, phases = fixture()
            if field == "empty":
                decoded["frames"] = []
            elif field == "one_frame":
                decoded["frames"].pop(50)
            elif field == "decoder":
                decoded["decoder_completed"] = False
            elif field == "writer":
                source["errors"].append("backpressure dropped source frame")
            elif field == "not_encoded":
                source["frames"][50]["encoded"] = False
            else:
                source["ready"] = False
            self.assertFalse(cf.analyze(source, decoded, phases)["passed"], field)

    def test_timestamp_mismatch_reordering_and_unknown_status_refuse(self):
        for kind in ("mismatch", "reverse", "nan", "no_host", "delivery", "status"):
            source, decoded, phases = fixture()
            if kind == "mismatch":
                decoded["frames"][50]["movie_pts"] += 0.01
            elif kind == "reverse":
                source["frames"][50]["display_host"] = source["frames"][49]["display_host"]
            elif kind == "nan":
                source["frames"][50]["pts"] = float("nan")
            elif kind == "no_host":
                source["frames"][50].pop("display_host")
            elif kind == "delivery":
                source["frames"][50]["delivered_host"] += 1
            else:
                source["frames"][50]["status"] = 3
            self.assertFalse(cf.analyze(source, decoded, phases)["passed"], kind)

    def test_sparse_source_frames_cannot_hide_video_blackout(self):
        source, decoded, phases = fixture()
        source["frames"] = [f for i, f in enumerate(source["frames"]) if not 45 <= i <= 52]
        decoded["frames"] = [f for i, f in enumerate(decoded["frames"]) if not 45 <= i <= 52]
        report = cf.analyze(source, decoded, phases)
        self.assertFalse(report["passed"])
        self.assertIn("source frame gap", " ".join(report["phases"]["fullscreen"]["errors"]))

    def test_missing_phase_start_end_or_duplicate_phase_refuses(self):
        for kind in ("start", "end", "phase", "duplicate"):
            source, decoded, phases = fixture()
            if kind == "start":
                phases[0]["start"] = 99
            elif kind == "end":
                phases[-1]["end"] = 104
            elif kind == "phase":
                phases.pop()
            else:
                phases.append(copy.deepcopy(phases[-1]))
            self.assertFalse(cf.analyze(source, decoded, phases)["passed"], kind)

    def test_blackout_upper_bound_includes_surrounding_good_frames(self):
        source, decoded, phases = fixture()
        for i in range(45, 50):
            decoded["frames"][i]["visible"] = False
        report = cf.analyze(source, decoded, phases)
        self.assertFalse(report["passed"])
        self.assertAlmostEqual(report["phases"]["fullscreen"]["blank_upper_seconds"], 0.15)
        # One missing frame is not reported as zero milliseconds.
        source, decoded, phases = fixture()
        decoded["frames"][45]["visible"] = False
        report = cf.analyze(source, decoded, phases)
        self.assertAlmostEqual(report["phases"]["fullscreen"]["blank_upper_seconds"], 0.05)

    def test_startup_budget_is_distinct_from_post_start_blackout(self):
        source, decoded, phases = fixture()
        for row, pixels in zip(source["frames"], decoded["frames"]):
            if row["display_host"] < 100.45:
                pixels["visible"] = False
        self.assertTrue(cf.analyze(source, decoded, phases)["passed"])
        for row, pixels in zip(source["frames"], decoded["frames"]):
            if row["display_host"] < 100.8:
                pixels["visible"] = False
        self.assertFalse(cf.analyze(source, decoded, phases)["passed"])

    def test_empty_or_short_phase_never_qualifies(self):
        self.assertFalse(cf.phase_report([], 100, 101)["passed"])
        frames, _ = cf.join_frames(*fixture()[:2])
        self.assertFalse(cf.phase_report(frames, 100, 100.1)["passed"])
        self.assertFalse(cf.phase_report(frames, float("nan"), 101)["passed"])


class ContinuousFrameEvidenceEdges(SFCase):
    def test_unknown_status_is_rejected_even_with_matching_video_count(self):
        source, decoded, _ = fixture()
        source["frames"].insert(20, {"status": 3})
        frames, errors = cf.join_frames(source, decoded)
        self.assertEqual(len(frames), len(decoded["frames"]))
        self.assertIn("source frame status is not complete/idle: 3", errors)

    def test_source_changing_after_count_check_cannot_overrun_decoded_frames(self):
        # A stateful input models a source row changing between the count and join
        # passes. Ordinary immutable JSON cannot reach this defensive branch.
        class ChangingStatus(dict):
            def get(self, key, default=None):
                if key == "status":
                    previous = super().get(key, default)
                    self[key] = 0
                    return previous
                return super().get(key, default)

        source, decoded, _ = fixture()
        source["frames"].append(ChangingStatus(status=1, display_host=104, delivered_host=104.01))
        frames, errors = cf.join_frames(source, decoded)
        self.assertEqual(len(frames), len(decoded["frames"]))
        self.assertIn("unmatched complete frame", errors)

    def test_sparse_phase_reports_observation_floor_and_absent_text(self):
        frames = [{"time": 100 + i / 10, "visible": False} for i in range(11)]
        report = cf.phase_report(frames, 100, 101)
        self.assertFalse(report["passed"])
        self.assertIn("insufficient phase frames for a 15fps observation floor", report["errors"])
        self.assertIn("no decoded price text in phase", report["errors"])
        self.assertIsNone(report["first_visible_delay"])
        self.assertAlmostEqual(report["blank_upper_seconds"], 1)

    def test_trailing_blank_is_bounded_by_next_sample_or_phase_end(self):
        for include_after in (True, False):
            with self.subTest(include_after=include_after):
                frames = [{"time": 100 + i / 40, "visible": i < 35}
                          for i in range(42 if include_after else 40)]
                report = cf.phase_report(frames, 100, 101)
                self.assertFalse(report["passed"])
                self.assertAlmostEqual(report["blank_upper_seconds"], 0.15)
                self.assertIn("decoded blank interval upper bound > 100ms", report["errors"])


class ContinuousFrameProcessBoundaries(SFCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "helper.swift"
        self.source.write_text("isolated helper source", encoding="utf-8")
        self.track_patch(mock.patch.object(cf, "ROOT", self.root))
        self.track_patch(mock.patch.object(cf, "SOURCE", self.source))
        self.process = mock.Mock(returncode=0)
        self.process.poll.return_value = None
        self.popen = mock.Mock(return_value=self.process)
        self.track_patch(mock.patch.object(cf.subprocess, "Popen", self.popen))
        self.run = mock.Mock(side_effect=AssertionError("unexpected external command"))
        self.track_patch(mock.patch.object(cf.subprocess, "run", self.run))

    def capture(self):
        binary = self.root / "native_frame_stream"
        binary.write_bytes(b"helper fixture")
        with mock.patch.object(cf, "build_helper", return_value=binary):
            capture = cf.ContinuousCapture(self.root / "stream")
        self.addCleanup(capture.log.close)
        return capture

    def test_compile_publishes_candidate_and_reuses_content_addressed_cache(self):
        def compile_helper(args, **kwargs):
            self.assertEqual(args[:7], ["/usr/bin/xcrun", "swiftc", "-parse-as-library",
                                       "-swift-version", "5", "-O", str(self.source)])
            self.assertEqual(args[7], "-o")
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            self.assertFalse(kwargs["check"])
            self.assertGreater(kwargs["timeout"], 0)
            Path(args[8]).write_bytes(b"compiled helper")
            return subprocess.CompletedProcess(args, 0, "compiler output", "compiler warning")

        self.run.side_effect = compile_helper
        binary = cf.build_helper()
        self.assertEqual(binary.read_bytes(), b"compiled helper")
        self.assertEqual(binary.parent.name, hashlib.sha256(self.source.read_bytes()).hexdigest()[:16])
        self.assertEqual((binary.parent / "compile.log").read_text(), "compiler outputcompiler warning")
        self.assertEqual(cf.build_helper(), binary)
        self.run.assert_called_once()
        self.assertEqual(set(binary.parent.iterdir()), {binary.parent / "compile.log", binary})

    def test_compile_error_keeps_log_without_publishing_binary(self):
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 1, "output", "invalid Swift")
        with self.assertRaisesRegex(RuntimeError, "compilation failed: invalid Swift"):
            cf.build_helper()
        logs = list(self.root.rglob("compile.log"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].read_text(), "outputinvalid Swift")
        self.assertFalse((logs[0].parent / "native_frame_stream").exists())

    def test_ready_waits_for_preceding_display_frame_and_pumps_events(self):
        capture = self.capture()
        (capture.out / "ready.json").write_text(json.dumps({"host_time": 100, "display_host": 100.02}))
        clock = [100.0]
        pump = mock.Mock()
        with mock.patch.object(cf.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(cf.time, "sleep", side_effect=lambda sec: clock.__setitem__(0, clock[0] + sec)):
            capture.wait_ready(pump)
        self.assertGreaterEqual(clock[0], 100.07)
        self.assertGreater(pump.call_count, 1)
        self.assertFalse((capture.out / "stop").exists())
        self.process.wait.assert_not_called()
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0], [str(capture.binary), str(capture.out / "config.json"), str(capture.out)])
        self.assertIs(kwargs["stdout"], capture.log)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)

    def test_missing_readiness_stops_on_process_exit_or_deadline(self):
        for exited in (False, True):
            # Separate output directory for each actual constructor invocation.
            with self.subTest(exited=exited), tempfile.TemporaryDirectory(dir=self.root) as td, \
                    mock.patch.object(cf, "build_helper", return_value=self.source):
                capture = cf.ContinuousCapture(Path(td) / "stream")
                self.addCleanup(capture.log.close)
                self.process.poll.return_value = 1 if exited else None
                clock = [100.0]
                with mock.patch.object(cf.time, "monotonic", side_effect=lambda clock=clock: clock[0]), \
                        mock.patch.object(cf.time, "sleep", side_effect=lambda sec, clock=clock: clock.__setitem__(0, clock[0] + sec)), \
                        self.assertRaisesRegex(RuntimeError, "not ready before window construction"):
                    capture.wait_ready(lambda: None)
                self.assertTrue((capture.out / "stop").exists())
                self.assertTrue(capture.log.closed)
                if not exited:
                    self.assertGreaterEqual(clock[0], 108)

    def prepare_finish(self):
        capture = self.capture()
        source, decoded, phases = fixture()
        capture.phases = phases
        (capture.out / "capture.mov").write_bytes(b"recorded bytes fixture, not a real recording")
        (capture.out / "source-frames.json").write_text(json.dumps(source))
        template = self.root / "template.png"
        template.write_bytes(b"template fixture")

        def decode(args, **kwargs):
            self.assertEqual(args, [str(capture.binary), "--decode", str(capture.out / "capture.mov"),
                                    str(template), str(capture.out / "decoded-frames.json")])
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            self.assertFalse(kwargs["check"])
            Path(args[-1]).write_text(json.dumps(decoded))
            return subprocess.CompletedProcess(args, 0, "decoded", "")

        self.run.side_effect = decode
        return capture, template

    def test_finish_reads_real_json_and_binds_analysis_to_all_evidence_bytes(self):
        capture, template = self.prepare_finish()
        result = capture.finish(template)
        self.assertTrue(result["passed"], result)
        self.assertEqual(json.loads((capture.out / "analysis.json").read_text()), result)
        self.assertEqual(json.loads((capture.out / "events.json").read_text()), capture.phases)
        paths = {name: capture.out / name for name in ("capture.mov", "source-frames.json",
                 "decoded-frames.json", "events.json", "config.json")}
        paths.update(template=template, helper_binary=capture.binary, helper_source=self.source,
                     driver_source=Path(cf.__file__))
        self.assertEqual(result["evidence_sha256"],
                         {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()})
        self.assertTrue((capture.out / "stop").exists())
        self.assertTrue(capture.log.closed)
        self.assertEqual((capture.out / "decode.log").read_text(), "decoded")
        self.process.wait.assert_called_once_with(timeout=15)

    def test_finish_rejects_recorder_failure_before_decode(self):
        capture = self.capture()
        self.process.returncode = 1
        with self.assertRaisesRegex(RuntimeError, "continuous recorder failed"):
            capture.finish(self.root / "template.png")
        self.run.assert_not_called()
        self.assertFalse((capture.out / "analysis.json").exists())

    def test_finish_rejects_changed_movie_helper_or_driver(self):
        for changed in ("movie", "helper", "driver"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as td:
                original_root = self.root
                self.root = Path(td)
                try:
                    capture, template = self.prepare_finish()
                    decode = self.run.side_effect

                    def tamper(args, *, decode=decode, changed=changed, capture=capture, **kwargs):
                        result = decode(args, **kwargs)
                        if changed == "movie":
                            (capture.out / "capture.mov").write_bytes(b"changed movie")
                        elif changed == "helper":
                            self.source.write_text("changed source")
                        else:
                            # Redirect only the integrity-check path; never edit the real driver.
                            driver = self.root / "changed-driver.py"
                            driver.write_bytes(b"changed driver")
                            driver_patch = mock.patch.object(cf, "__file__", str(driver))
                            self.track_patch(driver_patch)
                        return result

                    self.run.side_effect = tamper
                    message = "video changed" if changed == "movie" else "source changed"
                    with self.assertRaisesRegex(RuntimeError, message):
                        capture.finish(template)
                    self.assertFalse((capture.out / "analysis.json").exists())
                finally:
                    self.root = original_root


class ContinuousFrameLifecycle(SFCase):
    def test_capture_controls_require_explicit_complete_scenario(self):
        tool = cf.ROOT / "tools/native_glass_probe.py"
        for flags in (["--continuous-video"], ["--continuous-control", "hide"],
                      ["--continuous-video", "--quality-sequence", "--exercise", "--fullscreen"]):
            result = subprocess.run([sys.executable, str(tool), *flags], capture_output=True,
                                    text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_unknown_clock_readiness_is_rejected_before_triggering_window(self):
        for ready in ({"host_time": 200, "display_host": 200},
                      {"host_time": 100}, {"host_time": 100, "display_host": 101}):
            with tempfile.TemporaryDirectory() as td, mock.patch.object(cf, "build_helper", return_value=Path("helper")), \
                    mock.patch.object(cf.subprocess, "Popen"), mock.patch.object(cf.time, "monotonic", return_value=100):
                capture = cf.ContinuousCapture(Path(td) / "stream")
                (capture.out / "ready.json").write_text(json.dumps(ready))
                try:
                    with self.assertRaises(RuntimeError):
                        capture.wait_ready(lambda: None)
                finally:
                    capture.stop()

    def test_decode_failure_never_produces_a_qualified_analysis(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(cf, "build_helper", return_value=Path("helper")), \
                mock.patch.object(cf.subprocess, "Popen") as popen, mock.patch.object(cf.subprocess, "run") as run:
            popen.return_value.returncode = 0
            run.return_value.returncode = 1
            run.return_value.stdout, run.return_value.stderr = "", "incomplete video"
            capture = cf.ContinuousCapture(Path(td) / "stream")
            (capture.out / "capture.mov").write_bytes(b"invalid video fixture")
            with self.assertRaises(RuntimeError):
                capture.finish(capture.out / "template.png")
            self.assertFalse((capture.out / "analysis.json").exists())

    def test_phase_nesting_and_stop_timeout_are_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(cf, "build_helper", return_value=Path("helper")), \
                mock.patch.object(cf.subprocess, "Popen") as popen:
            capture = cf.ContinuousCapture(Path(td) / "stream")
            capture.begin("startup")
            with self.assertRaises(RuntimeError):
                capture.begin("drag")
            capture.end()
            with self.assertRaises(RuntimeError):
                capture.end()
            popen.return_value.wait.side_effect = [subprocess.TimeoutExpired("helper", 15), 0]
            with self.assertRaises(RuntimeError):
                capture.stop()
            popen.return_value.kill.assert_called_once()

    def test_native_pixel_engine_has_real_positive_and_negative_controls(self):
        if sys.platform != "darwin":
            self.skipTest("ScreenCaptureKit helper requires macOS; pure timestamp rules still ran")
        binary = cf.build_helper()
        result = subprocess.run([str(binary), "--self-test"], capture_output=True, text=True,
                                timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("blanks rejected", result.stdout)

