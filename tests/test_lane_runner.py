"""In-process contracts for the lane coordinator (tools/lane_runner.py) and its test_runner hooks.

During a lane run the coordinator itself is a subprocess, so coverage never sees its lines there.
Every branch is therefore driven here: tiny real child processes stand in for lanes (killed,
stubborn, timed out, signalled), and the main flow runs with discovery and lane execution faked.
"""
from __future__ import annotations

import gc
import io
import json
import os
import pathlib
import runpy
import signal
import subprocess
import sys
import tempfile
import threading
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
import lane_runner as lr
import test_runner as runner

Result = runner._Result  # discipline-ok(R5): the per-case result hook is the behavior under test

CHILD = r'''
import json, os, signal, sys, time
mode, result, durations, rc = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
elif mode == "sleep":
    time.sleep(60)
elif mode == "die":
    sys.exit(rc)
else:
    if mode == "slow":
        time.sleep(0.4)
    json.dump({"schema": 1, "rc": rc, "total": 3, "gc": os.environ.get("SF_GC_AT_TEST_BOUNDARY"),
               "extra": os.environ.get("SF_LANE_EXTRA"), "failures": [], "flaky": [],
               "ui_violations": 0, "dirty": []}, open(result, "w"))
    json.dump({"modules": [{"module": "m", "seconds": 1.5, "cases": 2}]}, open(durations, "w"))
    sys.exit(rc)
'''

def invoke(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*args)
    return rc, out.getvalue() + err.getvalue()


