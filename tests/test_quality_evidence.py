# -*- coding: utf-8 -*-
"""质量证据链的核心不变量：证据必须同源、可核对且能发现篡改。"""
from __future__ import annotations

import copy
import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import gate_stamp
from tools import quality_evidence as evidence
from sf_constants import APP_VERSION
from tests._base import SFCase


def isolated_source_root(base: Path) -> Path:
    root = base / "source"
    root.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(["git", "init", "--quiet", str(root)], env=env,
                   check=True, capture_output=True)
    (root / "probe.py").write_text("print('isolated protocol fixture')\n", encoding="utf-8")
    return root


class TestQualityEvidenceValidation(SFCase):
    def setUp(self):
        super().setUp()
        self.record = evidence.template()
        self.record.update({
            "intent": "验证质量证据链",
            "scope": ["tools/quality_evidence.py"],
            "acceptance": ["缺少证据时不能完成"],
            "verification_evidence": [{"command": "true", "exit_code": 0}],
        })

    def test_template_is_development_until_real_evidence_exists(self):
        self.assertEqual("development", self.record["status"])
        self.assertTrue(all(
            item["status"] == ("not_applicable" if item["id"] in evidence.PERMANENT_EXEMPTIONS or item["id"] in evidence.CAPABILITY_BOUNDARIES else "unverified")
            for item in self.record["scenarios"]
        ))
        self.assertTrue(evidence.validate(self.record) == [])

    def test_boolean_exit_code_is_not_an_integer_receipt(self):
        self.record["verification_evidence"][0]["exit_code"] = True
        self.assertTrue(any("exit_code" in item for item in evidence.validate(self.record)))

    def test_acceptance_map_is_required(self):
        self.record.pop("acceptance_map")
        self.assertTrue(any("acceptance_map" in item for item in evidence.validate(self.record)))

    def test_scenario_matrix_rejects_wrong_shapes_and_status_combinations(self):
        cases = [
            (None, "真实场景矩阵必须是数组"),
            (["not-object"], "必须是对象"),
            ([{"id": 1}], "id 必须是字符串"),
            ([{"id": "unknown", "status": "unverified", "required": False,
               "reason": "x"}], "未知真实场景"),
            ([{"id": "local_mac", "status": "broken", "required": True}], "状态无效"),
            ([{"id": "local_mac", "status": "unverified", "required": "yes"}], "required 必须是布尔值"),
            ([{"id": "local_mac", "status": "unverified", "required": False}], "缺少非空 reason"),
            ([{"id": "fullscreen", "status": "not_applicable", "required": True,
               "reason": "x"}], "豁免仍是必需"),
            ([{"id": "local_mac", "status": "passed", "required": False,
               "receipts": []}], "通过场景必须保持"),
            ([{"id": "local_mac", "status": "passed", "required": True,
               "receipts": [""]}], "receipt 路径无效"),
            ([{"id": "local_mac", "status": "unverified", "required": False,
               "reason": "x", "observation": {}}], "不得携带通过证据"),
        ]
        for scenarios, needle in cases:
            with self.subTest(needle=needle):
                record = {"scenarios": scenarios}
                errors = evidence.validate_scenarios(record)
                self.assertTrue(any(needle in item for item in errors), errors)

    def test_release_policy_rejects_missing_or_modified_closed_policy(self):
        record = evidence.template()
        for mutation in ("missing", "exclusions", "boundaries"):
            with self.subTest(mutation=mutation):
                candidate = copy.deepcopy(record)
                if mutation == "missing":
                    candidate.pop("release_qualification", None)
                elif mutation == "exclusions":
                    candidate["release_qualification"]["excluded_scenarios"] = {}
                else:
                    candidate["release_qualification"]["capability_boundaries"] = {}
                errors = evidence.validate_release_policy(candidate)
                self.assertTrue(errors, mutation)

    def test_validate_rejects_non_object_and_incomplete_records(self):
        self.assertIn("记录必须是 JSON 对象", evidence.validate([]))
        errors = evidence.validate({"schema_version": 99, "change_id": "", "intent": ""})
        self.assertTrue(any("schema_version" in item for item in errors))
        self.assertTrue(any("缺少必填字段" in item for item in errors))


