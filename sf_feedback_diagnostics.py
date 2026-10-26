"""Bounded diagnostic v3 snapshots collected on the UI thread."""

from __future__ import annotations

import locale
import os
import platform
from typing import Any

from sf_feedback_v3 import available_snapshot, unavailable_snapshot
from sf_logging import log_swallow


def _environment(window) -> dict[str, Any]:
    data: dict[str, Any] = {
        "macos_version": platform.mac_ver()[0] or "unknown",
        "machine_model_family": platform.machine() or "unknown",
        "hardware_arch": platform.machine() or "unknown",
        "process_arch": platform.machine() or "unknown",
        "is_rosetta": False,
        "appearance": "unknown",
        "screen_count": 0,
        "window": None,
    }
    try:
        language, region = locale.getlocale()
        data["locale"] = language or "unknown"
        data["region"] = region or "unknown"
    except Exception:
        data["locale"] = "unknown"
        data["region"] = "unknown"
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        memory = int(pages) * int(page_size)
        data["memory_bucket_gib"] = max(1, round(memory / (1024 ** 3)))
    except (AttributeError, OSError, ValueError):
        data["memory_bucket_gib"] = None
    try:
        from PyQt6.QtGui import QGuiApplication
        app: Any = QGuiApplication.instance()
        if app is not None:
            data["screen_count"] = len(app.screens())
            data["appearance"] = "dark" if window.palette().window().color().lightness() < 128 else "light"
    except Exception as exc:
        log_swallow("sf_feedback_diagnostics.environment.qt", exc)
    try:
        data["window"] = {"width": int(window.width()), "height": int(window.height()),
                          "visible": bool(window.isVisible())}
    except Exception as exc:
        log_swallow("sf_feedback_diagnostics.environment.window", exc)
    return data


def _permissions(window) -> dict[str, Any]:
    data: dict[str, Any] = {"automation": "unknown", "screen_recording": "unknown",
                            "accessibility": "unknown", "notifications": "unknown"}
    try:
        import sf_notify
        data["notifications"] = sf_notify.delivery_stats().get("authorization", "unknown")
    except Exception as exc:
        log_swallow("sf_feedback_diagnostics.permissions.notifications", exc)
    try:
        face = window.face_guard_field_evidence()
        data["camera"] = face.get("state", "unknown") if isinstance(face, dict) else "unknown"
    except Exception:
        data["camera"] = "unknown"
    return data


def v3_snapshots(window, *, feedback_context: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Return all four fixed snapshots; individual failures stay explicit."""
    result: dict[str, dict[str, Any]] = {}
    try:
        result["environment.json"] = available_snapshot("environment.json", _environment(window))
    except Exception:
        result["environment.json"] = unavailable_snapshot("environment.json", "query_failed")
    try:
        result["permissions.json"] = available_snapshot("permissions.json", _permissions(window))
    except Exception:
        result["permissions.json"] = unavailable_snapshot("permissions.json", "query_failed")
    try:
        features = getattr(window, "_feature_snapshot", None)
        value = features() if callable(features) else {}
        result["feedback_preferences.json"] = available_snapshot(
            "feedback_preferences.json", {"features": value if isinstance(value, dict) else {}})
    except Exception:
        result["feedback_preferences.json"] = unavailable_snapshot("feedback_preferences.json", "query_failed")
    result["feedback_context.json"] = available_snapshot(
        "feedback_context.json", dict(feedback_context or {"trigger": "manual"}))
    return result
