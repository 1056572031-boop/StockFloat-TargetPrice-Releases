"""Explicit bundled-camera check: aggregate counters only, never frames or images."""
import argparse
import time
from typing import Any

from sf_constants import APP_DISPLAY_NAME, APP_VERSION
from sf_config import _atomic_write_json
import sf_presence


def judge_camera_result(authorization, frames, errors, stopped):
    if authorization != "authorized":
        return "blocked"
    return "passed" if frames > 0 and errors == 0 and stopped else "failed"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 120:
        parser.error("seconds must be 1..120")
    from Foundation import NSBundle
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication
    bundle_id = NSBundle.mainBundle().bundleIdentifier()
    result: dict[str, Any] = {"version": APP_VERSION, "bundle_id": str(bundle_id or ""),
              "status": "waiting", "authorization": sf_presence.camera_access_status(),
              "frames": 0, "detect_errors": 0, "samples": [], "camera_stopped": False}
    if bundle_id not in ("com.skyrocket.app", "com.stockfloat.targetprice"):
        result.update(status="blocked", reason="requires-installed-app-identity")
        _atomic_write_json(args.output, result)
        return 2
    app = QApplication.instance() or QApplication([])
    if not isinstance(app, QApplication):
        raise RuntimeError("Camera check requires QApplication")
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setQuitOnLastWindowClosed(False)  # type: ignore[attr-defined]
    monitor = sf_presence.FacePresenceMonitor()
    timer = QTimer()
    timer.setInterval(125)
    started = time.monotonic()
    running_at: list[float | None] = [None]
    stopping_at: list[float | None] = [None]
    last_sample = [0.0]

    def finish():
        timer.stop()
        result["authorization"] = sf_presence.camera_access_status()
        result["status"] = judge_camera_result(result["authorization"], result["frames"],
                                                result["detect_errors"], result["camera_stopped"])
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        _atomic_write_json(args.output, result)
        app.exit(0 if result["status"] == "passed" else 2)

    def tick():
        now = time.monotonic()
        if stopping_at[0] is not None:
            result["camera_stopped"] = monitor.stop(wait=False)
            if result["camera_stopped"] or now - stopping_at[0] >= 3:
                finish()
            return
        authorization = sf_presence.camera_access_status()
        result["authorization"] = authorization
        if authorization == "authorized" and monitor.state != sf_presence.STATE_RUNNING and not monitor.is_starting():
            monitor.request_start()
        if monitor.state == sf_presence.STATE_RUNNING:
            if running_at[0] is None:
                running_at[0] = now
            count, stamp = monitor.snapshot()
            result["frames"] = monitor.frames
            result["detect_errors"] = monitor.detect_errors
            if stamp and stamp != last_sample[0]:
                last_sample[0] = stamp
                result["samples"].append({"seconds": round(now - started, 2), "faces": count})
                result["samples"] = result["samples"][-120:]
        complete = running_at[0] is not None and now - running_at[0] >= args.seconds
        blocked = authorization in ("denied", "restricted") or now - started >= 90
        if complete or blocked:
            stopping_at[0] = now
            result["camera_stopped"] = monitor.stop(wait=False)

    def request():
        from AppKit import NSApp
        NSApp.activateIgnoringOtherApps_(True)
        monitor.request_access_once()

    timer.timeout.connect(tick)
    timer.start()
    QTimer.singleShot(0, request)
    return app.exec()
