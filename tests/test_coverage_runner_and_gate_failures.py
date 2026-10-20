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

import bundle_closure_check as closure
import shell_cjk_scan as shell_gate
import test_runner as runner


def invoke(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*args)
    return rc, out.getvalue() + err.getvalue()


class RunnerExecutionContracts(SFCase):
    def run_suite(self, functions, *, rerun=True, random_order=False,
                  snapshots=None, ui_violation=False, modules=None):
        suite = unittest.TestSuite([
            unittest.TestSuite([unittest.FunctionTestCase(fn)]) for fn in functions])
        loader = mock.Mock()
        loader.discover.return_value = suite
        loader.loadTestsFromNames.return_value = suite
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
            args = [] if modules is None else [part for name in modules for part in ("--module", name)]
            rc, out = invoke(runner.main, args)
        guard.assert_called_once_with()
        if modules is None:
            loader.discover.assert_called_once()
            loader.loadTestsFromNames.assert_not_called()
        else:
            loader.discover.assert_not_called()
            loader.loadTestsFromNames.assert_called_once_with(
                list(dict.fromkeys(name.removeprefix("tests.") for name in modules)))
        return rc, out

    def test_selected_modules_use_guarded_runner_without_full_discovery(self):
        visits = []
        rc, out = self.run_suite([lambda: visits.append("ran")],
                                 modules=["tests.test_daily_workflow", "test_daily_workflow"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(visits, ["ran"])
        self.assertIn("定向模块", out)

    def test_selected_module_failure_is_not_hidden_when_retry_disabled(self):
        def failing():
            raise AssertionError("selected failure")

        rc, out = self.run_suite([failing], modules=["test_sample"], rerun=False)
        self.assertEqual(rc, 1, out)
        self.assertIn("selected failure", out)

    def test_selected_modules_preserve_ui_and_user_data_guards(self):
        rc, _ = self.run_suite([lambda: None], modules=["test_sample"], ui_violation=True)
        self.assertEqual(rc, 1)
        rc, _ = self.run_suite([lambda: None], modules=["test_sample"],
                               snapshots=[{"config.json": (1, 1)}, {"config.json": (1, 2)}])
        self.assertEqual(rc, 2)

    def test_empty_suites_cannot_pass_in_full_or_selected_mode(self):
        for modules in (None, ["test_sample"]):
            with self.subTest(modules=modules):
                rc, out = self.run_suite([], modules=modules)
                self.assertEqual(rc, 2)
                self.assertIn("零用例", out)

    def test_invalid_module_or_cli_flags_are_rejected_before_preimport(self):
        for args in (["--module", "os"], ["--module", "../test_x"],
                     ["--module", "test_x.test_case"], ["--unknown"],
                     ["--self-test", "--module", "test_sample"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), \
                    mock.patch.object(runner, "_preimport_project_modules") as preimport:
                with self.assertRaises(SystemExit) as error:
                    runner.main(args)
                self.assertEqual(error.exception.code, 2)
                preimport.assert_not_called()

    def test_focused_paths_exist_before_tests_and_environment_is_restored(self):
        original = dict(os.environ)
        observed = {}

        def inspect_paths(modules):
            self.assertEqual(modules, ["test_sample"])
            for name in ("SF_CONFIG_PATH", "SF_LAST_QUOTE_PATH", "SF_UNIVERSE_PATH",
                         "SF_LOG_PATH", "SF_SINGLE_SOCK"):
                path = pathlib.Path(os.environ[name])
                self.assertTrue(path.parent.is_dir())
                self.assertFalse(path.exists())
                observed[name] = path
            self.assertEqual(os.environ["SF_UNIVERSE_DISABLE"], "1")
            self.assertEqual(os.environ["SF_SEED_FIRST_SCREEN"], "0")
            return 7

        with mock.patch.object(runner, "run_tests", side_effect=inspect_paths):
            rc = runner.main(["--module", "test_sample"])
        self.assertEqual(rc, 7)
        self.assertEqual(dict(os.environ), original)
        self.assertTrue(observed)
        self.assertFalse(next(iter(observed.values())).parent.exists())

    def test_focused_environment_restores_paths_even_on_exception(self):
        original = dict(os.environ)
        with mock.patch.object(runner, "run_tests", side_effect=RuntimeError("injected")), \
                self.assertRaises(RuntimeError):
            runner.main(["--module", "test_sample"])
        self.assertEqual(dict(os.environ), original)

    def test_focused_environment_removes_previously_absent_variables(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with runner.focused_environment():
                self.assertIn("SF_CONFIG_PATH", os.environ)
                self.assertIn("SF_UNIVERSE_DISABLE", os.environ)
            self.assertEqual(dict(os.environ), {})

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
