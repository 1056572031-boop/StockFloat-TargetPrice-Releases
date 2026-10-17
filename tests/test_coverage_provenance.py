"""Real local databases; no network, GUI, user config or git-hook leakage."""
import io
import json
import os
import runpy
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import coverage

from tests._base import SFCase
from tools import coverage_provenance as p
from tools import gate_stamp


class ProvenanceCase(SFCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="sf_provenance_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        old = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old)
        # Remove only coverage-related inherited knobs in this isolated fixture;
        # production code never rewrites the caller's environment.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("COVERAGE_", "GIT_"))}
        env.update(GIT_DIR="/nonexistent/hook/git", GIT_INDEX_FILE="/nonexistent/index")
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        subprocess.run(["git", "init", "-q", str(self.root)],
                       env=gate_stamp.git_env(), check=True, capture_output=True)
        self.source = self.root / "tiny.py"
        self.source.write_text("x = 1\nif x:\n    x += 1\n", encoding="utf-8")
        self.config = self.root / "run.ini"
        self.config.write_text("[run]\nbranch = true\ncontext = tiny-test\n", encoding="utf-8")
        os.environ["COVERAGE_RCFILE"] = str(self.config)
        os.environ["COVERAGE_FILE"] = str(self.root / "custom.db")
        self.receipt = str(self.root / "build" / "receipt.json")

    def measure(self):
        cov = coverage.Coverage()
        cov.start()
        try:
            runpy.run_path(str(self.source))
        finally:
            cov.stop()
        cov.save()

    def completed(self):
        p.begin(self.receipt)
        self.measure()
        return p.finish(self.receipt, 0)

    def rejected(self, fn, *args):
        with self.assertRaises((ValueError, OSError, KeyError,
                                coverage.exceptions.CoverageException)):
            fn(*args)

    def test_real_branch_context_and_untracked_source(self):
        obj = self.completed()
        self.assertEqual(p.check(self.receipt), obj)
        self.assertTrue(obj["summary"]["has_arcs"])
        self.assertIn("tiny-test", obj["summary"]["contexts"])
        self.assertIn(str(self.source), obj["summary"]["measured_files"])
        self.assertEqual(obj["begin"]["data_file"], str(self.root / "custom.db"))
        self.assertEqual(obj["begin"]["source"], gate_stamp.current_info(str(self.root)))

    def test_no_data_and_stale_data(self):
        p.begin(self.receipt)
        self.rejected(p.finish, self.receipt, 0)
        self.measure()
        p.begin(self.receipt)
        self.rejected(p.finish, self.receipt, 0)

    def test_empty_and_invalid_databases(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                p.begin(self.receipt)
                db = Path(os.environ["COVERAGE_FILE"])
                if invalid:
                    db.write_bytes(b"not a coverage sqlite database")
                else:
                    db.unlink(missing_ok=True)
                    data = coverage.CoverageData(basename=str(db))
                    data.add_lines({str(self.source): []})
                    data.write()
                self.rejected(p.finish, self.receipt, 0)

    def test_failed_exit_and_incomplete_receipts(self):
        self.rejected(p.check, self.receipt)
        p.begin(self.receipt)
        self.rejected(p.check, self.receipt)
        self.measure()
        self.rejected(p.finish, self.receipt, 3)
        self.rejected(p.check, self.receipt)
        self.completed()
        self.rejected(p.finish, self.receipt, 0)
        self.rejected(p.check, self.receipt)

    def test_begin_clears_old_completion_even_on_failure(self):
        self.completed()
        with patch.object(p, "snapshot", side_effect=ValueError("bad config")):
            self.rejected(p.begin, self.receipt)
        self.rejected(p.check, self.receipt)

    def test_changes_at_both_observation_boundaries(self):
        changes = {
            "source": lambda: self.source.write_text("x = 123\n"),
            "mode": lambda: self.source.chmod(0o755),
            "untracked": lambda: (self.root / "new.py").write_text("y = 2\n"),
            "config_same_name": lambda: self.config.write_text(
                "[run]\nbranch = true\ncontext = tiny-test\n# replaced\n"),
            "data_path": lambda: os.environ.update(COVERAGE_FILE=str(self.root / "other.db")),
            "runtime": lambda: setattr(coverage, "__version__", "changed"),
        }
        for boundary in ("finish", "check"):
            for label, change in changes.items():
                with self.subTest(boundary=boundary, change=label):
                    original_version = coverage.__version__
                    original_data = os.environ["COVERAGE_FILE"]
                    original_source = self.source.read_bytes()
                    original_mode = self.source.stat().st_mode
                    original_config = self.config.read_bytes()
                    try:
                        p.begin(self.receipt)
                        self.measure()
                        if boundary == "check":
                            p.finish(self.receipt, 0)
                        change()
                        if boundary == "finish":
                            self.rejected(p.finish, self.receipt, 0)
                        else:
                            self.rejected(p.check, self.receipt)
                    finally:
                        coverage.__version__ = original_version
                        os.environ["COVERAGE_FILE"] = original_data
                        self.source.write_bytes(original_source)
                        self.source.chmod(original_mode)
                        self.config.write_bytes(original_config)
                        (self.root / "new.py").unlink(missing_ok=True)

    def test_database_tampering_and_missing(self):
        self.completed()
        db = Path(os.environ["COVERAGE_FILE"])
        st = db.stat()
        # Same path/length/mtime, changed content must still fail.
        raw = bytearray(db.read_bytes())
        raw[-1] ^= 1
        db.write_bytes(raw)
        os.utime(db, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.rejected(p.check, self.receipt)
        db.unlink()
        self.rejected(p.check, self.receipt)

    def test_old_mtime_even_when_database_changed(self):
        obj = p.begin(self.receipt)
        self.measure()
        old = obj["started_ns"] - 1_000_000_000
        os.utime(os.environ["COVERAGE_FILE"], ns=(old, old))
        self.rejected(p.finish, self.receipt, 0)

    def test_branch_cli_override_is_not_silently_accepted(self):
        p.begin(self.receipt)
        cov = coverage.Coverage(branch=False)
        cov.start()
        runpy.run_path(str(self.source))
        cov.stop()
        cov.save()
        self.rejected(p.finish, self.receipt, 0)

    def test_empty_repo_and_non_repo_rejected(self):
        self.source.unlink()
        self.rejected(p.begin, self.receipt)
        outside = self.root / "outside"
        outside.mkdir()
        with patch.object(gate_stamp, "resolve_root", return_value=str(outside)):
            self.rejected(p.begin, str(outside / "build" / "receipt.json"))

    def test_restored_source_is_explicitly_outside_boundary(self):
        self.completed()
        original = self.source.read_bytes()
        self.source.write_text("changed temporarily\n")
        self.source.write_bytes(original)
        # Endpoint evidence is intentionally not a filesystem history monitor.
        self.assertEqual(p.check(self.receipt)["state"], "completed")

    def test_receipt_location_and_corruption(self):
        self.rejected(p.begin, str(self.root / "receipt.json"))
        self.rejected(p.begin, str(self.root / "build" / "receipt.py"))
        self.completed()
        Path(self.receipt).write_text("{broken")
        self.rejected(p.check, self.receipt)
        Path(self.receipt).write_text(json.dumps({"schema": 999}))
        self.rejected(p.check, self.receipt)

    def test_cli_and_unknown_args(self):
        self.assertEqual(p.main(["begin", "--receipt", self.receipt]), 0)
        self.measure()
        self.assertEqual(p.main(["finish", "--receipt", self.receipt, "--exit-code", "0"]), 0)
        self.assertEqual(p.main(["check", "--receipt", self.receipt]), 0)
        with redirect_stderr(io.StringIO()):
            for args in (["--bad"], ["check", "--bad"], ["--self-test", "--bad"],
                         ["finish"], [], ["--self-test", "check"]):
                with self.subTest(args=args), self.assertRaises(SystemExit) as caught:
                    p.main(args)
                self.assertEqual(caught.exception.code, 2)
            self.source.write_text("x = 9\n")
            self.assertEqual(p.main(["check", "--receipt", self.receipt]), 1)

    def test_mutation_positive_control(self):
        self.completed()
        self.source.write_text("x = 456\n")
        self.rejected(p.check, self.receipt)
        # Removing the provenance comparison really lets the seeded error pass:
        # a positive control that the rejection fixture reaches this guard.
        with patch.object(p, "same_run", return_value=None):
            self.assertEqual(p.check(self.receipt)["state"], "completed")

    def test_receipt_cannot_be_a_fingerprint_input(self):
        # A tracked JSON name from the gate's explicit input list would make
        # writing the receipt itself invalidate every observation.
        with (
            patch.object(gate_stamp, "build_input_paths", return_value=["build/receipt.json"]),
            self.assertRaisesRegex(p.EvidenceError, "fingerprint input"),
        ):
            p.receipt_path(self.receipt)

    def test_load_rejects_non_object_and_wrong_schema(self):
        path = Path(self.receipt)
        path.parent.mkdir()
        for obj in ([], None, {"schema": 2}, {"state": "completed"}):
            with self.subTest(receipt=obj):
                path.write_text(json.dumps(obj), encoding="utf-8")
                with self.assertRaisesRegex(p.EvidenceError, "invalid receipt schema"):
                    p.load(path)

    def test_atomic_replace_failure_preserves_receipt_and_cleans_temp(self):
        path = Path(self.receipt)
        p.atomic_write(path, {"state": "original"})
        with (
            patch.object(p.os, "replace", side_effect=PermissionError("read-only destination")),
            self.assertRaisesRegex(PermissionError, "read-only destination"),
        ):
            p.atomic_write(path, {"state": "replacement"})
        self.assertEqual(json.loads(path.read_text()), {"state": "original"})
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_atomic_creation_failure_leaves_no_receipt(self):
        path = Path(self.receipt)
        with (
            patch.object(p.tempfile, "NamedTemporaryFile",
                         side_effect=OSError("no space left on device")),
            self.assertRaisesRegex(OSError, "no space left"),
        ):
            p.atomic_write(path, {"state": "begun"})
        self.assertEqual(list(path.parent.iterdir()), [])

    def test_snapshot_rejects_bad_configuration_and_non_string_data_file(self):
        self.config.write_text("[run]\nbranch = not-a-boolean\n", encoding="utf-8")
        with self.assertRaises(coverage.exceptions.ConfigError):
            p.snapshot()
        self.config.write_text("[run]\nbranch = true\n", encoding="utf-8")
        # A plugin/programmatic option can supply a non-string, unlike an INI
        # file. Preserve all other real configuration and runtime observations.
        get_option = coverage.Coverage.get_option

        def option(cov, name):
            return None if name == "run:data_file" else get_option(cov, name)

        with (
            patch.object(coverage.Coverage, "get_option", option),
            self.assertRaisesRegex(p.EvidenceError, "data_file must be a path string"),
        ):
            p.snapshot()

    def test_finish_rejects_database_changed_during_summary_read(self):
        p.begin(self.receipt)
        self.measure()
        summarize = p.data_summary

        def concurrent_write(path):
            summary = summarize(path)
            # A second writer appending to the database during observation is
            # enough to invalidate the identity, even if measured lines match.
            with path.open("ab") as stream:
                stream.write(b"concurrent writer")
            return summary

        with (
            patch.object(p, "data_summary", side_effect=concurrent_write),
            self.assertRaisesRegex(p.EvidenceError, "changed while reading"),
        ):
            p.finish(self.receipt, 0)
        self.assertEqual(p.load(Path(self.receipt))["state"], "failed")
        with self.assertRaisesRegex(p.EvidenceError, "not completed"):
            p.check(self.receipt)

    def test_check_rejects_summary_mismatch_with_unchanged_database(self):
        obj = self.completed()
        obj["summary"]["contexts"] = ["different-run"]
        p.atomic_write(Path(self.receipt), obj)
        with self.assertRaisesRegex(p.EvidenceError, "files/contexts/arcs changed"):
            p.check(self.receipt)

    def test_check_rejects_completed_receipt_with_nonzero_exit(self):
        obj = self.completed()
        obj["exit_code"] = 1
        p.atomic_write(Path(self.receipt), obj)
        with self.assertRaisesRegex(p.EvidenceError, "not completed successfully"):
            p.check(self.receipt)

    def test_source_file_count_is_part_of_provenance(self):
        before = p.snapshot()
        now = {**before, "source": {**before["source"], "files": before["source"]["files"] + 1}}
        with self.assertRaisesRegex(p.EvidenceError, "source fingerprint"):
            p.same_run(before, now)

    def test_cli_reports_bad_config_schema_and_database(self):
        with redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()):
            self.config.write_text("[run]\nbranch = invalid\n", encoding="utf-8")
            self.assertEqual(p.main(["begin", "--receipt", self.receipt]), 1)
            self.assertIn("coverage provenance rejected", errors.getvalue())
            self.config.write_text("[run]\nbranch = true\n", encoding="utf-8")
            Path(self.receipt).write_text('{"schema": 99}', encoding="utf-8")
            self.assertEqual(p.main(["check", "--receipt", self.receipt]), 1)
            self.assertIn("invalid receipt schema", errors.getvalue())
            self.assertEqual(p.main(["begin", "--receipt", self.receipt]), 0)
            Path(os.environ["COVERAGE_FILE"]).write_bytes(b"not sqlite")
            self.assertEqual(p.main(["finish", "--receipt", self.receipt,
                                     "--exit-code", "0"]), 1)
            self.assertIn("file is not a database", errors.getvalue())

    def test_cli_finish_requires_explicit_exit_code(self):
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as caught:
            p.main(["finish", "--receipt", self.receipt])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--exit-code", errors.getvalue())
        self.assertFalse(Path(self.receipt).exists())

    def test_selftest_requires_successful_nonempty_suite(self):
        # Do not recursively launch this suite: the runner's public result is
        # the boundary under test, including unittest's successful empty suite.
        for successful, count, expected in ((True, 1, 0), (False, 1, 1), (True, 0, 1)):
            with self.subTest(successful=successful, count=count):
                result = unittest.TestResult()
                result.testsRun = count
                if not successful:
                    result.failures.append((self, "fixture assertion failed"))
                suite = unittest.TestSuite()
                with (
                    patch.object(unittest.defaultTestLoader, "loadTestsFromName",
                                 return_value=suite) as loader,
                    patch.object(unittest.TextTestRunner, "run", return_value=result) as runner,
                ):
                    self.assertEqual(p.self_test(), expected)
                loader.assert_called_once_with("tests.test_coverage_provenance")
                runner.assert_called_once_with(suite)

    def test_cli_selftest_propagates_result_without_running_suite(self):
        for result in (0, 1):
            with self.subTest(result=result), patch.object(p, "self_test", return_value=result) as probe:
                self.assertEqual(p.main(["--self-test"]), result)
                probe.assert_called_once_with()

    def test_direct_script_entry_executes_requested_action(self):
        with (
            patch("sys.argv", [str(Path(p.__file__)), "begin", "--receipt", self.receipt]),
            redirect_stdout(io.StringIO()) as output,
            self.assertRaises(SystemExit) as caught,
        ):
            runpy.run_path(str(Path(p.__file__)), run_name="__main__")
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("begin OK", output.getvalue())
        self.assertEqual(p.load(Path(self.receipt))["state"], "begun")


if __name__ == "__main__":
    unittest.main()
