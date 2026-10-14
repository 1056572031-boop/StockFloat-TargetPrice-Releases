"""Execute gate verdict paths using tiny deterministic suites, never discovery.

The runner must preserve failure/rerun semantics and prioritize user-data
pollution. Mutant gate tests also prove that self-tests actually reject broken
collectors, instead of only exercising their normal successful result.
"""
from __future__ import annotations

import io
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

try:
    from _base import SFCase
except ImportError:  # pragma: no cover
    from tests._base import SFCase

import test_runner as runner
import shell_cjk_scan as shell_gate
import bundle_closure_check as closure


def invoke(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*args)
    return rc, out.getvalue() + err.getvalue()


class RunnerExecutionContracts(SFCase):
    def run_suite(self, functions, *, rerun=True, random_order=False,
                  snapshots=None, ui_violation=False):
        suite = unittest.TestSuite([
            unittest.TestSuite([unittest.FunctionTestCase(fn)]) for fn in functions])
        loader = mock.Mock()
        loader.discover.return_value = suite
        env = {"SF_TEST_TIMEOUT": "0", "SF_RANDOM_ORDER": "1" if random_order else "0",
               "SF_RANDOM_SEED": "123456", "SF_RERUN_FAILED": "1" if rerun else "0"}
        snapshots = snapshots or [{}, {}]
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(sys, "path", list(sys.path)), \
             mock.patch.object(runner, "install_ui_dialog_guard") as guard, \
             mock.patch.object(runner, "_preimport_project_modules"), \
             mock.patch.object(runner.unittest, "TestLoader", return_value=loader), \
             mock.patch.object(runner, "_snapshot_user_data", side_effect=snapshots), \
             mock.patch.object(runner, "_report_ui_dialog_violations", return_value=ui_violation):
            rc, out = invoke(runner.main, [])
        guard.assert_called_once_with()
        loader.discover.assert_called_once()
        return rc, out

    def test_clean_suite_is_green(self):
        visits = []
        rc, out = self.run_suite([lambda: visits.append("ran")])
        self.assertEqual(rc, 0, out)
        self.assertEqual(visits, ["ran"])
        self.assertIn("flaky 0", out)

    def test_persistent_failure_is_retried_once_and_stays_red(self):
        visits = []

        def failing():
            visits.append("ran")
            raise AssertionError("persistent failure")

        rc, out = self.run_suite([failing])
        self.assertEqual(rc, 1, out)
        self.assertEqual(len(visits), 2)
        self.assertIn("persistent failure", out)
        self.assertIn("flaky 0", out)

    def test_error_is_retried_once_and_remains_red(self):
        visits = []

        def error():
            visits.append("ran")
            raise RuntimeError("persistent error")

        rc, out = self.run_suite([error])
        self.assertEqual(rc, 1, out)
        self.assertEqual(len(visits), 2)

    def test_failed_first_attempt_successful_retry_is_explicitly_flaky(self):
        visits = []

        def intermittent():
            visits.append("ran")
            if len(visits) == 1:
                raise AssertionError("first attempt")

        rc, out = self.run_suite([intermittent])
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(visits), 2)
        self.assertIn("[flaky]", out)
        self.assertIn("flaky 1", out)

    def test_disabled_rerun_does_not_hide_failure(self):
        visits = []

        def failing():
            visits.append("ran")
            raise AssertionError("failure")

        rc, out = self.run_suite([failing], rerun=False)
        self.assertEqual(rc, 1, out)
        self.assertEqual(len(visits), 1)
        self.assertNotIn("[flaky]", out)

    def test_random_order_preserves_module_suites_and_disables_retry(self):
        visits = []

        def failing():
            visits.append("fail")
            raise AssertionError("ordered failure")

        rc, out = self.run_suite([failing, lambda: visits.append("good")], random_order=True)
        self.assertEqual(rc, 1, out)
        self.assertCountEqual(visits, ["fail", "good"])
        self.assertIn("seed=123456", out)
        self.assertNotIn("[flaky]", out)

    def test_user_data_pollution_wins_even_when_suite_fails(self):
        def failing():
            raise AssertionError("test red")

        rc, out = self.run_suite([failing], rerun=False,
                                snapshots=[{"config.json": (10, 100)},
                                           {"config.json": (10, 101)}])
        self.assertEqual(rc, 2, out)
        self.assertIn("config.json", out)
        self.assertIn("mtime", out)

    def test_ui_violation_is_red_even_when_tests_pass(self):
        rc, out = self.run_suite([lambda: None], ui_violation=True)
        self.assertEqual(rc, 1, out)

    def test_self_test_dispatch_never_discovers_cases(self):
        with mock.patch.object(runner, "self_test", return_value=7), \
             mock.patch.object(runner.unittest, "TestLoader") as loader:
            rc, _out = invoke(runner.main, ["--self-test"])
        self.assertEqual(rc, 7)
        loader.assert_not_called()


class GateSelfTestsHaveTeeth(SFCase):
    def test_shell_self_test_passes_without_repo_mutation(self):
        rc, out = invoke(shell_gate.self_test)
        self.assertEqual(rc, 0, out)
        self.assertIn("0 项失败", out)

    def test_shell_self_test_rejects_always_clean_token_collector(self):
        with mock.patch.object(shell_gate, "find_hits", return_value=[]):
            rc, out = invoke(shell_gate.self_test)
        self.assertEqual(rc, 1, out)
        self.assertIn("期望", out)

    def test_shell_self_test_rejects_scope_and_clean_file_false_positives(self):
        with mock.patch.object(shell_gate, "scan", return_value=[("bad.sh", 1, "$V", "line")]):
            rc, out = invoke(shell_gate.self_test)
        self.assertEqual(rc, 1, out)
        self.assertIn("空目录", out)
        self.assertIn("花括号", out)

    def test_bundle_self_test_rejects_empty_import_collection(self):
        with mock.patch.object(closure, "collect_imports", return_value=(set(), "not-a-list")):
            rc, out = invoke(closure.self_test)
        self.assertEqual(rc, 1, out)
        self.assertIn("收集", out)
        self.assertIn("列表", out)

    def test_bundle_self_test_rejects_broken_missing_and_allow_list_judgement(self):
        with mock.patch.object(closure, "judge_missing",
                               side_effect=[([], ["json"]), (["nacl"], ["nacl"])]):
            rc, out = invoke(closure.self_test)
        self.assertEqual(rc, 1, out)
        self.assertIn("_cffi_backend", out)
        self.assertIn("ALLOW_MISSING", out)

    def test_bundle_optional_layer_reports_missing_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out = invoke(closure.self_test, pathlib.Path(td) / "Missing.app")
        self.assertEqual(rc, 0, out)
        self.assertIn("本轮没有验", out)

    def test_bundle_optional_layer_rejects_incomplete_or_fabricated_archive(self):
        with tempfile.TemporaryDirectory() as td:
            app = pathlib.Path(td) / "Fake.app"
            app.mkdir()
            with mock.patch.object(closure, "bundle_available",
                                   return_value={"definitely_not_packaged_xyz"}), \
                 mock.patch.object(closure, "check", return_value={"checked": 1, "missing": ["lost"]}):
                rc, out = invoke(closure.self_test, app)
        self.assertEqual(rc, 1, out)
        self.assertIn("包内确实存在", out)
        self.assertIn("lost", out)
        self.assertIn("凭空", out)


if __name__ == "__main__":
    unittest.main()
