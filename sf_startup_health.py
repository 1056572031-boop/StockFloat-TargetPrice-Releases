"""One read-only post-frame sample; no repair, activation, transport or prompts.

``capture`` and ``evaluate`` only read.  ``schedule`` writes state once the sample
passes: it marks the run session healthy and acknowledges a pending update handoff.
"""

from __future__ import annotations

import copy
import datetime as dt
import math
import os
import plistlib
import sys
from pathlib import Path

from sf_constants import APP_DISPLAY_NAME, APP_VERSION, BUNDLE_ID

STARTUP_DELAY_MS = 2000


def _result(status, data=None, reason=""):
    return {"status": status, "data": data, "reason": reason}


def _aligned(desync: object, tolerance: object) -> bool | None:
    if not isinstance(desync, (int, float)) or not isinstance(tolerance, (int, float)):
        return None
    if not math.isfinite(desync) or not math.isfinite(tolerance) or min(desync, tolerance) < 0:
        return None
    return desync <= tolerance


def identity_snapshot():
    if not getattr(sys, "frozen", False):
        return {"frozen": False, "reason": "source_process"}
    path = Path(sys.executable).parent.parent / "Info.plist"
    with path.open("rb") as stream:
        raw = stream.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError("oversized_info_plist")
    info = plistlib.loads(raw)
    return {"frozen": True, "bundle_id": info.get("CFBundleIdentifier"),
            "version": info.get("CFBundleShortVersionString"),
            "executable": info.get("CFBundleExecutable"),
            "signature_status": "unverified_not_checked_at_startup"}


def update_snapshot(window):
    import sf_update

    # Whitelist values, never archive paths, URLs, raw errors or a pending object.
    state = getattr(window, "_update_state", None)
    last = getattr(state, "last_result", None)
    last = last if isinstance(last, dict) else {}
    return {"manifest": sf_update.manifest_diagnostics(),
            "flow": {name: getattr(state, name, None) for name in
                     ("checking", "downloading", "transition_pending", "failure_count",
                      "stale_resets", "last_check_at")},
            "last_outcome": last.get("outcome")}


def evaluate(data):
    """Unknown native answers remain unknown, even if Qt reports visible."""
    checks = {}
    identity = data.get("identity") or {}
    if identity.get("frozen") is not True:
        checks["identity"] = _result("unverified", identity, "not_frozen_or_unreadable")
    else:
        valid = (identity.get("bundle_id") == BUNDLE_ID
                 and identity.get("version") == APP_VERSION
                 and identity.get("executable") == APP_DISPLAY_NAME)
        checks["identity"] = _result("passed" if valid else "failed", identity,
                                     "" if valid else "bundle_identity_mismatch")
    visibility = data.get("visibility") or {}
    intentional = visibility.get("intentionally_hidden") is True
    for name in ("visibility", "glass", "space", "window_level"):
        checks[name] = _result("unverified", data.get(name), "native_sample_unavailable")
    if intentional:
        for name in ("visibility", "glass", "space", "window_level"):
            checks[name] = _result("not_applicable", data.get(name), "user_hidden_or_quitting")
    else:
        flags = [visibility.get(key) for key in ("qt_visible", "intersects_screen", "native_visible")]
        if False in flags:
            checks["visibility"] = _result("failed", visibility, "window_not_visible")
        elif all(value is True for value in flags):
            checks["visibility"] = _result("passed", visibility)
        space = data.get("space") or {}
        if type(space.get("on_active_space")) is bool:
            checks["space"] = _result("passed" if space["on_active_space"] else "failed", space,
                                       "" if space["on_active_space"] else "not_on_active_space")
        glass = data.get("glass") or {}
        real = glass.get("real_state") or {}
        level, expected = glass.get("level"), glass.get("expected_level")
        if level is not None and expected is not None:
            checks["window_level"] = _result("passed" if level == expected else "failed",
                                              {"level": level, "expected_level": expected})
        desync = real.get("desync_px")
        tolerance = glass.get("desync_tolerance_pt")
        aligned = _aligned(desync, tolerance)
        if glass.get("requested") is False:
            checks["glass"] = _result("not_applicable", glass, "material_disabled_by_user")
        elif (glass.get("present") is False or real.get("glass_on_top") is True
                or aligned is False):
            checks["glass"] = _result("failed", glass, "missing_misaligned_or_wrong_order")
        elif (glass.get("present") is True and real.get("glass_on_top") is False
              and aligned is True):
            checks["glass"] = _result("passed", glass)
    update = data.get("update") or {}
    manifest = update.get("manifest") or {}
    state = manifest.get("status")
    checks["ota_manifest"] = _result(
        "passed" if state == "verified" else "failed" if state == "failed" else "unverified",
        manifest, "" if state == "verified" else "existing_update_check_" + str(state or "not_run"))
    flow = update.get("flow") or {}
    has_flow = all(type(flow.get(name)) is bool for name in
                   ("checking", "downloading", "transition_pending"))
    failed = update.get("last_outcome") == "error"
    checks["update_state"] = _result("failed" if failed else "passed" if has_flow else "unverified",
                                      flow, "last_update_check_failed" if failed else "")
    states = {item["status"] for item in checks.values()}
    status = "failed" if "failed" in states else "unverified" if "unverified" in states else "passed"
    return {"schema_version": 1, "status": status, "checks": checks,
            "boundary": "Single read-only sample, not screenshot, cross-Space, signature or OTA install acceptance"}


