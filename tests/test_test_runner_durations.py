"""运行器的“用例耗时”输出：只观测，不参与任何判定。

耗时数字一律由注入的时钟构造，不靠真睡眠再断言；写盘失败的两个分支用确定性的
文件系统形状触发（父路径是普通文件 / 目标是目录），不依赖权限位。
"""
from __future__ import annotations

import io
import json
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

import coverage
import test_runner as runner


class _Clock:
    def __init__(self):
        self.reading = 100.0

    def __call__(self):
        return self.reading


def _ledger_with(spans):
    """spans: [(case_id, 用时秒)]，由假时钟逐条走完。"""
    clock = _Clock()
    ledger = runner.DurationLedger(clock=clock)
    for case_id, seconds in spans:
        ledger.start(case_id)
        clock.reading += seconds
        ledger.stop(case_id)
    return ledger


class ModuleAttribution(SFCase):
    def test_both_id_shapes_land_on_the_same_module(self):
        self.assertEqual(runner.module_of("test_x.Case.test_y"), "test_x")
        self.assertEqual(runner.module_of("tests.test_x.Case.test_y"), "test_x")

    def test_function_style_and_empty_ids_do_not_crash(self):
        self.assertEqual(runner.module_of("some_function"), "some_function")
        self.assertEqual(runner.module_of(""), "<未知>")
        self.assertEqual(runner.module_of("tests"), "tests")


class LedgerAccounting(SFCase):
    def test_time_accumulates_per_module_and_counts_cases(self):
        ledger = _ledger_with([("test_a.C.t1", 2.0), ("test_a.C.t2", 3.0), ("test_b.C.t1", 1.5)])
        self.assertEqual(ledger.seconds, {"test_a": 5.0, "test_b": 1.5})
        self.assertEqual(ledger.cases, {"test_a": 2, "test_b": 1})
        self.assertEqual(ledger.total(), 6.5)

    def test_ranking_is_slowest_first_with_name_as_tiebreak(self):
        ledger = _ledger_with([("test_b.C.t", 2.0), ("test_a.C.t", 2.0), ("test_c.C.t", 9.0)])
        self.assertEqual([name for name, _ in ledger.ranked()], ["test_c", "test_a", "test_b"])

    def test_stop_without_start_is_ignored(self):
        ledger = runner.DurationLedger(clock=_Clock())
        ledger.stop("test_a.C.t")
        self.assertEqual(ledger.total(), 0.0)
        self.assertEqual(ledger.ranked(), [])

    def test_a_clock_that_goes_backwards_never_yields_negative_time(self):
        clock = _Clock()
        ledger = runner.DurationLedger(clock=clock)
        ledger.start("test_a.C.t")
        clock.reading -= 5.0
        ledger.stop("test_a.C.t")
        self.assertEqual(ledger.seconds["test_a"], 0.0)


class Rendering(SFCase):
    def test_summary_reconciles_case_time_with_wall_clock(self):
        ledger = _ledger_with([("test_a.C.t", 6.0), ("test_b.C.t", 2.0)])
        text = "\n".join(runner.render_durations(ledger, 10.0, False))
        self.assertIn("用例体合计 8.0s", text)
        self.assertIn("主轮墙钟 10.0s", text)
        self.assertIn("差额 2.0s", text)
        self.assertIn("未采集覆盖率", text)
        self.assertIn("75.0%", text)
        self.assertIn("25.0%", text)

    def test_coverage_label_warns_that_numbers_are_inflated(self):
        ledger = _ledger_with([("test_a.C.t", 1.0)])
        text = "\n".join(runner.render_durations(ledger, 1.0, True))
        self.assertIn("覆盖率采集下", text)

    def test_only_the_top_modules_are_listed_and_the_rest_is_summed(self):
        ledger = _ledger_with([("test_a.C.t", 5.0), ("test_b.C.t", 3.0), ("test_c.C.t", 1.0)])
        lines = runner.render_durations(ledger, 9.0, False, top=2)
        text = "\n".join(lines)
        self.assertIn("test_a", text)
        self.assertIn("test_b", text)
        self.assertNotIn("test_c ", text)
        self.assertIn("其余 1 个模块合计 1.0s", text)

    def test_an_empty_ledger_renders_without_dividing_by_zero(self):
        lines = runner.render_durations(runner.DurationLedger(clock=_Clock()), 0.0, False)
        self.assertEqual(len(lines), 2)


class ReportAndWrite(SFCase):
    def test_report_carries_the_same_numbers_as_the_summary(self):
        ledger = _ledger_with([("test_a.C.t1", 4.0), ("test_a.C.t2", 1.0), ("test_b.C.t", 2.0)])
        report = runner.durations_report(ledger, 9.0, True)
        self.assertEqual(report["schema"], runner.DURATIONS_SCHEMA)
        self.assertTrue(report["under_coverage"])
        self.assertEqual(report["wall_seconds"], 9.0)
        self.assertEqual(report["case_seconds"], 7.0)
        self.assertEqual(report["modules"], [{"module": "test_a", "seconds": 5.0, "cases": 2},
                                             {"module": "test_b", "seconds": 2.0, "cases": 1}])

    def test_write_creates_missing_directories_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "nested" / "durations.json"
            self.assertEqual(runner.write_durations(str(target), {"schema": 1, "modules": []}), "")
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["schema"], 1)
            self.assertEqual([p.name for p in target.parent.iterdir()], ["durations.json"])

    def test_write_replaces_an_existing_report_whole(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "durations.json"
            target.write_text("old", encoding="utf-8")
            self.assertEqual(runner.write_durations(str(target), {"schema": 2}), "")
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"schema": 2})

    def test_a_parent_that_is_a_file_reports_the_reason_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = pathlib.Path(directory) / "blocker"
            blocker.write_text("not a directory", encoding="utf-8")
            problem = runner.write_durations(str(blocker / "durations.json"), {"schema": 1})
            self.assertNotEqual(problem, "")
            self.assertEqual([p.name for p in pathlib.Path(directory).iterdir()], ["blocker"])

    def test_a_target_that_is_a_directory_cleans_up_its_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "durations.json"
            target.mkdir()
            problem = runner.write_durations(str(target), {"schema": 1})
            self.assertNotEqual(problem, "")
            self.assertEqual([p.name for p in pathlib.Path(directory).iterdir()], ["durations.json"])


