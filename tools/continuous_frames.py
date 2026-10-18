"""Tool-only continuous compositor capture and independently timed acceptance.

Uses the system ScreenCaptureKit/AVFoundation helper, not screenshot subprocesses.
No new Python packages, camera, audio, product changes or permission requests.
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/native_frame_stream.swift"
MAX_FRAME_GAP = 0.100
MAX_DELIVERY_LAG = 0.150
MAX_BLANK_UPPER = 0.100
MAX_STARTUP_DELAY = 0.500


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def join_frames(source, decoded):
    """Reconcile every encoded PTS and hold pixels only for explicit idle samples."""
    errors = list(source.get("errors", []))
    raw = source.get("frames", [])
    video = decoded.get("frames", [])
    complete = [f for f in raw if f.get("status") == 0]
    if source.get("ready") is not True or decoded.get("decoder_completed") is not True:
        errors.append("capture or decoder did not complete readiness/EOF")
    if not complete or not video or len(complete) != len(video):
        return [], errors + ["source-complete/decoded frame count mismatch or empty video"]
    if any(f.get("encoded") is not True for f in complete):
        errors.append("complete source frame was not encoded")
    if not all(finite(f.get("pts")) for f in complete) or not all(finite(f.get("movie_pts")) for f in video):
        return [], errors + ["non-finite video/source timestamps"]
    origin, movie_origin = complete[0]["pts"], video[0]["movie_pts"]
    for native, movie in zip(complete, video):
        if abs((native["pts"] - origin) - (movie["movie_pts"] - movie_origin)) > 0.002:
            errors.append("encoded/decoded presentation timestamp mismatch")
            break
    frames: list[dict[str, Any]] = []
    index, previous_pixels = 0, None
    for row in raw:
        status = row.get("status")
        stamp, delivered = row.get("display_host"), row.get("delivered_host")
        if status not in (0, 1):
            errors.append(f"source frame status is not complete/idle: {status}")
            continue
        if not finite(stamp) or not finite(delivered):
            errors.append("source display/delivery host clock missing")
            continue
        if not -MAX_FRAME_GAP <= delivered - stamp <= MAX_DELIVERY_LAG:
            errors.append("source display/delivery clock skew or late delivery")
        if frames and stamp <= frames[-1]["time"]:
            errors.append("source display timestamps not strictly increasing")
        if status == 0:
            if index >= len(video):
                errors.append("unmatched complete frame")
                continue
            previous_pixels = video[index]
            index += 1
        if previous_pixels is None:
            errors.append("idle without a preceding decoded frame")
            continue
        frames.append({"time": stamp, "delivered_host": delivered, "status": status,
                       "visible": previous_pixels.get("visible") is True,
                       "window_x": previous_pixels.get("window_x"),
                       "window_y": previous_pixels.get("window_y")})
    return frames, errors


def phase_report(frames, start, end, *, startup=False, first_visible_budget=None):
    errors = []
    if not finite(start) or not finite(end) or end - start < 0.5:
        return {"passed": False, "errors": ["invalid/too-short phase interval"]}
    if not frames or any(not finite(f.get("time")) for f in frames):
        return {"passed": False, "errors": ["missing/invalid source frames"]}
    before = [f for f in frames if f["time"] <= start]
    after = [f for f in frames if f["time"] >= end]
    selected = [f for f in frames if start <= f["time"] <= end]
    if not before or start - before[-1]["time"] > MAX_FRAME_GAP:
        errors.append("capture does not cover phase start")
    if not after or after[0]["time"] - end > MAX_FRAME_GAP:
        errors.append("capture does not cover phase end")
    extended = before[-1:] + selected + after[:1]
    stamps = sorted({f["time"] for f in extended})
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    max_gap = max(gaps, default=float("inf"))
    if max_gap > MAX_FRAME_GAP + 1e-6:
        errors.append("source frame gap > 100ms; cannot bound unseen blank interval")
    if len(selected) < max(8, int((end - start) * 15)):
        errors.append("insufficient phase frames for a 15fps observation floor")
    visible = [f for f in selected if f["visible"]]
    if not visible:
        errors.append("no decoded price text in phase")
        first_delay = None
    else:
        first_delay = max(0, visible[0]["time"] - start)
        # first_visible_budget：调用方显式覆盖（冻结包冷启动含进程引导，
        # 源码探针的构造→首价预算不适用）；不给时维持原有两档预算。
        budget = first_visible_budget if first_visible_budget is not None else (
            MAX_STARTUP_DELAY if startup else MAX_FRAME_GAP)
        if first_delay > budget:
            errors.append("first visible price frame exceeds phase budget")
    # Upper bound includes both neighbouring known-good samples, never just the
    # difference between bad-frame timestamps (a one-frame loss must not be zero).
    longest_blank, run_start = 0.0, None
    active = selected
    if startup and visible:
        active = [f for f in selected if f["time"] >= visible[0]["time"]]
    previous = start
    for frame in active:
        if not frame["visible"] and run_start is None:
            run_start = previous
        elif frame["visible"] and run_start is not None:
            longest_blank = max(longest_blank, frame["time"] - run_start)
            run_start = None
        previous = frame["time"]
    if run_start is not None:
        longest_blank = max(longest_blank, (after[0]["time"] if after else end) - run_start)
    if longest_blank > MAX_BLANK_UPPER + 1e-6:
        errors.append("decoded blank interval upper bound > 100ms")
    return {"passed": not errors, "errors": errors, "frames": len(selected),
            "first_visible_delay": first_delay, "max_source_gap": max_gap if math.isfinite(max_gap) else None,
            "observed_bad_frames": sum(not f["visible"] for f in selected),
            "blank_upper_seconds": longest_blank, "duration": end - start}


def analyze(source, decoded, phases):
    frames, errors = join_frames(source, decoded)
    reports = {}
    transitions = {}
    for phase in phases:
        name = phase["name"]
        report = phase_report(frames, phase["start"], phase["end"], startup=name == "startup")
        if name.endswith("_transition"):
            transitions[name] = {**report, "qualified": False,
                "reason": "Space animation can move the window outside the fixed recording region; not a product blank verdict"}
        else:
            reports[name] = report
    required = {"startup", "fullscreen", "desktop_return", "drag", "fullscreen_transition", "desktop_transition"}
    names = [phase["name"] for phase in phases]
    if set(names) != required or len(phases) != len(required):
        errors.append("missing or duplicate required phase")
    return {"passed": not errors and all(r["passed"] for r in reports.values()),
            "errors": errors, "phases": reports, "unqualified_transitions": transitions,
            "source_callbacks": len(source.get("frames", [])), "decoded_frames": len(decoded.get("frames", [])),
            "limits": ["ScreenCaptureKit source timestamps, not screenshot arrival times",
                       "100ms maximum unobserved source interval; not a proof of zero sub-frame loss",
                       "Price pixel visibility is not a measurement of glass-edge pixel geometry",
                       "Space-animation intervals are retained but not qualified in the fixed capture region",
                       "Fixed seeded 184pt source probe, not frozen app or cross-machine qualification"]}


def build_helper():
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    directory = ROOT / "build/continuous-frames" / digest[:16]
    binary = directory / "native_frame_stream"
    if not binary.exists():
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as td:
            candidate = Path(td) / "native_frame_stream"
            proc = subprocess.run(["/usr/bin/xcrun", "swiftc", "-parse-as-library", "-swift-version", "5", "-O",
                                   str(SOURCE), "-o", str(candidate)], capture_output=True, text=True, timeout=120, check=False)
            (directory / "compile.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
            if proc.returncode:
                raise RuntimeError("continuous capture helper compilation failed: " + proc.stderr[-2000:])
            candidate.replace(binary)
    return binary


class ContinuousCapture:
    def __init__(self, out, rect=None, seconds=60.0):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=False)
        self.binary = build_helper()
        self.helper_source_sha256 = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
        self.driver_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.phases = []
        self.active = None
        # 矩形默认与源码探针的 ProbeWindow(400,180,184,224) 匹配；冻结包无缓存变体
        # 的窗口落在默认几何 (120,120)，必须用别的录制区 —— 由调用方显式传入。
        self.rect = list(rect) if rect is not None else [360, 140, 460, 360]
        config = {"rect": self.rect, "seconds": seconds}
        (self.out / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.log = (self.out / "capture.log").open("w", encoding="utf-8")
        self.process = subprocess.Popen([str(self.binary), str(self.out / "config.json"), str(self.out)],
                                        stdout=self.log, stderr=subprocess.STDOUT)

    def wait_ready(self, pump):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            pump()
            if (self.out / "ready.json").exists():
                ready = json.loads((self.out / "ready.json").read_text())
                if not finite(ready.get("host_time")) or not 0 <= time.monotonic() - ready["host_time"] <= 1:
                    raise RuntimeError("capture/Python monotonic clocks cannot be reconciled")
                display_time = ready.get("display_host")
                if not finite(display_time) or abs(display_time - ready["host_time"]) > MAX_FRAME_GAP:
                    raise RuntimeError("capture readiness has no trustworthy display clock")
                # Some frame timestamps target the next display refresh. Do not
                # trigger the window until an already-captured frame precedes it.
                while time.monotonic() < display_time + 0.05:
                    pump()
                    time.sleep(0.002)
                return
            if self.process.poll() is not None:
                break
            time.sleep(0.005)
        self.stop()
        raise RuntimeError("continuous capture not ready before window construction; see capture.log")

    def begin(self, name):
        if self.active is not None:
            raise RuntimeError("overlapping capture phases")
        self.active = {"name": name, "start": time.monotonic()}

    def end(self):
        if self.active is None:
            raise RuntimeError("phase ended without beginning")
        self.active["end"] = time.monotonic()
        self.phases.append(self.active)
        self.active = None

    def stop(self):
        (self.out / "stop").touch()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise RuntimeError("continuous recorder failed to stop cleanly") from None
        finally:
            self.log.close()

    def finish_evidence(self, template):
        """停止录制、解码并绑定证据哈希；返回 (source, decoded, 哈希表)。

        分析口径由调用方选择：finish() 走六阶段 analyze()（源码探针全场景），
        冻结包冷启动等单阶段取证直接取返回值再调 join_frames/phase_report，
        避免"少几个阶段就永远 fail"的分析壳变成绕开判据的理由。
        """
        self.stop()
        (self.out / "events.json").write_text(json.dumps(self.phases, indent=2), encoding="utf-8")
        if self.process.returncode:
            raise RuntimeError("continuous recorder failed; source evidence not qualified")
        movie_digest = hashlib.sha256((self.out / "capture.mov").read_bytes()).hexdigest()
        proc = subprocess.run([str(self.binary), "--decode", str(self.out / "capture.mov"), str(template),
                               str(self.out / "decoded-frames.json")], capture_output=True, text=True,
                              timeout=120, check=False)
        (self.out / "decode.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        if proc.returncode:
            raise RuntimeError("native video decode failed: " + proc.stderr[-1500:])
        if movie_digest != hashlib.sha256((self.out / "capture.mov").read_bytes()).hexdigest():
            raise RuntimeError("recorded video changed while decoding")
        if self.helper_source_sha256 != hashlib.sha256(SOURCE.read_bytes()).hexdigest() \
                or self.driver_sha256 != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
            raise RuntimeError("capture helper/driver source changed during recording")
        source = json.loads((self.out / "source-frames.json").read_text())
        decoded = json.loads((self.out / "decoded-frames.json").read_text())
        hashes = {name: hashlib.sha256((self.out / name).read_bytes()).hexdigest()
                  for name in ("capture.mov", "source-frames.json", "decoded-frames.json", "events.json", "config.json")}
        hashes.update(template=hashlib.sha256(Path(template).read_bytes()).hexdigest(),
                      helper_binary=hashlib.sha256(self.binary.read_bytes()).hexdigest(),
                      helper_source=self.helper_source_sha256, driver_source=self.driver_sha256)
        return source, decoded, hashes

    def finish(self, template):
        source, decoded, hashes = self.finish_evidence(template)
        result = analyze(source, decoded, self.phases)
        result["evidence_sha256"] = hashes
        (self.out / "analysis.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
