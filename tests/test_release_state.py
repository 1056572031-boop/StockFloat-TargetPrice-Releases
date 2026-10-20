"""临时 Git 仓库中的阶段/失效判断；夹具资格不冒充正式关卡通过。"""
import contextlib
import copy
import datetime as dt
import io
import json
import os
import runpy
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from tests._base import SFCase
from tools import release_state as release


class ReleaseStateCase(SFCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="sf_release_state_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.state = self.root / "build/state.json"
        self.env = release.gate_stamp.git_env()
        self.env.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.test",
                        GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.test")
        self.git("init", "-q")
        for name in release.DOCS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        (self.root / "x.py").write_text("value = 1\n")
        (self.root / ".gitignore").write_text("build/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.version = "fixture-version"
        self.console = contextlib.redirect_stdout(io.StringIO())
        self.console.__enter__()
        self.addCleanup(self.console.__exit__, None, None, None)
        release.init_state(self.root, self.state, self.version)
        self.app = self.root / "build/Skyrocket.app"
        self.app.mkdir()
        (self.app / "payload").write_bytes(b"candidate")
        self.dmg = self.root / "build/candidate.dmg"
        self.dmg.write_bytes(b"dmg")
        self.archive = self.root / "build/candidate.zip"
        self.archive.write_bytes(b"archive")
        self.ota = self.root / "build/manifest.json"
        self.ota.write_text(json.dumps({"version": self.version, "archive": {
            "sha256": release.evidence.sha256_file(self.archive), "size": self.archive.stat().st_size}}))

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], env=self.env, check=True,
                              capture_output=True, text=True)

    def qualify(self):
        with mock.patch.object(release.gate_stamp, "check", return_value=(True, "fixture")), \
                mock.patch.object(release, "check_coverage"), \
                mock.patch.object(release.public_docs_check, "problems", return_value=[]):
            return release.qualify_source(self.root, self.state)

    def candidate(self):
        self.qualify()
        return release.make_candidate(self.root, self.state, self.version, self.app, self.dmg,
                                      self.archive, self.ota)

    def record(self, complete=False):
        path = self.root / ("build/complete.json" if complete else "build/quality.json")
        state = release.validate(self.root, self.state)
        roles = {role: {"path": str(Path(item["path"]).relative_to(path.parent))}
                 for role, item in state["artifacts"].items()}
        value = {"release_artifacts": {"version": self.version, "roles": roles},
                 "release_qualification": {"stage": "complete" if complete else "pre_release"}}
        path.write_text(json.dumps(value))
        path.with_suffix(".manifest.json").write_text("{}")
        return path

    def acceptance_value(self, state):
        source_zip = self.root / "build/source.zip"
        source_zip.write_bytes(b"source archive fixture")
        sums = self.root / "build/SHA256SUMS.txt"
        sums.write_bytes(b"checksum fixture")
        public = self.root / "build/publication.json"
        public.write_text(json.dumps({"source": state["source"], "version": state["version"],
                          "public_commit": "public-fixture", "source_zip": release.reference(source_zip),
                          "checksums": release.reference(sums)}))
        folder = self.root / "build/downloads"
        folder.mkdir(exist_ok=True)
        files = {}
        for name, original in ((self.archive.name, self.archive), (self.dmg.name, self.dmg),
                               ("online-manifest.json", self.ota), (source_zip.name, source_zip), (sums.name, sums)):
            target = folder / name
            target.write_bytes(original.read_bytes())
            files[name] = release.reference(target)
        value = {key: state[key] for key in ("version", "source", "artifacts")}
        value.update(verified=True, generated_at=state["updated_at"], publication=release.reference(public),
                     downloaded_files=files, checks={name: {"status": "passed"} for name in
                     ("ota_manifest", "zip", "dmg", "public_source", "checksums", "public_ci", "final_startup")})
        value["checks"]["public_source"]["observation"] = {"commit": "public-fixture"}
        value["checks"]["public_ci"]["observation"] = {"head_sha": "public-fixture"}
        return value

    def test_snapshot_and_duplicate_init(self):
        state = release.validate(self.root, self.state, "source_open")
        self.assertEqual(state["source"]["head"], self.git("rev-parse", "HEAD").stdout.strip())
        self.assertIsInstance(state["source"]["gate_input_count"], int)
        with self.assertRaisesRegex(ValueError, "不覆盖"):
            release.init_state(self.root, self.state, self.version)

    def test_malformed_state_and_coverage_command_environment_are_rejected(self):
        original = self.state.read_bytes()
        self.state.write_text('{"schema_version": 2, "stage": "complete"}')
        with self.assertRaisesRegex(ValueError, "结构无效"):
            release.validate(self.root, self.state)
        self.state.write_bytes(original)
        with mock.patch.dict(os.environ, {"GIT_DIR": "bad-route"}), \
                mock.patch.object(release.subprocess, "run") as runner:
            release.check_coverage(self.root)
        self.assertNotIn("GIT_DIR", runner.call_args.kwargs["env"])
        self.assertIn("tools/coverage_provenance.py", runner.call_args.args[0])

    def test_source_change_during_qualification_never_advances(self):
        def mutate(root):
            (root / "README.md").write_text("changed during qualification")
        with mock.patch.object(release.gate_stamp, "check", return_value=(True, "fixture")), \
                mock.patch.object(release, "check_coverage", side_effect=mutate), \
                mock.patch.object(release.public_docs_check, "problems", return_value=[]), \
                self.assertRaisesRegex(ValueError, "检查期间"):
            release.qualify_source(self.root, self.state)
        self.assertEqual(json.loads(self.state.read_text())["stage"], "source_open")

    def test_changes_invalidate_source_even_while_already_dirty(self):
        for name in ("x.py", "release_notes.txt", "docs/INSTALL.md", "README.md"):
            path = self.root / name
            original = path.read_bytes()
            path.write_bytes(original + b"changed")
            with self.assertRaisesRegex(ValueError, "已失效"):
                release.validate(self.root, self.state)
            path.write_bytes(original)
        (self.root / "new.py").write_text("new = True")
        with self.assertRaisesRegex(ValueError, "已失效"):
            release.validate(self.root, self.state)

    def test_unqualified_source_cannot_make_candidate(self):
        with self.assertRaisesRegex(ValueError, "source_qualified"):
            release.make_candidate(self.root, self.state, self.version, self.app, self.dmg,
                                   self.archive, self.ota)
        with mock.patch.object(release.gate_stamp, "check", return_value=(False, "missing")), \
                self.assertRaisesRegex(ValueError, "完整回归"):
            release.qualify_source(self.root, self.state)
        self.assertEqual(release.validate(self.root, self.state)["stage"], "source_open")

    def test_provenance_and_docs_failure_do_not_advance(self):
        with mock.patch.object(release.gate_stamp, "check", return_value=(True, "fixture")), \
                mock.patch.object(release, "check_coverage", side_effect=subprocess.CalledProcessError(1, "fixture")), \
                self.assertRaises(subprocess.CalledProcessError):
            release.qualify_source(self.root, self.state)
        with mock.patch.object(release.gate_stamp, "check", return_value=(True, "fixture")), \
                mock.patch.object(release, "check_coverage"), \
                mock.patch.object(release.public_docs_check, "problems", return_value=["stale"]), \
                self.assertRaisesRegex(ValueError, "公开资料"):
            release.qualify_source(self.root, self.state)
        self.assertEqual(release.validate(self.root, self.state)["stage"], "source_open")

    def test_candidate_hashes_directory_content_permissions_and_files(self):
        state = self.candidate()
        self.assertEqual(set(state["artifacts"]), {"app", "zip", "dmg", "ota_manifest"})
        for path in (self.app / "payload", self.dmg, self.archive, self.ota):
            original = path.read_bytes()
            path.write_bytes(b"replaced")
            with self.assertRaisesRegex(ValueError, "发生变化"):
                release.validate(self.root, self.state)
            path.write_bytes(original)
        (self.app / "payload").chmod(0o755)
        with self.assertRaisesRegex(ValueError, "发生变化"):
            release.validate(self.root, self.state)

    def test_wrong_version_and_ota_binding_reject(self):
        self.qualify()
        with self.assertRaisesRegex(ValueError, "版本"):
            release.make_candidate(self.root, self.state, "other", self.app, self.dmg, self.archive, self.ota)
        with self.assertRaisesRegex(ValueError, "类型错误"):
            release.make_candidate(self.root, self.state, self.version, self.archive, self.dmg, self.archive, self.ota)
        self.ota.write_text("{}")
        with self.assertRaisesRegex(ValueError, "OTA"):
            release.make_candidate(self.root, self.state, self.version, self.app, self.dmg, self.archive, self.ota)
        self.assertEqual(release.validate(self.root, self.state)["stage"], "source_qualified")

    def test_publish_requires_authoritative_quality_and_exact_artifact_paths(self):
        self.candidate()
        record = self.record()
        with mock.patch.object(release.evidence, "verify_release_candidate", return_value=["missing rollback"]), \
                self.assertRaisesRegex(ValueError, "missing rollback"):
            release.begin_publish(self.root, self.state, record)
        with mock.patch.object(release.evidence, "verify_release_candidate", return_value=["missing rollback"]):
            self.assertIn("missing rollback", release.report(self.root, self.state, record)["blockers"][0])
        changed = json.loads(record.read_text())
        changed["release_artifacts"]["version"] = "other-release"
        record.write_text(json.dumps(changed))
        with mock.patch.object(release.evidence, "verify_release_candidate", return_value=[]), \
                self.assertRaisesRegex(ValueError, "版本不匹配"):
            release.begin_publish(self.root, self.state, record)
        changed["release_artifacts"]["version"] = self.version
        record.write_text(json.dumps(changed))
        changed = json.loads(record.read_text())
        changed["release_artifacts"]["roles"]["zip"]["path"] = self.dmg.name
        record.write_text(json.dumps(changed))
        with mock.patch.object(release.evidence, "verify_release_candidate", return_value=[]), \
                self.assertRaisesRegex(ValueError, "候选路径"):
            release.begin_publish(self.root, self.state, record)

    def test_complete_requires_separate_complete_record_and_all_channel_checks(self):
        self.candidate()
        record = self.record()
        with mock.patch.object(release.evidence, "verify_release_candidate", return_value=[]):
            pending = release.begin_publish(self.root, self.state, record)
            self.assertEqual(release.report(self.root, self.state)["recorded_stage"], "released_pending_acceptance")
            readback = self.root / "build/readback.json"
            readback.write_text("{}")
            with self.assertRaisesRegex(ValueError, "真实验收"):
                release.finish(self.root, self.state, record, readback)
            complete_record = self.record(complete=True)
            with self.assertRaisesRegex(ValueError, "独立读回"):
                release.finish(self.root, self.state, complete_record, readback)
            value = {key: pending[key] for key in ("version", "source", "artifacts")}
            value.update(verified=True, checks={}, generated_at=pending["updated_at"])
            readback.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "未全部通过"):
                release.finish(self.root, self.state, complete_record, readback)
            value["checks"] = {name: {"status": "passed"} for name in
                               ("ota_manifest", "zip", "dmg", "public_source", "checksums", "public_ci", "final_startup")}
            readback.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "缺少实际下载"):
                release.finish(self.root, self.state, complete_record, readback)
            value = self.acceptance_value(pending)
            value["publication"]["sha256"] = "changed"
            readback.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "公开发布回执已变化"):
                release.finish(self.root, self.state, complete_record, readback)
            value = self.acceptance_value(pending)
            value["downloaded_files"][self.archive.name]["sha256"] = "changed"
            readback.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "下载证据已变化"):
                release.finish(self.root, self.state, complete_record, readback)
            value = self.acceptance_value(pending)
            readback.write_text(json.dumps(value))
            result = release.finish(self.root, self.state, complete_record, readback)
            self.assertEqual(result["stage"], "complete")
            self.assertEqual([item["to"] for item in result["history"]], list(release.STAGES[1:]))
            readback.write_text("{}")
            self.assertFalse(release.report(self.root, self.state)["complete"])

    def test_complete_rejects_stale_or_incomplete_downloads_and_wrong_public_commit(self):
        self.candidate()
        record = self.record()
        with mock.patch.object(release.evidence, "verify_release_candidate", return_value=[]):
            pending = release.begin_publish(self.root, self.state, record)
            complete = self.record(complete=True)
            valid = self.acceptance_value(pending)
            path = self.root / "build/final-readback.json"
            start = dt.datetime.fromisoformat(pending["updated_at"])
            cases = []
            for timestamp in ("bad-time", start.replace(tzinfo=None).isoformat(),
                              (start - dt.timedelta(seconds=1)).isoformat(),
                              (start + dt.timedelta(days=1)).isoformat()):
                bad = copy.deepcopy(valid)
                bad["generated_at"] = timestamp
                cases.append(bad)
            bad = copy.deepcopy(valid)
            bad["downloaded_files"].pop("online-manifest.json")
            cases.append(bad)
            bad = copy.deepcopy(valid)
            bad["checks"]["public_ci"]["observation"]["head_sha"] = "other-commit"
            cases.append(bad)
            for value in cases:
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    release.finish(self.root, self.state, complete, path)
                self.assertEqual(release.validate(self.root, self.state)["stage"], "released_pending_acceptance")
            publication = Path(valid["publication"]["path"])
            original = publication.read_bytes()
            body = json.loads(original)
            for broken in (dict(body, version="other-version"), dict(body, source={}),
                           dict(body, source_zip=None),
                           dict(body, source_zip=dict(body["source_zip"], path=str(self.archive)))):
                publication.write_text(json.dumps(broken))
                bad = copy.deepcopy(valid)
                bad["publication"] = release.reference(publication)
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    release.finish(self.root, self.state, complete, path)
            publication.write_bytes(original)
            valid["publication"] = release.reference(publication)
            downloaded = Path(valid["downloaded_files"][self.archive.name]["path"])
            downloaded.write_bytes(b"wrong downloaded bytes")
            valid["downloaded_files"][self.archive.name] = release.reference(downloaded)
            path.write_text(json.dumps(valid))
            with self.assertRaises(ValueError):
                release.finish(self.root, self.state, complete, path)

    def test_report_keeps_unverified_and_writes_json_markdown(self):
        out = self.root / "build/report.json"
        result = release.write_report(self.root, self.state, out)
        self.assertFalse(result["complete"])
        self.assertEqual(set(result["pending"]), set(release.evidence.RELEASE_REQUIRED_SCENARIOS))
        self.assertIn("不等于通过", out.with_suffix(".md").read_text())
        self.assertTrue(release.report(self.root, self.state, self.root / "build/missing-quality.json")["blockers"])
        (self.root / "x.py").write_text("changed")
        self.assertTrue(release.report(self.root, self.state)["blockers"])

    def test_cli_validates_required_arguments_and_errors(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for args in (["stage"], ["init", "--state", str(self.state)], ["report", "--state", str(self.state)]):
                with self.assertRaises(SystemExit) as raised:
                    release.main(args)
                self.assertEqual(raised.exception.code, 2)
            self.assertEqual(release.main(["validate", "--root", str(self.root), "--state", str(self.state)]), 0)
            self.assertEqual(release.main(["validate", "--root", str(self.root), "--state", str(self.state),
                                           "--stage", "complete"]), 1)
            with mock.patch.object(release.sys, "argv", [release.__file__]), self.assertRaises(SystemExit) as raised:
                runpy.run_path(release.__file__, run_name="__main__")
            self.assertEqual(raised.exception.code, 2)

    def test_cli_dispatches_guarded_actions_without_generic_stage_setter(self):
        common = ["--root", str(self.root), "--state", str(self.state)]
        operations = [("init", "init_state", ["--version", self.version]),
                      ("qualify-source", "qualify_source", []),
                      ("candidate", "make_candidate", ["--version", self.version, "--app", str(self.app),
                        "--dmg", str(self.dmg), "--zip", str(self.archive), "--ota", str(self.ota)]),
                      ("begin-publish", "begin_publish", ["--quality", "quality.json"]),
                      ("complete", "finish", ["--quality", "complete.json", "--readback", "readback.json"]),
                      ("report", "write_report", ["--out", "report.json"])]
        for command, function, arguments in operations:
            with mock.patch.object(release, function, return_value={"blockers": []}) as runner:
                self.assertEqual(release.main([command, *common, *arguments]), 0)
                runner.assert_called_once()
        with mock.patch.object(release, "self_test", return_value=0) as runner:
            self.assertEqual(release.main(["--self-test"]), 0)
            runner.assert_called_once()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            release.main(["--self-test", "validate", *common])
        self.assertEqual(raised.exception.code, 2)

    def test_selftest_has_real_negative_controls(self):
        self.assertEqual(release.self_test(), 0)
        with mock.patch.object(release, "begin_publish"):
            self.assertEqual(release.self_test(), 1, "种掉跳阶段防线时自检必须失败")

    def test_report_binds_candidate_delivery_and_independent_channels(self):
        state = self.candidate()
        delivery = self.root / "build/delivery.json"
        delivery.write_text(json.dumps({"artifacts": {role: {key: data for key, data in value.items() if key != "path"}
                                        for role, value in state["artifacts"].items()},
                                       "matrix": {"fresh_install": {"status": "passed"}}}))
        readback = self.root / "build/readback.json"
        readback.write_text(json.dumps({"source": state["source"], "artifacts": state["artifacts"],
                                       "checks": {"public_ci": {"status": "failed", "reason": "pending"}}}))
        result = release.report(self.root, self.state, readback=readback, delivery=delivery)
        self.assertEqual(result["candidate_delivery"]["fresh_install"]["status"], "passed")
        self.assertEqual(result["channels"]["public_ci"]["status"], "failed")
        self.assertFalse(result["complete"])
        readback.write_text("{}")
        delivery.write_text("{}")
        self.assertEqual(len(release.report(self.root, self.state, readback=readback, delivery=delivery)["blockers"]), 2)