class TestReleaseStatusMatrix(SFCase):
    """发布、站点和真实场景必须分别可见，不能由历史读回越级完成。"""

    PUBLISHED_AT = "2026-11-09T11:20:08+00:00"
    DEPLOYED_AT = "2026-11-09T11:21:04+00:00"
    OBSERVED_AT = "2026-11-09T11:22:44+00:00"

    def record(self, publication=None, site=None, scenarios=None, **extra):
        record = evidence.template()
        record.update({"publication": publication, "site": site})
        if scenarios is not None:
            record["scenarios"] = scenarios
        record.update(extra)
        return record

    def all_required_scenarios(self):
        scenarios = copy.deepcopy(evidence.template()["scenarios"])
        for scenario in scenarios:
            if scenario["required"]:
                scenario.update(status="passed", observed_at=self.OBSERVED_AT)
        return scenarios

    def expected(self, **overrides):
        result = {
            "publication_status": "pending",
            "site_status": "pending",
            "acceptance_status": "pending",
            "verified_scenarios": [],
            "unverified_scenarios": [
                "local_mac", "fresh_install", "upgrade_install", "ota_rollback",
            ],
            "not_applicable_scenarios": [
                "second_mac", "intel", "camera_upgrade", "fullscreen", "browser_download",
            ],
        }
        result.update(overrides)
        return result

    def test_status_matrix_keeps_publication_site_and_acceptance_states_separate(self):
        cases = (
            (
                "not published",
                self.record(
                    publication={"status": "not_published"},
                    site={"status": "not_deployed"},
                ),
                self.expected(publication_status="not_published", site_status="not_deployed"),
            ),
            (
                "published but not deployed",
                self.record(
                    publication={"status": "published", "published_at": self.PUBLISHED_AT},
                    site={"status": "not_deployed"},
                ),
                self.expected(publication_status="published", site_status="not_deployed"),
            ),
            (
                "deployed with required scenario unverified",
                self.record(
                    publication={"status": "published", "published_at": self.PUBLISHED_AT},
                    site={"status": "deployed", "deployed_at": self.DEPLOYED_AT},
                ),
                self.expected(publication_status="published", site_status="deployed"),
            ),
            (
                "all required scenarios passed",
                self.record(
                    publication={"status": "published", "published_at": self.PUBLISHED_AT},
                    site={"status": "deployed", "deployed_at": self.DEPLOYED_AT},
                    scenarios=self.all_required_scenarios(),
                ),
                self.expected(
                    publication_status="published",
                    site_status="deployed",
                    acceptance_status="complete",
                    verified_scenarios=[
                        "local_mac", "fresh_install", "upgrade_install", "ota_rollback",
                    ],
                    unverified_scenarios=[],
                ),
            ),
        )
        for name, record, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(expected, evidence.status_matrix(record))

    def test_status_matrix_treats_malformed_publication_and_site_as_not_ready(self):
        cases = (
            ({"publication": [], "site": {"status": "deployed", "deployed_at": self.DEPLOYED_AT}},
             "not_published", "deployed"),
            ({"publication": {"status": "published", "published_at": " "},
              "site": {"status": []}},
             "not_published", "not_deployed"),
            ({"publication": {"status": "published", "published_at": 1},
              "site": {"status": "deployed", "deployed_at": " "}},
             "not_published", "not_deployed"),
        )
        for values, publication_status, site_status in cases:
            with self.subTest(values=values):
                result = evidence.status_matrix(self.record(**values))
                self.assertEqual(publication_status, result["publication_status"])
                self.assertEqual(site_status, result["site_status"])

    def test_status_matrix_keeps_no_required_scenarios_pending(self):
        scenarios = [
            {"id": "optional", "status": "passed", "required": False,
             "receipts": ["evidence/optional.json"]},
        ]
        result = evidence.status_matrix(self.record(
            publication={"status": "published", "published_at": self.PUBLISHED_AT},
            site={"status": "deployed", "deployed_at": self.DEPLOYED_AT},
            scenarios=scenarios,
        ))
        self.assertEqual("pending", result["acceptance_status"])
        self.assertEqual([], result["verified_scenarios"])
        self.assertEqual(list(evidence.RELEASE_REQUIRED_SCENARIOS), result["unverified_scenarios"])
        self.assertEqual(
            ["second_mac", "intel", "camera_upgrade", "fullscreen", "browser_download"],
            result["not_applicable_scenarios"],
        )

    def test_status_matrix_reports_verified_and_partial_required_scenarios(self):
        scenarios = [
            {"id": "verified", "status": "passed", "required": True,
             "receipts": ["evidence/verified.json"]},
            {"id": "pending", "status": "unverified", "required": True,
             "receipts": []},
        ]
        result = evidence.status_matrix(self.record(scenarios=scenarios))
        self.assertEqual("pending", result["acceptance_status"])
        self.assertEqual([], result["verified_scenarios"])
        self.assertEqual(list(evidence.RELEASE_REQUIRED_SCENARIOS), result["unverified_scenarios"])

    def test_status_matrix_requires_evidence_for_passed_scenarios(self):
        scenarios = [{"id": "verified", "status": "passed", "required": True,
                      "receipts": []}]
        result = evidence.status_matrix(self.record(scenarios=scenarios))
        self.assertEqual("pending", result["acceptance_status"])
        self.assertEqual([], result["verified_scenarios"])
        self.assertEqual(list(evidence.RELEASE_REQUIRED_SCENARIOS), result["unverified_scenarios"])

        self.assertEqual(self.expected(), evidence.status_matrix(self.record()))

    def test_retrospective_readback_cannot_complete_acceptance(self):
        record = self.record(
            publication={"status": "published", "published_at": self.PUBLISHED_AT},
            site={"status": "deployed", "deployed_at": self.DEPLOYED_AT},
            scenarios=self.all_required_scenarios(),
            evidence_kind="retrospective_readback_not_execution_receipt",
        )
        result = evidence.status_matrix(record)
        self.assertEqual("published", result["publication_status"])
        self.assertEqual("deployed", result["site_status"])
        self.assertEqual("pending", result["acceptance_status"])

    def test_status_matrix_with_no_required_policy_is_pending(self):
        with patch.object(evidence, "RELEASE_REQUIRED_SCENARIOS", ()):
            result = evidence.status_matrix(self.record())
        self.assertEqual("pending", result["acceptance_status"])

    def test_status_matrix_does_not_allow_record_to_disable_required_policy(self):
        scenarios = [{"id": "local_mac", "status": "passed", "required": False,
                      "receipts": ["evidence/local.json"]}]
        result = evidence.status_matrix(self.record(scenarios=scenarios))
        self.assertEqual("partial", result["acceptance_status"])
        self.assertEqual(["local_mac"], result["verified_scenarios"])
        self.assertEqual(
            [name for name in evidence.RELEASE_REQUIRED_SCENARIOS if name != "local_mac"],
            result["unverified_scenarios"],
        )

    def test_status_matrix_ignores_malformed_scenario_entries(self):
        result = evidence.status_matrix(self.record(scenarios=["not-an-object", {"id": 7}]))
        self.assertEqual("pending", result["acceptance_status"])
        self.assertEqual([], result["verified_scenarios"])
        self.assertEqual(
            list(evidence.RELEASE_REQUIRED_SCENARIOS),
            result["unverified_scenarios"],
        )
        self.assertEqual(
            ["second_mac", "intel", "camera_upgrade", "fullscreen", "browser_download"],
            result["not_applicable_scenarios"],
        )


