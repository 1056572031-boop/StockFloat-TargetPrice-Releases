"""Shared-worktree gate edges, using only a disposable repository."""
import contextlib
import io
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests._base import SFCase
from tools import commit_preflight as preflight
from tools.gate_stamp import git_env


class CommitPreflightEdges(SFCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="preflight-edges-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.env = git_env()
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_CONFIG_NOSYSTEM="1", SF_RERUN_FAILED="0")
        for key in ("SF_GUARD_PROBE", "SF_SKIP_COVERAGE", "SF_RANDOM_ORDER", "SF_PUBLIC_REGRESSION"):
            self.env.pop(key, None)
        self.track_patch(patch.dict(os.environ, self.env, clear=True))
        self.git("init", "-q", "--template=", "-b", "fixture")
        (self.root / "base.py").write_text("VALUE = 1\n")
        hook = self.root / "git-hooks/pre-commit"
        hook.parent.mkdir()
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        self.actual_hook = self.root / ".git/hooks/pre-commit"
        self.actual_hook.parent.mkdir(exist_ok=True)
        self.actual_hook.write_bytes(hook.read_bytes())
        self.actual_hook.chmod(0o755)
        self.git("add", "--", "base.py", "git-hooks/pre-commit")
        self.git("commit", "-qm", "isolated fixture")
        (self.root / "base.py").write_text("VALUE = 2\n")
        self.git("add", "--", "base.py")

    def git(self, *args, input=None):
        return subprocess.run(["git", *args], cwd=self.root, env=self.env, input=input,
                              check=True, capture_output=True, text=True, timeout=20)

    def assert_issue(self, report, code):
        self.assertFalse(report["checks_passed"])
        self.assertIn(code, [item["code"] for item in report["issues"]])

    def test_whole_probe_timeout_is_not_a_success(self):
        with patch.object(preflight.time, "perf_counter", side_effect=[0.0, 2.0, 3.0]):
            report = preflight.inspect_repo(self.root, ["base.py"], timeout=1)
        self.assert_issue(report, "probe_error")
        self.assertEqual(report["issues"][0]["reason"], "probe_timeout")

    def test_nested_directory_does_not_masquerade_as_repository_root(self):
        child = self.root / "child"
        child.mkdir()
        report = preflight.inspect_repo(child, ["base.py"])
        self.assert_issue(report, "probe_error")
        self.assertEqual(report["issues"][0]["reason"], "run_from_repository_root")

    def test_unmerged_index_without_operation_marker_is_still_rejected(self):
        blob = self.git("rev-parse", "HEAD:base.py").stdout.strip()
        self.git("update-index", "--index-info", input="0 " + "0" * 40 + "\tbase.py\n"
                 + "100644 " + blob + " 1\tbase.py\n")
        self.assert_issue(preflight.inspect_repo(self.root, ["base.py"]), "unresolved_conflict")

    def test_executable_hook_without_tracked_source_is_not_trusted(self):
        self.git("rm", "--cached", "--", "git-hooks/pre-commit")
        self.assert_issue(preflight.inspect_repo(self.root, ["base.py", "git-hooks/pre-commit"]),
                          "hook_source_not_tracked")

    def test_hook_change_after_snapshot_is_detected(self):
        original = subprocess.run
        def mutate(args, **kwargs):
            result = original(args, **kwargs)
            if args[-2:] == ["rev-parse", "HEAD"]:
                self.actual_hook.write_text("#!/bin/sh\nexit 1\n")
            return result
        with patch.object(preflight.subprocess, "run", side_effect=mutate):
            self.assert_issue(preflight.inspect_repo(self.root, ["base.py"]), "hook_changed_during_probe")

    def test_root_quality_records_are_evidence_not_execution_inputs(self):
        """发布流程把质量记录放在工程根，别的会话遗留的旧版记录不该让预检红。
        只认根目录这一层的 quality-v*.json；子目录、其他后缀、其他名字仍按原规则拦。"""
        for name in ("quality-v1.4.100.json", "quality-v1.4.100.manifest.json",
                     "quality-v1.4.100-final-complete.json"):
            (self.root / name).write_text("{}\n")
        report = preflight.inspect_repo(self.root, ["base.py"])
        self.assertTrue(report["checks_passed"], report)
        self.assertIn("quality-v1.4.100.json", report["untracked_paths"], "豁免只是不当作执行输入，仍如实列出")

    def test_lookalikes_of_quality_records_are_still_blocked(self):
        cases = {"settings.json": "{}\n", "quality.json": "{}\n", "quality-v1.py": "X = 1\n",
                 "sub/quality-v1.4.100.json": "{}\n"}
        for name, text in cases.items():
            with self.subTest(name=name):
                path = self.root / name
                path.parent.mkdir(exist_ok=True)
                path.write_text(text)
                report = preflight.inspect_repo(self.root, ["base.py"])
                self.assert_issue(report, "untracked_possible_execution_inputs")
                path.unlink()

    def test_direct_cli_reports_success_and_failure_without_touching_real_repository(self):
        previous_path = list(sys.path)
        self.addCleanup(lambda: setattr(sys, "path", previous_path))
        script = Path(preflight.__file__)
        for expected, paths in ((0, ["base.py"]), (1, ["missing.py"])):
            argv = [str(script)] + [arg for name in paths for arg in ("--path", name)]
            with self.subTest(expected=expected), patch.object(sys, "argv", argv), \
                    patch.object(Path, "cwd", return_value=self.root), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(script), run_name="__main__")
            self.assertEqual(raised.exception.code, expected)
