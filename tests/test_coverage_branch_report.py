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

    def test_nested_coverage_shapes_are_validated_and_empty_functions_skipped(self):
        cases = []

        raw = raw_data()
        raw["files"][report.FOCUS[0]]["summary"] = []
        cases.append(raw)

        raw = raw_data()
        raw["files"][1] = raw["files"].pop(report.FOCUS[0])
        cases.append(raw)

        raw = raw_data()
        raw["files"][report.FOCUS[0]]["functions"] = []
        cases.append(raw)

        raw = raw_data()
        raw["files"][report.FOCUS[0]]["functions"] = {"unit": []}
        cases.append(raw)

        for invalid in cases:
            with self.subTest(raw=invalid), self.assertRaises(ValueError):
                report.summarize(invalid)

        raw = raw_data()
        functions = raw["files"][report.FOCUS[0]]["functions"]
        functions[""] = copy.deepcopy(functions["unit"])
        result = report.summarize(raw)
        self.assertEqual([item["name"] for item in result["focus"][0]["functions"]], ["unit"])

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
        self.track_patch(mock.patch.object(report.gate_stamp, "current_info", return_value={
            "fingerprint": "source-a", "files": 5, "head": "archival-only"}))
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

    def fake_collection(self, command, **kwargs):
        env = kwargs.get("env", {})
        if command == [str(self.root / "run_tests.sh")]:
            data = pathlib.Path(env["COVERAGE_FILE"])
            data.write_bytes(b"fresh branch database")
            proof = {"state": "completed", "exit_code": 0,
                     "begin": {"data_file": str(data.resolve()),
                               "source": {"fingerprint": "source-a"}},
                     "data": {"sha256": report.digest(data.read_bytes())}}
            path = self.root / "build" / ".coverage-provenance.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(proof), encoding="utf-8")
        elif "check" in command:
            self.assertTrue(pathlib.Path(env["COVERAGE_RCFILE"]).is_file())
        else:
            self.assertIn("--data-file=" + env["COVERAGE_FILE"], command)
            pathlib.Path(command[command.index("-o") + 1]).write_text(
                json.dumps(raw_data()), encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_collection_uses_ephemeral_config_and_preserves_line_data_and_settings(self):
        line_data = self.root / ".coverage"
        line_data.write_bytes(b"line sentinel")
        original_config = (self.root / ".coveragerc").read_bytes()
        data = self.root / "build" / "branch.coverage"
        captured = {}

        def run(command, **kwargs):
            if command != [str(self.root / "run_tests.sh")]:
                return self.fake_collection(command, **kwargs)
            captured.update(kwargs)
            config = configparser.ConfigParser()
            config.read(kwargs["env"]["COVERAGE_RCFILE"])
            self.assertTrue(config.getboolean("run", "branch"))
            self.assertEqual(config.get("run", "omit"), "tests/*")
            self.assertEqual(config.get("report", "fail_under"), "65")
            self.assertEqual(command, [str(self.root / "run_tests.sh")])
            return self.fake_collection(command, **kwargs)

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

    def test_completed_collection_survives_temporary_config_cleanup(self):
        data = self.root / "branch.coverage"
        output = self.root / "out"
        with mock.patch.object(report.subprocess, "run", side_effect=self.fake_collection):
            rc, text = self.invoke(["--collect", "--data", str(data), "--output-dir", str(output)])
        self.assertEqual(rc, 0, text)
        result = json.loads((output / "summary.json").read_text())
        evidence = result["evidence"]
        self.assertEqual(evidence["status"], "verified-local")
        self.assertEqual(evidence["source"]["fingerprint"], "source-a")
        self.assertEqual(evidence["command"], [str(self.root / "run_tests.sh")])
        self.assertEqual(evidence["raw_sha256"], report.digest((output / "raw.json").read_bytes()))
        with mock.patch.object(report.subprocess, "run") as run:
            raw, archived = report.observed_input(data)
        run.assert_not_called()
        self.assertEqual(raw, raw_data())
        self.assertEqual(archived["status"], "verified-local")

    def test_source_edit_during_collection_invalidates_old_receipt(self):
        data = self.root / "branch.coverage"

        def changed(command, **kwargs):
            result = self.fake_collection(command, **kwargs)
            report.gate_stamp.current_info.return_value = {"fingerprint": "source-b", "files": 5}
            return result

        with mock.patch.object(report.subprocess, "run", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "fingerprint changed"):
                report.collect(data, self.root / "log")
        with self.assertRaisesRegex(ValueError, "not completed"):
            report.observed_input(data)

    def test_export_time_source_or_data_changes_cannot_leave_completed_evidence(self):
        for change in ("source", "data"):
            with self.subTest(change=change):
                report.gate_stamp.current_info.return_value = {"fingerprint": "source-a", "files": 5}
                data = self.root / "branch.coverage"

                def changed(command, **kwargs):
                    result = self.fake_collection(command, **kwargs)
                    if "json" in command:
                        if change == "source":
                            report.gate_stamp.current_info.return_value = {
                                "fingerprint": "source-b", "files": 5}
                        else:
                            data.write_bytes(b"changed during export")
                    return result

                with mock.patch.object(report.subprocess, "run", side_effect=changed):
                    with self.assertRaisesRegex(ValueError, "changed during branch export"):
                        report.collect(data, self.root / "log")
                self.assertEqual(json.loads(report.receipt_path(data).read_text())["state"], "invalid")

    def test_mismatched_provenance_cannot_certify_collection(self):
        data = self.root / "branch.coverage"

        def wrong(command, **kwargs):
            result = self.fake_collection(command, **kwargs)
            if "check" in command:
                path = self.root / "build" / ".coverage-provenance.json"
                proof = json.loads(path.read_text())
                proof["data"]["sha256"] = "wrong"
                path.write_text(json.dumps(proof))
            return result

        with mock.patch.object(report.subprocess, "run", side_effect=wrong):
            with self.assertRaisesRegex(ValueError, "does not match"):
                report.collect(data, self.root / "log")

    def test_failed_provenance_check_cannot_certify_collection(self):
        data = self.root / "branch.coverage"

        def reject(command, **kwargs):
            if "check" in command:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="stale receipt")
            return self.fake_collection(command, **kwargs)

        with mock.patch.object(report.subprocess, "run", side_effect=reject):
            with self.assertRaisesRegex(ValueError, "stale receipt"):
                report.collect(data, self.root / "log")

    def test_changed_database_raw_or_source_is_rejected(self):
        data = self.root / "branch.coverage"
        with mock.patch.object(report.subprocess, "run", side_effect=self.fake_collection):
            report.collect(data, self.root / "log")
        receipt = report.receipt_path(data)
        original = receipt.read_text()
        for key, value in (("source", {"fingerprint": "other"}),
                           ("data_sha256", "other"), ("raw_sha256", "other")):
            with self.subTest(key=key):
                evidence = json.loads(original)
                evidence[key] = value
                receipt.write_text(json.dumps(evidence))
                with self.assertRaisesRegex(ValueError, "mismatch"):
                    report.observed_input(data)
        receipt.write_text(original)
        data.write_bytes(b"replacement database")
        with self.assertRaisesRegex(ValueError, "database mismatch"):
            report.observed_input(data)

    def test_empty_source_and_wrong_receipt_origin_are_rejected(self):
        with mock.patch.object(report.gate_stamp, "current_info", return_value={"files": 0}):
            with self.assertRaisesRegex(ValueError, "Empty source"):
                report.source_info()
        data = self.root / "branch.coverage"
        with mock.patch.object(report.subprocess, "run", side_effect=self.fake_collection):
            report.collect(data, self.root / "log")
        path = report.receipt_path(data)
        proof = json.loads(path.read_text())
        proof["origin"] = "not-a-collection"
        path.write_text(json.dumps(proof))
        with self.assertRaisesRegex(ValueError, "origin/status"):
            report.observed_input(data)

    def test_failed_real_collection_preserves_invalid_receipt(self):
        data = self.root / "branch.coverage"
        with mock.patch.object(report.subprocess, "run", return_value=types.SimpleNamespace(returncode=7)):
            self.assertEqual(report.collect(data, self.root / "log"), 7)
        self.assertEqual(json.loads(report.receipt_path(data).read_text())["state"], "invalid")

    def test_data_without_receipt_is_explicitly_unverified(self):
        with mock.patch.object(report, "read_data", return_value=raw_data()):
            rc, text = self.invoke(["--data", str(self.root / "old.coverage"),
                                    "--output-dir", str(self.root / "out")])
        self.assertEqual(rc, 0, text)
        evidence = json.loads((self.root / "out/summary.json").read_text())["evidence"]
        self.assertEqual(evidence["status"], "unverified")
        self.assertEqual(evidence["origin"], "data-without-receipt")
        self.assertIsNone(evidence["source"])

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
        self.assertEqual(parsed["evidence"]["status"], "unverified")
        self.assertEqual(parsed["evidence"]["origin"], "historical-input-json")
        self.assertIsNone(parsed["evidence"]["source"])
        self.assertIsNone(parsed["evidence"]["command"])
        self.assertEqual(parsed["evidence"]["raw_sha256"],
                         report.digest((output / "raw.json").read_bytes()))
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

    def test_collect_success_reads_the_real_exported_branch_json(self):
        data = self.root / "branch.coverage"
        output = self.root / "out"
        with mock.patch.object(report.subprocess, "run", side_effect=self.fake_collection) as run, \
             mock.patch.object(report, "read_data", wraps=report.read_data) as read:
            rc, text = self.invoke(["--collect", "--data", str(data),
                                    "--output-dir", str(output)])
        self.assertEqual(rc, 0, text)
        # Collection exports once while the temporary config is alive; main
        # then reads the completed receipt instead of exporting a second time.
        read.assert_called_once()
        self.assertEqual(read.call_args.args, (data,))
        self.assertIn("COVERAGE_RCFILE", read.call_args.kwargs["env"])
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[0], [str(self.root / "run_tests.sh")])
        self.assertEqual(commands[1], [str(self.root / ".venv/bin/python3"),
                         str(self.root / "tools/coverage_provenance.py"), "check",
                         "--receipt", str(self.root / "build/.coverage-provenance.json")])
        self.assertIn("json", commands[2])
        self.assertEqual(json.loads((output / "raw.json").read_text()), raw_data())
        self.assertEqual(json.loads((output / "summary.json").read_text())
                         ["evidence"]["status"], "verified-local")
        self.assertIn("not a release gate", text.lower())
        self.assertTrue((output / "report.md").is_file())

    def test_collect_failure_receipt_cannot_be_replaced_by_stale_data(self):
        output = self.root / "out"
        output.mkdir()
        (output / "report.md").write_text("stale", encoding="utf-8")
        with mock.patch.object(report, "collect", return_value=7), \
             mock.patch.object(report, "read_data") as read:
            rc, text = self.invoke(["--collect", "--output-dir", str(output)])
        self.assertEqual(rc, 7)
        read.assert_not_called()
        self.assertEqual((output / "report.md").read_text(), "stale")
        self.assertIn("no branch verdict", text)

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

    def test_read_data_exports_a_real_tiny_coverage_database(self):
        path = self.root / "tiny.coverage"
        script = self.root / "tiny.py"
        script.write_text("value = 1\n", encoding="utf-8")
        proc = report.subprocess.run(
            [sys.executable, "-m", "coverage", "run", "--branch",
             "--data-file=" + str(path), str(script)],
            cwd=self.root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        exported = report.read_data(path)
        self.assertTrue(exported["meta"]["branch_coverage"])
        self.assertTrue(exported["files"])


if __name__ == "__main__":
    unittest.main()