def ready_for_update_handoff(sample):
    """Return whether the new app itself is ready to acknowledge an OTA handoff.

    The update check starts after this two-second startup sample.  Its manifest state
    therefore cannot gate the acknowledgement: doing so makes every fresh launch
    miss the helper's 30-second deadline and roll back a correctly installed app.
    Only a user-disabled glass material may be not_applicable; identity, visibility,
    Space and window level must each prove the new app is actually usable.
    """
    if not isinstance(sample, dict):
        return False
    checks = sample.get("checks")
    if not isinstance(checks, dict):
        return False
    # The helper may only accept a real, visible new application.  The optional
    # glass material is the sole user-selected exception; hidden/minimized windows
    # make visibility, Space and level not_applicable and must not acknowledge.
    for name in ("identity", "visibility", "space", "window_level"):
        check = checks.get(name)
        if not isinstance(check, dict) or check.get("status") != "passed":
            return False
    glass = checks.get("glass")
    if not isinstance(glass, dict):
        return False
    if glass.get("status") == "passed":
        return True
    return (glass.get("status") == "not_applicable"
            and glass.get("reason") == "material_disabled_by_user")


def capture(window):
    from PyQt6.QtWidgets import QApplication
    import sf_glass

    data = {}
    errors = {}

    def read(name, provider):
        try:
            data[name] = provider()
        except Exception as exc:
            errors[name] = type(exc).__name__

    read("identity", identity_snapshot)
    read("update", lambda: update_snapshot(window))
    read("visibility", lambda: {
        "qt_visible": window.isVisible(),
        "intersects_screen": any(window.frameGeometry().intersects(screen.geometry())
                                 for screen in QApplication.screens()),
        "intentionally_hidden": bool(getattr(window, "_minimized", False)
                                      or getattr(window, "_quitting", False))})
    # Never reinterpret an offscreen winId as an NSView pointer.
    if sf_glass.native_cocoa_active():
        def native():
            nsw = sf_glass.content_ns_window(window)
            if nsw is None:
                raise RuntimeError("native_window_unavailable")
            return nsw

        def visibility():
            data.setdefault("visibility", {})["native_visible"] = bool(native().isVisible())
            return data["visibility"]

        read("visibility", visibility)
        read("space", lambda: {"on_active_space": bool(native().isOnActiveSpace()),
                               "collection_behavior": int(native().collectionBehavior())})

        def glass():
            sample = window.glass_diagnostics()
            return {"present": sample.get("glass_present"), "surface": sample.get("surface"),
                    "real_state": sample.get("real_state"), "level": int(native().level()),
                    "requested": getattr(window, "_bg_cfg", None) == "glass",
                    "desync_tolerance_pt": sf_glass._Z_DESYNC_TOLERANCE_PT,
                    "expected_level": sf_glass._TOP_WINDOW_LEVEL}

        read("glass", glass)
    else:
        errors["native"] = "non_cocoa_no_appkit_access"
    result = evaluate(data)
    result["sampled_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    result["errors"] = errors
    return result


def schedule(window):
    """Parent-owned single-shot: deleted with the window, no persistent polling."""
    from PyQt6.QtCore import QTimer
    import sf_glass

    if getattr(window, "_startup_health_timer", None) is not None:
        return
    window._startup_health = _result("unverified", reason="not_sampled")
    if not getattr(sys, "frozen", False) or not sf_glass.native_cocoa_active():
        return

    def sample():
        if getattr(window, "_quitting", False):
            window._startup_health = _result("unverified", reason="quit_before_sample")
            return
        try:
            window._startup_health = capture(window)
            session = vars(window).get("_runtime_session")
            session_id = vars(window).get("_runtime_session_id")
            if window._startup_health.get("status") == "passed":
                if session is not None and isinstance(session_id, str):
                    session.mark_healthy(session_id)
            # The handoff acknowledgement has its own, narrower gate: it must not wait
            # for the update check that only begins after this sample (see the helper).
            if ready_for_update_handoff(window._startup_health):
                try:
                    import sf_update
                    sf_update.acknowledge_pending_handoff(
                        version=APP_VERSION, bundle_id=BUNDLE_ID, session_id=session_id or "",
                        handoff_id=os.environ.get("SF_UPDATE_HANDOFF"))
                except Exception as exc:
                    from sf_logging import log_swallow
                    log_swallow("sf_startup_health.handoff_ack", exc)
        except Exception as exc:
            window._startup_health = _result("unverified", reason=type(exc).__name__)
            from sf_logging import log_swallow
            log_swallow("sf_startup_health.sample", exc)

    timer = QTimer(window)
    timer.setSingleShot(True)
    timer.timeout.connect(sample)
    window._startup_health_timer = timer
    timer.start(STARTUP_DELAY_MS)


def diagnostics(window):
    return {"startup": copy.deepcopy(getattr(window, "_startup_health", None)
                                    or _result("unverified", reason="not_scheduled")),
            "current_update": update_snapshot(window)}