class CoverageDetection(SFCase):
    def test_missing_coverage_package_means_not_active(self):
        with mock.patch.dict(sys.modules, {"coverage": None}):
            self.assertFalse(runner.coverage_active())

    def test_active_follows_whether_a_collector_is_running(self):
        with mock.patch.object(coverage.Coverage, "current", return_value=None):
            self.assertFalse(runner.coverage_active())
        with mock.patch.object(coverage.Coverage, "current", return_value=object()):
            self.assertTrue(runner.coverage_active())


class WiredIntoTheRunner(SFCase):
    def run_main(self, functions, durations_out=None):
        suite = unittest.TestSuite([
            unittest.TestSuite([unittest.FunctionTestCase(fn)]) for fn in functions])
        loader = mock.Mock()
        loader.discover.return_value = suite
        env = {"SF_TEST_TIMEOUT": "0", "SF_RANDOM_ORDER": "0", "SF_RERUN_FAILED": "0"}
        if durations_out is None:
            env["SF_TEST_DURATIONS_OUT"] = ""
        else:
            env["SF_TEST_DURATIONS_OUT"] = durations_out
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(sys, "path", list(sys.path)), \
             mock.patch.object(runner, "install_ui_dialog_guard"), \
             mock.patch.object(runner, "_preimport_project_modules"), \
             mock.patch.object(runner.unittest, "TestLoader", return_value=loader), \
             mock.patch.object(runner, "_snapshot_user_data", side_effect=[{}, {}]), \
             mock.patch.object(runner, "_report_ui_dialog_violations", return_value=False), \
             redirect_stdout(out), redirect_stderr(err):
            rc = runner.main([])
        return rc, out.getvalue() + err.getvalue()

    def test_a_green_run_prints_the_duration_summary_without_changing_the_verdict(self):
        rc, out = self.run_main([lambda: None])
        self.assertEqual(rc, 0, out)
        self.assertIn("== 用例耗时", out)
        self.assertIn("主轮墙钟", out)
        self.assertNotIn("耗时明细", out)

    def test_a_failing_run_still_fails_and_still_prints_the_summary(self):
        def failing():
            raise AssertionError("boom")

        rc, out = self.run_main([failing])
        self.assertEqual(rc, 1, out)
        self.assertIn("== 用例耗时", out)

    def test_the_report_is_written_when_a_path_is_given(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "build" / "test-durations.json"
            rc, out = self.run_main([lambda: None, lambda: None], durations_out=str(target))
            self.assertEqual(rc, 0, out)
            self.assertIn("耗时明细已写入", out)
            report = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(sum(item["cases"] for item in report["modules"]), 2)
            self.assertEqual(report["under_coverage"], runner.coverage_active())

    def test_an_unwritable_report_path_is_reported_but_does_not_change_the_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = pathlib.Path(directory) / "blocker"
            blocker.write_text("not a directory", encoding="utf-8")
            rc, out = self.run_main([lambda: None], durations_out=str(blocker / "durations.json"))
            self.assertEqual(rc, 0, out)
            self.assertIn("耗时明细没写成", out)

    def test_retry_attempts_are_not_counted_in_the_main_round(self):
        calls = []

        def flaky():
            calls.append("ran")
            if len(calls) == 1:
                raise AssertionError("first time only")

        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "durations.json"
            suite = unittest.TestSuite([unittest.TestSuite([unittest.FunctionTestCase(flaky)])])
            loader = mock.Mock()
            loader.discover.return_value = suite
            env = {"SF_TEST_TIMEOUT": "0", "SF_RANDOM_ORDER": "0", "SF_RERUN_FAILED": "1",
                   "SF_TEST_DURATIONS_OUT": str(target)}
            out = io.StringIO()
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(sys, "path", list(sys.path)), \
                 mock.patch.object(runner, "install_ui_dialog_guard"), \
                 mock.patch.object(runner, "_preimport_project_modules"), \
                 mock.patch.object(runner.unittest, "TestLoader", return_value=loader), \
                 mock.patch.object(runner, "_snapshot_user_data", side_effect=[{}, {}]), \
                 mock.patch.object(runner, "_report_ui_dialog_violations", return_value=False), \
                 redirect_stdout(out), redirect_stderr(io.StringIO()):
                rc = runner.main([])
            self.assertEqual(rc, 0, out.getvalue())
            self.assertEqual(len(calls), 2)
            report = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(sum(item["cases"] for item in report["modules"]), 1)


if __name__ == "__main__":
    unittest.main()