class TestReleaseTimeline(SFCase):
    """发布历史时间线只投影已有证据，不猜测缺失事实。"""

    VERSION = APP_VERSION
    EVENTS = (
        "candidate_frozen",
        "zip_uploaded",
        "dmg_uploaded",
        "source_uploaded",
        "checksums_uploaded",
        "ota_pointer_updated",
        "site_deployed",
        "acceptance_completed",
    )
    TIMES = {
        "candidate_frozen": "2026-11-09T10:00:00+00:00",
        "zip_uploaded": "2026-11-09T10:01:00+00:00",
        "dmg_uploaded": "2026-11-09T10:02:00+00:00",
        "source_uploaded": "2026-11-09T10:03:00+00:00",
        "checksums_uploaded": "2026-11-09T10:04:00+00:00",
        "ota_pointer_updated": "2026-11-09T10:05:00+00:00",
        "site_deployed": "2026-11-09T10:06:00+00:00",
        "acceptance_completed": "2026-11-09T10:07:00+00:00",
    }

    def record_with_timeline(self, entries=None):
        record = evidence.template()
        record["release_artifacts"]["version"] = self.VERSION
        record["release_timeline"] = entries if entries is not None else {
            event: {
                "at": self.TIMES[event],
                "version": self.VERSION,
                "status": "verified",
                "evidence_path": f"evidence/{event}.json",
            }
            for event in self.EVENTS
        }
        return record

    def test_returns_all_release_events_with_required_fields(self):
        result = evidence.release_timeline(self.record_with_timeline(), self.VERSION)

        self.assertEqual([], result["errors"])
        self.assertEqual(list(self.EVENTS), [item["event"] for item in result["events"]])
        for item in result["events"]:
            with self.subTest(event=item["event"]):
                self.assertEqual(
                    {"event", "at", "version", "status", "evidence_path"},
                    set(item),
                )
                self.assertEqual(self.VERSION, item["version"])
                self.assertEqual("verified", item["status"])
                self.assertTrue(item["evidence_path"])

    def test_missing_historical_evidence_is_unverified_without_guessing_time(self):
        record = self.record_with_timeline({
            "candidate_frozen": {
                "at": self.TIMES["candidate_frozen"],
                "version": self.VERSION,
                "status": "verified",
                "evidence_path": "evidence/candidate.json",
            }
        })
        original = copy.deepcopy(record)
        with patch.object(evidence, "utc_now", side_effect=AssertionError("不应读取当前时间")):
            result = evidence.release_timeline(record, self.VERSION)

        self.assertEqual([], result["errors"])
        missing = {
            item["event"]: item for item in result["events"]
            if item["event"] != "candidate_frozen"
        }
        self.assertEqual(set(self.EVENTS) - {"candidate_frozen"}, set(missing))
        for item in missing.values():
            self.assertEqual("unverified", item["status"])
            self.assertIsNone(item["at"])
            self.assertIsNone(item["evidence_path"])
        self.assertEqual(original, record, "时间线投影不得改写历史记录")

    def test_timeline_rejects_nonzero_utc_offsets(self):
        record = self.record_with_timeline()
        record["release_timeline"]["zip_uploaded"]["at"] = "2026-11-09T10:01:00+08:00"
        result = evidence.release_timeline(record, self.VERSION)
        event = next(item for item in result["events"] if item["event"] == "zip_uploaded")
        self.assertTrue(any("时间无效" in error for error in result["errors"]), result)
        self.assertEqual("unverified", event["status"])
        self.assertIsNone(event["at"])
        self.assertIsNone(event["evidence_path"])

    def test_timeline_downgrades_missing_evidence_or_status(self):
        record = self.record_with_timeline()
        record["release_timeline"]["dmg_uploaded"].pop("evidence_path")
        record["release_timeline"]["source_uploaded"]["status"] = ""
        result = evidence.release_timeline(record, self.VERSION)
        for event_name in ("dmg_uploaded", "source_uploaded"):
            with self.subTest(event=event_name):
                event = next(item for item in result["events"] if item["event"] == event_name)
                self.assertEqual("unverified", event["status"])
                self.assertIsNone(event["at"])
                self.assertIsNone(event["evidence_path"])
        self.assertEqual([], result["errors"])

        record = self.record_with_timeline()
        result = evidence.release_timeline(record, self.VERSION)
        at = {item["event"]: item["at"] for item in result["events"]}

        for asset_event in (
            "zip_uploaded", "dmg_uploaded", "source_uploaded", "checksums_uploaded"
        ):
            with self.subTest(asset_event=asset_event):
                self.assertLess(at[asset_event], at["ota_pointer_updated"])

    def test_timeline_reports_time_order_anomaly(self):
        record = self.record_with_timeline()
        record["release_timeline"]["ota_pointer_updated"]["at"] = "2026-11-09T09:59:00+00:00"
        result = evidence.release_timeline(record, self.VERSION)
        self.assertTrue(result["errors"])
        self.assertTrue(any("时间" in error for error in result["errors"]), result)

    def test_timeline_reports_record_and_asset_version_mismatches(self):
        cases = (
            ("record version", {"version": "historical-version"}, "record.version"),
            ("asset version", {"release_artifacts": {"version": "historical-version"}},
             "release_artifacts.version"),
            ("event version", {"release_timeline": {
                "zip_uploaded": {
                    "at": self.TIMES["zip_uploaded"],
                    "version": "historical-version",
                    "status": "verified",
                    "evidence_path": "evidence/zip.json",
                },
            }}, "时间线事件版本不一致"),
        )
        for name, mutation, needle in cases:
            with self.subTest(name=name):
                record = self.record_with_timeline()
                if name == "event version":
                    record["release_timeline"]["zip_uploaded"].update(
                        mutation["release_timeline"]["zip_uploaded"]
                    )
                else:
                    record.update(mutation)
                result = evidence.release_timeline(record, self.VERSION)
                self.assertTrue(any(needle in error for error in result["errors"]), result)

    def test_timeline_rejects_invalid_or_naive_event_times(self):
        cases = (
            ("invalid", "not-a-time"),
            ("naive", "2026-11-09T10:01:00"),
            ("missing", None),
        )
        for name, value in cases:
            with self.subTest(name=name):
                record = self.record_with_timeline()
                if value is None:
                    record["release_timeline"]["zip_uploaded"].pop("at")
                else:
                    record["release_timeline"]["zip_uploaded"]["at"] = value
                result = evidence.release_timeline(record, self.VERSION)
                event = next(item for item in result["events"] if item["event"] == "zip_uploaded")
                if value is not None:
                    self.assertTrue(any("时间无效" in error for error in result["errors"]), result)
                    self.assertEqual("unverified", event["status"])
                    self.assertIsNone(event["at"])
                    self.assertIsNone(event["evidence_path"])
                else:
                    self.assertEqual([], result["errors"])
                    self.assertIsNone(event["at"])
                    self.assertEqual("unverified", event["status"])
    def test_timeline_reports_all_phase_order_violations(self):
        record = self.record_with_timeline()
        record["release_timeline"]["candidate_frozen"]["at"] = self.TIMES["acceptance_completed"]
        record["release_timeline"]["site_deployed"]["at"] = self.TIMES["zip_uploaded"]
        record["release_timeline"]["acceptance_completed"]["at"] = self.TIMES["candidate_frozen"]
        result = evidence.release_timeline(record, self.VERSION)
        self.assertGreaterEqual(len(result["errors"]), 3)
        self.assertTrue(any("candidate_frozen" in error for error in result["errors"]), result)
        self.assertTrue(any("site_deployed" in error for error in result["errors"]), result)
        self.assertTrue(any("acceptance_completed" in error for error in result["errors"]), result)

    def test_candidate_freeze_rejects_same_directory_non_receipt_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            attachment = base / "candidate.json"
            attachment.write_text(json.dumps({"claim": "candidate frozen"}), encoding="utf-8")
            record = self.record_with_timeline({
                "candidate_frozen": {
                    "at": self.TIMES["candidate_frozen"],
                    "version": self.VERSION,
                    "status": "verified",
                    "evidence_path": attachment.name,
                    "evidence_sha256": evidence.sha256_file(attachment),
                }
            })

            result = evidence.release_timeline(record, self.VERSION, base,
                                                required_events=("candidate_frozen",))

        candidate = next(item for item in result["events"] if item["event"] == "candidate_frozen")
        self.assertEqual("unverified", candidate["status"])
        self.assertIsNone(candidate["at"])
        self.assertTrue(any("candidate_frozen" in item for item in result["errors"]), result)

    def test_candidate_freeze_rejects_receipt_with_tool_name_only_as_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            clean_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
            subprocess.run(["git", "init", "--quiet", str(source)], env=clean_env,
                           check=True, capture_output=True)
            (source / "probe.py").write_text("print('fixture')\n", encoding="utf-8")
            subprocess.run(["git", "add", "probe.py"], cwd=source, env=clean_env,
                           check=True, capture_output=True)
            subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                            "commit", "-qm", "fixture"], cwd=source, env=clean_env,
                           check=True, capture_output=True)
            app = base / "Skyrocket.app"
            app.mkdir()
            archive = base / "Skyrocket.zip"
            archive.write_bytes(b"zip")
            receipt_path = evidence.run_command(
                source, base / "evidence", ["/usr/bin/true", "tools/verify_candidate_delivery.py",
                                              "--app", app.relative_to(base).as_posix(),
                                              "--zip", archive.relative_to(base).as_posix()], 3,
                timeout=10)
            receipt = evidence.load(receipt_path)
            record = self.record_with_timeline({
                "candidate_frozen": {
                    "at": receipt["ended_at"],
                    "version": self.VERSION,
                    "status": "verified",
                    "evidence_path": receipt_path.relative_to(base).as_posix(),
                    "evidence_sha256": evidence.sha256_file(receipt_path),
                }
            })
            record["artifact_fingerprint"]["source"] = receipt["source_start"]
            record["release_artifacts"]["roles"] = {
                "app": {"path": app.relative_to(base).as_posix()},
                "zip": {"path": archive.relative_to(base).as_posix()},
            }

            result = evidence.release_timeline(record, self.VERSION, base,
                                                required_events=("candidate_frozen",))

        candidate = next(item for item in result["events"] if item["event"] == "candidate_frozen")
        self.assertEqual("unverified", candidate["status"])
        self.assertTrue(any("候选冻结" in item for item in result["errors"]), result)

    def test_required_timeline_receipt_covers_missing_tampered_and_valid_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            receipt_path = base / "receipt.json"
            receipt_path.write_text("receipt", encoding="utf-8")
            app, archive = "dist/Skyrocket.app", "update.zip"
            command = ["./.venv/bin/python3", "tools/verify_candidate_delivery.py", "--app", app,
                       "--zip", archive, "--out", "delivery.json", "--old-app", "old.app"]
            receipt = {"ended_at": "2030-01-01T00:00:00+00:00", "argv": command}
            def record(entry):
                value = self.record_with_timeline({"candidate_frozen": entry})
                value["artifact_fingerprint"]["source"] = "fp"
                value["release_artifacts"]["roles"] = {"app": {"path": app}, "zip": {"path": archive}}
                return value
            common = {"at": receipt["ended_at"], "version": self.VERSION, "status": "verified",
                      "evidence_path": receipt_path.name, "evidence_sha256": evidence.sha256_file(receipt_path),
                      "command": command}
            with patch.object(evidence, "check_receipt", return_value=[]), \
                    patch.object(evidence, "load", return_value=receipt):
                cases = (
                    ({key: value for key, value in common.items() if key != "evidence_sha256"}, "SHA-256"),
                    ({**common, "evidence_path": ""}, "路径无效"),
                    ({**common, "evidence_path": "../escape.json"}, "路径无效"),
                    ({**common, "evidence_sha256": "changed"}, "缺失或篡改"),
                    ({**common, "at": "2030-01-01T00:01:00+00:00"}, "结束时刻"),
                )
                for entry, needle in cases:
                    with self.subTest(needle=needle):
                        result = evidence.release_timeline(record(entry), self.VERSION, base,
                                                           required_events=("candidate_frozen",))
                        self.assertTrue(any(needle in item for item in result["errors"]), result)
                valid = evidence.release_timeline(record(common), self.VERSION, base,
                                                  required_events=("candidate_frozen",))
                self.assertEqual([], valid["errors"])
                absent_record = self.record_with_timeline({})
                absent_record["artifact_fingerprint"]["source"] = "fp"
                absent_record["release_artifacts"]["roles"] = {"app": {"path": app}, "zip": {"path": archive}}
                missing = evidence.release_timeline(absent_record, self.VERSION, base,
                                                    required_events=("candidate_frozen",))
                self.assertTrue(any("事件缺失" in item for item in missing["errors"]), missing)
                other = self.record_with_timeline({"zip_uploaded": common})
                other["artifact_fingerprint"]["source"] = "fp"
                result = evidence.release_timeline(other, self.VERSION, base,
                                                   required_events=("zip_uploaded",))
                self.assertEqual([], result["errors"])


