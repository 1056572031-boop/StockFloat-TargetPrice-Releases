"""Low-overhead, bounded performance evidence for one Qt window.

This module deliberately records process-level evidence, not screen FPS.  It has no
worker thread, network access, camera access, or stack capture.
"""
from __future__ import annotations

from collections import deque
import math
import resource
import time
from typing import Any, Callable

from PyQt6.QtCore import QObject, QTimer, Qt


SCHEMA = "performance.v1"
DEFAULT_SAMPLE_CAPACITY = 60
DEFAULT_PAINT_CAPACITY = 120
# Report thresholds for the whole-run counters.  They are labels for "how often
# did it look like a stall", not acceptance limits.  33 ms is two 60 Hz frames.
LAG_WARN_MS = 50.0
LAG_STALL_MS = 200.0
SLOW_PAINT_MS = 33.0


class PerformanceMonitor(QObject):
    """A parent-owned, opt-in monitor for one window."""

    def __init__(self, parent: QObject | None = None, *, clock: Callable[[], float] = time.monotonic,
                 process_clock: Callable[[], float] = time.process_time,
                 resource_usage: Callable[[], Any] | None = None,
                 capacity: int = DEFAULT_SAMPLE_CAPACITY,
                 paint_capacity: int = DEFAULT_PAINT_CAPACITY,
                 interval_ms: int = 1000) -> None:
        super().__init__(parent)
        self._clock = clock
        self._process_clock = process_clock
        self._resource_usage: Callable[..., Any] = resource_usage or resource.getrusage
        self._interval_ms = max(1, int(interval_ms))
        self._samples: deque[dict[str, Any]] = deque(maxlen=max(1, int(capacity)))
        self._paint_samples: deque[dict[str, Any]] = deque(maxlen=max(1, int(paint_capacity)))
        self._last_wall: float | None = None
        self._last_cpu: float | None = None
        self._started_at: float | None = None
        self._ended_at: float | None = None
        self._paint_count = 0
        self._sample_count = 0
        self._paint_total_ms = 0.0
        self._paint_max_ms = 0.0
        self._last_paint_at: float | None = None
        # Whole-run aggregates.  The retained windows above only cover the last
        # minute or two, which cannot answer "did it stall sometime today?".
        # Everything here is O(1) state updated from the samples that already exist.
        self._wall_total = 0.0
        self._cpu_total = 0.0
        self._cpu_max = 0.0
        self._cpu_max_at: float | None = None
        self._lag_max_ms = 0.0
        self._lag_max_at: float | None = None
        self._lag_over_warn = 0
        self._lag_over_stall = 0
        self._peak_rss = 0
        self._paint_slow = 0
        self._paint_max_at: float | None = None
        self._failures: deque[str] = deque(maxlen=8)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.setInterval(self._interval_ms)
        self._timer.timeout.connect(self.sample)

    @property
    def timer(self) -> QTimer:
        return self._timer

    def start(self) -> None:
        if self._started_at is not None:
            return
        try:
            now = self._clock()
            cpu = self._process_clock()
            if not math.isfinite(now) or not math.isfinite(cpu):
                raise ValueError("non-finite clock")
            self._started_at = now
            self._last_wall = now
            self._last_cpu = cpu
            self._timer.start()
        except Exception as exc:
            self._remember_failure("start:%s" % type(exc).__name__)

    def stop(self) -> None:
        self._timer.stop()
        if self._ended_at is None:
            try:
                self._ended_at = self._clock()
            except Exception as exc:
                self._remember_failure("stop:%s" % type(exc).__name__)

    def _remember_failure(self, label: str) -> None:
        self._failures.append(label)

    def sample(self) -> None:
        """Take one low-frequency sample; sampling failures never escape Qt."""
        try:
            now = self._clock()
            cpu_now = self._process_clock()
            if not math.isfinite(now) or not math.isfinite(cpu_now):
                raise ValueError("non-finite clock")
            previous_wall = self._last_wall
            previous_cpu = self._last_cpu
            self._last_wall, self._last_cpu = now, cpu_now
            if previous_wall is None or previous_cpu is None:
                return
            wall = max(0.0, now - previous_wall)
            cpu = max(0.0, cpu_now - previous_cpu)
            usage = self._resource_usage(resource.RUSAGE_SELF)
            # macOS reports ru_maxrss in bytes; retain the source explicitly.
            peak_rss = int(usage.ru_maxrss)
            lag_ms = max(0.0, (wall - self._interval_ms / 1000.0) * 1000.0)
            cpu_pct = (cpu / wall * 100.0) if wall > 0 else None
            self._sample_count += 1
            self._samples.append({
                "at": now,
                "interval_sec": wall,
                "event_loop_lag_ms": lag_ms,
                "cpu_single_core_pct": cpu_pct,
                "peak_rss_bytes": peak_rss,
            })
            self._wall_total += wall
            self._cpu_total += cpu
            self._peak_rss = max(self._peak_rss, peak_rss)
            if cpu_pct is not None and cpu_pct > self._cpu_max:
                self._cpu_max, self._cpu_max_at = cpu_pct, now
            if lag_ms > self._lag_max_ms:
                self._lag_max_ms, self._lag_max_at = lag_ms, now
            if lag_ms >= LAG_WARN_MS:
                self._lag_over_warn += 1
            if lag_ms >= LAG_STALL_MS:
                self._lag_over_stall += 1
        except Exception as exc:
            self._remember_failure("sample:%s" % type(exc).__name__)

    def record_paint(self, duration_ms: float, *, at: float | None = None) -> None:
        """Record a paint duration and call spacing, without making FPS claims."""
        try:
            now = self._clock() if at is None else float(at)
            raw_duration = float(duration_ms)
            if not math.isfinite(now) or not math.isfinite(raw_duration):
                raise ValueError("non-finite paint sample")
            duration = max(0.0, raw_duration)
            spacing = None if self._last_paint_at is None else max(0.0, now - self._last_paint_at)
            self._last_paint_at = now
            self._paint_count += 1
            self._paint_total_ms += duration
            if duration > self._paint_max_ms:
                self._paint_max_ms, self._paint_max_at = duration, now
            if duration >= SLOW_PAINT_MS:
                self._paint_slow += 1
            self._paint_samples.append({"at": now, "duration_ms": duration,
                                        "interval_sec": spacing})
        except Exception as exc:
            self._remember_failure("paint:%s" % type(exc).__name__)

    def export(self) -> dict[str, Any]:
        if self._started_at is None:
            return {"available": False, "schema": SCHEMA,
                    "reason": "no window performance monitor was started",
                    "failures": list(self._failures),
                    "limitations": _LIMITATIONS}
        ended = self._ended_at
        samples = list(self._samples)
        paints = list(self._paint_samples)
        try:
            exported_at = self._clock()
        except Exception:
            exported_at = None
        return {
            "available": True, "schema": SCHEMA,
            "sampled_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "units": {"event_loop_lag_ms": "ms", "cpu_single_core_pct": "% of one core",
                       "peak_rss_bytes": "bytes", "paint_duration_ms": "ms"},
            "sources": {"clock": "time.monotonic", "cpu": "time.process_time",
                        "rss": "resource.getrusage(RUSAGE_SELF).ru_maxrss (macOS bytes)"},
            "window": {"started_at_monotonic": self._started_at,
                       "ended_at_monotonic": ended,
                       "exported_at_monotonic": exported_at,
                       "retained_from_monotonic": samples[0]["at"] - samples[0]["interval_sec"] if samples else None,
                       "retained_to_monotonic": samples[-1]["at"] if samples else None,
                       "samples_discarded": max(0, self._sample_count - len(samples))},
            "recent_samples": samples,
            "run": {"samples": self._sample_count,
                    "wall_sec": self._wall_total,
                    "cpu_mean_pct": ((self._cpu_total / self._wall_total * 100.0)
                                     if self._wall_total > 0 else None),
                    "cpu_max_pct": self._cpu_max,
                    "cpu_max_at_monotonic": self._cpu_max_at,
                    "event_loop_lag_max_ms": self._lag_max_ms,
                    "event_loop_lag_max_at_monotonic": self._lag_max_at,
                    "event_loop_lag_over_warn": self._lag_over_warn,
                    "event_loop_lag_over_stall": self._lag_over_stall,
                    "lag_warn_ms": LAG_WARN_MS, "lag_stall_ms": LAG_STALL_MS,
                    "peak_rss_bytes": self._peak_rss},
            "paint": {"count": self._paint_count,
                      "total_ms": self._paint_total_ms,
                      "max_ms": self._paint_max_ms,
                      "max_at_monotonic": self._paint_max_at,
                      "slow_count": self._paint_slow,
                      "slow_ms": SLOW_PAINT_MS,
                      "samples_discarded": max(0, self._paint_count - len(paints)),
                      "recent_samples": paints},
            "failures": list(self._failures),
            "limitations": _LIMITATIONS,
        }


_LIMITATIONS = [
    "peak RSS is the process peak, not current window RSS",
    "paint calls and intervals are not real screen presentation FPS",
    "no current RSS, thread stacks, or screen capture is collected",
    "event-loop lag also includes suspend and scheduling delay; it is not a blocking-stack attribution",
    "CPU is process-wide percent of one core and may exceed 100 on multicore workloads",
    "run.* aggregates cover the whole process lifetime; *_at_monotonic values share the clock of window.*_monotonic",
    "lag counters include system sleep and scheduling delay, so a count is a hint, not proof of a blocked UI",
]
