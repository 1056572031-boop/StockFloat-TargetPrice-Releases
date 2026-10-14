"""Separate branch denominators, strict input and isolated collection contracts."""
from __future__ import annotations

import configparser
import copy
import io
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

try:
    from _base import SFCase
except ImportError:  # pragma: no cover
    from tests._base import SFCase

import branch_coverage_report as report


def summary():
    return {"num_statements": 10, "covered_lines": 8, "missing_lines": 2,
            "num_branches": 4, "covered_branches": 3, "missing_branches": 1,
            "num_partial_branches": 1}


def raw_data():
    files = {name: {"summary": summary(), "missing_branches": [[3, 7]],
                    "functions": {"unit": {"summary": summary(),
                                            "missing_branches": [[3, 7]]}}}
             for name in report.FOCUS}
    files["tools/example.py"] = {"summary": summary(), "functions": {}}
    return {"meta": {"branch_coverage": True}, "files": files}


class BranchSummaryContracts(SFCase):
    def test_branch_ratio_is_not_the_combined_coverage_percent(self):
        raw = raw_data()
        for entry in raw["files"].values():
            entry["summary"]["percent_covered"] = 99.99
        result = report.summarize(raw)
        for tier in result["tiers"].values():
            self.assertEqual(tier["line_percent"], 80)
            self.assertEqual(tier["branch_percent"], 75)
        self.assertFalse(result["release_gate"])

    def test_missing_edges_and_partial_lines_are_preserved(self):
        result = report.summarize(raw_data())
        self.assertEqual(result["focus"][0]["functions"][0]["missing_edges"], [[3, 7]])
        self.assertEqual(result["tiers"]["product"]["num_partial_branches"], len(report.FOCUS))

    def test_line_only_input_refuses_a_branch_conclusion(self):
        raw = raw_data()
        raw["meta"]["branch_coverage"] = False
        with self.assertRaisesRegex(ValueError, "line coverage only"):
            report.summarize(raw)

    def test_empty_or_test_only_scope_is_not_full_coverage(self):
        for files in ({}, {"tests/fake.py": {"summary": summary()}}):
            with self.subTest(files=files), self.assertRaisesRegex(ValueError, "No product"):
                report.summarize({"meta": {"branch_coverage": True}, "files": files})

    def test_missing_high_risk_module_is_named(self):
        raw = raw_data()
        raw["files"].pop("sf_glass.py")
        with self.assertRaisesRegex(ValueError, "sf_glass.py"):
            report.summarize(raw)

    def test_zero_branch_denominator_is_not_reported_as_100_percent(self):
        raw = raw_data()
        for entry in raw["files"].values():
            for stats in [entry["summary"]] + [f["summary"] for f in entry["functions"].values()]:
                stats.update(num_branches=0, covered_branches=0, missing_branches=0,
                             num_partial_branches=0)
        result = report.summarize(raw)
        self.assertIsNone(result["tiers"]["product"]["branch_percent"])
        self.assertIn("n/a", report.markdown(result))

    def test_invalid_negative_boolean_or_missing_counts_are_rejected(self):
        for value in (-1, True, "3", None):
            with self.subTest(value=value):
                stats = summary()
                stats["covered_branches"] = value
                with self.assertRaises(ValueError):
                    report.counts(stats)

    def test_inconsistent_line_or_branch_totals_are_rejected(self):
        for key in ("covered_branches", "covered_lines"):
            with self.subTest(key=key):
                stats = summary()
                stats[key] += 1
                with self.assertRaisesRegex(ValueError, "reconcile"):
                    report.counts(stats)

    def test_scope_paths_cannot_escape_source_root(self):
        for filename in ("/elsewhere/module.py", "../module.py"):
            raw = raw_data()
            raw["files"][filename] = {"summary": summary()}
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                report.summarize(raw)

    def test_markdown_includes_function_edges_and_observation_warning(self):
        text = report.markdown(report.summarize(raw_data()))
        self.assertIn("Not a release gate", text)
        self.assertIn("75.00%", text)
        self.assertIn("`unit`", text)
        self.assertIn("[[3, 7]]", text)

    def test_bad_input_shapes_refuse_without_attribute_errors(self):
        for raw in ([], {}, {"meta": None}, {"meta": {"branch_coverage": True}, "files": []}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                report.summarize(raw)

    def test_summarizing_never_mutates_raw_snapshot(self):
        raw = raw_data()
        before = copy.deepcopy(raw)
        report.summarize(raw)
        self.assertEqual(raw, before)


class BranchCollectionContracts(SFCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.track_patch(mock.patch.object(report, "ROOT", self.root))
        (self.root / ".coveragerc").write_text(
            "[run]\nsource = .\nomit = tests/*\n[report]\nfail_under = 65\n", encoding="utf-8")

    def invoke(self, args):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            try:
                rc = report.main(args)
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue()

    def test_collection_uses_ephemeral_config_and_preserves_line_data_and_settings(self):
        line_data = self.root / ".coverage"
        line_data.write_bytes(b"line sentinel")
        original_config = (self.root / ".coveragerc").read_bytes()
        data = self.root / "build" / "branch.coverage"
        captured = {}

        def run(command, **kwargs):
            captured.update(kwargs)
            config = configparser.ConfigParser()
            config.read(kwargs["env"]["COVERAGE_RCFILE"])
            self.assertTrue(config.getboolean("run", "branch"))
            self.assertEqual(config.get("run", "omit"), "tests/*")
            self.assertEqual(config.get("report", "fail_under"), "65")
            self.assertEqual(command, [str(self.root / "run_tests.sh")])
            return types.SimpleNamespace(returncode=0)

        with mock.patch.dict(os.environ, {"SF_SKIP_COVERAGE": "1", "SF_RANDOM_ORDER": "1",
                                         "SF_GUARD_PROBE": "1"}), \
             mock.patch.object(report.subprocess, "run", side_effect=run):
            self.assertEqual(report.collect(data, self.root / "log"), 0)
        env = captured["env"]
        self.assertEqual(env["COVERAGE_FILE"], str(data.resolve()))
        self.assertEqual(env["SF_SKIP_COVERAGE"], "0")
        self.assertEqual(env["SF_RANDOM_ORDER"], "0")
        self.assertNotIn("SF_GUARD_PROBE", env)
        self.assertFalse(pathlib.Path(env["COVERAGE_RCFILE"]).exists())
        self.assertEqual(line_data.read_bytes(), b"line sentinel")
        self.assertEqual((self.root / ".coveragerc").read_bytes(), original_config)

    def test_default_line_database_cannot_be_used_as_branch_collection_target(self):
        with mock.patch.object(report.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "must not overwrite"):
                report.collect(self.root / ".coverage", self.root / "log")
        run.assert_not_called()

    def test_missing_run_config_cannot_start_tests(self):
        (self.root / ".coveragerc").unlink()
        with mock.patch.object(report.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                report.collect(self.root / "branch.coverage", self.root / "log")
        run.assert_not_called()

    def test_failed_collection_does_not_export_or_issue_a_new_report(self):
        output = self.root / "out"
        with mock.patch.object(report, "collect", return_value=1), \
             mock.patch.object(report, "read_data") as read:
            rc, text = self.invoke(["--collect", "--output-dir", str(output)])
        self.assertEqual(rc, 1)
        read.assert_not_called()
        self.assertFalse((output / "report.md").exists())
        self.assertIn("no branch verdict", text)

    def test_input_json_writes_three_independent_artifacts(self):
        path = self.root / "snapshot.json"
        path.write_text(json.dumps(raw_data()), encoding="utf-8")
        output = self.root / "out"
        with mock.patch.object(report, "read_data") as read:
            rc, text = self.invoke(["--input-json", str(path), "--output-dir", str(output)])
        self.assertEqual(rc, 0, text)
        read.assert_not_called()
        parsed = json.loads((output / "summary.json").read_text())
        self.assertFalse(parsed["release_gate"])
        self.assertEqual(json.loads((output / "raw.json").read_text()), raw_data())
        self.assertIn("Branch", (output / "report.md").read_text())

    def test_invalid_or_missing_json_gives_no_conclusion_not_traceback(self):
        path = self.root / "snapshot.json"
        for body in (None, "not json", "[]"):
            if body is not None:
                path.write_text(body, encoding="utf-8")
            rc, text = self.invoke(["--input-json", str(path), "--output-dir", str(self.root / "out")])
            self.assertEqual(rc, 2)
            self.assertIn("No branch conclusion", text)
            self.assertNotIn("Traceback", text)

    def test_collect_and_input_json_are_mutually_exclusive(self):
        rc, _text = self.invoke(["--collect", "--input-json", "x.json"])
        self.assertEqual(rc, 2)

    def test_unknown_argument_cannot_collect_or_overwrite(self):
        with mock.patch.object(report, "collect") as collect:
            rc, _text = self.invoke(["--collet"])
        self.assertEqual(rc, 2)
        collect.assert_not_called()

    def test_read_data_requires_a_file_and_an_exported_json(self):
        path = self.root / "branch.coverage"
        with self.assertRaises(ValueError):
            report.read_data(path)
        path.write_bytes(b"invalid")
        with mock.patch.object(report.subprocess, "run", return_value=types.SimpleNamespace(
                returncode=1, stdout="", stderr="bad database")):
            with self.assertRaisesRegex(ValueError, "bad database"):
                report.read_data(path)

    def test_read_data_uses_public_json_export_and_explicit_database(self):
        path = self.root / "branch.coverage"
        path.write_bytes(b"sentinel")

        def export(command, **kwargs):
            self.assertIn("--data-file=" + str(path.resolve()), command)
            output = pathlib.Path(command[command.index("-o") + 1])
            output.write_text(json.dumps(raw_data()), encoding="utf-8")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(report.subprocess, "run", side_effect=export):
            self.assertEqual(report.read_data(path), raw_data())
        self.assertEqual(path.read_bytes(), b"sentinel")


if __name__ == "__main__":
    unittest.main()