class TestArtifactFingerprint(SFCase):
    def setUp(self):
        super().setUp()
    def test_content_and_permissions_are_part_of_artifact_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact"
            path.write_bytes(b"same")
            first = evidence.artifact_token(path)
            os.chmod(path, 0o700)
            second = evidence.artifact_token(path)
            self.assertNotEqual(first, second)

    def test_top_level_symlink_and_escape_link_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_bytes(b"payload")
            top = root / "top"
            top.symlink_to(target)
            with self.assertRaises(ValueError):
                evidence.artifact_token(top)

            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "inside").symlink_to(target)
            with self.assertRaises(ValueError):
                evidence.artifact_token(bundle)

    def test_source_snapshot_rejects_empty_input_and_matches_gate_stamp_for_new_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], env=evidence.gate_stamp.git_env(),
                           check=True, capture_output=True)
            with self.assertRaisesRegex(ValueError, "输入清单为空"):
                evidence.source_snapshot(root)

            isolated = isolated_source_root(root)
            tracked = isolated / "probe.py"
            os.chmod(tracked, 0o700)
            added = isolated / "new.py"
            added.write_text("print('new')\n", encoding="utf-8")
            expected = gate_stamp.current_info(str(isolated))
            actual = evidence.source_snapshot(isolated)
            self.assertEqual(expected["fingerprint"], actual["fingerprint"])
            self.assertEqual(expected["files"], len(actual["files"]))
            self.assertTrue(any(item["path"] == "new.py" for item in actual["files"]))


class TestExecutionReceipt(SFCase):
    def setUp(self):
        super().setUp()

    def _root(self, directory):
        return isolated_source_root(Path(directory))

    def test_run_command_writes_isolated_receipt_and_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            output = Path(directory) / "evidence"
            receipt_path = evidence.run_command(
                root, output, ["/usr/bin/true"], level=1, timeout=10
            )
            receipt = evidence.load(receipt_path)
            self.assertEqual(0, receipt["exit_code"])
            self.assertTrue(receipt["stable"])
            self.assertEqual([], evidence.check_receipt(
                receipt_path, receipt["source_start"]
            ))
            self.assertTrue((receipt_path.parent / "command.log").is_file())
            self.assertNotEqual(str(output), receipt_path.parent.name)

    def test_log_tampering_invalidates_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            receipt_path = evidence.run_command(
                root, Path(directory) / "evidence", ["/usr/bin/true"], level=1, timeout=10
            )
            receipt = evidence.load(receipt_path)
            log = receipt_path.parent / receipt["log"]
            log.write_text("tampered\n", encoding="utf-8")
            errors = evidence.check_receipt(receipt_path, receipt["source_start"])
            self.assertTrue(any("日志" in item for item in errors))

    def test_workspace_change_invalidates_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            with patch.object(evidence, "workspace_token", side_effect=["before", "after"]):
                receipt_path = evidence.run_command(
                    root, Path(directory) / "evidence", ["/usr/bin/true"], level=1
                )
            receipt = evidence.load(receipt_path)
            self.assertTrue(any("并发修改" in item for item in evidence.check_receipt(
                receipt_path, receipt["source_start"]
            )))

    def test_stale_and_future_receipts_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            receipt_path = evidence.run_command(
                root, Path(directory) / "evidence", ["/usr/bin/true"], level=1
            )
            receipt = evidence.load(receipt_path)
            for hours in (-25, 1):
                moment = evidence.utc_now()
                receipt["ended_at"] = (moment + evidence.dt.timedelta(hours=hours)).isoformat()
                evidence.atomic_json(receipt_path, receipt)
                with patch.object(evidence, "utc_now", return_value=moment):
                    self.assertTrue(any("有效窗口" in item for item in evidence.check_receipt(
                        receipt_path, receipt["source_start"]
                    )))

    def test_invalid_level_cannot_be_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            for level in (True, 0, 5):
                with self.assertRaises(ValueError):
                    evidence.run_command(root, Path(directory) / "evidence", ["true"], level)

    def test_lock_rejects_a_second_verification_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            lock_path = root / "build" / ".quality-session.lock"
            lock_path.parent.mkdir(exist_ok=True)
            with lock_path.open("a+") as lock:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
                with self.assertRaises(ValueError):
                    evidence.run_command(
                        root, Path(directory) / "evidence", ["/usr/bin/true"], level=1
                    )
                fcntl.flock(lock, fcntl.LOCK_UN)


