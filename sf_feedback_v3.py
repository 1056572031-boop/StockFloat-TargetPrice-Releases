"""Shared Python contract for feedback payload v3 diagnostic snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

V3_DIAGNOSTIC_SCHEMA_VERSION = 3
V3_DIAGNOSTIC_FILES = frozenset({
    "environment.json",
    "permissions.json",
    "feedback_preferences.json",
    "feedback_context.json",
})
V3_SNAPSHOT_SCHEMAS = {
    "environment.json": "environment.v1",
    "permissions.json": "permissions.v1",
    "feedback_preferences.json": "feedback_preferences.v1",
    "feedback_context.json": "feedback_context.v1",
}
V3_UNAVAILABLE_REASONS = frozenset({
    "query_failed",
    "unsupported",
    "no_bundle",
    "not_applicable",
})


def unavailable_snapshot(name: str, reason: str) -> dict[str, Any]:
    if name not in V3_SNAPSHOT_SCHEMAS or reason not in V3_UNAVAILABLE_REASONS:
        raise ValueError("invalid diagnostic v3 unavailable snapshot")
    return {"schema": V3_SNAPSHOT_SCHEMAS[name], "available": False, "reason": reason}


def available_snapshot(name: str, data: Mapping[str, Any]) -> dict[str, Any]:
    if name not in V3_SNAPSHOT_SCHEMAS or not isinstance(data, Mapping):
        raise ValueError("invalid diagnostic v3 available snapshot")
    return {"schema": V3_SNAPSHOT_SCHEMAS[name], "available": True, "data": dict(data)}


def validate_snapshot(name: str, value: Any) -> bool:
    if name not in V3_SNAPSHOT_SCHEMAS or not isinstance(value, dict):
        return False
    if value.get("schema") != V3_SNAPSHOT_SCHEMAS[name] or type(value.get("available")) is not bool:
        return False
    if value["available"]:
        return set(value) == {"schema", "available", "data"} and isinstance(value["data"], dict)
    return (set(value) == {"schema", "available", "reason"}
            and value.get("reason") in V3_UNAVAILABLE_REASONS)


def is_v3_export(exported: Any) -> bool:
    return isinstance(exported, dict) and exported.get("diagnostic_schema_version") == V3_DIAGNOSTIC_SCHEMA_VERSION
