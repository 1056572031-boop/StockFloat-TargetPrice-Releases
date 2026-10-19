"""Check ordinary commit mechanisms in disposable repositories, not the real branch."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from tests._base import SFCase
from tools.gate_stamp import check, current_info, git_env, write_stamp


class TestCommitWorkflow(SFCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(prefix="sf-commit-workflow-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.repo = self.root / "repository with spaces"
        self.repo.mkdir()
        self.env = git_env()
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        self.git("init", "-q", "--template=", "-b", "ordinary")
        self.git("config", "user.name", "workflow-test")
        self.git("config", "user.email", "workflow@example.invalid")
        self.write("base.py", "VALUE = 1\n")
        self.git("add", "--", "base.py")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()

    def command(self, args, *, cwd=None, expected=0):
        result = subprocess.run(args, cwd=cwd or self.repo, env=self.env,
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def git(self, *args, cwd=None, expected=0):
        return self.command(["git", *args], cwd=cwd, expected=expected)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")
        return path

    def stage_change(self):
        self.write("base.py", "VALUE = 2\n")
        self.git("add", "--", "base.py")
        return self.git("write-tree").stdout.strip()

    def hook(self, name, body):
        path = Path(self.git("rev-parse", "--path-format=absolute", "--git-path",
                             "hooks/" + name).stdout.strip())
        path.parent.mkdir(exist_ok=True)
        path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="ascii")
        path.chmod(0o755)
        return path

    def test_normal_commit_keeps_reviewed_tree_parent_and_index(self):
        tree = self.stage_change()
        self.hook("pre-commit", "printf ran > hook-ran\n")
        self.git("diff", "--cached", "--quiet", "--exit-code", expected=1)
        self.git("diff", "--exit-code")
        self.git("commit", "-qm", "fix: ordinary change")
        self.assertEqual((self.repo / "hook-ran").read_text(), "ran")
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}").stdout.strip(), tree)
        self.assertEqual(self.git("write-tree").stdout.strip(), tree)
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", "HEAD")
                         .stdout.split()[1:], [self.base])
        self.git("diff", "HEAD", "--exit-code")
        self.git("diff", "--cached", "--exit-code", "HEAD")

    def test_partially_staged_worktree_is_not_the_reviewed_tree(self):
        tree = self.stage_change()
        self.write("base.py", "VALUE = 3\n")
        self.git("diff", "--exit-code", expected=1)
        self.assertEqual(self.git("write-tree").stdout.strip(), tree)
        self.assertEqual(self.git("show", ":base.py").stdout, "VALUE = 2\n")

    def test_hidden_index_flags_can_hide_modified_worktree_from_diff(self):
        for flag, marker in (("--assume-unchanged", "h "), ("--skip-worktree", "S ")):
            with self.subTest(flag=flag):
                self.write("base.py", "VALUE = 1\n")
                self.git("update-index", flag, "base.py")
                self.write("base.py", "VALUE = 999\n")
                self.git("diff", "--exit-code")
                self.assertIn(marker + "base.py", self.git("ls-files", "-v").stdout)
                self.assertNotEqual(self.git("show", ":base.py").stdout,
                                    (self.repo / "base.py").read_text())
                self.git("update-index", flag.replace("--", "--no-", 1), "base.py")

    def test_pre_commit_index_mutation_invalidates_reviewed_tree(self):
        tree = self.stage_change()
        self.hook("pre-commit", "printf injected > injected.txt\ngit add -- injected.txt\n")
        self.git("commit", "-qm", "fix: hook changes scope")
        self.assertNotEqual(self.git("rev-parse", "HEAD^{tree}").stdout.strip(), tree)

    def test_post_commit_index_mutation_is_not_detected_by_worktree_head_diff(self):
        tree = self.stage_change()
        self.hook("post-commit", "blob=$(printf 'VALUE = 777\\n' | git hash-object -w --stdin)\n"
                  'git update-index --cacheinfo "100644,$blob,base.py"\n')
        self.git("commit", "-qm", "fix: staged blob after commit")
        self.git("diff", "HEAD", "--exit-code")
        self.git("diff", "--cached", "--exit-code", "HEAD", expected=1)
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}").stdout.strip(), tree)
        self.assertNotEqual(self.git("write-tree").stdout.strip(), tree)

    def test_commit_message_rejection_preserves_head_and_staged_work(self):
        tree = self.stage_change()
        self.hook("pre-commit", "printf checked > check-ran\n")
        self.hook("commit-msg", "exit 1\n")
        self.git("commit", "-qm", "fix: rejected message", expected=1)
        self.assertEqual((self.repo / "check-ran").read_text(), "checked")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.base)
        self.assertEqual(self.git("write-tree").stdout.strip(), tree)

    def test_wrapper_failure_does_not_prove_no_commit_was_created(self):
        tree = self.stage_change()
        wrapper = ("import subprocess, sys\n"
                   "subprocess.run(['git', 'commit', '-qm', 'fix: saved'], check=True)\n"
                   "sys.exit(23)\n")
        self.command([sys.executable, "-c", wrapper], expected=23)
        self.assertNotEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.base)
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}").stdout.strip(), tree)
        self.assertEqual(self.git("rev-list", "--count", self.base + "..HEAD")
                         .stdout.strip(), "1")

    def test_ignored_dependency_is_not_proved_by_clean_status_or_tracked_tree(self):
        self.write(".gitignore", "local_dependency.py\n")
        self.git("add", "--", ".gitignore")
        self.git("commit", "-qm", "chore: fixture ignore rule")
        self.write("local_dependency.py", "VALUE = 99\n")
        self.assertEqual(self.git("status", "--porcelain=v1").stdout, "")
        self.assertEqual(self.git("ls-tree", "-r", "HEAD", "--",
                                  "local_dependency.py").stdout, "")
        self.assertIn("local_dependency.py", self.git("ls-files", "--others", "--ignored",
                                                     "--exclude-standard").stdout)
        probe = [sys.executable, "-c", "import local_dependency; assert local_dependency.VALUE == 99"]
        self.command(probe)
        clean = self.root / "clean dependency check"
        self.git("worktree", "add", "-q", "--detach", str(clean), "HEAD")
        result = self.command(probe, cwd=clean, expected=1)
        self.assertIn("ModuleNotFoundError", result.stderr)
        self.assertIn("local_dependency", result.stderr)

    def test_post_gate_input_change_can_match_a_stamp_but_not_pre_gate_snapshot(self):
        before = current_info(str(self.repo))
        self.write("untracked.py", "CHANGED = True\n")
        write_stamp(str(self.repo))
        self.assertTrue(check(str(self.repo))[0])
        self.assertNotEqual(before["fingerprint"], current_info(str(self.repo))["fingerprint"])
        self.assertEqual(self.git("diff", "HEAD").stdout, "")

    def test_commit_changes_head_but_not_fingerprint_and_unlisted_docs_change_tree(self):
        tree = self.stage_change()
        before = current_info(str(self.repo))
        self.git("commit", "-qm", "fix: compare content not head")
        after = current_info(str(self.repo))
        self.assertNotEqual(before["head"], after["head"])
        self.assertEqual(before["fingerprint"], after["fingerprint"])
        self.write("internal.md", "documentation outside fingerprint list\n")
        self.git("add", "--", "internal.md")
        self.assertEqual(after["fingerprint"], current_info(str(self.repo))["fingerprint"])
        self.assertNotEqual(self.git("write-tree").stdout.strip(), tree)

    def test_binary_patch_isolation_keeps_original_dirty_worktree_and_branch(self):
        payload = self.repo / "payload.bin"
        payload.write_bytes(bytes(range(256)))
        self.git("add", "--", "payload.bin")
        patch = self.root / "approved.patch"
        result = subprocess.run(["git", "diff", "--cached", "--binary", "--full-index"],
                                cwd=self.repo, env=self.env, capture_output=True,
                                timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        patch.write_bytes(result.stdout)
        tree = self.git("write-tree").stdout.strip()
        self.write("base.py", "VALUE = 999\n")
        status = self.git("status", "--porcelain=v1").stdout
        isolated = self.root / "isolated worktree"
        self.git("worktree", "add", "-qb", "reviewed", str(isolated), self.base)
        self.git("apply", "--index", str(patch), cwd=isolated)
        self.assertEqual(self.git("write-tree", cwd=isolated).stdout.strip(), tree)
        self.assertEqual((isolated / "payload.bin").read_bytes(), bytes(range(256)))
        self.git("diff", "--exit-code", cwd=isolated)
        self.git("commit", "-qm", "feat: isolated payload", cwd=isolated)
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}", cwd=isolated).stdout.strip(), tree)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.base)
        self.assertEqual(self.git("status", "--porcelain=v1").stdout, status)
        self.assertEqual((self.repo / "base.py").read_text(), "VALUE = 999\n")

    def test_fixture_environment_removes_all_inherited_git_routing(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": "foreign", "GIT_INDEX_FILE": "foreign",
                                          "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "x.y",
                                          "GIT_CONFIG_VALUE_0": "foreign"}):
            self.assertFalse(any(name.startswith("GIT_") for name in git_env()))
