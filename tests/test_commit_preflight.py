"""Fail-fast checks must not create commits, stage changes or certify a full gate."""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from tests._base import SFCase
from tools.commit_preflight import inspect_repo, main, screen_state
from tools.gate_stamp import git_env


class TestCommitPreflight(SFCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(prefix="sf-preflight-")
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name).resolve() / "repository with spaces"
        self.repo.mkdir()
        self.env = git_env()
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                        SF_RERUN_FAILED="0", LC_ALL="C")
        for name in ("SF_GUARD_PROBE", "SF_SKIP_COVERAGE", "SF_RANDOM_ORDER",
                     "SF_PUBLIC_REGRESSION"):
            self.env.pop(name, None)
        self.track_patch(mock.patch.dict(os.environ, self.env, clear=True))
        self.git("init", "-q", "--template=", "-b", "ordinary")
        self.git("config", "user.name", "preflight-test")
        self.git("config", "user.email", "preflight@example.invalid")
        self.write("base.py", "VALUE = 1\n")
        self.write(".gitignore", "ignored.py\n")
        hook = self.write("git-hooks/pre-commit", "#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        actual = self.repo / ".git/hooks/pre-commit"
        actual.parent.mkdir(exist_ok=True)
        actual.write_bytes(hook.read_bytes())
        actual.chmod(0o755)
        self.git("add", "--", "base.py", ".gitignore", "git-hooks/pre-commit")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.write("base.py", "VALUE = 2\n")
        self.git("add", "--", "base.py")

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                                capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="ascii")
        return path

    def inspect(self, paths=None, **kwargs):
        return inspect_repo(self.repo, ["base.py"] if paths is None else paths, **kwargs)

    def assert_blocked(self, code, **kwargs):
        report = self.inspect(**kwargs)
        self.assertFalse(report["checks_passed"], report)
        self.assertIn(code, [issue["code"] for issue in report["issues"]], report)
        return report

    def test_positive_checks_do_not_claim_authorization_or_full_gate(self):
        report = self.inspect(expected_head=self.base)
        self.assertTrue(report["checks_passed"], report)
        self.assertEqual(report["staged_paths"], ["base.py"])
        self.assertEqual(report["head"], self.base)
        self.assertEqual(len(report["hook_sha256"]), 64)
        self.assertIn("full_gate", report["not_checked"])
        self.assertIn("authorization", report["not_checked"])
        self.assertGreater(report["elapsed_ms"], 0)

    def test_probe_does_not_change_index_objects_head_config_or_run_hooks(self):
        actual = self.repo / ".git/hooks/pre-commit"
        body = "#!/bin/sh\nprintf ran > should-not-exist\n"
        actual.write_text(body, encoding="ascii")
        self.write("git-hooks/pre-commit", body)
        self.git("add", "--", "git-hooks/pre-commit")
        git = self.repo / ".git"
        tracked = {name: (git / name).read_bytes() for name in ("index", "HEAD", "config")}
        objects = sorted(str(path.relative_to(git)) for path in (git / "objects").rglob("*"))
        report = self.inspect(paths=["base.py", "git-hooks/pre-commit"])
        self.assertTrue(report["checks_passed"], report)
        self.assertEqual(tracked, {name: (git / name).read_bytes() for name in tracked})
        self.assertEqual(objects, sorted(str(path.relative_to(git)) for path in
                                         (git / "objects").rglob("*")))
        self.assertFalse((self.repo / "should-not-exist").exists())

    def test_scope_mismatch_and_omitted_file_are_blocked(self):
        self.write("extra.py", "EXTRA = True\n")
        self.git("add", "--", "extra.py")
        self.assert_blocked("staged_outside_scope")
        self.assert_blocked("scope_paths_not_staged", paths=["base.py", "extra.py", "missing.py"])

    def test_empty_or_invalid_scope_is_never_a_success(self):
        for paths in ([], ["."], ["../base.py"], ["/base.py"], ["base.py", "base.py"]):
            with self.subTest(paths=paths):
                self.assert_blocked("probe_error", paths=paths)

    def test_nothing_staged_is_not_a_success(self):
        self.git("commit", "-qm", "fix: stage consumed")
        self.assert_blocked("nothing_staged")

    def test_partial_staging_is_blocked(self):
        self.write("base.py", "VALUE = 3\n")
        self.assert_blocked("unstaged_tracked_changes")

    def test_hidden_index_flag_stops_before_worktree_diff(self):
        self.git("update-index", "--assume-unchanged", "base.py")
        self.write("base.py", "VALUE = 3\n")
        self.assert_blocked("hidden_index_flags")

    def test_untracked_execution_input_is_blocked_but_notes_are_reported(self):
        self.write("untracked.py", "INPUT = True\n")
        self.assert_blocked("untracked_possible_execution_inputs")
        (self.repo / "untracked.py").unlink()
        self.write("notes.md", "not an execution input\n")
        report = self.inspect()
        self.assertTrue(report["checks_passed"], report)
        self.assertIn("notes.md", report["untracked_paths"])
        self.assertIn("ignored_dependencies", report["not_checked"])

    def test_whitespace_failure_is_not_treated_as_a_success(self):
        self.write("base.py", "VALUE = 2  \n")
        self.git("add", "--", "base.py")
        self.assert_blocked("staged_whitespace_errors")

    def test_hook_mismatch_or_nonexecutable_hook_is_blocked_without_installing(self):
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="ascii")
        self.assert_blocked("hook_source_mismatch")
        hook.chmod(0o644)
        self.assert_blocked("hook_missing_or_not_executable")

    def test_custom_hook_path_is_resolved_by_git(self):
        custom = self.repo / "custom hooks/pre-commit"
        custom.parent.mkdir()
        custom.write_bytes((self.repo / "git-hooks/pre-commit").read_bytes())
        custom.chmod(0o755)
        self.git("config", "core.hooksPath", str(custom.parent))
        report = self.inspect()
        self.assertTrue(report["checks_passed"], report)
        self.assertEqual(Path(report["hook_path"]), custom)

    def test_guard_zero_and_retry_environment_fail_before_git_without_leaking_values(self):
        for overrides, code in (({"SF_GUARD_PROBE": "0"}, "quality_environment_override"),
                                ({"SF_RERUN_FAILED": "1"}, "automatic_failure_retry_enabled"),
                                ({"GIT_INDEX_FILE": "secret-location"}, "git_environment_override")):
            with self.subTest(overrides=overrides), mock.patch.dict(os.environ, overrides):
                report = self.assert_blocked(code)
                self.assertEqual(report["git_calls"], 0)
                self.assertNotIn("secret-location", str(report))

    def test_special_operation_or_index_lock_is_not_removed(self):
        for name in ("MERGE_HEAD", "index.lock", "rebase-merge"):
            with self.subTest(name=name):
                path = self.repo / ".git" / name
                path.write_text("preserve\n", encoding="ascii")
                self.assert_blocked("operation_or_index_lock_present")
                self.assertEqual(path.read_text(), "preserve\n")
                path.unlink()

    def test_checkout_filter_is_rejected_without_executing_it(self):
        self.git("config", "filter.example.clean", "touch unexpected-filter-execution")
        self.write(".gitattributes", "*.py filter=example\n")
        self.assert_blocked("special_checkout_configuration")
        self.assertFalse((self.repo / "unexpected-filter-execution").exists())

    def test_tracked_venv_is_rejected(self):
        self.write(".venv/placeholder", "must not be tracked\n")
        self.git("add", "--", ".venv/placeholder")
        self.assert_blocked("tracked_runtime_environment", paths=["base.py", ".venv/placeholder"])

    def test_changed_head_or_detached_head_is_blocked(self):
        self.assert_blocked("head_changed", expected_head="0" * 40)
        self.git("switch", "--detach", "-q", "HEAD")
        self.assert_blocked("detached_head")

    def test_nul_delimited_paths_handle_newlines_and_non_ascii(self):
        name = "odd name\n\u80a1.py"
        self.write(name, "VALUE = True\n")
        self.git("add", "--", name)
        report = self.inspect(paths=["base.py", name])
        self.assertTrue(report["checks_passed"], report)
        self.assertIn(name, report["staged_paths"])

    def test_command_failure_or_timeout_is_not_a_success(self):
        for failure in (subprocess.TimeoutExpired("git", 1), OSError("missing git")):
            with self.subTest(failure=type(failure).__name__), mock.patch(
                "tools.commit_preflight.subprocess.run", side_effect=failure
            ):
                self.assert_blocked("probe_error")
        self.assert_blocked("probe_error", timeout=0)

    def test_unknown_cli_option_is_rejected_before_probe(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(["--path", "base.py", "--skip-checks"])
        self.assertEqual(raised.exception.code, 2)

    def test_diff_infrastructure_error_does_not_look_like_no_changes(self):
        original = subprocess.run

        def fail_diff(args, **kwargs):
            if "diff" in args and "--exit-code" in args:
                return subprocess.CompletedProcess(args, 128, stdout=b"", stderr=b"failure")
            return original(args, **kwargs)

        with mock.patch("tools.commit_preflight.subprocess.run", side_effect=fail_diff):
            self.assert_blocked("probe_error")

    def test_index_change_during_probe_is_blocked(self):
        original = subprocess.run

        def concurrent_stage(args, **kwargs):
            result = original(args, **kwargs)
            if args[-2:] == ["rev-parse", "HEAD"]:
                self.write("raced.py", "RACED = True\n")
                staged = original(["git", "add", "--", "raced.py"], cwd=self.repo,
                                  env=self.env, capture_output=True, check=False, timeout=10)
                self.assertEqual(staged.returncode, 0)
            return result

        with mock.patch("tools.commit_preflight.subprocess.run", side_effect=concurrent_stage):
            self.assert_blocked("snapshot_changed_during_probe")

    def test_rename_requires_both_source_and_destination_in_scope(self):
        self.git("mv", "--", "base.py", "renamed.py")
        self.assert_blocked("staged_outside_scope", paths=["renamed.py"])
        report = self.inspect(paths=["base.py", "renamed.py"])
        self.assertTrue(report["checks_passed"], report)

    def test_linked_worktree_is_checked_without_touching_original_index(self):
        original_index = (self.repo / ".git/index").read_bytes()
        linked = self.repo.parent / "linked worktree"
        self.git("worktree", "add", "-qb", "isolated", str(linked), "HEAD")
        (linked / "base.py").write_text("VALUE = 3\n", encoding="ascii")
        self.git("-C", str(linked), "add", "--", "base.py")
        report = inspect_repo(linked, ["base.py"])
        self.assertTrue(report["checks_passed"], report)
        self.assertEqual(report["branch"], "isolated")
        self.assertEqual((self.repo / ".git/index").read_bytes(), original_index)

    def test_submodule_index_entry_is_not_treated_as_an_ordinary_file(self):
        self.git("update-index", "--add", "--cacheinfo", "160000," + self.base + ",dependency")
        self.assert_blocked("submodule_requires_separate_review", paths=["base.py", "dependency"])

    def test_extra_script_config_and_executable_inputs_are_not_missed(self):
        for name in ("task.mjs", "settings.jsonc", ".env.local", "custom-command"):
            with self.subTest(name=name):
                path = self.write(name, "potential execution input\n")
                if name == "custom-command":
                    path.chmod(0o755)
                self.assert_blocked("untracked_possible_execution_inputs")
                path.unlink()

    def test_external_diff_is_not_executed(self):
        self.git("config", "diff.external", "touch unexpected-diff-execution")
        report = self.inspect()
        self.assertTrue(report["checks_passed"], report)
        self.assertFalse((self.repo / "unexpected-diff-execution").exists())

    def test_replacement_refs_and_sparse_checkout_require_explicit_review(self):
        self.git("update-ref", "refs/replace/" + self.base, self.base)
        self.assert_blocked("replacement_refs_require_separate_review")
        self.git("update-ref", "-d", "refs/replace/" + self.base)
        self.git("config", "core.sparseCheckout", "true")
        self.assert_blocked("special_checkout_configuration")

    def test_locked_screen_warns_but_never_blocks_the_commit(self):
        report = self.inspect(screen_probe=lambda: {"locked": True, "display_asleep": False})
        self.assertTrue(report["checks_passed"], report)
        self.assertEqual(report["warnings"],
                         ["screen_locked_full_gate_may_fail_for_unrelated_reasons"])

    def test_sleeping_display_warns_separately_from_a_locked_session(self):
        report = self.inspect(screen_probe=lambda: {"locked": False, "display_asleep": True})
        self.assertTrue(report["checks_passed"], report)
        self.assertEqual(report["warnings"],
                         ["display_asleep_full_gate_may_fail_for_unrelated_reasons"])

    def test_unreadable_screen_state_is_named_not_reported_as_awake(self):
        report = self.inspect(screen_probe=lambda: {"locked": None, "display_asleep": False})
        self.assertTrue(report["checks_passed"], report)
        self.assertEqual(report["warnings"], ["screen_state_unreadable"])

    def test_awake_readable_screen_has_no_warnings(self):
        report = self.inspect(screen_probe=lambda: {"locked": False, "display_asleep": False})
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["screen"], {"locked": False, "display_asleep": False})

    def test_screen_state_is_reported_even_when_an_early_check_stops_the_probe(self):
        # Retry enabled stops the probe before any git call; the screen line must still be there.
        with mock.patch.dict(os.environ, {"SF_RERUN_FAILED": "1"}):
            report = self.inspect(screen_probe=lambda: {"locked": True, "display_asleep": False})
        self.assertFalse(report["checks_passed"], report)
        self.assertEqual(report["screen"]["locked"], True)
        self.assertIn("screen_locked_full_gate_may_fail_for_unrelated_reasons", report["warnings"])

    def test_screen_state_without_quartz_reports_unknown_for_both(self):
        with mock.patch.dict(sys.modules, {"Quartz": None}):
            self.assertEqual(screen_state(), {"locked": None, "display_asleep": None})

    def test_one_unreadable_signal_does_not_hide_the_other(self):
        class FakeQuartz:
            @staticmethod
            def CGSessionCopyCurrentDictionary():
                raise RuntimeError("no session")

            @staticmethod
            def CGMainDisplayID():
                return 1

            @staticmethod
            def CGDisplayIsAsleep(_display):
                return True

        with mock.patch.dict(sys.modules, {"Quartz": FakeQuartz}):
            self.assertEqual(screen_state(), {"locked": None, "display_asleep": True})

    def test_unreadable_display_does_not_hide_a_locked_session(self):
        class FakeQuartz:
            @staticmethod
            def CGSessionCopyCurrentDictionary():
                return {"CGSSessionScreenIsLocked": 1}

            @staticmethod
            def CGMainDisplayID():
                raise RuntimeError("no display")

        with mock.patch.dict(sys.modules, {"Quartz": FakeQuartz}):
            self.assertEqual(screen_state(), {"locked": True, "display_asleep": None})

    def test_live_probe_returns_only_booleans_or_none(self):
        # Not asserting the machine's real state, only the shape other tools rely on.
        state = screen_state()
        self.assertEqual(sorted(state), ["display_asleep", "locked"])
        for value in state.values():
            self.assertIn(value, (True, False, None))
