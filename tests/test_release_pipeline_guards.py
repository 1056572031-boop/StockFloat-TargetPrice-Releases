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

    def test_check_version_requires_no_release_notes(self):
        manifest = Path(tempfile.mkstemp(prefix="manifest_", suffix=".json")[1])
        self.addCleanup(lambda: manifest.unlink(missing_ok=True))
        manifest.write_text(json.dumps({"version": "1.4.118"}), encoding="utf-8")
        old = list(sys.argv)
        try:
            sys.argv = ["publish_update_release.py", "--check-version", "--manifest", str(manifest)]
            with mock.patch.object(publish, "github_token", return_value="token"), \
                    mock.patch.object(publish, "live_manifest_version", return_value="1.4.117"):
                self.assertEqual(publish.main(), 0)
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
            manifest.write_text(json.dumps({"version": "1.4.118"}), encoding="utf-8")
            sys.argv = ["publish_update_release.py", "--notes", str(notes), "--check-version",
                        "--manifest", str(manifest)]
            with mock.patch.object(publish, "github_token", return_value="token"), \
                    mock.patch.object(publish, "live_manifest_version", return_value="1.4.117"):
                self.assertEqual(publish.main(), 0)
        finally:
            sys.argv = old

        newer = ".".join(("1", "4", "118"))
        live = ".".join(("1", "4", "117"))
        publish.ensure_forward_version(newer, live, resume=False)
        publish.ensure_forward_version(newer, newer, resume=True)



if __name__ == "__main__":
    unittest.main()