class LaneCase(SFCase):
    """Every lane file lives in a temp directory; the loop timings are shrunk so the paths run fast."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sf_lane_test_")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.track_patch(mock.patch.object(lr, "LANES_DIR", self.tmp))
        self.track_patch(mock.patch.object(lr, "POLL_SEC", 0.05))
        self.track_patch(mock.patch.object(lr, "PROGRESS_SEC", 3600.0))
        self.track_patch(mock.patch.object(lr, "TERM_GRACE_SEC", 0.6))

    def fake_command(self, modes):
        """lane_command stand-in: lane i runs the child script in modes[i] = (mode, rc)."""
        script = self.tmp / "child.py"
        script.write_text(CHILD, encoding="utf-8")

        def command(python, index, modules, coverage_on):
            mode, rc = modes[index]
            files = lr.lane_files(index)
            return [python, str(script), mode, str(files["result"]), str(files["durations"]), str(rc)]

        return mock.patch.object(lr, "lane_command", command)


class TestRunLanes(LaneCase):
    def test_lanes_finish_with_their_own_exit_codes_and_environment(self):
        with self.fake_command([("ok", 0), ("slow", 1)]):
            lanes, wall, timed_out = lr.run_lanes(sys.executable, [["a"], ["b"]], False, 30.0,
                                                  {"SF_LANE_EXTRA": "x"})
        self.assertEqual([lanes[0]["rc"], lanes[1]["rc"]], [0, 1])
        self.assertFalse(timed_out)
        self.assertGreater(wall, 0.3)
        first = json.loads(lr.lane_files(0)["result"].read_text(encoding="utf-8"))
        self.assertEqual((first["gc"], first["extra"]), ("1", "x"), "lane children must get the GC switch and the extras")
        self.assertGreaterEqual(lanes[1]["seconds"], lanes[0]["seconds"])

    def test_a_lane_that_fails_to_start_does_not_leave_the_earlier_lanes_running(self):
        """后面的车道起不来（句柄耗尽之类）时，前面已起好的车道必须被回收，不能变孤儿继续跑。"""
        started = []
        real_popen = subprocess.Popen

        def flaky_popen(cmd, **kw):
            if started:
                raise OSError(24, "Too many open files (simulated)")
            proc = real_popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              stdout=kw.get("stdout"), stderr=kw.get("stderr"))
            started.append(proc)
            return proc

        with mock.patch.object(lr.subprocess, "Popen", flaky_popen):
            with self.assertRaises(OSError):
                lr.run_lanes(sys.executable, [["a"], ["b"]], False, 30.0, {})
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll(), "第一条车道在后面的车道起不来之后还活着")

    def test_progress_text_names_every_lane(self):
        with self.fake_command([("slow", 0), ("slow", 0)]), mock.patch.object(lr, "PROGRESS_SEC", 0.0):
            rc, out = invoke(lambda: lr.run_lanes(sys.executable, [["a"], ["b"]], False, 30.0, {})[0][0]["rc"])
        self.assertEqual(rc, 0)
        self.assertIn("各车道已完成用例", out)
        self.assertIn("L0=", out)
        self.assertIn("L1=", out)

    def test_overall_timeout_terminates_sleeping_lanes(self):
        with self.fake_command([("sleep", 0), ("sleep", 0)]):
            lanes, _wall, timed_out = lr.run_lanes(sys.executable, [["a"], ["b"]], False, 0.5, {})
        self.assertTrue(timed_out)
        self.assertEqual([lanes[0]["rc"], lanes[1]["rc"]], [-signal.SIGTERM, -signal.SIGTERM])

    def test_a_lane_that_ignores_termination_is_killed_after_the_grace_period(self):
        with self.fake_command([("stubborn", 0), ("ok", 0)]):
            lanes, _wall, timed_out = lr.run_lanes(sys.executable, [["a"], ["b"]], False, 3.0, {})
        self.assertTrue(timed_out)
        self.assertIn(lanes[0]["rc"], (-signal.SIGKILL, -signal.SIGTERM))
        self.assertEqual(lanes[1]["rc"], 0)

    def test_a_lane_that_dies_without_a_result_file_is_visible_to_the_judge(self):
        with self.fake_command([("ok", 0), ("die", 7)]):
            lanes, _wall, _timed = lr.run_lanes(sys.executable, [["a"], ["b"]], False, 30.0, {})
        self.assertEqual(lanes[1]["rc"], 7)
        self.assertFalse(lr.lane_files(1)["result"].exists())
        code, problems, _summary = lr.judge([["a"], ["b"]], {"a": 3, "b": 3}, lanes, False)
        self.assertEqual(code, 1)
        self.assertTrue(any("没有结果文件" in item for item in problems))

    def test_the_termination_signal_stops_children_and_exits_with_128_plus_signal(self):
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("signal handlers can only be installed from the main thread")
        before = signal.getsignal(signal.SIGTERM)
        timer = threading.Timer(0.4, lambda: os.kill(os.getpid(), signal.SIGTERM))
        timer.start()
        self.addCleanup(timer.cancel)
        with self.fake_command([("sleep", 0), ("sleep", 0)]), self.assertRaises(SystemExit) as caught:
            lr.run_lanes(sys.executable, [["a"], ["b"]], False, 30.0, {})
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)
        self.assertIs(signal.getsignal(signal.SIGTERM), before, "the previous handler must be restored")

class TestMain(LaneCase):
    """main() with discovery and lane execution faked; the judge, merge and printing are real."""

    MODULES = {"a": 10, "b": 7, "c": 5}      # no weights -> plan is [["a"], ["b", "c"]] on two lanes

    def run_main(self, argv, results=None, no_result=(), no_log=(), timed_out=False,
                 cov=(True, "merged"), discover_error=None, out_path=""):
        results = results or {}
        ran = []

        def fake_discover(_python):
            if discover_error:
                raise RuntimeError(discover_error)
            return dict(self.MODULES)

        def fake_run(_python, plan, _coverage, _timeout, _extra):
            ran.append(plan)
            lanes = {}
            for index, lane in enumerate(plan):
                files = lr.lane_files(index)
                body = {"schema": 1, "rc": 0, "total": sum(self.MODULES[m] for m in lane), "failures": [],
                        "flaky": [], "ui_violations": 0, "dirty": []}
                body.update(results.get(index, {}))
                if index not in no_result:
                    files["result"].write_text(json.dumps(body), encoding="utf-8")
                if index not in no_log:
                    files["log"].write_text("lane %d line one\nlane %d last line\n" % (index, index), encoding="utf-8")
                files["durations"].write_text(json.dumps(
                    {"modules": [{"module": "m%d" % index, "seconds": 1.0, "cases": 2}]}), encoding="utf-8")
                lanes[index] = {"rc": body["rc"], "seconds": 2.0}
            return lanes, 3.0, timed_out

        env = {"SF_TEST_DURATIONS_OUT": out_path}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(lr, "discover", fake_discover), \
                mock.patch.object(lr, "run_lanes", fake_run), \
                mock.patch.object(lr, "load_weights", lambda: {}), \
                mock.patch.object(lr, "combine_coverage", lambda *a, **k: cov):
            rc, out = invoke(lr.main, argv)
        return rc, out, ran

    def test_green_run_reports_totals_and_writes_merged_durations(self):
        target = self.tmp / "merged" / "d.json"
        rc, out, ran = self.run_main(["--lanes", "2"], out_path=str(target))
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(ran[0]), 2)
        self.assertIn("用例总数 22｜失败/错误 0｜flaky 0", out)
        self.assertIn("用户数据未改动", out)
        self.assertIn("耗时明细已写入", out)
        merged = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual((len(merged["modules"]), merged["lanes"]["count"]), (2, 2))

    def test_plan_only_prints_the_plan_and_never_runs_lanes(self):
        rc, out, ran = self.run_main(["--lanes", "2", "--plan-only"])
        self.assertEqual((rc, ran), (0, []))
        self.assertIn("L0:", out)
        self.assertIn("L1:", out)

    def test_an_absurd_lane_count_is_clamped_and_announced(self):
        asked = []
        real_plan = lr.plan_lanes

        def spy(modules, lanes, weights):
            asked.append(lanes)
            return real_plan(modules, lanes, weights)

        with mock.patch.object(lr, "plan_lanes", spy):
            rc, out, _ran = self.run_main(["--lanes", "64", "--plan-only"])
        self.assertEqual((rc, asked), (0, [lr.MAX_LANES]))
        self.assertIn("超过上限", out)

    def test_a_lane_count_within_the_cap_is_passed_through_silently(self):
        asked = []
        real_plan = lr.plan_lanes

        def spy(modules, lanes, weights):
            asked.append(lanes)
            return real_plan(modules, lanes, weights)

        with mock.patch.object(lr, "plan_lanes", spy):
            rc, out, _ran = self.run_main(["--lanes", str(lr.MAX_LANES), "--plan-only"])
        self.assertEqual((rc, asked), (0, [lr.MAX_LANES]))
        self.assertNotIn("超过上限", out)

    def test_unknown_only_module_is_refused(self):
        rc, out, ran = self.run_main(["--only", "zzz_not_a_module"])
        self.assertEqual((rc, ran), (2, []))
        self.assertIn("不存在", out)

    def test_discovery_failure_is_red_not_skipped(self):
        rc, out, ran = self.run_main(["--lanes", "2"], discover_error="boom: nothing found")
        self.assertEqual((rc, ran), (1, []))
        self.assertIn("boom: nothing found", out)

    def test_subset_run_says_it_is_not_the_full_gate_and_reports_the_coverage_merge(self):
        rc, out, _ran = self.run_main(["--only", "a", "--coverage"])
        self.assertEqual(rc, 0, out)
        self.assertIn("冒烟子集", out)
        self.assertIn("覆盖率已合并到 merged", out)
        rc, out, _ran = self.run_main(["--only", "a"])
        self.assertIn("未采集覆盖率", out)

    def test_failing_lane_is_named_and_its_log_tail_is_shown(self):
        rc, out, _ran = self.run_main(["--lanes", "2"], results={
            1: {"rc": 1, "failures": ["mod_b.Case.test_broken"]}})
        self.assertEqual(rc, 1)
        self.assertIn("失败：mod_b.Case.test_broken", out)
        self.assertIn("L1 日志末尾", out)
        self.assertIn("lane 1 last line", out)

    def test_missing_result_and_missing_log_are_both_reported(self):
        rc, out, _ran = self.run_main(["--lanes", "2"], no_result=(1,), no_log=(1,))
        self.assertEqual(rc, 1)
        self.assertIn("L1 没有结果文件", out)
        self.assertIn("(日志读不到)", out)

    def test_dirty_user_data_is_the_red_line_with_exit_code_2(self):
        rc, out, _ran = self.run_main(["--lanes", "2"], results={
            0: {"rc": 2, "dirty": ["config.json：大小 93->94"]}})
        self.assertEqual(rc, 2)
        self.assertIn("用户数据被改动：config.json", out)

    def test_a_failed_coverage_merge_turns_the_round_red(self):
        rc, out, _ran = self.run_main(["--lanes", "2", "--coverage"], cov=(False, "合并后的覆盖率数据为空：x"))
        self.assertEqual(rc, 1)
        self.assertIn("合并后的覆盖率数据为空", out)

    def test_an_unwritable_durations_target_is_reported_but_does_not_change_the_verdict(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("a file where a directory is needed", encoding="utf-8")
        rc, out, _ran = self.run_main(["--lanes", "2"], out_path=str(blocker / "d.json"))
        self.assertEqual(rc, 0, out)
        self.assertIn("耗时明细没写成", out)

    def test_overall_timeout_is_a_problem_line_and_red(self):
        rc, out, _ran = self.run_main(["--lanes", "2"], timed_out=True)
        self.assertEqual(rc, 1)
        self.assertIn("车道整体超时", out)

    def test_default_lane_count_never_exceeds_the_module_count(self):
        with mock.patch.object(lr, "auto_lanes", lambda: 9):
            rc, out, ran = self.run_main([])
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(len(lane) for lane in ran[0]), [1, 1, 1])

    def test_self_test_flag_and_module_entry_point(self):
        before = lr.LANES_DIR
        rc, out = invoke(lr.main, ["--self-test"])
        self.assertEqual((rc, lr.LANES_DIR), (0, before))
        self.assertIn("self-test 通过", out)
        out_buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["lane_runner.py", "--self-test"]), \
                redirect_stdout(out_buf), self.assertRaises(SystemExit) as caught:
            runpy.run_path(str(ROOT / "tools" / "lane_runner.py"), run_name="__main__")
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("self-test 通过", out_buf.getvalue())

    def test_auto_lanes_scales_with_cores_and_has_a_floor_and_a_cap(self):
        for cores, expected in ((None, 1), (1, 1), (4, 2), (64, 4)):
            with self.subTest(cores=cores), mock.patch.object(lr.os, "cpu_count", lambda c=cores: c):
                self.assertEqual(lr.auto_lanes(), expected)

class TestCommandAndJudgeEdges(LaneCase):
    def test_lane_command_lists_every_module_and_adds_coverage_only_when_asked(self):
        plain = lr.lane_command("py", 2, ["test_a", "test_b"], False)
        self.assertEqual(plain[0], "py")
        self.assertEqual(plain[2:], ["--module", "test_a", "--module", "test_b"])
        self.assertTrue(plain[1].endswith("test_runner.py"))
        covered = lr.lane_command("py", 2, ["test_a"], True)
        self.assertEqual(covered[1:3], ["-m", "coverage"])
        self.assertIn("--data-file=%s" % lr.lane_files(2)["cov"], covered)
        self.assertEqual(covered[-2:], ["--module", "test_a"])

    def test_a_lane_that_exits_with_an_unknown_code_is_a_problem_even_with_a_matching_result(self):
        lr.lane_files(0)["result"].write_text(json.dumps(
            {"schema": 1, "rc": -11, "total": 4, "failures": [], "flaky": [], "ui_violations": 0, "dirty": []}),
            encoding="utf-8")
        code, problems, _summary = lr.judge([["a"]], {"a": 4}, {0: {"rc": -11}}, False)
        self.assertEqual(code, 1)
        self.assertTrue(any("异常退出" in item for item in problems))

    def test_self_test_reports_a_failed_check_and_returns_1(self):
        with mock.patch.object(lr, "judge", lambda *a, **k: (0, [], {})):
            rc, out = invoke(lr.self_test)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", out)


class TestDiscoverAndCombine(LaneCase):
    def fake_discovery(self, rc=0, payload=None):
        def run(cmd, **_kw):
            if payload is not None:
                pathlib.Path(cmd[-1]).write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, rc, "out", "err")
        return mock.patch.object(lr.subprocess, "run", run)

    def test_discovery_returns_module_counts(self):
        with self.fake_discovery(payload={"modules": {"a": 3, "b": 4}}):
            self.assertEqual(lr.discover(sys.executable), {"a": 3, "b": 4})

    def test_discovery_failures_raise_instead_of_degrading(self):
        cases = (("nonzero exit", dict(rc=3, payload={"modules": {"a": 1}}), "模块发现失败"),
                 ("no output file", dict(rc=0, payload=None), "模块发现失败"),
                 ("empty module table", dict(rc=0, payload={"modules": {}}), "为空"),
                 ("all zero cases", dict(rc=0, payload={"modules": {"a": 0}}), "为空"))
        for label, kwargs, needle in cases:
            with self.subTest(label), self.fake_discovery(**kwargs), self.assertRaises(RuntimeError) as caught:
                lr.discover(sys.executable)
            self.assertIn(needle, str(caught.exception))

    def write_lane_data(self, index, lines):
        data = coverage.CoverageData(basename=str(lr.lane_files(index)["cov"]))
        data.add_lines({str(self.tmp / "mod.py"): lines})
        data.write()

    def test_real_combine_unions_the_lines_of_every_lane(self):
        self.write_lane_data(0, [1, 2])
        self.write_lane_data(1, [2, 3])
        target = self.tmp / "combined.cov"
        ok, where = lr.combine_coverage(sys.executable, 2, target)
        self.assertEqual((ok, where), (True, str(target)))
        merged = coverage.CoverageData(basename=str(target))
        merged.read()
        names = [f for f in merged.measured_files() if f.endswith("mod.py")]
        self.assertEqual(sorted(merged.lines(names[0])), [1, 2, 3])


class TestRunnerLaneHooks(SFCase):
    def finish_one_case(self, flag):
        env = {"SF_GC_AT_TEST_BOUNDARY": flag} if flag is not None else {}
        calls = []
        with mock.patch.dict(os.environ, env), mock.patch.object(runner.gc, "collect", lambda *a: calls.append(1)):
            if flag is None:
                os.environ.pop("SF_GC_AT_TEST_BOUNDARY", None)
            result = Result(io.StringIO(), True, 0, sf_timeout=0)
            case = unittest.FunctionTestCase(lambda: None)
            result.startTest(case)
            result.stopTest(case)
        return len(calls)

    def test_collection_happens_at_the_end_of_each_case_only_when_the_switch_is_on(self):
        self.assertEqual(self.finish_one_case("1"), 1)
        self.assertEqual(self.finish_one_case("0"), 0)
        self.assertEqual(self.finish_one_case(None), 0)

    def test_automatic_collection_is_off_during_the_run_and_restored_after(self):
        seen = []
        for flag in ("1", "0"):
            with mock.patch.dict(os.environ, {"SF_GC_AT_TEST_BOUNDARY": flag}), \
                    mock.patch.object(runner, "_run_tests", lambda modules=None: seen.append(gc.isenabled()) or 0):  # discipline-ok(R5): the wrapper's collaborator is what the switch must bracket
                self.assertEqual(runner.run_tests(), 0)
                self.assertTrue(gc.isenabled(), "collection must be back on afterwards")
        self.assertEqual(seen, [False, True])

    def test_a_process_that_started_with_collection_off_stays_off(self):
        self.addCleanup(gc.enable)
        gc.disable()
        with mock.patch.dict(os.environ, {"SF_GC_AT_TEST_BOUNDARY": "1"}), \
                mock.patch.object(runner, "_run_tests", lambda modules=None: 0):  # discipline-ok(R5): same collaborator as above
            runner.run_tests()
        self.assertFalse(gc.isenabled())

class TestListModules(SFCase):
    def make_tests_dir(self):
        tmp = tempfile.TemporaryDirectory(prefix="sf_listmod_")
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        for name, count in (("test_zz_lane_alpha", 2), ("test_zz_lane_beta", 3)):
            body = "import unittest\n\nclass C(unittest.TestCase):\n" + "".join(
                "    def test_%d(self):\n        pass\n" % i for i in range(count))
            (root / (name + ".py")).write_text(body, encoding="utf-8")
            self.addCleanup(sys.modules.pop, name, None)
        return root

    def test_counts_are_per_file_and_written_as_json(self):
        root = self.make_tests_dir()
        out = root / "modules.json"
        with mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.object(runner, "_preimport_project_modules"):
            rc, text = invoke(runner.list_modules, str(out), str(root))
        self.assertEqual(rc, 0, text)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["modules"], {"test_zz_lane_alpha": 2, "test_zz_lane_beta": 3})
        self.assertEqual(payload["total"], 5)

    def test_an_unwritable_target_is_reported_with_exit_code_2(self):
        root = self.make_tests_dir()
        blocker = root / "blocker"
        blocker.write_text("a file where a directory is needed", encoding="utf-8")
        with mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.object(runner, "_preimport_project_modules"):
            rc, text = invoke(runner.list_modules, str(blocker / "m.json"), str(root))
        self.assertEqual(rc, 2)
        self.assertIn("模块清单没写成", text)

    def test_the_cli_flag_routes_to_the_listing_without_running_anything(self):
        with mock.patch.object(runner, "list_modules", return_value=0) as listing:
            rc, _text = invoke(runner.main, ["--list-json", "/tmp/never-written.json"])
        self.assertEqual(rc, 0)
        listing.assert_called_once_with("/tmp/never-written.json")


class TestLaneResultFile(SFCase):
    def run_suite(self, fn, out_path, ui_violation=False, snapshots=None):
        suite = unittest.TestSuite([unittest.TestSuite([unittest.FunctionTestCase(fn)])])
        loader = mock.Mock()
        loader.loadTestsFromNames.return_value = suite
        env = {"SF_TEST_TIMEOUT": "0", "SF_RANDOM_ORDER": "0", "SF_RERUN_FAILED": "0",
               "SF_LANE_RESULT_OUT": str(out_path), "SF_TEST_DURATIONS_OUT": ""}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.object(runner, "install_ui_dialog_guard"), \
                mock.patch.object(runner, "_preimport_project_modules"), \
                mock.patch.object(runner, "emit_dirty_detail"), \
                mock.patch.object(runner.unittest, "TestLoader", return_value=loader), \
                mock.patch.object(runner, "_snapshot_user_data", side_effect=snapshots or [{}, {}]), \
                mock.patch.object(runner, "_report_ui_dialog_violations", return_value=ui_violation):
            return invoke(runner.main, ["--module", "test_sample"])

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="sf_laneres_")
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)

    def test_a_failing_case_is_written_with_its_id_and_the_red_exit_code(self):
        out = self.tmp / "r.json"

        def broken():
            raise AssertionError("planted")

        rc, _text = self.run_suite(broken, out)
        body = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((rc, body["rc"], body["total"], len(body["failures"]), body["ui_violations"]), (1, 1, 1, 1, 0))

    def test_a_dialog_violation_is_counted_even_when_the_case_passes(self):
        out = self.tmp / "r.json"
        rc, _text = self.run_suite(lambda: None, out, ui_violation=True)
        body = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((rc, body["ui_violations"], body["failures"]), (1, 1, []))

    def test_changed_user_data_is_listed_and_wins_with_exit_code_2(self):
        out = self.tmp / "r.json"
        before = {"config.json": (93, 1000), "securities.json": (100, 2000), "debug.log": (5000, 3000)}
        after = dict(before, **{"config.json": (94, 1001)})
        rc, _text = self.run_suite(lambda: None, out, snapshots=[before, after])
        body = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((rc, body["rc"]), (2, 2))
        self.assertTrue(any("config.json" in item for item in body["dirty"]))

    def test_an_unwritable_result_path_is_announced_and_leaves_the_verdict_alone(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("a file where a directory is needed", encoding="utf-8")
        rc, text = self.run_suite(lambda: None, blocker / "r.json")
        self.assertEqual(rc, 0)
        self.assertIn("车道结果没写成", text)


class TestPreCommitLanesDefault(SFCase):
    """git-hooks/pre-commit 里 lanes_default 片段：没人设置 SF_LANES 时按核数给默认值，设置了就尊重。

    抽出 BEGIN/END lanes_default 之间那一段，用钩子同款的 `set -euo pipefail` 在 bash 里逐个环境跑，
    环境从零构造。期望值取自 lane_runner.auto_lanes()，不写死本机核数。
    """

    @classmethod
    def block(cls):
        text = (ROOT / "git-hooks" / "pre-commit").read_text(encoding="utf-8")
        begin, end = "# BEGIN lanes_default", "# END lanes_default"
        if begin not in text or end not in text:
            raise AssertionError("pre-commit 里找不到 lanes_default 的起止标记，本判据无从核对")
        # 标记行后面可能跟着说明文字，整行丢掉，只取下一行起到结束标记之前
        return text.split(begin, 1)[1].split("\n", 1)[1].split(end, 1)[0]

    def resolve(self, lanes=None, py=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if lanes is not None:
            env["SF_LANES"] = lanes
        env["PY"] = py or sys.executable
        env["REPO_ROOT"] = str(ROOT)
        script = "set -euo pipefail\n" + self.block() + '\nprintf "%s" "${SF_LANES-UNSET}"\n'
        proc = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, "片段在 set -euo pipefail 下不该失败：%s" % proc.stderr)
        return proc.stdout

    def test_unset_gets_the_machine_default_or_serial_when_too_few_cores(self):
        auto = lr.auto_lanes()
        expected = str(auto) if auto >= 2 else ""
        self.assertEqual(self.resolve(), expected)

    def test_an_explicit_value_is_never_overridden(self):
        for given in ("0", "1", "3", "7", ""):
            with self.subTest(given=given):
                self.assertEqual(self.resolve(lanes=given), given)

    def test_a_broken_interpreter_falls_back_to_serial_and_never_fails_the_hook(self):
        self.assertEqual(self.resolve(py="/nonexistent/python3"), "")

    def test_a_useless_computed_value_is_dropped_instead_of_passed_on(self):
        """用桩模块让“按核数算出的数”可控：少于 2 条或不是数字都要留空，走串行。"""
        for stub_value, expected in (("1", ""), ("0", ""), ("'abc'", ""), ("''", ""), ("5", "5"), ("2", "2")):
            with self.subTest(computed=stub_value), tempfile.TemporaryDirectory(prefix="sf_hookstub_") as tmp:
                tools = pathlib.Path(tmp) / "tools"
                tools.mkdir()
                (tools / "lane_runner.py").write_text("def auto_lanes():\n    return %s\n" % stub_value, encoding="utf-8")
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PY": sys.executable, "REPO_ROOT": tmp}
                script = "set -euo pipefail\n" + self.block() + '\nprintf "%s" "${SF_LANES-UNSET}"\n'
                proc = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual((proc.returncode, proc.stdout), (0, expected))

    def test_the_default_is_exported_so_run_tests_can_see_it(self):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PY": sys.executable, "REPO_ROOT": str(ROOT)}
        script = "set -euo pipefail\n" + self.block() + '\n/bin/sh -c \'printf "%s" "${SF_LANES-UNSET}"\'\n'
        proc = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
        auto = lr.auto_lanes()
        self.assertEqual(proc.stdout, str(auto) if auto >= 2 else "")


class TestRunTestsLanesSwitch(SFCase):
    """run_tests.sh 里 SF_LANES 的取值规则：只有“正整数”才开车道，其余一律回到串行。

    把脚本里 BEGIN/END lanes_switch 之间那一段原样抽出来，在 /bin/sh 里逐个环境值跑，
    只看它算出的 SF_LANES_N。抽段而不是整脚本跑：整脚本一跑就是一整轮回归。
    环境从零开始构造，不继承本机的 SF_LANES / SF_RANDOM_ORDER，免得判据读到的是本机设置。
    """

    @classmethod
    def switch_block(cls):
        text = (ROOT / "run_tests.sh").read_text(encoding="utf-8")
        begin, end = "# BEGIN lanes_switch", "# END lanes_switch"
        if begin not in text or end not in text:
            raise AssertionError("run_tests.sh 里找不到 lanes_switch 的起止标记，本判据无从核对")
        # 标记行后面可能跟着说明文字，整行丢掉，只取下一行起到结束标记之前
        return text.split(begin, 1)[1].split("\n", 1)[1].split(end, 1)[0]

    def resolve(self, lanes=None, random_order=None, quality_session=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if lanes is not None:
            env["SF_LANES"] = lanes
        if random_order is not None:
            env["SF_RANDOM_ORDER"] = random_order
        if quality_session is not None:
            env["SF_QUALITY_SESSION_DIR"] = quality_session
        script = "set -u\n" + self.switch_block() + '\nprintf "%s" "$SF_LANES_N"\n'
        proc = subprocess.run(["/bin/sh", "-c", script], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, "取值片段在 set -u 下不该失败：%s" % proc.stderr)
        return proc.stdout

    def test_only_a_positive_integer_turns_the_lanes_on(self):
        for given, expected in (("4", "4"), ("12", "12"), ("1", "1")):
            with self.subTest(given=given):
                self.assertEqual(self.resolve(lanes=given), expected)

    def test_unset_empty_zero_and_garbage_all_mean_serial(self):
        for given in (None, "", "0", "00", "abc", "4x", "-3", " 4", "4 ", "1.5"):
            with self.subTest(given=given):
                self.assertEqual(self.resolve(lanes=given), "", "SF_LANES=%r 应当走串行" % (given,))

    def test_random_order_always_wins_over_the_lanes(self):
        self.assertEqual(self.resolve(lanes="4", random_order="1"), "")
        self.assertEqual(self.resolve(lanes="4", random_order="0"), "4")

    def test_a_quality_session_directory_forces_serial(self):
        """质量执行器指定的会话路径与车道模式的定向环境互相冲突，有会话目录时一律串行。"""
        self.assertEqual(self.resolve(lanes="4", quality_session="/some/session"), "")
        self.assertEqual(self.resolve(lanes="4", quality_session=""), "4")

    def test_an_absurdly_long_number_does_not_break_the_snippet(self):
        value = self.resolve(lanes="9" * 40)
        self.assertIn(value, ("", "9" * 40), "超长数字要么被拒（串行），要么原样交给总指挥，不许崩")


if __name__ == "__main__":
    unittest.main(verbosity=2)
