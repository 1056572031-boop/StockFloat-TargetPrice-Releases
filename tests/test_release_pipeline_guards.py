import base64
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import gate_stamp
from tools import publish_update_release as publish
from tools import release_resume
from tools import release_wrapup
from sf_git_identity import checked_out_branch, GitIdentityError
import sf_git_identity
from tests._base import SFCase


class GitIdentityTests(SFCase):
    def make_repo(self):
        temp = tempfile.TemporaryDirectory(prefix="sf_git_identity_")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        env = gate_stamp.git_env()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, env=env, check=True)
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                        "commit", "-q", "--allow-empty", "-m", "base"],
                       cwd=root, env=env, check=True)
        return root

    def test_rejects_reserved_head_branch_instead_of_returning_empty_branch(self):
        root = self.make_repo()
        subprocess.run(["git", "update-ref", "refs/heads/HEAD", "HEAD"], cwd=root, env=gate_stamp.git_env(), check=True)
        with self.assertRaisesRegex(GitIdentityError, "HEAD.*歧义|保留"):
            checked_out_branch(root)

    def test_returns_checked_out_branch_for_normal_repository(self):
        root = self.make_repo()
        self.assertEqual(checked_out_branch(root), "main")

    def test_gate_stamp_direct_execution_can_import_root_module(self):
        proc = subprocess.run([sys.executable, "tools/gate_stamp.py", "--self-test"],
                              cwd=Path(__file__).parents[1], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_git_identity_main_handles_error_and_valid_commands(self):
        with mock.patch.object(sf_git_identity, "checked_out_branch",
                               side_effect=GitIdentityError("bad branch")):
            self.assertEqual(sf_git_identity.main(["check", "--root", str(self.make_repo())]), 2)
        root = self.make_repo()
        self.assertEqual(sf_git_identity.main(["check", "--root", str(root)]), 0)
        self.assertEqual(sf_git_identity.main(["branch", "--root", str(root)]), 0)
        self.assertEqual(sf_git_identity.main(["commit", "--root", str(root)]), 0)

    def test_git_identity_cli_handles_reserved_branch_error(self):
        root = self.make_repo()
        subprocess.run(["git", "update-ref", "refs/heads/HEAD", "HEAD"], cwd=root,
                       env=gate_stamp.git_env(), check=True)
        proc = subprocess.run([sys.executable, "sf_git_identity.py", "check", "--root", str(root)],
                              cwd=Path(__file__).parents[1], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_checked_out_branch_rejects_detached_and_invalid_ref(self):
        root = self.make_repo()
        subprocess.run(["git", "checkout", "-q", "--detach"], cwd=root, env=gate_stamp.git_env(), check=True)
        with self.assertRaises(GitIdentityError):
            checked_out_branch(root)


    def test_checked_out_commit_rejects_missing_branch_commit(self):
        root = self.make_repo()
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/missing"], cwd=root,
                       env=gate_stamp.git_env(), check=True)
        with self.assertRaises(GitIdentityError):
            from sf_git_identity import checked_out_commit
            checked_out_commit(root)

    def test_release_resume_refuses_ambiguous_branch_before_planning(self):
        root = self.make_repo()
        subprocess.run(["git", "update-ref", "refs/heads/HEAD", "HEAD"], cwd=root, env=gate_stamp.git_env(), check=True)
        (root / "sf_constants.py").write_text('APP_VERSION = "1.2.3"\n', encoding="utf-8")
        state = root / "state.json"
        state.write_text(json.dumps({
            "stage": release_resume.PENDING_STAGE,
            "version": "1.2.3",
            "source": {"head": subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"], cwd=root, env=gate_stamp.git_env(),
                check=True, capture_output=True, text=True).stdout.strip()},
            "history": [{"from": "candidate_ready", "to": release_resume.PENDING_STAGE,
                         "at": "2026-01-01T00:00:00+00:00"}],
        }), encoding="utf-8")
        code = release_resume.main(["run", "--root", str(root), "--state", str(state),
                                    "--evidence", str(root / "evidence")])
        self.assertEqual(code, 2)
    def test_checked_out_branch_rejects_reserved_and_malformed_names(self):
        with mock.patch.object(sf_git_identity, "_run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="HEAD\n", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="feature//bad\n", stderr=""),
                subprocess.CompletedProcess([], 1, stdout="", stderr=""),
            ]
            with self.assertRaisesRegex(GitIdentityError, "保留名称 HEAD"):
                checked_out_branch(Path("/tmp"))
            with self.assertRaisesRegex(GitIdentityError, "分支名无效"):
                checked_out_branch(Path("/tmp"))

    def test_checked_out_branch_and_commit_reject_git_failures(self):
        root = self.make_repo()
        with mock.patch.object(sf_git_identity, "_run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="missing")
            with self.assertRaises(GitIdentityError):
                sf_git_identity.checked_out_branch(root)
        with mock.patch.object(sf_git_identity, "checked_out_branch", return_value="main"), \
                mock.patch.object(sf_git_identity, "_run", return_value=subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="missing")):
            with self.assertRaises(GitIdentityError):
                sf_git_identity.checked_out_commit(root)

    def test_git_identity_module_main_entry_is_covered_in_process(self):
        old = list(sys.argv)
        try:
            sys.argv = ["sf_git_identity.py", "branch", "--root", str(self.make_repo())]
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(Path(__file__).parents[1] / "sf_git_identity.py"), run_name="__main__")
            self.assertEqual(raised.exception.code, 0)
        finally:
            sys.argv = old

    def test_gate_stamp_import_path_fallback_is_covered_in_process(self):
        script = Path(__file__).parents[1] / "tools" / "gate_stamp.py"
        old = list(sys.argv)
        try:
            sys.argv = [str(script)]
            with mock.patch.object(sys, "path", [item for item in sys.path if item != str(script.parent.parent)]):
                with self.assertRaises(SystemExit) as raised:
                    runpy.run_path(str(script), run_name="__main__")
            self.assertEqual(raised.exception.code, 2)
        finally:
            sys.argv = old

    def test_gate_stamp_falls_back_to_rev_parse_when_identity_is_rejected(self):
        root = self.make_repo()
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=gate_stamp.git_env(),
                              check=True, capture_output=True, text=True).stdout.strip()
        with mock.patch.object(gate_stamp.git_identity, "checked_out_commit",
                               side_effect=GitIdentityError("reserved ref")):
            self.assertEqual(gate_stamp.git_head(str(root)), head)

    def test_release_resume_release_branch_covers_fallback_paths(self):
        root = self.make_repo()
        with mock.patch.object(release_resume, "git_output", side_effect=ValueError("git unavailable")), \
                mock.patch.object(release_resume.git_identity, "checked_out_branch", return_value="main"):
            self.assertEqual(release_resume.release_branch(root), "main")
        (root / ".git").mkdir(exist_ok=True)
        with mock.patch.object(release_resume, "git_output", return_value="main"), \
                mock.patch.object(release_resume.git_identity, "validate_reserved_head_ref") as validate:
            self.assertEqual(release_resume.release_branch(root), "main")
            validate.assert_called_once_with(root)

    def test_release_wrapup_wraps_non_detached_git_identity_errors(self):
        with mock.patch.object(release_wrapup.git_identity, "checked_out_branch",
                               side_effect=release_wrapup.git_identity.GitIdentityError("reserved ref")):
            with self.assertRaisesRegex(release_wrapup.WrapupError, "reserved ref"):
                release_wrapup.branch_and_head(Path("/tmp"), None)
        with mock.patch.object(release_wrapup.git_identity, "checked_out_branch",
                               side_effect=release_wrapup.git_identity.GitIdentityError("reserved ref")):
            with self.assertRaisesRegex(release_wrapup.WrapupError, "reserved ref"):
                release_wrapup.push(Path("/tmp"), "main", "origin", False)