class TestScenarioEvidence(SFCase):
    def setUp(self):
        super().setUp()
        self.track_patch(patch.object(evidence, "utc_now", return_value=
                                     evidence.dt.datetime.fromisoformat("2030-01-01T12:00:00+00:00")))
    def test_arbitrary_success_cannot_prove_a_real_scenario(self):
        for name in evidence.SCENARIOS:
            scenario = {"id": name, "status": "passed", "receipts": ["true.json"]}
            self.assertTrue(evidence.scenario_errors(scenario, "source", Path(".")))

    def test_observation_requires_each_signal_and_untampered_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            raw = base / "capture.json"
            raw.write_text("real observation fixture", encoding="utf-8")
            scenario = {"id": "fullscreen", "status": "passed", "observation": {
                "source": "source", "device_id": "fixture-device", "observer": "fixture",
                "procedure": "fixture procedure", "observed_at": "2030-01-01T12:00:00+00:00",
                "artifact_sha256": "fixture-artifact",
                "signals": {signal: True for signal in evidence.SCENARIO_SIGNALS["fullscreen"]},
                "attachments": [{"path": raw.name, "sha256": evidence.sha256_file(raw)}],
            }}
            self.assertEqual([], evidence.scenario_errors(scenario, "source", base))
            scenario["observation"]["signals"]["editor_onscreen"] = False
            self.assertTrue(evidence.scenario_errors(scenario, "source", base))
            scenario["observation"]["signals"]["editor_onscreen"] = True
            raw.write_text("changed", encoding="utf-8")
            self.assertTrue(evidence.scenario_errors(scenario, "source", base))


class TestVerifyMalformedInputs(SFCase):
    def setUp(self):
        super().setUp()
        self.record = evidence.template()
        self.record.update({
            "intent": "验证损坏输入不会令核验器崩溃",
            "scope": ["tools/quality_evidence.py"],
            "acceptance": ["证据可核验"],
            "acceptance_map": [{"acceptance": "证据可核验", "evidence_index": 0}],
            "verification_evidence": [{"command": "/usr/bin/true", "exit_code": 0,
                                         "receipt": "missing.json"}],
            "artifact_fingerprint": {"artifacts": [{"path": "artifact"}]},
        })

    def _verify(self, record=None, manifest_value=None):
        record = record or self.record
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            record_path = base / "change.json"
            manifest_path = base / "change.manifest.json"
            record_path.write_text(json.dumps(record), encoding="utf-8")
            if manifest_value is not None:
                manifest_path.write_text(manifest_value, encoding="utf-8")
            with patch.object(evidence, "source_snapshot", return_value={"fingerprint": "source"}), \
                 patch.object(evidence, "impact", return_value={"unmapped_changes": []}), \
                 patch.object(evidence, "manifest", side_effect=ValueError("bad artifact")):
                return evidence.verify(evidence.ROOT, record, base, manifest_path)

    def test_missing_manifest_is_reported_not_raised(self):
        errors = self._verify()
        self.assertTrue(any("manifest" in item for item in errors))

    def test_malformed_manifest_is_reported_not_raised(self):
        errors = self._verify(manifest_value="[]")
        self.assertTrue(any("manifest" in item for item in errors))

    def test_bad_receipt_reference_is_reported_not_raised(self):
        record = dict(self.record)
        record["verification_evidence"] = [{
            "command": "/usr/bin/true", "exit_code": 0, "receipt": "../escape.json"
        }]
        errors = self._verify(record)
        self.assertTrue(errors)
        self.assertTrue(any("receipt" in item for item in errors))

    def test_non_object_scenario_is_reported_not_raised(self):
        record = dict(self.record)
        record["scenarios"] = ["not-an-object"]
        errors = self._verify(record)
        self.assertTrue(any("场景" in item for item in errors))


class TestEvidenceSelfContainment(SFCase):
    def setUp(self):
        super().setUp()
    def test_git_environment_is_not_inherited_by_evidence_git_calls(self):
        # discipline-ok(R5): 必须直接核验给 git 子进程的环境不携带父进程控制变量。
        with patch.dict(os.environ, {"GIT_DIR": "/wrong", "GIT_INDEX_FILE": "/wrong"}):
            env = evidence._git_env()  # discipline-ok(R5): 核验 git 子进程环境边界
        self.assertNotIn("GIT_DIR", env)
        self.assertNotIn("GIT_INDEX_FILE", env)

    def test_relative_references_cannot_escape_evidence_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for value in ("../receipt.json", "/tmp/receipt.json", ""):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    evidence.resolve_reference(base, value)
            (base / "escape").symlink_to(base.parent)
            with self.assertRaises(ValueError):
                evidence.resolve_reference(base, "escape/receipt.json")
            self.assertEqual(base.resolve() / "receipt.json", evidence.resolve_reference(base, "receipt.json"))

    def test_run_cli_rejects_concurrent_workspace_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = isolated_source_root(base)
            output = base / "sessions"
            with patch.object(evidence, "workspace_token", side_effect=["before", "after"]):
                code = evidence.main([
                    "run", str(output), "--root", str(root), "--", "/usr/bin/true"
                ])
            self.assertNotEqual(0, code)
            receipts = list(output.rglob("receipt.json"))
            self.assertEqual(1, len(receipts))
            receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
            self.assertFalse(receipt["workspace_stable"])
            self.assertTrue(any(
                "工作区发生并发修改" in error
                for error in evidence.check_receipt(receipts[0], receipt["source_start"])
            ))

    def test_malformed_receipt_is_a_failure_not_an_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            for value in ("not json", "[]", "{}"):
                path.write_text(value, encoding="utf-8")
                self.assertTrue(evidence.check_receipt(path, "source"))

    def test_receipt_json_is_machine_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = isolated_source_root(Path(directory))
            receipt_path = evidence.run_command(
                root, Path(directory) / "evidence", ["/usr/bin/true"], level=2, timeout=10
            )
            parsed = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual("quality_evidence.run", parsed["producer"])
            self.assertEqual(2, parsed["level"])


