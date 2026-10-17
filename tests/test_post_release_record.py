"""Retrospective receipts bind actual bytes without inventing completed scenarios."""

import copy
import contextlib
import hashlib
import io
import json
import plistlib
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sf_update
from sf_constants import APP_DISPLAY_NAME, APP_VERSION, BUNDLE_ID
from tools import quality_evidence as evidence
from tests._base import SFCase


class PostReleaseRecordTests(SFCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        app = self.root / "dist" / (APP_DISPLAY_NAME + ".app")
        (app / "Contents").mkdir(parents=True)
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": BUNDLE_ID, "CFBundleShortVersionString": APP_VERSION}))
        self.installed = self.root / "Applications" / app.name
        shutil.copytree(app, self.installed)
        self.raw = b'{"signed":"fixture"}'
        roles = {}
        for role, path, content in (("app", app, None), ("dmg", self.root / "fixture.dmg", b"dmg"),
                                    ("zip", self.root / "fixture.zip", b"zip"),
                                    ("ota_manifest", self.root / "ota.json", self.raw)):
            if content is not None:
                path.write_bytes(content)
            roles[role] = {"path": path.relative_to(self.root).as_posix(),
                           **evidence.artifact_token(path)}
        self.record = evidence.template()
        self.record["release_artifacts"]["version"] = APP_VERSION
        self.record["scenarios"][1].update(status="not_applicable", required=False,
                                           reason="historical scope exclusion")
        self.candidate = self.root / "candidate.json"
        self.bound = {"record_sha256": self.record_hash(),
                      "release_artifacts": {"roles": roles},
                      "source": {"head": "fixture", "fingerprint": "historical", "algorithm": "fixture"}}
        self.save()
        self.log = self.root / "original.log"
        self.log.write_text("\n".join(("已安装: " + str(self.installed), "版本: " + APP_VERSION,
              "published_archive=Skyrocket-" + APP_VERSION + "-macos.zip",
              "最终启动复验：" + str(self.installed), "结论: 冒烟通过", "发版完成 v" + APP_VERSION)))
        info = SimpleNamespace(version=APP_VERSION, archive_sha256=hashlib.sha256(b"zip").hexdigest())
        self.track_patch(mock.patch.object(sf_update, "fetch_manifest", return_value=self.raw))
        self.track_patch(mock.patch.object(sf_update, "verify_manifest", return_value=info))
        self.track_patch(mock.patch.object(evidence.subprocess, "run",
                                          return_value=SimpleNamespace(returncode=0)))

    def record_hash(self):
        return hashlib.sha256(json.dumps(self.record, sort_keys=True, ensure_ascii=True).encode()).hexdigest()

    def save(self):
        evidence.atomic_json(self.candidate, self.record)
        evidence.atomic_json(self.candidate.with_suffix(".manifest.json"), self.bound)

    def run_record(self):
        return evidence.post_release_record(self.root, self.candidate, self.log, self.installed)

    def test_positive_binds_install_log_online_bytes_and_never_completes(self):
        before = self.candidate.read_bytes()
        path = self.run_record()
        result = evidence.load(path)
        self.assertEqual(result["stage"], "post_release")
        self.assertEqual(result["status"], "incomplete")
        expected_excluded = set(evidence.PERMANENT_EXEMPTIONS) | set(evidence.CAPABILITY_BOUNDARIES)
        self.assertEqual(set(result["pending_scenarios"]),
                         set(evidence.SCENARIOS) - expected_excluded)
        self.assertTrue(all(
            item["status"] == ("not_applicable" if item["id"] in expected_excluded else "unverified")
            for item in result["scenarios"]))
        self.assertEqual(result["historical_publish_log"]["sha256"], evidence.sha256_file(self.log))
        self.assertEqual((path.parent / "online-manifest.json").read_bytes(), self.raw)
        self.assertEqual(self.candidate.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.run_record()

    def test_cli_requires_log_before_reading_or_writing_evidence(self):
        before = self.candidate.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()) as error:
            rc = evidence.main(["post-release", str(self.candidate), "--root", str(self.root),
                                "--installed-app", str(self.installed)])
        self.assertEqual(rc, 2)
        self.assertIn("post-release 需要 --release-log", error.getvalue())
        self.assertEqual(self.candidate.read_bytes(), before)
        sf_update.fetch_manifest.assert_not_called()
        self.assertFalse((self.root / "outputs").exists())

    def test_cli_readback_restores_import_path_and_nonexempt_required_scenario(self):
        scenario = next(item for item in self.record["scenarios"] if item["id"] == "ota_rollback")
        scenario.update(status="not_applicable", required=False, reason="历史错误豁免")
        self.bound["record_sha256"] = self.record_hash()
        self.save()
        isolated_path = [item for item in sys.path if item != str(evidence.ROOT)]
        with mock.patch.object(sys, "path", isolated_path), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            rc = evidence.main(["post-release", str(self.candidate), "--root", str(self.root),
                                "--release-log", str(self.log), "--installed-app", str(self.installed)])
            self.assertIn(str(evidence.ROOT), sys.path)
        self.assertEqual(rc, 0)
        result = evidence.load(Path(output.getvalue().strip()))
        restored = next(item for item in result["scenarios"] if item["id"] == "ota_rollback")
        self.assertEqual(restored["status"], "unverified")
        self.assertIs(restored["required"], True)
        self.assertIn("非永久政策专项", restored["reason"])
        self.assertEqual(set(result["pending_scenarios"]), set(evidence.RELEASE_REQUIRED_SCENARIOS))

    def test_matching_candidate_and_installed_bytes_cannot_hide_wrong_version(self):
        roles = self.bound["release_artifacts"]["roles"]
        app = self.root / roles["app"]["path"]
        for bundle in (app, self.installed):
            path = bundle / "Contents" / "Info.plist"
            identity = plistlib.loads(path.read_bytes())
            identity.pop("CFBundleShortVersionString")
            path.write_bytes(plistlib.dumps(identity))
        roles["app"] = {"path": roles["app"]["path"], **evidence.artifact_token(app)}
        self.save()
        with self.assertRaisesRegex(ValueError, "装机版本不一致"):
            self.run_record()
        sf_update.fetch_manifest.assert_not_called()
        self.assertFalse((self.root / "outputs").exists())

    def test_verified_manifest_with_different_archive_hash_cannot_create_record(self):
        info = SimpleNamespace(version=APP_VERSION, archive_sha256=hashlib.sha256(b"other zip").hexdigest())
        with mock.patch.object(sf_update, "verify_manifest", return_value=info):
            with self.assertRaisesRegex(ValueError, "线上清单 ZIP 哈希不一致"):
                self.run_record()
        self.assertFalse((self.root / "outputs").exists())

    def test_record_tamper_is_rejected(self):
        self.record["intent"] = "tampered"
        self.save()
        with self.assertRaisesRegex(ValueError, "候选记录"):
            self.run_record()

    def test_artifact_and_installed_tamper_are_rejected(self):
        for path in (self.root / "fixture.zip", self.installed / "Contents" / "Info.plist"):
            with self.subTest(path=path):
                before = path.read_bytes()
                path.write_bytes(b"tampered")
                with self.assertRaises(ValueError):
                    self.run_record()
                path.write_bytes(before)

    def test_failed_signature_missing_log_and_different_manifest_fail_closed(self):
        with mock.patch.object(evidence.subprocess, "run",
                               return_value=SimpleNamespace(returncode=1, stderr="invalid")):
            with self.assertRaisesRegex(ValueError, "签名无效"):
                self.run_record()
        before = self.log.read_text()
        self.log.write_text("not proof")
        with self.assertRaisesRegex(ValueError, "发布日志"):
            self.run_record()
        self.log.write_text(before)
        with mock.patch.object(sf_update, "fetch_manifest", return_value=b"other"):
            with self.assertRaisesRegex(ValueError, "线上签名清单"):
                self.run_record()
        with mock.patch.object(sf_update, "verify_manifest", side_effect=sf_update.UpdateError("bad signature")):
            with self.assertRaisesRegex(ValueError, "验签失败"):
                self.run_record()

    def test_invalid_version_cannot_escape_output_directory(self):
        self.record["release_artifacts"]["version"] = "../../outside"
        self.bound["record_sha256"] = self.record_hash()
        self.save()
        with self.assertRaisesRegex(ValueError, "版本无效"):
            self.run_record()

    def test_log_requirement_and_manifest_byte_binding_mutants_are_detected(self):
        # Execute actual modified tool logic in isolation, rather than assert its spelling.
        import ast
        tree = ast.parse(Path(evidence.__file__).read_text())
        for condition in ("log_markers", "online_bytes"):
            with self.subTest(mutant=condition):
                class Mutate(ast.NodeTransformer):
                    def visit_If(self, node):
                        if (condition == "log_markers" and isinstance(node.test, ast.Call)
                                and isinstance(node.test.func, ast.Name) and node.test.func.id == "any"
                                and any(isinstance(n, ast.Name) and n.id == "markers"
                                        for n in ast.walk(node.test))):
                            node.test = ast.Constant(False)
                        if condition == "online_bytes" and isinstance(node.test, ast.BoolOp):
                            if any(isinstance(n, ast.Compare) and isinstance(n.left, ast.Name)
                                   and n.left.id == "raw" for n in ast.walk(node.test)):
                                node.test = ast.Constant(False)
                        return self.generic_visit(node)
                namespace = {"__file__": evidence.__file__, "__name__": "post_release_mutant"}
                mutant = ast.fix_missing_locations(Mutate().visit(copy.deepcopy(tree)))
                exec(compile(mutant, evidence.__file__, "exec"), namespace)
                before = self.log.read_text()
                if condition == "log_markers":
                    self.log.write_text("not proof")
                raw = b"other" if condition == "online_bytes" else self.raw
                with mock.patch.object(sf_update, "fetch_manifest", return_value=raw):
                    # Reuse the negative oracle: it must fail on the mutated implementation.
                    with self.assertRaises(AssertionError):
                        with self.assertRaises(ValueError):
                            namespace["post_release_record"](self.root, self.candidate,
                                                             self.log, self.installed)
                (self.root / "outputs/release-candidates" / ("v" + APP_VERSION)
                 / "post_release.json").unlink()
                self.log.write_text(before)