class ReleaseVersionGuardTests(SFCase):
    def test_candidate_must_be_newer_than_live_version(self):
        newer = ".".join(("1", "4", "118"))
        live = ".".join(("1", "4", "117"))
        previous = ".".join(("1", "4", "116"))
        self.assertEqual(publish.compare_release_versions(newer, live), 1)
        self.assertEqual(publish.compare_release_versions(live, live), 0)
        self.assertEqual(publish.compare_release_versions(previous, live), -1)

    def test_version_parser_rejects_non_x_y_z_inputs(self):
        for left, right in (("1.4", "1.4.117"), (None, "1.4.117"), ("1.a.118", "1.4.117")):
            with self.subTest(left=left, right=right), self.assertRaisesRegex(SystemExit, "版本号必须是 X.Y.Z"):
                publish.compare_release_versions(left, right)

    def test_version_guard_rejects_downgrade_and_same_version_without_resume(self):
        newer = ".".join(("1", "4", "118"))
        live = ".".join(("1", "4", "117"))
        with self.assertRaisesRegex(SystemExit, "低于线上|相同"):
            publish.ensure_forward_version(live, newer, resume=False)
        with self.assertRaisesRegex(SystemExit, "相同"):
            publish.ensure_forward_version(newer, newer, resume=False)

    def test_release_version_guard_allows_resume(self):
        newer = ".".join(("1", "4", "118"))
        live = ".".join(("1", "4", "117"))
        publish.ensure_forward_version(newer, live, resume=False)
        publish.ensure_forward_version(newer, newer, resume=True)

        with mock.patch.object(publish, "read_contents_main", return_value={"content": "%%%"}):
            with self.assertRaises(SystemExit):
                publish.live_manifest_version("token")
        with mock.patch.object(publish, "read_contents_main", return_value={"content": "eyJ2ZXJzaW9uIjogIiJ9"}):
            with self.assertRaises(SystemExit):
                publish.live_manifest_version("token")

    def test_sequence_guard_requires_higher_sequence_but_allows_same_version_stage_promotion(self):
        candidate = {"version": "1.4.118", "release_sequence": 11, "stage": "stable"}
        live = {"version": "1.4.118", "release_sequence": 11, "stage": "beta"}
        publish.ensure_release_sequence(candidate, live, resume=False)

        with self.assertRaisesRegex(SystemExit, "序号"):
            publish.ensure_release_sequence(
                {"version": "1.4.119", "release_sequence": 10}, live, resume=False)

    def test_explicit_rollback_requires_matching_ledger_record_and_newer_timestamp(self):
        ledger = {"releases": [
            {"version": "1.4.116", "sequence": 9, "status": "published"},
            {"version": "1.4.118", "sequence": 11, "status": "published"},
        ]}
        candidate = {"version": "1.4.116", "release_sequence": 9}
        live = {"version": "1.4.118", "release_sequence": 11,
                "published_at": "2026-11-19T10:00:00Z"}
        stamped = publish.prepare_rollback(candidate, live, ledger, "1.4.116", "service regression",
                                           now="2026-11-19T10:01:00Z")
        self.assertEqual(stamped["published_at"], "2026-11-19T10:01:00Z")
        with self.assertRaisesRegex(SystemExit, "账本"):
            publish.prepare_rollback(candidate, live, {"releases": []}, "1.4.116", "service regression",
                                     now="2026-11-19T10:01:00Z")

    def test_live_manifest_version_requires_a_nonempty_value(self):
        version = "1.2.3"
        with mock.patch.object(publish, "live_manifest_metadata", return_value={"version": version}):
            result = publish.live_manifest_version("token")
        self.assertEqual(result, version)
        with mock.patch.object(publish, "live_manifest_metadata", return_value={}):
            with self.assertRaisesRegex(SystemExit, "缺少有效 version"):
                publish.live_manifest_version("token")

    def test_sequence_guard_allows_first_sequenced_release_from_legacy_live_manifest(self):
        candidate = {"version": "1.4.119", "release_sequence": 1,
                     "stage": "beta", "published_at": "2026-11-19T10:01:00Z"}
        live = {"version": "1.4.118", "stage": "beta",
                "published_at": "2026-11-19T10:00:00Z"}
        publish.ensure_release_sequence(candidate, live, resume=False)

    def test_version_guard_allows_beta_to_stable_promotion_without_resume(self):
        publish.ensure_forward_version("1.4.118", "1.4.118", resume=False,
                                       allow_same_version=True)

    def test_published_at_guard_rejects_older_normal_candidate(self):
        with self.assertRaisesRegex(SystemExit, "published_at"):
            publish.ensure_published_at_forward(
                {"published_at": "2026-11-19T09:59:00Z"},
                {"published_at": "2026-11-19T10:00:00Z"},
            )

    def test_same_sequence_archive_replacement_requires_explicit_audit(self):
        live = {"version": "1.4.118", "release_sequence": 11,
                "stage": "beta", "archive": {"sha256": "old"}}
        candidate = {"version": "1.4.118", "release_sequence": 11,
                     "stage": "beta", "archive": {"sha256": "new"}}
        with self.assertRaisesRegex(SystemExit, "同序号"):
            publish.ensure_release_sequence(candidate, live, resume=False)

    def test_same_sequence_replacement_is_rejected_even_with_resume_without_audit(self):
        live = {"version": "1.4.118", "release_sequence": 11,
                "stage": "beta", "archive": {"sha256": "old"}}
        candidate = {"version": "1.4.118", "release_sequence": 11,
                     "stage": "beta", "archive": {"sha256": "new"}}
        with self.assertRaisesRegex(SystemExit, "同序号"):
            publish.ensure_release_sequence(candidate, live, resume=True)

    def test_legacy_sequence_migration_still_requires_newer_published_at(self):
        with self.assertRaisesRegex(SystemExit, "published_at"):
            publish.ensure_published_at_forward(
                {"published_at": "2026-11-19T09:59:00Z"},
                {"published_at": "2026-11-19T10:00:00Z"},
            )

    def test_upload_archive_can_reuse_existing_asset_for_authorized_same_bytes_flow(self):
        archive = Path(tempfile.mkstemp(prefix="archive_", suffix=".zip")[1])
        self.addCleanup(lambda: archive.unlink(missing_ok=True))
        archive.write_bytes(b"zip")
        release = {"id": 1, "assets": [{"name": archive.name}]}
        with mock.patch.object(publish, "reuse_uploaded_archive", return_value={"name": archive.name}) as reuse:
            result = publish.upload_archive("token", release, archive, b"zip",
                                           resume=False, reuse_existing=True)
        reuse.assert_called_once()
        self.assertEqual(result["name"], archive.name)

    def test_same_sequence_different_version_is_rejected_even_with_audit_reason(self):
        live = {"version": "1.4.118", "release_sequence": 11,
                "stage": "beta", "archive": {"sha256": "old"}}
        candidate = {"version": "1.4.119", "release_sequence": 11,
                     "stage": "beta", "archive": {"sha256": "new"}}
        with self.assertRaisesRegex(SystemExit, "版本|同序号"):
            publish.ensure_release_sequence(candidate, live, resume=True)

    def test_rollback_uses_default_ledger_path(self):
        self.assertEqual(
            publish.rollback_ledger_path(None, Path("/tmp/source")),
            Path("/tmp/source/tools/release_sequence_ledger.json"),
        )
        with self.assertRaisesRegex(SystemExit, "默认账本"):
            publish.rollback_ledger_path(Path("/tmp/custom.json"), Path("/tmp/source"))

        from nacl.signing import SigningKey
        key = SigningKey.generate()
        candidate = {"schema": 1, "version": "1.4.116", "release_sequence": 9,
                     "published_at": "2026-11-19T09:00:00Z", "notes": "old"}
        signed = publish.resign_manifest(candidate, key,
                                         "2026-11-19T10:01:00Z")
        self.assertEqual(signed["published_at"], "2026-11-19T10:01:00Z")
        self.assertNotEqual(signed["signature"]["value"], "")
        key.verify_key.verify(publish.canonical(signed),
                              base64.b64decode(signed["signature"]["value"]))

    def test_check_version_rejects_sequence_regression_before_publication(self):
        with tempfile.TemporaryDirectory(prefix="sequence-preflight-") as root:
            manifest = Path(root) / "manifest.json"
            manifest.write_text(json.dumps({"version": "2.0.0", "release_sequence": 4,
                                            "published_at": "2026-11-21T00:00:00Z"}), encoding="utf-8")
            live = {"version": "1.0.0", "release_sequence": 5,
                    "published_at": "2026-11-20T00:00:00Z"}
            with mock.patch.object(publish, "github_token", return_value="token"), \
                 mock.patch.object(publish, "live_manifest_metadata", return_value=live):
                with self.assertRaisesRegex(SystemExit, "序号"):
                    publish.check_version_main(["--check-version", "--manifest", str(manifest)])

    def test_check_version_allows_only_same_identity_beta_promotion(self):
        with tempfile.TemporaryDirectory(prefix="promotion-preflight-") as root:
            manifest = Path(root) / "manifest.json"
            candidate = {"version": "1.2.3", "release_sequence": 1, "stage": "stable",
                         "published_at": "2026-11-21T00:00:00Z", "archive": {"sha256": "a" * 64}}
            live = {**candidate, "stage": "beta", "published_at": "2026-11-20T00:00:00Z"}
            manifest.write_text(json.dumps(candidate), encoding="utf-8")
            with mock.patch.object(publish, "github_token", return_value="fixture"), \
                    mock.patch.object(publish, "live_manifest_metadata", return_value=live):
                self.assertEqual(publish.check_version_main(["--check-version", "--manifest", str(manifest)]), 0)
                candidate["archive"] = {"sha256": "b" * 64}
                manifest.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaisesRegex(SystemExit, "摘要"):
                    publish.check_version_main(["--check-version", "--manifest", str(manifest)])

    def test_pointer_write_uses_checked_sha_without_refreshing_it(self):
        content = b"bound manifest"
        payload = {"sha": "newer-unchecked", "content": base64.b64encode(content).decode()}
        with mock.patch.object(publish, "request") as request, \
                mock.patch.object(publish, "read_contents_main", return_value=payload) as read:
            publish.publish_manifest_pointer("fixture", content, expected_sha="checked-sha")
        self.assertEqual(json.loads(request.call_args.kwargs["data"])["sha"], "checked-sha")
        read.assert_called_once_with("fixture")

    def test_rollback_publication_preserves_final_signature_and_rejects_tampering(self):
        import sf_constants
        from nacl.signing import SigningKey
        key = SigningKey.generate()
        version = "1.2.3"
        candidate = {"version": version, "release_sequence": 1,
                     "rollback_reason": "regression", "published_at": "2026-11-23T00:00:00Z"}
        signed = publish.resign_manifest(candidate, key, candidate["published_at"])
        original = json.dumps(signed, sort_keys=True)
        ledger = {"releases": [{"version": version, "sequence": 1, "status": "published"}]}
        live = {"published_at": "2026-11-22T00:00:00Z"}
        with mock.patch.object(sf_constants, "UPDATE_PUBLIC_KEY_B64",
                               base64.b64encode(bytes(key.verify_key)).decode()):
            publish.validate_rollback_publication(signed, live, ledger, version, "regression")
            self.assertEqual(json.dumps(signed, sort_keys=True), original)
            with self.assertRaisesRegex(SystemExit, "签名"):
                publish.validate_rollback_publication({**signed, "notes": "tampered"}, live,
                                                      ledger, version, "regression")
            with self.assertRaisesRegex(SystemExit, "published_at"):
                publish.validate_rollback_publication(signed, candidate, ledger, version, "regression")

    def test_publication_guards_reject_invalid_shapes_and_unsupported_signatures(self):
        with self.assertRaisesRegex(SystemExit, "对象"):
            publish.ensure_release_sequence([], {}, resume=False)
        same = {"version": "fixture", "release_sequence": 1, "archive": {"sha256": "a" * 64}}
        with self.assertRaisesRegex(SystemExit, "与线上相同"):
            publish.ensure_release_sequence(same, same, resume=False)
        with self.assertRaisesRegex(SystemExit, "不是有效时间戳"):
            publish.ensure_published_at_forward({"published_at": "not-a-dateZ"},
                                               {"published_at": "2026-01-01T00:00:00Z"})
        version = "1.2.3"
        candidate = {"version": version, "release_sequence": 1,
                     "published_at": "2026-11-23T00:00:00Z"}
        live = {"published_at": "2026-11-22T00:00:00Z"}
        ledger = {"releases": [{"version": version, "sequence": 1}]}
        with self.assertRaisesRegex(SystemExit, "完全一致"):
            publish.prepare_rollback(candidate, live, ledger, "different", "reason",
                                     now=candidate["published_at"])
        with self.assertRaisesRegex(SystemExit, "质量证据"):
            publish.validate_rollback_publication(candidate, live, ledger, version, "reason")
        unsupported = {**candidate, "rollback_reason": "reason",
                       "signature": {"algorithm": "unsupported", "value": "bad"}}
        with self.assertRaisesRegex(SystemExit, "签名无效"):
            publish.validate_rollback_publication(unsupported, live, ledger, version, "reason")
        with mock.patch.object(publish, "request") as request:
            with self.assertRaisesRegex(SystemExit, "SHA 无效"):
                publish.publish_manifest_pointer("fixture", b"{}", expected_sha="")
        request.assert_not_called()

    def test_check_version_rejects_missing_sequence_and_missing_live_version(self):
        with tempfile.TemporaryDirectory(prefix="sf-version-guard-") as root:
            path = Path(root) / "manifest.json"
            path.write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")
            with mock.patch.object(publish, "github_token") as credentials:
                with self.assertRaisesRegex(SystemExit, "release_sequence"):
                    publish.check_version_main(["--check-version", "--manifest", str(path)])
            credentials.assert_not_called()
            path.write_text(json.dumps({"version": "1.2.3", "release_sequence": 1}), encoding="utf-8")
            with mock.patch.object(publish, "github_token", return_value="fixture"), \
                    mock.patch.object(publish, "live_manifest_metadata", return_value={}):
                with self.assertRaisesRegex(SystemExit, "version"):
                    publish.check_version_main(["--check-version", "--manifest", str(path)])

    def test_rollback_prepare_main_dispatch_and_bad_inputs_stay_local(self):
        import sf_constants
        from nacl.signing import SigningKey
        with mock.patch.object(sys, "argv", ["publisher", "--prepare-rollback"]), \
                mock.patch.object(publish, "prepare_rollback_main", return_value=7) as prepare:
            self.assertEqual(publish.main(), 7)
        prepare.assert_called_once_with(["--prepare-rollback"])
        with tempfile.TemporaryDirectory(prefix="sf-rollback-guard-") as root:
            source = Path(root)
            manifest, archive, output = source / "manifest.json", source / "archive.zip", source / "out.json"
            manifest.write_text(json.dumps({"version": "1.2.3", "release_sequence": 1}), encoding="utf-8")
            argv = ["--prepare-rollback", "--manifest", str(manifest), "--archive", str(archive),
                    "--output", str(output), "--rollback-to", "different", "--reason", "reason",
                    "--published-at", "2026-11-23T00:00:00Z"]
            with mock.patch.object(publish, "SOURCE_ROOT", source), \
                    mock.patch.object(publish, "verify_rollback_sequence"), \
                    mock.patch.object(publish, "request") as request:
                with self.assertRaisesRegex(SystemExit, "版本或原因"):
                    publish.prepare_rollback_main(argv)
                argv[argv.index("different")] = "1.2.3"
                key_dir = source / ".private_tools"
                key_dir.mkdir()
                key = SigningKey.generate()
                (key_dir / "update_signing_key.txt").write_text(base64.b64encode(bytes(key)).decode())
                with mock.patch.object(sf_constants, "UPDATE_PUBLIC_KEY_B64",
                                       base64.b64encode(bytes(SigningKey.generate().verify_key)).decode()):
                    with self.assertRaisesRegex(SystemExit, "公钥不一致"):
                        publish.prepare_rollback_main(argv)
                self.assertFalse(output.exists())
                request.assert_not_called()

    def test_current_timestamp_is_utc_and_second_precision(self):
        from datetime import datetime, timezone
        instant = datetime(2026, 11, 23, 12, 30, 45, 123456, tzinfo=timezone.utc)
        with mock.patch.object(publish, "datetime") as fixed_clock:
            fixed_clock.now.return_value = instant
            value = publish.utc_now()
        fixed_clock.now.assert_called_once_with(timezone.utc)
        self.assertEqual(value, "2026-11-23T12:30:45Z")

    def test_check_version_requires_no_release_notes(self):
        manifest = Path(tempfile.mkstemp(prefix="manifest_", suffix=".json")[1])
        self.addCleanup(lambda: manifest.unlink(missing_ok=True))
        manifest.write_text(json.dumps({"version": "1.4.118", "release_sequence": 10, "published_at": "2026-11-20T00:00:00Z"}), encoding="utf-8")
        old = list(sys.argv)
        try:
            sys.argv = ["publish_update_release.py", "--check-version", "--manifest", str(manifest)]
            with mock.patch.object(publish, "github_token", return_value="token"), \
                    mock.patch.object(publish, "live_manifest_metadata", return_value={"version": "1.4.117", "published_at": "2026-11-19T00:00:00Z"}):
                self.assertEqual(publish.main(), 0)
        finally:
            sys.argv = old

    def test_verify_candidate_sequence_wraps_missing_app_and_identity_mismatch(self):
        with tempfile.TemporaryDirectory(prefix="verify-sequence-") as td:
            root = Path(td)
            constants = root / "sf_constants.py"
            constants.write_text("APP_VERSION='1.0.0'\nRELEASE_SEQUENCE=1\n", encoding="utf-8")
            ledger = root / "tools" / "release_sequence_ledger.json"
            ledger.parent.mkdir()
            ledger.write_text(json.dumps({"schema": 1, "releases": []}), encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            archive = root / "archive.zip"
            with self.assertRaisesRegex(SystemExit, "没有冻结"):
                publish.verify_candidate_sequence(archive, manifest, {}, ledger_path=ledger, source_root=root)
            app = root / "dist" / "Skyrocket.app" / "Contents"
            app.mkdir(parents=True)
            with mock.patch.object(publish, "verify_release_sequence", return_value={
                "version": "2.0.0", "release_sequence": 2}):
                with self.assertRaisesRegex(SystemExit, "不一致"):
                    publish.verify_candidate_sequence(archive, manifest,
                                                      {"version": "1.0.0", "release_sequence": 1},
                                                      ledger_path=ledger, source_root=root)
            with mock.patch.object(publish, "verify_release_sequence",
                                   side_effect=ValueError("bad identity")):
                with self.assertRaisesRegex(SystemExit, "发布前序号核验失败"):
                    publish.verify_candidate_sequence(archive, manifest, {},
                                                      ledger_path=ledger, source_root=root)

    def test_main_rejects_rollback_flags_before_network(self):
        old = list(sys.argv)
        try:
            sys.argv = ["publish_update_release.py", "--notes", "/tmp/notes", "--rollback-to", "1.0.0"]
            with self.assertRaisesRegex(SystemExit, "必须同时提供"):
                publish.main()
            sys.argv = ["publish_update_release.py", "--notes", "/tmp/notes", "--reason", "why"]
            with self.assertRaisesRegex(SystemExit, "只能与"):
                publish.main()
        finally:
            sys.argv = old

    def test_publish_check_version_usage_and_bad_manifest(self):
        notes = Path(tempfile.mkstemp(prefix="notes_", suffix=".txt")[1])
        self.addCleanup(lambda: notes.unlink(missing_ok=True))
        notes.write_text("1. 检查版本\n", encoding="utf-8")
        old = list(sys.argv)
        try:
            sys.argv = ["publish_update_release.py", "--notes", str(notes), "--check-version"]
            with self.assertRaisesRegex(SystemExit, "必须提供 --manifest"):
                publish.main()
            sys.argv = ["publish_update_release.py", "--notes", str(notes), "--check-version",
                        "--manifest", str(Path("/nonexistent"))]
            with self.assertRaisesRegex(SystemExit, "候选清单无法读取"):
                publish.main()
            manifest = Path(tempfile.mkstemp(prefix="manifest_", suffix=".json")[1])
            self.addCleanup(lambda: manifest.unlink(missing_ok=True))
            manifest.write_text(json.dumps({"version": "1.4.118", "release_sequence": 10, "published_at": "2026-11-20T00:00:00Z"}), encoding="utf-8")
            sys.argv = ["publish_update_release.py", "--notes", str(notes), "--check-version",
                        "--manifest", str(manifest)]
            with mock.patch.object(publish, "github_token", return_value="token"), \
                    mock.patch.object(publish, "live_manifest_metadata", return_value={"version": "1.4.117", "published_at": "2026-11-19T00:00:00Z"}):
                self.assertEqual(publish.main(), 0)
        finally:
            sys.argv = old

        newer = ".".join(("1", "4", "118"))
        live = ".".join(("1", "4", "117"))
        publish.ensure_forward_version(newer, live, resume=False)
        publish.ensure_forward_version(newer, newer, resume=True)



if __name__ == "__main__":
    unittest.main()
