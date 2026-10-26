"""Pre-implementation acceptance checks for the planned diagnostic v2 envelope.

The production implementation is deliberately not enabled by these tests.  They
freeze the compatibility contract: v1 remains readable; v2 uses four fixed,
small JSON snapshots; and each unavailable provider remains explicit instead of
silently omitting a category.
"""

from __future__ import annotations

import unittest

from tests._base import SFCase


V2_FILES = frozenset({
    "environment.json",
    "permissions.json",
    "feedback_preferences.json",
    "feedback_context.json",
})
V2_REASONS = frozenset({"query_failed", "unsupported", "no_bundle", "not_applicable"})


def validate_planned_snapshot(name, value):
    """Pure v2 shape judge, kept independent until protocol wiring lands."""
    schemas = {
        "environment.json": "environment.v1",
        "permissions.json": "permissions.v1",
        "feedback_preferences.json": "feedback_preferences.v1",
        "feedback_context.json": "feedback_context.v1",
    }
    if name not in schemas or not isinstance(value, dict) or set(value) not in ({"schema", "available", "data"},
                                                                                  {"schema", "available", "reason"}):
        return False
    if value.get("schema") != schemas[name] or type(value.get("available")) is not bool:
        return False
    if value["available"]:
        return set(value) == {"schema", "available", "data"} and isinstance(value["data"], dict)
    return set(value) == {"schema", "available", "reason"} and value.get("reason") in V2_REASONS


class PlannedDiagnosticV2ContractTests(SFCase):
    def test_v2_uses_a_fixed_complete_set_of_metadata_files(self):
        self.assertEqual(len(V2_FILES), 4)
        self.assertNotIn("debug.log", V2_FILES)
        self.assertNotIn("config.json", V2_FILES)
        self.assertNotIn("crash_reports", V2_FILES)

    def test_every_available_snapshot_has_a_small_schema_and_data_object(self):
        for name in V2_FILES:
            with self.subTest(name=name):
                self.assertTrue(validate_planned_snapshot(name, {
                    "schema": name.removesuffix(".json") + ".v1",
                    "available": True,
                    "data": {},
                }))

    def test_every_provider_failure_is_explicit_and_reason_is_enumerated(self):
        for name in V2_FILES:
            for reason in V2_REASONS:
                with self.subTest(name=name, reason=reason):
                    self.assertTrue(validate_planned_snapshot(name, {
                        "schema": name.removesuffix(".json") + ".v1",
                        "available": False,
                        "reason": reason,
                    }))
        self.assertFalse(validate_planned_snapshot("permissions.json", {
            "schema": "permissions.v1", "available": False, "reason": "raw exception text"}))

    def test_partial_or_mixed_v2_snapshots_are_rejected_by_the_contract(self):
        self.assertFalse(validate_planned_snapshot("environment.json", {
            "schema": "environment.v1", "available": True, "reason": "query_failed"}))
        self.assertFalse(validate_planned_snapshot("permissions.json", {
            "schema": "permissions.v1", "available": False, "data": {}}))
        self.assertFalse(validate_planned_snapshot("unknown.json", {
            "schema": "unknown.v1", "available": True, "data": {}}))

    def test_v1_remains_distinct_from_and_does_not_require_v2_files(self):
        v1_inventory = {"runtime.json", "export.json", "diagnostics.txt"}
        self.assertTrue(v1_inventory.isdisjoint(V2_FILES))
        self.assertEqual(v1_inventory & V2_FILES, set())


if __name__ == "__main__":
    unittest.main()
