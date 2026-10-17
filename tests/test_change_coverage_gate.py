# -*- coding: utf-8 -*-
"""Focused tests for the changed-line coverage observer."""
from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
from unittest import mock

import coverage
from tests._base import SFCase
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import change_coverage_gate as G  # noqa: E402


def git(root: Path, *args: str) -> None:
    env = G.git_env()
    subprocess.run(["git", *args], cwd=root, env=env, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def coverage_for(root: Path, source: str, *, run_source: str | None = None,
                 source_name: str = "app.py") -> Path:
    import coverage
    path = root / ".coverage"
    cov = coverage.Coverage(data_file=str(path), config_file=False)
    cov.start()
    exec(compile(run_source if run_source is not None else source,
                 str(root / source_name), "exec"), {"__name__": "__main__"})
    cov.stop(); cov.save()
    return path


class ChangeCoverageTests(SFCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(prefix="change_cov_test_")
        self.root = Path(self.tmp.name).resolve()
        git(self.root, "init", "-q")

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def commit_base(self, text="x = 1\n", name="app.py"):
        (self.root / name).write_text(text, encoding="utf-8")
        git(self.root, "add", "--", name)
        subprocess.run(["git", "-c", "user.name=gate", "-c", "user.email=gate@example.invalid",
                        "commit", "-qm", "base"], cwd=self.root, env=G.git_env(),
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root,
                                       env=G.git_env(), text=True).strip()

    def test_new_executable_lines_are_missing_when_not_measured(self):
        base = self.commit_base()
        source = "x = 1\ny = 2\n"
        (self.root / "app.py").write_text(source, encoding="utf-8")
        data = coverage_for(self.root, source, run_source="x = 1\n")
        result = G.inspect(self.root, base, data)
        self.assertEqual(result["status"], "fail")
        self.assertIn(2, result["files"][0]["missing"])

    def test_tracked_cjk_filename_uses_name_only_z_and_hunk_lines(self):
        filename = "中文 空格.py"
        base = self.commit_base("x = 1\n", name=filename)
        source = "x = 1\ny = 2\n"
        (self.root / filename).write_text(source, encoding="utf-8")
        data = coverage_for(self.root, source, run_source="x = 1\n", source_name=filename)
        result = G.inspect(self.root, base, data)
        entry = next(f for f in result["files"] if f["path"] == filename)
        self.assertEqual(entry["executable_changed"], [2])
        self.assertEqual(entry["missing"], [2])

    def test_measured_untracked_file_still_targets_all_statements(self):
        base = self.commit_base()
        filename = "new.py"
        source = "x = 1\ny = 2\n"
        (self.root / filename).write_text(source, encoding="utf-8")
        data = coverage_for(self.root, source, run_source="x = 1\n", source_name=filename)
        result = G.inspect(self.root, base, data)
        entry = next(f for f in result["files"] if f["path"] == filename)
        self.assertEqual(entry["status"], "new")
        self.assertEqual(entry["executable_changed"], [1, 2])
        self.assertEqual(entry["missing"], [2])
        data = coverage_for(self.root, source, source_name=filename)
        complete = G.inspect(self.root, base, data)
        self.assertEqual(complete["status"], "pass")
        self.assertEqual(complete["files"][0]["missing"], [])

    def test_bad_and_empty_coverage_databases_are_rejected(self):
        base = self.commit_base()
        for contents in (b"", b"not a coverage database"):
            path = self.root / ".coverage"
            path.write_bytes(contents)
            with self.subTest(contents=contents):
                with self.assertRaises(ValueError):
                    G.inspect(self.root, base, path)

    def test_relative_measured_paths_are_resolved_against_source_root(self):
        base = self.commit_base()
        source = self.root / "app.py"
        source.write_text("x = 2\n", encoding="utf-8")
        path = self.root / "relative.coverage"
        data = coverage.CoverageData(basename=str(path))
        data.add_lines({"app.py": {1}})
        data.write()
        before = path.read_bytes(), path.stat().st_mtime_ns
        result = G.inspect(self.root, base, path)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["files"][0]["missing"], [])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertFalse((self.root / ".coverage").exists())

    def test_comment_only_change_is_not_applicable(self):
        base = self.commit_base("x = 1\n")
        (self.root / "app.py").write_text("# changed\nx = 1\n", encoding="utf-8")
        data = coverage_for(self.root, "x = 1\n")
        result = G.inspect(self.root, base, data)
        self.assertEqual(result["status"], "not_applicable")
        self.assertFalse(result["ok"])

    def test_untracked_python_is_included_and_deleted_is_reported(self):
        base = self.commit_base()
        (self.root / "new name.py").write_text("z = 3\n", encoding="utf-8")
        data = coverage_for(self.root, "x = 1\n")
        result = G.inspect(self.root, base, data)
        self.assertTrue(any(f["path"] == "new name.py" for f in result["files"]))
        (self.root / "app.py").unlink()
        result = G.inspect(self.root, base, data)
        self.assertTrue(any(f["status"] == "deleted" for f in result["files"]))

    def test_excluded_tests_and_archive_are_explicit(self):
        base = self.commit_base()
        (self.root / "tests").mkdir(); (self.root / "tests" / "x.py").write_text("x=1\n")
        (self.root / "tools").mkdir(); (self.root / "tools" / "x.py").write_text("x=1\n")
        (self.root / "tools" / "archive").mkdir(); (self.root / "tools" / "archive" / "old.py").write_text("x=1\n")
        data = coverage_for(self.root, "x = 1\n")
        result = G.inspect(self.root, base, data)
        excluded = {x["path"] for x in result["excluded_files"]}
        self.assertIn("tests/x.py", excluded)
        self.assertIn("tools/archive/old.py", excluded)

    def test_unknown_and_missing_base_cli(self):
        proc = subprocess.run([sys.executable, str(ROOT / "tools" / "change_coverage_gate.py"), "--unknown"],
                              cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 2)
        proc = subprocess.run([sys.executable, str(ROOT / "tools" / "change_coverage_gate.py"), "--base", "nope"],
                              cwd=self.root, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 2)

    def test_report_contains_selection_and_data_hash(self):
        base = self.commit_base()
        data = coverage_for(self.root, "x = 1\n")
        result = G.inspect(self.root, base, data)
        self.assertIn("selection", result); self.assertIn("data_sha256", result)
        json.dumps(result, ensure_ascii=False)

    def cli(self, *args):
        output = io.StringIO()
        with mock.patch.object(Path, "cwd", return_value=self.root), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = G.main(list(args))
        return code, output.getvalue()

    def test_cli_selftest_normal_and_rejects_false_green(self):
        # CLI subprocess proves the real entrypoint works even with hostile config.
        (self.root / ".coveragerc").write_text("[run]\nsource = nonexistent_package\n")
        env = dict(os.environ, COVERAGE_RCFILE=str(self.root / ".coveragerc"),
                   COVERAGE_FILE=str(self.root / "unused"))
        proc = subprocess.run([sys.executable, str(Path(G.__file__)), "--self-test"],
                              cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("self-test passed", proc.stdout)
        self.assertFalse((self.root / "unused").exists())
        self.assertEqual(self.cli("--self-test")[0], 0)
        real_inspect = G.inspect

        def corrupt(*args, **kwargs):
            report = real_inspect(*args, **kwargs)
            report["status"] = "pass"
            report["ok"] = True
            report["summary"] = {"executable_changed": 0, "missing": 0}
            return report

        with mock.patch.object(G, "inspect", side_effect=corrupt):
            code, output = self.cli("--self-test")
        self.assertEqual(code, 1)
        self.assertIn("self-test failed", output)
        # Also reject a judge that never accepts actually measured changed lines.
        def always_fail(*args, **kwargs):
            report = real_inspect(*args, **kwargs)
            report["status"] = "fail"
            report["ok"] = False
            return report

        with mock.patch.object(G, "inspect", side_effect=always_fail):
            self.assertEqual(self.cli("--self-test")[0], 1)
        for args in (("--base", "HEAD"), ("--report-json", "report.json"),
                     ("--data-file", ".coverage")):
            with self.subTest(args=args), self.assertRaises(SystemExit) as caught:
                self.cli("--self-test", *args)
            self.assertEqual(caught.exception.code, 2)

    def test_cli_pass_fail_reports_and_environment_default(self):
        base = self.commit_base()
        source = "x = 1\ny = 2\n"
        (self.root / "app.py").write_text(source)
        data = coverage_for(self.root, source, run_source="x = 1\n")
        dedicated = self.root / "dedicated.coverage"
        data.rename(dedicated)
        with mock.patch.dict(os.environ, {"COVERAGE_FILE": str(dedicated)}):
            code, output = self.cli("--base", base, "--report-json", "reports/result.json")
        self.assertEqual(code, 1)
        self.assertIn("未覆盖行：2", output)
        report = json.loads((self.root / "reports/result.json").read_text())
        self.assertEqual(report["data_file"], str(dedicated))
        self.assertFalse(report["ok"])
        coverage_for(self.root, source)
        with mock.patch.dict(os.environ, {"COVERAGE_FILE": str(self.root / "missing")}):
            code, output = self.cli("--base", base, "--data-file", ".coverage",
                                    "--report-json", str(self.root / "absolute.json"))
        self.assertEqual(code, 0)
        self.assertIn("pass (1 executable, 0 missing)", output)
        self.assertEqual(self.cli("--base", base, "--data-file", str(data),
                                  "--report-json", str(self.root.parent / "outside.json"))[0], 2)
        self.assertEqual(self.cli("--base", base, "--data-file", "absent")[0], 2)
        with self.assertRaises(SystemExit) as caught:
            self.cli()
        self.assertEqual(caught.exception.code, 2)

    def test_cli_not_applicable_does_not_claim_coverage_pass(self):
        base = self.commit_base()
        data = coverage_for(self.root, "x = 1\n")
        for change in (None, "docs", "comment"):
            if change == "docs":
                (self.root / "note.txt").write_text("old\n")
                git(self.root, "add", "note.txt")
            elif change == "comment":
                (self.root / "app.py").write_text("x = 1\n# comment\n")
            code, output = self.cli("--base", base, "--data-file", str(data))
            self.assertEqual(code, 0)
            self.assertIn("不算 coverage 通过", output)
            report = G.inspect(self.root, base, data)
            self.assertEqual(report["status"], "not_applicable")
            self.assertFalse(report["ok"])

    def test_low_level_paths_and_errors(self):
        # These helpers enforce filesystem/git trust boundaries, not UI internals.
        binary = G.__dict__["_git_binary"]
        with mock.patch.object(G.subprocess, "run", side_effect=OSError("git absent")):
            self.assertIsNone(binary(self.root, []))
        safe = G.__dict__["_safe_rel"]
        for path in ("", "a\x00b", "/absolute.py", "../escape.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe(path)
        self.assertEqual(safe("tools\\x.py"), "tools/x.py")
        select = G.__dict__["_selected"]
        self.assertEqual(select("pkg/app.py"), (False, "不在产品根目录或 tools/"))
        self.assertFalse(select("readme.txt")[0])
        parse = G.__dict__["_parse_hunks"]
        self.assertEqual(parse(b"@@ -1 +2,2 @@\n", "a.py")[0], {"a.py": {2, 3}})
        self.assertEqual(parse(b"@@ malformed @@\n", "a.py"), ({"a.py": set()}, {}))
        self.assertEqual(parse(b"@@ -1 +2 @@\n"), ({}, {}))
        resolve = G.__dict__["_resolve_source"]
        with self.assertRaises(ValueError):
            resolve(self.root, "../escape.py")
        self.assertIsNone(G.__dict__["_digest"](self.root / "absent"))
        self.assertEqual(G.__dict__["_list_length"](None), 0)
        cov = mock.Mock()
        cov.analysis2.side_effect = SyntaxError("broken source")
        with self.assertRaisesRegex(ValueError, "analysis2"):
            G.__dict__["_coverage_lines"](cov, self.root / "app.py")

    def test_selftest_collection_lifecycle(self):
        # Real collection is covered by the CLI test; isolate orchestration here
        # so its own nested collector cannot hide exec/finally from the caller.
        reports = [
            {"status": status, "ok": status == "pass",
             "summary": {"executable_changed": 1, "missing": len(missing)},
             "files": [{"executable_changed": [1], "missing": missing}]}
            for status, missing in (("fail", [1]), ("pass", []))
        ]
        with mock.patch.object(G.coverage, "Coverage") as collector, \
                mock.patch.object(G, "inspect", side_effect=reports):
            self.assertEqual(self.cli("--self-test")[0], 0)
        self.assertEqual(collector.return_value.start.call_count, 2)
        self.assertEqual(collector.return_value.stop.call_count, 2)
        self.assertEqual(collector.return_value.save.call_count, 2)

    def test_git_changes_rejects_each_failed_read(self):
        with self.assertRaisesRegex(ValueError, "base 不存在"):
            G.__dict__["_resolve_base"](self.root, "absent")
        base = self.commit_base()
        (self.root / "app.py").write_text("x = 2\n")
        real_binary = G.__dict__["_git_binary"]
        for marker in ("--name-only", "--unified=0", "ls-files"):
            def fail_one(root, args):
                return None if marker in args else real_binary(root, args)
            with self.subTest(marker=marker), \
                    mock.patch.object(G, "_git_binary", side_effect=fail_one), \
                    self.assertRaises(ValueError):
                G.__dict__["_git_changes"](self.root, base)
        with mock.patch.object(G, "_git_text", side_effect=[base, None]):
            self.assertEqual(G.__dict__["_git_changes"](self.root, base)[2], [base, ""])

    def test_database_without_measurements_and_outside_tree_rejected(self):
        base = self.commit_base()
        data = self.root / "dedicated.coverage"
        empty = coverage.CoverageData(basename=str(data))
        empty.add_lines({})
        empty.write()
        with self.assertRaisesRegex(ValueError, "没有 measured"):
            G.inspect(self.root, base, data)
        for filename in (str(self.root.parent / "elsewhere.py"), "../elsewhere.py"):
            outside = coverage.CoverageData(basename=str(data))
            outside.add_lines({filename: {1}})
            outside.write()
            with self.subTest(filename=filename), self.assertRaisesRegex(ValueError, "工作树外"):
                G.inspect(self.root, base, data)

    def test_branch_data_is_observation_only(self):
        base = self.commit_base()
        (self.root / "app.py").write_text("x = 2\n")
        data = self.root / "dedicated.coverage"
        arcs = coverage.CoverageData(basename=str(data))
        arcs.add_arcs({str(self.root / "app.py"): {(-1, 1), (1, -1)}})
        arcs.write()
        report = G.inspect(self.root, base, data)
        self.assertTrue(report["has_arcs"])
        self.assertEqual(report["files"][0]["changed_source_arcs_observed"], 2)
        self.assertIn("仅观察", report["files"][0]["branch_note"])

    def test_script_entrypoint_and_loader_failure(self):
        # Execute the actual module guard in-process so dedicated coverage sees it.
        with mock.patch.object(sys, "argv", [str(Path(G.__file__))]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            runpy.run_path(str(Path(G.__file__)), run_name="__main__")
        self.assertEqual(caught.exception.code, 2)
        for spec in (None, mock.Mock(loader=None)):
            with mock.patch("importlib.util.spec_from_file_location", return_value=spec), \
                    self.assertRaisesRegex(ImportError, "cannot load gate_stamp"):
                runpy.run_path(str(Path(G.__file__)))


if __name__ == "__main__":
    unittest.main()
