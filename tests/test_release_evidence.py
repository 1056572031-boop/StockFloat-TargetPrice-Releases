# -*- coding: utf-8 -*-
"""Behavioral coverage for the versioned release evidence orchestrator."""
from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests._base import SFCase
from tools import release_evidence as tool

quality = tool.evidence


class TestReleaseEvidence(SFCase):
    def setUp(self):
        super().setUp()
        self.version = __import__("sf_constants").APP_VERSION

    @staticmethod
    def limitations():
        return {"pre": ["pre limitation"], "bound": ["bound limitation"],
                "complete": ["complete limitation"]}

    @staticmethod
    def receipt(path, command="true", fingerprint="fp", **extra):
        path.mkdir(parents=True, exist_ok=True)
        value = {"argv": command.split(), "exit_code": 0, "stable": True,
                 "source_start": fingerprint, "machine": {"device_id": "device"},
                 "ended_at": "2030-01-01T00:00:00+00:00", "log": "command.log"}
        value.update(extra)
        (path / "receipt.json").write_text(json.dumps(value), encoding="utf-8")
        (path / "command.log").write_text("ok\n", encoding="utf-8")
        return path / "receipt.json", value

    def test_helpers_and_invalid_limitations(self):
        self.assertEqual(self.version, tool.version_value(None))
        with self.assertRaises(tool.EvidenceError):
            tool.version_value("0.0.0")
        self.assertEqual("quality-v1.4.107-complete.json", tool.record_path(Path("."), self.version, True).name)
        self.assertEqual("quality-v1.4.107.manifest.json", tool.manifest_path(Path("."), self.version).name)
        self.assertEqual("true", tool.command_text(["true"]))
        self.assertEqual(3, len(tool.pre_commands(self.version, "base")))
        self.assertEqual("Skyrocket-1.4.107-macos.zip", tool.app_paths(Path("/tmp"), self.version, None, None)[1].name)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "limitations.json"
            path.write_text(json.dumps(self.limitations()), encoding="utf-8")
            self.assertEqual(self.limitations(), tool.load_limitations(path))
            for value in ({"pre": [], "bound": ["x"], "complete": ["x"]},
                          {"pre": ["x"], "bound": [""], "complete": ["x"]}, []):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(tool.EvidenceError):
                    tool.load_limitations(path)
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(tool.EvidenceError):
                tool.load_limitations(path)

    def test_receipt_selection_scope_and_qualification_helpers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            good_path, picked_value = self.receipt(sessions / "0", fingerprint="source")
            self.receipt(sessions / "1", "other", fingerprint="old")
            picked = tool.pick_receipt(sessions, "true", "source")
            self.assertEqual(good_path, picked[0])
            self.assertEqual(good_path, tool.pick_receipt_contains(sessions, "true", "source")[0])
            self.assertEqual("sessions/0/receipt.json", tool.relative_receipt(good_path, root))
            self.assertEqual("0/receipt.json", tool.evidence_item("true", picked)["receipt"])
            (sessions / "bad").mkdir()
            (sessions / "bad" / "receipt.json").write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(tool.EvidenceError, "恰好一份"):
                tool.pick_receipt(sessions, "true", "other")
            duplicate = sessions / "2"
            self.receipt(duplicate, fingerprint="source")
            with self.assertRaisesRegex(tool.EvidenceError, "恰好一份"):
                tool.pick_receipt(sessions, "true", "source")
            self.assertEqual(["record.json"], tool.expanded_scope(root, ["record.json"]))
            with patch.object(quality, "_git", side_effect=["changed\0", "untracked\0"]):
                self.assertEqual(["changed", "record.json", "untracked"],
                                 tool.expanded_scope(root, ["record.json"]))
            self.assertEqual("source", picked_value["source_start"])

    def test_pre_record_success_and_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = tool.evidence_dir(root, self.version)
            commands = tool.pre_commands(self.version, "baseline")
            for index, command in enumerate(commands):
                self.receipt(sessions / str(index), command, "fingerprint")
            with patch.object(quality, "source_snapshot", return_value={"fingerprint": "fingerprint"}), \
                    patch.object(tool, "expanded_scope", return_value=["quality-v1.4.107.json", "quality-v1.4.107.manifest.json"]):
                path = tool.pre_record(root, self.version, "baseline", self.limitations())
            record = quality.load(path)
            self.assertEqual("development", record["status"])
            self.assertEqual(3, len(record["verification_evidence"]))
            self.assertTrue(all(item["status"] in {"unverified", "not_applicable"} for item in record["scenarios"]))
            with self.assertRaises(tool.EvidenceError):
                tool.pre_record(root, self.version, "", self.limitations())
            with self.assertRaises(tool.EvidenceError):
                tool.set_pre_record(root, self.version, "base", ["x"], [])

    def test_bind_success_and_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record = quality.template()
            record.update({"intent": "fixture", "scope": ["quality.json"],
                           "acceptance": ["fixture"],
                           "acceptance_map": [{"acceptance": "fixture", "evidence_index": 0}],
                           "verification_evidence": [{"command": "true", "exit_code": 0}],
                           "unverified_limitations": ["fixture"],
                           "release_qualification": {"stage": "pre_release", "baseline": "base"},
                           "artifact_fingerprint": {"source": "pending", "artifacts": []}})
            quality.atomic_json(tool.record_path(root, self.version), record)
            app = root / "dist" / "Skyrocket.app"
            app.mkdir(parents=True)
            (app / "payload").write_bytes(b"app")
            app = app.resolve()
            old = root / "old.app"
            old.mkdir()
            zip_path = root / "build" / "update_assets" / f"V{self.version}" / f"Skyrocket-{self.version}-macos.zip"
            zip_path.parent.mkdir(parents=True)
            zip_path.write_bytes(b"zip")
            (root / "build" / f"Skyrocket-V{self.version}.dmg").write_bytes(b"dmg")
            (zip_path.parent / "Skyrocket-update.json").write_text("{}", encoding="utf-8")
            sessions = root / "sessions"
            pre = tool.pre_commands(self.version, "base")
            bound = [item.format(app="dist/Skyrocket.app", zip=zip_path.relative_to(root),
                                 delivery="sessions/delivery.json", old_app="old.app")
                     for item in tool.RECEIPT_RULES["bound"]]
            for index, command in enumerate(pre + bound):
                self.receipt(sessions / str(index), command, "fp")
            (sessions / "9").mkdir(parents=True, exist_ok=True)
            (sessions / "9" / "delivery.json").write_text("{}", encoding="utf-8")
            with patch.object(tool, "evidence_dir", return_value=sessions), \
                    patch.object(tool, "write_qualification"), \
                    patch.object(quality, "source_snapshot", return_value={"fingerprint": "fp"}), \
                    patch.object(quality, "validate", return_value=[]), \
                    patch.object(quality, "artifact_token", return_value={"sha256": "artifact", "entries": 1, "kind": "directory"}):
                path = tool.bind(root, self.version, self.limitations(), old, app, zip_path)
            bound_record = quality.load(path)
            self.assertEqual("release_candidate", bound_record["status"])
            self.assertEqual("fp", bound_record["artifact_fingerprint"]["source"])
            with patch.object(tool, "write_qualification"), \
                    patch.object(tool, "pick_receipt", return_value=self.receipt(root / "sessions" / "bad", "true", "fp")), \
                    patch.object(quality, "source_snapshot", return_value={"fingerprint": "fp"}), \
                    patch.object(quality, "validate", return_value=[]):
                with self.assertRaises(tool.EvidenceError):
                    tool.bind(root, self.version, self.limitations(), root.parent / "old.app", root / "dist" / "Skyrocket.app", root / "x.zip")
            with patch.object(tool.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='[{"headSha":"sha","status":"completed","conclusion":"success"},{"headSha":"other"}]', stderr="")):
                self.assertEqual(1, len(tool.gh_runs("r/o", "sha")))

    def test_complete_and_write_qualification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clean_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
            subprocess.run(["git", "init", "--quiet", str(root)], env=clean_env, check=True, capture_output=True)
            (root / "probe.py").write_text("print('fixture')\n", encoding="utf-8")
            subprocess.run(["git", "add", "probe.py"], cwd=root, env=clean_env, check=True, capture_output=True)
            subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                            "commit", "-qm", "fixture"], cwd=root, env=clean_env, check=True, capture_output=True)
            record = quality.template()
            record.update({"intent": "fixture", "scope": ["quality.json"], "acceptance": ["fixture"],
                           "acceptance_map": [{"acceptance": "fixture", "evidence_index": 0}],
                           "verification_evidence": [{"command": "true", "exit_code": 0}],
                           "unverified_limitations": ["fixture"],
                           "artifact_fingerprint": {"source": "fp", "artifacts": []},
                           "status": "release_candidate"})
            quality.atomic_json(tool.record_path(root, self.version), record)
            evidence_dir = root / "complete-evidence"
            read = self.receipt(evidence_dir / "0", "python readback.json", "fp")
            walk = self.receipt(evidence_dir / "1", "python click.json", "fp")
            state = root / "state.json"
            state.write_text("{}", encoding="utf-8")
            with patch.object(quality, "source_snapshot", return_value={"fingerprint": "fp"}), \
                    patch.object(tool, "write_qualification"), \
                    patch.object(tool.release_state, "finish"), \
                    patch.object(tool, "pick_receipt_contains", side_effect=[read, walk]):
                out = tool.complete(root, self.version, self.limitations(), state,
                                    root / "readback.json", root / "click.json", evidence_dir)
            self.assertTrue(out.exists())
            self.assertEqual("complete", quality.load(out)["release_qualification"]["stage"])

    def test_gh_parser_and_qualification_errors_are_observable(self):
        with patch.object(tool.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="not-json", stderr="")):
            with self.assertRaises(tool.EvidenceError):
                tool.gh_runs("r", "s")
        with patch.object(tool.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="denied")):
            with self.assertRaisesRegex(tool.EvidenceError, "gh run 查询失败"):
                tool.gh_runs("r", "s")


        clock = iter((0.0, 0.0, 1.0, 1.0, 2.0))
        self.assertEqual(0, tool.wait_ci("repo", "sha", timeout=5, interval=1,
            query=lambda *_: [{"workflowName": "ci", "status": "completed", "conclusion": "success", "databaseId": 9}],
            clock=lambda: next(clock), sleeper=lambda _: None))
        self.assertEqual(1, tool.wait_ci("repo", "sha", timeout=5, interval=1,
            query=lambda *_: [{"workflowName": "ci", "status": "completed", "conclusion": "failure", "databaseId": 10}],
            clock=lambda: 0.0, sleeper=lambda _: None))
        self.assertEqual(3, tool.wait_ci("repo", "sha", timeout=0, query=lambda *_: [], clock=lambda: 0.0, sleeper=lambda _: None))
        self.assertEqual(3, tool.wait_ci("repo", "sha", timeout=0, query=lambda *_: [{"workflowName": "other", "status": "completed", "conclusion": "success"}], clock=lambda: 0.0, sleeper=lambda _: None))
        with patch.object(tool, "gh_runs", side_effect=tool.EvidenceError("gh unavailable")):
            self.assertEqual(2, tool.main(["wait-ci", "--repo", "r", "--sha", "s", "--timeout", "1"]))
        with patch.object(tool, "gh_runs", return_value=[]):
            self.assertEqual(3, tool.main(["wait-ci", "--repo", "r", "--sha", "s", "--timeout", "0.001", "--interval", "0"]))
        with patch.object(tool, "self_test", return_value=0):
            self.assertEqual(0, tool.main(["--self-test"]))
        self.assertEqual(2, tool.main(["--self-test", "--version", self.version]))
        self.assertEqual(2, tool.main(["pre-record", "--version", self.version]))
    def test_error_branches_and_cli_orchestration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            with self.assertRaisesRegex(tool.EvidenceError, "包含目标路径"):
                tool.pick_receipt_contains(sessions, "missing", "fp")
            with patch.object(quality, "source_snapshot", return_value={"fingerprint": "missing"}):
                with self.assertRaises(tool.EvidenceError):
                    tool.pre_record(root, self.version, "base", self.limitations())
            app, zip_path = tool.app_paths(root, self.version, None, None)
            with patch.object(quality, "source_snapshot", return_value={"fingerprint": "fp"}):
                fp = tool.artifact_fingerprint(root, self.version, app, zip_path)
            self.assertEqual("fp", fp["source"])
            receipt = {"source_start": "fp", "machine": {"device_id": "d"},
                       "ended_at": "2030-01-01T00:00:00+00:00"}
            attachment = root / "x"
            attachment.write_bytes(b"evidence")
            obs = tool.observation(receipt, "true", "sha", {"ok": True}, attachment, root)
            self.assertEqual("sha", obs["artifact_sha256"])
            with self.assertRaises(tool.EvidenceError):
                tool.set_pre_record(root, self.version, "base", ["x"], [])
            limits = root / "limits.json"
            limits.write_text(json.dumps(self.limitations()), encoding="utf-8")
            with patch.object(tool, "pre_record", return_value=root / "record.json"):
                self.assertEqual(0, tool.main(["pre-record", "--root", str(root),
                                               "--limitations-file", str(root / "limits.json"),
                                               "--baseline", "base"]))
            with self.assertRaises(SystemExit):
                tool.main(["unknown"])
            with patch.object(tool, "gh_runs", return_value=[]):
                self.assertEqual(3, tool.main(["wait-ci", "--repo", "r/o", "--sha", "s", "--timeout", "0.001", "--interval", "0"]))
            with patch.object(tool, "gh_runs", side_effect=tool.EvidenceError("gh unavailable")):
                self.assertEqual(2, tool.main(["wait-ci", "--repo", "r/o", "--sha", "s", "--timeout", "1"]))
            self.assertEqual(2, tool.main(["bind", "--limitations-file", str(root / "limits.json")]))
            self.assertEqual(2, tool.main(["complete", "--limitations-file", str(root / "limits.json")]))
            self.assertEqual(0, tool.self_test())
            with patch.object(sys, "argv", ["release_evidence.py", "--self-test"]), \
                    patch.object(tool, "self_test", return_value=0), \
                    self.assertRaises(SystemExit) as exited:
                runpy.run_path(str(Path(tool.__file__).resolve()), run_name="__main__")
            self.assertEqual(0, exited.exception.code)

    def test_qualification_and_complete_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = quality.template()
            record.update({"intent": "fixture", "scope": ["quality.json"],
                           "acceptance": ["fixture"],
                           "acceptance_map": [{"acceptance": "fixture", "evidence_index": 0}],
                           "verification_evidence": [{"command": "true", "exit_code": 0}],
                           "unverified_limitations": ["fixture"],
                           "artifact_fingerprint": {"source": "fp", "artifacts": []}})
            path = tool.record_path(root, self.version)
            quality.atomic_json(path, record)
            with patch.object(quality, "manifest", return_value={"source": {}}), \
                    patch.object(quality, "verify_release_candidate", return_value=["bad"]):
                with self.assertRaisesRegex(tool.EvidenceError, "正式候选资格"):
                    tool.write_qualification(root, record, path)
            with patch.object(quality, "source_snapshot", return_value={"fingerprint": "fp"}):
                with self.assertRaises(tool.EvidenceError):
                    tool.complete(root, self.version, self.limitations(), root / "state", root / "read", root / "walk", root / "evidence")
            out = root / "quality-v1.4.107-complete.json"
            out.write_text("{}", encoding="utf-8")
            with patch.object(quality, "source_snapshot", return_value={"fingerprint": "fp"}):
                with self.assertRaises(tool.EvidenceError):
                    tool.complete(root, self.version, self.limitations(), root / "state", root / "read", root / "walk", root / "evidence")
    def test_display_path_uses_absolute_path_outside_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = Path.home() / ".release-evidence-display.app"
            self.assertEqual(str(outside.resolve()), tool.display_path(outside, root))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wanted = root / "reports" / "readback.json"
            wrong = root / "other" / "readback.json"
            good = self.receipt(root / "sessions" / "good", "python", "fp", output_paths=[str(wanted)])
            self.receipt(root / "sessions" / "wrong", "python", "fp", output_paths=[str(wrong)])
            self.assertEqual(good[0], tool.pick_receipt_contains(root / "sessions", wanted, "fp", root)[0])
            with self.assertRaisesRegex(tool.EvidenceError, "恰好一份"):
                tool.pick_receipt_contains(root / "sessions", "readback.json", "fp", root)

    def test_complete_rejects_stale_or_unbound_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = tool.record_path(root, self.version)
            record = quality.template()
            record["status"] = "release_candidate"
            record["artifact_fingerprint"]["source"] = "old"
            quality.atomic_json(path, record)
            with patch.object(quality, "source_snapshot", return_value={"fingerprint": "new"}):
                with self.assertRaisesRegex(tool.EvidenceError, "源码指纹"):
                    tool.complete(root, self.version, self.limitations(), root / "state", root / "read", root / "walk")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = tool.record_path(root, self.version)
            record = quality.template()
            record["status"] = "development"
            record["artifact_fingerprint"]["source"] = "old"
            quality.atomic_json(path, record)
            with patch.object(quality, "source_snapshot", return_value={"fingerprint": "new"}):
                with self.assertRaisesRegex(tool.EvidenceError, "release_candidate|源码指纹"):
                    tool.complete(root, self.version, self.limitations(), root / "state", root / "read", root / "walk")

    def test_remaining_guard_and_cli_branches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = tool.record_path(root, self.version)
            record = quality.template()
            record["artifact_fingerprint"]["source"] = "old"
            quality.atomic_json(path, record)
            with patch.object(quality, "source_snapshot", return_value={"fingerprint": "new"}):
                with self.assertRaisesRegex(tool.EvidenceError, "候选记录源码指纹"):
                    tool.bind(root, self.version, self.limitations(), root / "old")
            outside_old = Path.home() / ".release-evidence-fixture-old.app"
            record["artifact_fingerprint"]["source"] = "pending"
            quality.atomic_json(path, record)
            diag = self.receipt(root / "sessions" / "outside-diag",
                                f"./.venv/bin/python3 tools/diag_coverage_check.py --version {self.version}", "fp")
            with patch.object(tool, "evidence_dir", return_value=root / "sessions"), \
                    patch.object(tool, "receipt_candidates", return_value=[diag]), \
                    patch.object(tool, "pick_receipt", return_value=self.receipt(root / "sessions" / "outside", "true", "fp")), \
                    patch.object(quality, "source_snapshot", return_value={"fingerprint": "fp"}), \
                    patch.object(quality, "validate", return_value=[]), \
                    patch.object(quality, "artifact_token", return_value={"sha256": "artifact", "entries": 1, "kind": "directory"}):
                with self.assertRaises(tool.EvidenceError):
                    tool.bind(root, self.version, self.limitations(), outside_old, root / "dist" / "Skyrocket.app", root / "x.zip")
            with patch.object(tool.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="{}", stderr="")):
                with self.assertRaises(tool.EvidenceError):
                    tool.gh_runs("r/o", "sha")
            with patch.object(tool.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="[]", stderr="")):
                self.assertEqual([], tool.gh_runs("r/o", "sha"))
            queued = iter(([{"workflowName": "ci", "status": "queued", "databaseId": 1}],
                           [{"workflowName": "ci", "status": "completed", "conclusion": "success", "databaseId": 2}]))
            self.assertEqual(0, tool.wait_ci("r/o", "sha", timeout=5, interval=0,
                                             query=lambda *_: next(queued), clock=lambda: 0.0,
                                             sleeper=lambda _: None))
            limits = root / "limits.json"
            limits.write_text(json.dumps(self.limitations()), encoding="utf-8")
            with patch.object(tool, "bind", return_value=path):
                self.assertEqual(0, tool.main(["bind", "--old-app", str(root / "old"),
                                               "--limitations-file", str(limits)]))
            with patch.object(tool, "complete", return_value=path):
                self.assertEqual(0, tool.main(["complete", "--state", str(root / "state"),
                                               "--readback", str(root / "read"),
                                               "--walkthrough", str(root / "walk"),
                                               "--limitations-file", str(limits)]))
            self.assertEqual(2, tool.main(["wait-ci", "--repo", "r/o", "--sha", "s", "--timeout", "0"]))
            with patch.object(tool, "wait_ci", return_value=0):
                self.assertEqual(1, tool.self_test())
            record["artifact_fingerprint"]["source"] = "pending"
            record["status"] = "release_candidate"
            record["release_qualification"]["baseline"] = "base"
            quality.atomic_json(path, record)
            receipt = self.receipt(root / "sessions" / "validation", "true", "fp")
            diag = self.receipt(root / "sessions" / "diag",
                                f"./.venv/bin/python3 tools/diag_coverage_check.py --version {self.version} --baseline base", "fp")
            with patch.object(tool, "pick_receipt", return_value=receipt), \
                    patch.object(tool, "receipt_candidates", return_value=[diag]), \
                    patch.object(quality, "artifact_token", return_value={"sha256": "artifact"}), \
                    patch.object(quality, "validate", return_value=["bad record"]), \
                    patch.object(quality, "source_snapshot", return_value={"fingerprint": "fp"}):
                with self.assertRaisesRegex(tool.EvidenceError, "绑定记录无效"):
                    tool.bind(root, self.version, self.limitations(), root / "old")
            out = tool.record_path(root, self.version, True)
            out.write_text("{}", encoding="utf-8")
            record["artifact_fingerprint"]["source"] = "fp"
            quality.atomic_json(path, record)
            complete_receipt = self.receipt(root / "complete" / "receipt", "python read", "fp", output_paths=[str(root / "read")])
            with patch.object(tool, "pick_receipt_contains", return_value=complete_receipt), \
                    patch.object(quality, "source_snapshot", return_value={"fingerprint": "fp"}):
                with self.assertRaisesRegex(tool.EvidenceError, "不覆盖已有发布后记录"):
                    tool.complete(root, self.version, self.limitations(), root / "state", root / "read", root / "walk", root / "evidence")


        record = quality.template()
        errors = quality.verify_release_candidate(Path("."), record, Path("."), Path("missing.json"))
        self.assertTrue(any("release_candidate" in item for item in errors))
        self.assertTrue(any("正式产物角色" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