class TestVerifyIntegration(SFCase):
    """真实执行隔离探针；合成观测仅测试核验协议，不代表产品真机通过。"""

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "source"
        self.root.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.root)],
                       env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
                       check=True, capture_output=True)
        (self.root / "probe.py").write_text("print('isolated protocol fixture')\n", encoding="utf-8")
        self.artifact = self.base / "artifact.bin"
        self.artifact.write_bytes(b"isolated artifact fixture")
        self.receipt_path = evidence.run_command(
            self.root, self.base / "sessions", ["/usr/bin/true"], level=3, timeout=10)
        receipt = evidence.load(self.receipt_path)
        reference = self.receipt_path.relative_to(self.base).as_posix()
        self.record = evidence.template()
        self.record.update({
            "intent": "隔离核验协议集成测试，非产品真机验收",
            "scope": ["probe.py"], "acceptance": ["核验协议闭环"],
            "acceptance_map": [{"acceptance": "核验协议闭环", "evidence_index": 0}],
            "verification_evidence": [{"command": shlex.join(receipt["argv"]),
                                       "exit_code": 0, "receipt": reference}],
            "artifact_fingerprint": {"artifacts": [{"path": self.artifact.name}]},
            "unverified_limitations": ["合成观测仅验证协议；全部产品真实场景均未执行"],
        })
        for scenario in self.record["scenarios"]:
            if scenario["id"] not in evidence.CAPABILITY_BOUNDARIES:
                scenario.update(required=False, reason="隔离协议测试未执行此产品场景")
        local = self.record["scenarios"][0]
        local.update(status="passed", required=True, receipts=[reference], observation={
            "source": receipt["source_start"], "device_id": receipt["machine"]["device_id"],
            "observer": "synthetic-protocol-fixture", "procedure": "仅验证数据绑定协议",
            "observed_at": receipt["started_at"],
            "artifact_sha256": evidence.artifact_token(self.artifact)["sha256"],
            "signals": {key: True for key in evidence.SCENARIO_SIGNALS["local_mac"]},
            "attachments": [{"path": reference, "sha256": evidence.sha256_file(self.receipt_path)}],
        })
        self.manifest_path = self.base / "change.manifest.json"
        self.seal()

    def seal(self):
        evidence.atomic_json(self.manifest_path, evidence.manifest(self.root, self.record, self.base))

    def verify(self):
        return evidence.verify(self.root, self.record, self.base, self.manifest_path)

    def make_release_candidate(self):
        app = self.base / "Skyrocket.app"
        (app / "Contents").mkdir(parents=True)
        (app / "Contents" / "Info.plist").write_bytes(b"app-fixture")
        dmg = self.base / "Skyrocket-1.4.92.dmg"
        dmg.write_bytes(b"dmg-fixture")
        archive = self.base / "Skyrocket-1.4.92-macos.zip"
        archive.write_bytes(b"zip-fixture")
        ota = self.base / "Skyrocket-update.json"
        ota.write_text(json.dumps({
            "version": "1.4.92",
            "archive": {"url": "https://example.invalid/Skyrocket-1.4.92-macos.zip",
                        "size": archive.stat().st_size,
                        "sha256": evidence.sha256_file(archive)},
        }), encoding="utf-8")
        roles = {"app": app, "dmg": dmg, "zip": archive, "ota_manifest": ota}
        self.record.update({
            "status": "release_candidate",
            "release_qualification": {"stage": "pre_release",
                                       "policy_version": evidence.RELEASE_POLICY_VERSION,
                                       "excluded_scenarios": dict(evidence.PERMANENT_EXEMPTIONS),
                                       "capability_boundaries": copy.deepcopy(evidence.CAPABILITY_BOUNDARIES)},
            "release_artifacts": {
                "version": "1.4.92",
                "roles": {name: {"path": path.relative_to(self.base).as_posix()}
                          for name, path in roles.items()},
                "ota": {"archive_role": "zip", "manifest_role": "ota_manifest"},
            },
            "artifact_fingerprint": {"artifacts": [
                {"path": path.relative_to(self.base).as_posix()} for path in roles.values()
            ]},
        })
        for scenario in self.record["scenarios"]:
            scenario.update(required=True, reason="发布后真实设备证据待补")
            if scenario["id"] in evidence.CAPABILITY_BOUNDARIES:
                scenario.update(evidence.CAPABILITY_BOUNDARIES[scenario["id"]], receipts=[])
            elif scenario["id"] in evidence.PERMANENT_EXEMPTIONS:
                scenario.update(status="not_applicable", required=False,
                                reason=evidence.PERMANENT_EXEMPTIONS[scenario["id"]], receipts=[])
        local = copy.deepcopy(self.record["scenarios"][0])
        artifact_hash = evidence.artifact_token(archive)["sha256"]
        for name in evidence.RELEASE_PRE_SCENARIOS:
            scenario = next(item for item in self.record["scenarios"] if item["id"] == name)
            scenario.update(status="passed", required=True, receipts=local["receipts"],
                            observation=copy.deepcopy(local["observation"]))
            scenario["observation"]["artifact_sha256"] = artifact_hash
            scenario["observation"]["signals"] = {
                signal: True for signal in evidence.SCENARIO_SIGNALS[name]}
        self.seal()
        return archive

    def test_closed_policy_rejects_missing_policy_and_false_exemption_status(self):
        record = evidence.template()
        record.pop("release_qualification")
        self.assertTrue(any("缺少 release_qualification" in item
                            for item in evidence.validate_release_policy(record)))
        record = evidence.template()
        scenario = next(item for item in record["scenarios"] if item["id"] == "fullscreen")
        scenario.update(status="unverified", required=True)
        self.assertTrue(any("not_applicable + required=false" in item
                            for item in evidence.validate_release_policy(record)))

    def test_permanent_exemptions_are_closed_and_required_flags_cannot_bypass(self):
        self.make_release_candidate()
        self.assertEqual([], evidence.validate_release_policy(self.record))
        self.record["release_qualification"]["excluded_scenarios"]["ota_rollback"] = "绕过"
        errors = evidence.validate_release_policy(self.record)
        self.assertTrue(any("严格等于" in item for item in errors))
        self.record["release_qualification"]["excluded_scenarios"] = dict(evidence.PERMANENT_EXEMPTIONS)
        ota = next(item for item in self.record["scenarios"] if item["id"] == "ota_rollback")
        ota["required"] = False
        self.assertTrue(any("required=false 绕过" in item
                            for item in evidence.validate_release_policy(self.record)))

    def test_complete_uses_valid_required_scenarios_not_exemptions(self):
        self.make_release_candidate()
        self.record["release_qualification"].update(stage="complete")
        local = self.record["scenarios"][0]
        for scenario in self.record["scenarios"]:
            if scenario["id"] not in evidence.RELEASE_REQUIRED_SCENARIOS:
                continue
            scenario.update(status="passed", required=True, receipts=local["receipts"],
                            observation=copy.deepcopy(local["observation"]))
            scenario["observation"]["signals"] = {
                signal: True for signal in evidence.SCENARIO_SIGNALS[scenario["id"]]}
        self.seal()
        self.assertEqual([], evidence.verify_release_candidate(
            self.root, self.record, self.base, self.manifest_path))
        for stage in ("post_release", "complete"):
            for name in evidence.RELEASE_REQUIRED_SCENARIOS:
                with self.subTest(stage=stage, missing=name):
                    broken = copy.deepcopy(self.record)
                    broken["release_qualification"]["stage"] = stage
                    scenario = next(item for item in broken["scenarios"] if item["id"] == name)
                    scenario.update(status="unverified", reason="缺少真实场景证据", receipts=[])
                    scenario.pop("observation", None)
                    evidence.atomic_json(self.manifest_path, evidence.manifest(self.root, broken, self.base))
                    errors = evidence.verify_release_candidate(
                        self.root, broken, self.base, self.manifest_path)
                    self.assertTrue(any("必需场景" in item or "全部真实场景" in item for item in errors), errors)
        self.seal()
        report = evidence.qualification_report(self.root, self.record, self.base, self.manifest_path)
        self.assertEqual("qualified", report["status"])
        self.assertEqual("not_applicable", report["release"]["scenarios"]["intel"])
        self.assertNotIn("intel", report["policy"]["required_scenarios"])
        self.assertIn("ota_rollback", report["policy"]["required_scenarios"])
        self.assertEqual(4, len(report["policy"]["permanent_exemptions"]))

    def test_capability_boundary_cannot_expand_change_reason_or_claim_success(self):
        self.make_release_candidate()
        baseline = copy.deepcopy(self.record)
        mutations = ("extra", "reason", "missing", "passed", "required", "scenario_reason", "fifth_exemption")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.record = copy.deepcopy(baseline)
                qualification = self.record["release_qualification"]
                intel = next(item for item in self.record["scenarios"] if item["id"] == "intel")
                if mutation == "extra":
                    qualification["capability_boundaries"]["ota_rollback"] = dict(
                        evidence.CAPABILITY_BOUNDARIES["intel"])
                elif mutation == "reason":
                    qualification["capability_boundaries"]["intel"]["reason"] = "Intel 已通过"
                elif mutation == "missing":
                    qualification.pop("capability_boundaries")
                elif mutation == "passed":
                    intel.update(status="passed", required=True)
                elif mutation == "required":
                    intel["required"] = True
                elif mutation == "scenario_reason":
                    intel["reason"] = "本轮暂时不测"
                else:
                    qualification["excluded_scenarios"]["intel"] = intel["reason"]
                self.seal()
                errors = evidence.verify_release_candidate(
                    self.root, self.record, self.base, self.manifest_path)
                self.assertTrue(any("能力边界" in item or "严格等于" in item for item in errors), errors)
                if mutation in {"passed", "required", "scenario_reason"}:
                    self.assertTrue(any("固定能力边界" in item for item in self.verify()))

    def test_post_release_readback_preserves_separate_intel_boundary(self):
        import plistlib
        from types import SimpleNamespace

        import sf_update

        self.make_release_candidate()
        app = self.base / "Skyrocket.app"
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleShortVersionString": self.record["release_artifacts"]["version"],
            "CFBundleIdentifier": "com.skyrocket.app",
        }))
        self.seal()
        candidate = self.base / "change.json"
        evidence.atomic_json(candidate, self.record)
        version = self.record["release_artifacts"]["version"]
        log = self.base / "release.log"
        log.write_text("\n".join([
            "已安装: " + str(app), "版本: " + version,
            "published_archive=Skyrocket-" + version + "-macos.zip",
            "最终启动复验：" + str(app), "结论: 冒烟通过", "发布动作完成 v" + version,
        ]), encoding="utf-8")
        raw = (self.base / "Skyrocket-update.json").read_bytes()
        info = SimpleNamespace(version=version, archive_sha256=evidence.sha256_file(
            self.base / ("Skyrocket-" + version + "-macos.zip")))
        with patch.object(sf_update, "fetch_manifest", return_value=raw), \
                patch.object(sf_update, "verify_manifest", return_value=info), \
                patch.object(evidence.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            result = evidence.load(evidence.post_release_record(self.base, candidate, log, app))
        scenarios = {item["id"]: item for item in result["scenarios"]}
        for key, value in evidence.CAPABILITY_BOUNDARIES["intel"].items():
            self.assertEqual(value, scenarios["intel"][key])
        self.assertEqual("incomplete", result["status"])
        self.assertEqual("unverified", scenarios["ota_rollback"]["status"])
        self.assertIs(True, scenarios["ota_rollback"]["required"])

    def test_development_record_cannot_be_release_qualified(self):
        errors = evidence.verify_release_candidate(
            self.root, self.record, self.base, self.manifest_path)
        self.assertTrue(any("正式产物角色缺失" in item for item in errors))
        self.assertTrue(any("正式候选缺少发布前必需场景" in item for item in errors))

    def test_formal_candidate_binds_roles_and_ota_raw_hash(self):
        archive = self.make_release_candidate()
        errors = evidence.verify_release_candidate(
            self.root, self.record, self.base, self.manifest_path)
        self.assertEqual([], errors)
        archive.write_bytes(b"bad-fixture")
        errors = evidence.verify_release_candidate(
            self.root, self.record, self.base, self.manifest_path)
        self.assertTrue(any("OTA 清单 SHA-256" in item for item in errors), errors)

    def test_post_release_scope_exclusions_require_reasons(self):
        self.make_release_candidate()
        exclusions = {
            name: evidence.PERMANENT_EXEMPTIONS[name]
            for name in evidence.PERMANENT_EXEMPTIONS
        }
        self.record["release_qualification"]["excluded_scenarios"] = exclusions
        for scenario in self.record["scenarios"]:
            if scenario["id"] in exclusions:
                scenario.update(status="not_applicable", required=False,
                                reason=exclusions[scenario["id"]], receipts=[])
                scenario.pop("observation", None)
        self.seal()
        errors = evidence.verify_release_candidate(
            self.root, self.record, self.base, self.manifest_path)
        self.assertFalse(any("发布后场景状态无效" in item for item in errors), errors)
        self.assertFalse(any("排除必须有独立理由" in item for item in errors), errors)

    def test_qualification_report_keeps_blockers_machine_readable(self):
        report = evidence.qualification_report(
            self.root, self.record, self.base, self.manifest_path)
        self.assertEqual("blocked", report["status"])
        self.assertEqual("development", report["release"]["status"])
        self.assertTrue(report["blockers"])
        self.assertIn("artifact_roles", report["policy"])

    def test_real_receipt_artifact_manifest_protocol_passes(self):
        self.assertEqual([], self.verify())
        self.assertEqual("development", self.record["status"])
        self.assertTrue(all(
            item["status"] == ("passed" if item["id"] == "local_mac" else
                                "not_applicable" if item["id"] in evidence.PERMANENT_EXEMPTIONS or item["id"] in evidence.CAPABILITY_BOUNDARIES else "unverified")
            for item in self.record["scenarios"]
        ))

    def test_each_corruption_returns_its_specific_error(self):
        baseline_record = copy.deepcopy(self.record)
        baseline_receipt = self.receipt_path.read_bytes()
        log = self.receipt_path.parent / "command.log"
        baseline_log = log.read_bytes()
        baseline_manifest = self.manifest_path.read_bytes()
        cases = (
            ("receipt_json", "执行凭据无法读取"), ("log", "日志缺失或篡改"),
            ("manifest_hash", "manifest 不一致: artifacts"),
            ("device", "场景与执行设备不一致"), ("artifact", "场景产物不属于 manifest"),
            ("time", "场景观测不在执行会话时间内"), ("duplicate", "真实场景重复"),
            ("missing", "真实场景矩阵不完整"), ("unknown", "未知真实场景"),
            ("no_receipt", "通过场景缺少 receipts"), ("intel_arm", "固定能力边界"),
            ("attachment_escape", "场景附件缺失或篡改"),
            ("non_object", "必须是对象"), ("manifest_json", "manifest 无法读取"),
            ("bad_argv", "不是执行器留据"), ("bad_reference", "receipt 路径无效"),
            ("bad_id", "场景 id 必须是字符串"),
        )
        for mutation, expected in cases:
            with self.subTest(mutation=mutation):
                self.record = copy.deepcopy(baseline_record)
                self.receipt_path.write_bytes(baseline_receipt)
                log.write_bytes(baseline_log)
                self.manifest_path.write_bytes(baseline_manifest)
                local = self.record["scenarios"][0]
                if mutation == "receipt_json":
                    self.receipt_path.write_text("{broken", encoding="utf-8")
                elif mutation == "log":
                    log.write_bytes(b"tampered")
                elif mutation == "manifest_hash":
                    stored = evidence.load(self.manifest_path)
                    stored["artifacts"][0]["sha256"] = "changed"
                    evidence.atomic_json(self.manifest_path, stored)
                elif mutation == "device":
                    local["observation"]["device_id"] = "wrong-device"
                elif mutation == "artifact":
                    local["observation"]["artifact_sha256"] = "unknown-artifact"
                elif mutation == "time":
                    started = evidence.dt.datetime.fromisoformat(local["observation"]["observed_at"])
                    local["observation"]["observed_at"] = (started - evidence.dt.timedelta(seconds=1)).isoformat()
                elif mutation == "duplicate":
                    self.record["scenarios"].append(copy.deepcopy(local))
                elif mutation == "missing":
                    self.record["scenarios"].pop()
                elif mutation == "unknown":
                    local["id"] = "unknown"
                elif mutation == "no_receipt":
                    local["receipts"] = []
                elif mutation == "intel_arm":
                    intel = self.record["scenarios"][2]
                    intel.update(copy.deepcopy(local))
                    intel["id"] = "intel"
                    intel["observation"]["signals"]["native_x86_64"] = True
                    receipt = evidence.load(self.receipt_path)
                    receipt["machine"]["architecture"] = "arm64"
                    evidence.atomic_json(self.receipt_path, receipt)
                    for scenario in (local, intel):
                        scenario["observation"]["attachments"][0]["sha256"] = evidence.sha256_file(self.receipt_path)
                elif mutation == "attachment_escape":
                    local["observation"]["attachments"][0]["path"] = "../outside"
                elif mutation == "non_object":
                    self.record["scenarios"][0] = []
                elif mutation == "manifest_json":
                    self.manifest_path.write_text("[]", encoding="utf-8")
                elif mutation == "bad_argv":
                    receipt = evidence.load(self.receipt_path)
                    receipt["argv"] = [None]
                    evidence.atomic_json(self.receipt_path, receipt)
                elif mutation == "bad_reference":
                    self.record["verification_evidence"][0]["receipt"] = []
                elif mutation == "bad_id":
                    local["id"] = []
                if mutation not in {"manifest_hash", "manifest_json"}:
                    self.seal()
                errors = self.verify()
                self.assertTrue(any(expected in error for error in errors), errors)


# 摘自真实发布日志（v1.4.103 的 publish.log）里被校验器要求的那六行，只把版本号换成占位符。
# 为什么随测试文件走：公开快照不含 outputs/，归档日志在那里不存在，这条金标就整条红
# （v1.4.104 公开 CI 实测）。摘录让"校验器认得真实文本"在任何一棵树里都有判据。
REAL_LOG_EXCERPT = """\
   已安装: /Applications/Skyrocket.app
   版本: {version}
published_archive=Skyrocket-{version}-macos.zip
== 8) 最终启动复验：/Applications/Skyrocket.app ==
== 结论: 冒烟通过 ==
== 发布动作完成 v{version}，发布后验收待完成 ==
"""
EXCERPT_VERSION = "9.9.9"


class TestPublishLogAgainstARealRelease(SFCase):
    """拿真实发布日志当金标，而不是每个夹具各抄一份六条证据行。

    为什么要金标：那六条里有五条仍由别的脚本打印（build_release.sh、publish_update_release.py、
    smoke_frozen_app.py），原先只有夹具里的字符串互相印证 —— 4874df1 改措辞时就是这么漏过去的。
    金标只能证明「校验器对上一次真实发布的文本仍成立」，脚本以后再改措辞，仍要靠真跑收尾段的判据。
    两份金标：随测试文件的摘录（任何树都有），和私有树里最新的已归档完整日志（公开快照没有）。
    """

    def _assert_reader_accepts(self, text, version):
        app = Path("/Applications/Skyrocket.app")
        # 安装那一步在同一份真实日志里，和证据行是同一段输出：先证明它确实被打印过。
        self.assertIn("已安装: " + str(app), text)
        markers = evidence.publish_log_markers(app, version)
        missing = [marker for marker in markers if marker not in text]
        self.assertEqual(missing, [], "真实发布的日志已对不上校验器要求的证据行")
        # 反向对照：把金标里最后那行收尾语换掉，必须恰好那一条被判缺；否则断言会恒真。
        assert markers[-1] in text
        drifted = text.replace(markers[-1], "发版完成 v" + version)
        self.assertNotEqual(drifted, text)
        self.assertEqual([m for m in markers if m not in drifted], [markers[-1]])

    def test_real_log_excerpt_still_satisfies_the_reader(self):
        self._assert_reader_accepts(REAL_LOG_EXCERPT.format(version=EXCERPT_VERSION), EXCERPT_VERSION)

    def test_archived_real_publish_log_still_satisfies_the_reader(self):
        from sf_constants import APP_VERSION
        # 未发布版本没有自己的 publish.log；取最新、且低于当前版本的已归档真实日志。
        # 不能按目录名的字典序挑（1.4.100 会排在 1.4.99 前），按三段整数比较。
        current = tuple(int(part) for part in APP_VERSION.lstrip("v").split("."))
        candidates = []
        for folder in (evidence.ROOT / "outputs/release-candidates").glob("v*"):
            match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", folder.name)
            log = folder / "publish.log"
            if match and log.is_file():
                version = tuple(int(part) for part in match.groups())
                if version < current:
                    candidates.append((version, log))
        if not candidates and os.environ.get("SF_PUBLIC_REGRESSION") == "1":
            # 只有公开快照允许没有归档：outputs/ 按设计不对外，金标由上面的摘录用例承担。
            # 私有树里没有归档仍然是红 —— 那说明归档被误删了。
            self.skipTest("公开快照不含 outputs/ 归档；金标由 test_real_log_excerpt 承担")
        self.assertTrue(candidates, "缺少已归档的真实发布日志金标；不要改成跳过，先恢复归档或给出替代金标")
        version_tuple, log = max(candidates)
        version = ".".join(str(part) for part in version_tuple)
        self._assert_reader_accepts(log.read_text(encoding="utf-8"), version)


if __name__ == "__main__":
    unittest.main()
