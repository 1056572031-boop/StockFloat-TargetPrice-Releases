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


class TestPublishLogAgainstARealRelease(SFCase):
    """拿已入库的真实发布日志当金标，而不是每个夹具各抄一份六条证据行。

    为什么要金标：那六条里有五条仍由别的脚本打印（build_release.sh、publish_update_release.py、
    smoke_frozen_app.py），原先只有夹具里的字符串互相印证 —— 4874df1 改措辞时就是这么漏过去的。
    金标只能证明「校验器对上一次真实发布的文本仍成立」，脚本以后再改措辞，仍要靠真跑收尾段的判据。
    """

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
        self.assertTrue(candidates, "缺少已归档的真实发布日志金标；不要改成跳过，先恢复归档或给出替代金标")
        version_tuple, log = max(candidates)
        version = ".".join(str(part) for part in version_tuple)
        text = log.read_text(encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
