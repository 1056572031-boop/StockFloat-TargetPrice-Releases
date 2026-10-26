"""提交前改动覆盖率预判：选取、拒绝边界与判定本身的测试。

不在测试里起产品子集进程；子集执行入口由工具自身的临时 Git 自检和一次手工演练覆盖，
这里用确定性的改动表与 coverage 报告检查“选哪批、何时不能下结论”。
"""
from __future__ import annotations

import ast
import io
import json
import pathlib
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

try:
    from _base import SFCase
except ImportError:  # pragma: no cover
    from tests._base import SFCase

import change_predict as predict


class IndexAndSymbols(SFCase):
    def test_imports_and_patch_strings_become_references(self):
        tree = ast.parse('import sf_quote\nfrom tools import gate_stamp\nvalue = "sf_notify._post"\n')
        refs = predict.referenced_names(tree)
        self.assertTrue({"sf_quote", "tools", "gate_stamp", "sf_notify", "_post"} <= refs)

    def test_sentence_strings_are_not_mistaken_for_a_module_reference(self):
        tree = ast.parse('value = "this is a sentence about sf_quote"\n')
        self.assertNotIn("sf_quote", predict.referenced_names(tree))

    def test_changed_lines_choose_the_innermost_definition(self):
        source = "class Box:\n    def action(self):\n        return 1\n"
        self.assertEqual(predict.changed_symbols(source, {3}), {"action"})

    def test_module_level_change_has_no_symbol_for_narrowing(self):
        self.assertEqual(predict.changed_symbols("VALUE = 1\n", {1}), set())


class _PlanningBase(SFCase):
    def make_root(self, files):
        temp = tempfile.TemporaryDirectory(prefix="sf_change_predict_test_")
        self.addCleanup(temp.cleanup)
        root = pathlib.Path(temp.name)
        (root / "tests").mkdir()
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return root

    def plan_with(self, root, changed):
        refs = ["base-sha", "head-sha"]
        with mock.patch.object(predict.change_gate, "_git_changes", return_value=(changed, {}, refs)):
            return predict.plan(root, "HEAD")


class Planning(_PlanningBase):
    def test_changed_test_module_is_selected_even_though_tests_are_not_gate_files(self):
        root = self.make_root({"tests/test_alpha.py": "def test_alpha():\n    assert True\n"})
        value = self.plan_with(root, {"tests/test_alpha.py": {1}})
        self.assertEqual(value["status"], "planned")
        self.assertEqual(value["modules"], ["test_alpha"])
        self.assertFalse(value["files"][0]["gated"])

    def test_unmapped_product_file_is_partial_instead_of_clean(self):
        root = self.make_root({"sf_orphan.py": "VALUE = 1\n", "tests/test_other.py": "def test_other():\n    assert True\n"})
        value = self.plan_with(root, {"sf_orphan.py": {1}})
        self.assertEqual(value["status"], "partial")
        self.assertEqual(predict.EXIT[value["status"]], 3)

    def test_imported_product_file_selects_its_test(self):
        root = self.make_root({"sf_price.py": "def quote():\n    return 1\n",
                               "tests/test_price.py": "from sf_price import quote\n\ndef test_quote():\n    assert quote() == 1\n"})
        value = self.plan_with(root, {"sf_price.py": {2}})
        self.assertEqual(value["status"], "planned")
        self.assertEqual(value["modules"], ["test_price"])
        self.assertTrue(value["files"][0]["gated"])

    def test_many_related_tests_are_narrowed_by_changed_symbol(self):
        files = {"sf_price.py": "def selected():\n    return 1\n"}
        for number in range(predict.NARROW_ABOVE + 1):
            symbol = "selected" if number == 0 else "other_%d" % number
            files["tests/test_price_%d.py" % number] = (
                "from sf_price import %s\n\ndef test_value():\n    assert callable(%s)\n" % (symbol, symbol))
        root = self.make_root(files)
        value = self.plan_with(root, {"sf_price.py": {2}})
        self.assertEqual(value["modules"], ["test_price_0"])
        self.assertEqual(value["files"][0]["selection_mode"], "symbol reference")

    def test_too_many_selected_modules_refuses_to_spend_nearly_a_full_gate(self):
        root = self.make_root({"sf_price.py": "VALUE = 1\n",
                               "tests/test_price.py": "import sf_price\n"})
        with mock.patch.object(predict, "MAX_MODULES", 0):
            value = self.plan_with(root, {"sf_price.py": {1}})
        self.assertEqual(value["status"], "too_broad")


class Judgement(SFCase):
    def planned(self):
        return {"status": "planned", "base": "base", "files": [
            {"path": "sf_price.py", "status": "planned", "gated": True},
            {"path": "tests/test_price.py", "status": "planned", "gated": False},
        ]}

    def judge_with(self, report):
        with tempfile.TemporaryDirectory() as directory:
            data = pathlib.Path(directory) / ".coverage"
            data.write_bytes(b"x")
            with mock.patch.object(predict.change_gate, "inspect", return_value=report):
                return predict.judge(pathlib.Path(directory), self.planned(), data, 1.25, "selected log")

    def test_missing_changed_line_is_predicted_miss(self):
        result = self.judge_with({"files": [{"path": "sf_price.py", "executable_changed": [2], "missing": [2]}]})
        self.assertEqual(result["status"], "predicted_miss")
        self.assertEqual(result["missing"], {"sf_price.py": [2]})

    def test_unrelated_gate_file_in_coverage_report_cannot_make_this_prediction_red(self):
        result = self.judge_with({"files": [
            {"path": "sf_price.py", "executable_changed": [2], "missing": []},
            {"path": "sf_other.py", "executable_changed": [4], "missing": [4]},
        ]})
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["executable"], 1)

    def test_zero_executable_lines_is_not_reported_as_coverage_pass(self):
        result = self.judge_with({"files": [{"path": "sf_price.py", "executable_changed": [], "missing": []}]})
        self.assertEqual(result["status"], "not_applicable")

    def test_partial_mapping_stays_partial_after_selected_lines_are_green(self):
        planned = self.planned()
        planned["files"].append({"path": "sf_unknown.py", "status": "unmapped", "gated": True})
        with tempfile.TemporaryDirectory() as directory:
            data = pathlib.Path(directory) / ".coverage"
            data.write_bytes(b"x")
            report = {"files": [{"path": "sf_price.py", "executable_changed": [2], "missing": []}]}
            with mock.patch.object(predict.change_gate, "inspect", return_value=report):
                result = predict.judge(pathlib.Path(directory), planned, data, 1.0, "")
        self.assertEqual(result["status"], "partial")


class PresentationAndOutput(SFCase):
    def test_pass_message_is_explicit_that_it_is_not_the_full_gate(self):
        planned = {"base": "base", "modules": ["test_price"], "reason": ""}
        result = {"status": "pass", "elapsed_seconds": 1.5, "executable": 2}
        output = io.StringIO()
        with redirect_stdout(output):
            predict.print_result(planned, result)
        self.assertIn("不替代完整关卡", output.getvalue())
        self.assertIn("提交钩子仍会跑全量", output.getvalue())

    def test_outcome_for_partial_is_exit_three_not_a_false_green(self):
        value = predict.outcome({"status": "partial", "base": "b", "head": "h", "files": [], "modules": []},
                                {"status": "partial", "elapsed_seconds": 0.0})
        self.assertEqual(value["exit_code"], 3)

    def test_json_output_stays_inside_the_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            value = {"schema": 1, "status": "pass"}
            predict.write_json("build/result.json", value, root)
            self.assertEqual(json.loads((root / "build" / "result.json").read_text(encoding="utf-8")), value)
            with self.assertRaises(ValueError):
                predict.write_json("../outside.json", value, root)


class PlanningEdges(_PlanningBase):
    """沿用同一套夹具，只加排除、不可读和命名边界。"""

    def test_only_limits_the_prediction_to_the_named_paths(self):
        root = self.make_root({"sf_mine.py": "def mine():\n    return 1\n",
                               "sf_theirs.py": "def theirs():\n    return 1\n",
                               "tests/test_mine.py": "import sf_mine\n",
                               "tests/test_theirs.py": "import sf_theirs\n"})
        changed = {"sf_mine.py": {2}, "sf_theirs.py": {2}}
        everything = self.plan_with(root, changed)
        refs = ["base-sha", "head-sha"]
        with mock.patch.object(predict.change_gate, "_git_changes", return_value=(changed, {}, refs)):
            narrowed = predict.plan(root, "HEAD", only={"sf_mine.py"})
        self.assertEqual(everything["modules"], ["test_mine", "test_theirs"])
        self.assertEqual(narrowed["modules"], ["test_mine"])
        self.assertEqual([item["path"] for item in narrowed["files"]], ["sf_mine.py"])

    def test_docs_only_change_selects_nothing_and_is_not_applicable(self):
        root = self.make_root({"README.md": "doc\n"})
        value = self.plan_with(root, {"README.md": {1}})
        self.assertEqual(value["status"], "not_applicable")
        self.assertEqual(value["modules"], [])

    def test_deleted_product_file_is_excluded_not_mapped(self):
        root = self.make_root({"tests/test_x.py": "def test_x():\n    assert True\n"})
        value = self.plan_with(root, {"sf_gone.py": set()})
        self.assertEqual(value["status"], "not_applicable")
        self.assertEqual(value["files"][0]["status"], "excluded")

    def test_unparseable_test_file_makes_the_prediction_unknown(self):
        root = self.make_root({"tests/test_bad.py": "def (:\n"})
        value = self.plan_with(root, {"sf_x.py": {1}})
        self.assertEqual(value["status"], "unknown")
        self.assertEqual(predict.EXIT[value["status"]], 3)

    def test_test_files_that_the_runner_cannot_name_are_not_indexed(self):
        root = self.make_root({"tests/test_ok.py": "x = 1\n", "tests/test_bad-name.py": "x = 1\n"})
        index, unreadable = predict.build_index(root)
        self.assertEqual(sorted(index), ["test_ok"])
        self.assertEqual(unreadable, [])

    def test_source_that_cannot_be_parsed_gives_no_symbols(self):
        self.assertEqual(predict.changed_symbols("def (:\n", {1}), set())


class RunPlan(SFCase):
    def planned(self):
        return {"status": "planned", "base": "base", "modules": ["test_price"], "files": [
            {"path": "sf_price.py", "status": "planned", "gated": True}]}

    def run_with(self, fake, running="", **kwargs):
        # 本机的正式版可能正开着：守卫的结论由用例显式给定，不依赖当下有没有进程。
        with mock.patch.object(predict, "spawn", side_effect=fake), \
                mock.patch.object(predict, "installed_app_running", return_value=running):
            return predict.run_plan(ROOT, self.planned(), 5.0, **kwargs)

    def test_a_running_installed_app_blocks_the_subset_before_anything_is_spawned(self):
        spawned = []

        def never(cmd, cwd, env, sink, timeout):
            spawned.append(cmd)
            return 0

        result = self.run_with(never, running="Skyrocket")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("正式版 Skyrocket 正在运行", result["log"])
        self.assertEqual(spawned, [])

    def test_guard_matches_by_exact_process_name_for_both_generations(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.Mock(returncode=0 if cmd[-1] == "StockFloatTargetPrice" else 1)

        with mock.patch.object(predict.subprocess, "run", side_effect=fake_run):
            self.assertEqual(predict.installed_app_running(), "StockFloatTargetPrice")
        self.assertEqual(calls, [["pgrep", "-x", "Skyrocket"], ["pgrep", "-x", "StockFloatTargetPrice"]])

    def test_guard_reports_nothing_when_neither_generation_is_running(self):
        with mock.patch.object(predict.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertEqual(predict.installed_app_running(), "")

    def test_guard_refuses_when_it_cannot_tell(self):
        with mock.patch.object(predict.subprocess, "run", side_effect=OSError("no pgrep")):
            self.assertNotEqual(predict.installed_app_running(), "")

    def test_a_plan_that_is_not_runnable_is_passed_through_untouched(self):
        result = predict.run_plan(ROOT, {"status": "too_broad"}, 5.0)
        self.assertEqual(result["status"], "too_broad")
        self.assertEqual(result["elapsed_seconds"], 0.0)

    def test_failing_subset_names_the_red_cases_before_the_log_tail(self):
        def failing(cmd, cwd, env, sink, timeout):
            sink.write("noise\nFAIL: test_price.Case.test_a\nmore noise\n")
            return 1

        result = self.run_with(failing)
        self.assertEqual(result["status"], "tests_failed")
        self.assertTrue(result["log"].startswith("FAIL: test_price.Case.test_a"))

    def test_timeout_or_spawn_failure_is_unknown_not_a_test_failure(self):
        def slow(cmd, cwd, env, sink, timeout):
            raise TimeoutError("子集预判超过 5s 超时")

        def broken(cmd, cwd, env, sink, timeout):
            raise OSError("cannot exec")

        self.assertEqual(self.run_with(slow)["status"], "unknown")
        self.assertEqual(self.run_with(broken)["status"], "unknown")

    def test_green_subset_without_coverage_data_is_unknown(self):
        result = self.run_with(lambda cmd, cwd, env, sink, timeout: 0)
        self.assertEqual(result["status"], "unknown")

    def test_green_subset_with_coverage_is_judged_by_the_gate_ruler(self):
        def passing(cmd, cwd, env, sink, timeout):
            pathlib.Path(env["COVERAGE_FILE"]).write_bytes(b"x")
            return 0

        report = {"files": [{"path": "sf_price.py", "executable_changed": [2], "missing": []}]}
        with mock.patch.object(predict.change_gate, "inspect", return_value=report):
            result = self.run_with(passing)
        self.assertEqual(result["status"], "pass")

    def test_subset_environment_drops_short_circuit_switches_and_forces_offscreen(self):
        seen = {}

        def capture(cmd, cwd, env, sink, timeout):
            seen.update(cmd=cmd, env=env)
            return 1

        with mock.patch.dict("os.environ", {"SF_GUARD_PROBE": "1", "SF_TEST_DURATIONS_OUT": "/x"}):
            self.run_with(capture)
        self.assertNotIn("SF_GUARD_PROBE", seen["env"])
        self.assertNotIn("SF_TEST_DURATIONS_OUT", seen["env"])
        self.assertEqual(seen["env"]["QT_QPA_PLATFORM"], "offscreen")
        self.assertIn("test_price", seen["cmd"])

    def test_priority_is_lowered_only_when_asked_and_available(self):
        commands = []

        def capture(cmd, cwd, env, sink, timeout):
            commands.append(cmd)
            return 1

        with mock.patch.object(predict, "NICE", "/bin/echo"):
            self.run_with(capture)
            self.run_with(capture, nice=False)
        with mock.patch.object(predict, "NICE", "/nonexistent/nice"):
            self.run_with(capture)
        self.assertEqual(commands[0][:3], ["/bin/echo", "-n", "10"])
        self.assertNotIn("-n", commands[1][:2])
        self.assertNotIn("-n", commands[2][:2])


class JudgeFailures(SFCase):
    def test_missing_coverage_file_and_unreadable_data_are_unknown(self):
        planned = {"base": "base", "files": []}
        with tempfile.TemporaryDirectory() as directory:
            data = pathlib.Path(directory) / ".coverage"
            self.assertEqual(predict.judge(ROOT, planned, data, 1.0, "")["status"], "unknown")
            data.write_bytes(b"x")
            with mock.patch.object(predict.change_gate, "inspect", side_effect=ValueError("broken")):
                self.assertEqual(predict.judge(ROOT, planned, data, 1.0, "")["status"], "unknown")


class Spawn(SFCase):
    def test_returns_the_exit_code_and_the_output(self):
        import os
        with tempfile.TemporaryFile("w+") as sink:
            code = predict.spawn([sys.executable, "-c", "print('hello'); raise SystemExit(3)"],
                                 str(ROOT), dict(os.environ), sink, 30)
            sink.seek(0)
            text = sink.read()
        self.assertEqual(code, 3)
        self.assertIn("hello", text)

    def test_a_hung_child_is_stopped_and_reported_as_timeout(self):
        import os
        with tempfile.TemporaryFile("w+") as sink:
            with self.assertRaises(TimeoutError):
                predict.spawn([sys.executable, "-c", "import time; time.sleep(60)"],
                              str(ROOT), dict(os.environ), sink, 0.5)


class PrintBranches(SFCase):
    def render(self, status, **result):
        planned = {"base": "base", "modules": [], "reason": "why"}
        output = io.StringIO()
        with redirect_stdout(output):
            predict.print_result(planned, dict(result, status=status))
        return output.getvalue()

    def test_every_status_has_its_own_honest_wording(self):
        self.assertIn("price.py: 2,3", self.render("predicted_miss", missing={"price.py": [2, 3]}))
        self.assertIn("FAIL: x", self.render("tests_failed", log="FAIL: x"))
        self.assertIn("范围过大", self.render("too_broad"))
        self.assertIn("只能部分预判", self.render("partial"))
        self.assertIn("不算覆盖率通过", self.render("not_applicable"))
        self.assertIn("未运行", self.render("planned"))
        self.assertIn("无法给出完整结论", self.render("unknown", log="boom"))
        self.assertIn("子集耗时: 2.5s", self.render("unknown", elapsed_seconds=2.5, log="x"))


class CommandLine(SFCase):
    PLANNED = {"status": "planned", "base": "b", "head": "h", "modules": ["test_x"], "files": [],
               "reason": ""}

    def invoke(self, argv, planned=None, ran=None, plan_error=None):
        output, errors = io.StringIO(), io.StringIO()
        plan_patch = mock.patch.object(predict, "plan", return_value=planned or self.PLANNED,
                                       side_effect=plan_error)
        run_patch = mock.patch.object(predict, "run_plan", return_value=ran or {
            "status": "pass", "elapsed_seconds": 1.0, "executable": 2, "log": ""})
        with plan_patch as plan_mock, run_patch as run_mock, redirect_stdout(output), \
                mock.patch("sys.stderr", errors):
            code = predict.main(argv)
        return code, output.getvalue(), errors.getvalue(), plan_mock, run_mock

    def test_list_mode_never_runs_the_subset(self):
        code, out, _err, _plan, run_mock = self.invoke(["--list"])
        self.assertEqual(code, 0)
        run_mock.assert_not_called()
        self.assertIn("未运行", out)

    def test_normal_run_returns_the_verdict_exit_code(self):
        code, _out, _err, _plan, run_mock = self.invoke([])
        self.assertEqual(code, 0)
        run_mock.assert_called_once()
        miss = {"status": "predicted_miss", "elapsed_seconds": 1.0, "missing": {"a.py": [1]}, "log": ""}
        self.assertEqual(self.invoke([], ran=miss)[0], 1)
        unknown = {"status": "unknown", "elapsed_seconds": 1.0, "log": "x"}
        self.assertEqual(self.invoke([], ran=unknown)[0], 3)

    def test_path_flags_become_the_plan_scope(self):
        _code, _out, _err, plan_mock, _run = self.invoke(["--path", "a.py", "--path", "b.py"])
        self.assertEqual(plan_mock.call_args.kwargs["only"], {"a.py", "b.py"})
        _code, _out, _err, plan_mock, _run = self.invoke([])
        self.assertIsNone(plan_mock.call_args.kwargs["only"])

    def test_no_nice_flag_reaches_the_runner(self):
        _code, _out, _err, _plan, run_mock = self.invoke(["--no-nice", "--timeout", "7"])
        self.assertIs(run_mock.call_args.kwargs["nice"], False)
        self.assertEqual(run_mock.call_args.args[2], 7.0)

    def test_json_out_is_written_through_the_bounded_writer(self):
        with mock.patch.object(predict, "write_json") as writer:
            self.invoke(["--json-out", "build/p.json"])
        self.assertEqual(writer.call_args.args[0], "build/p.json")
        self.assertEqual(writer.call_args.args[1]["status"], "pass")

    def test_a_bad_base_is_a_clear_error_not_a_traceback(self):
        code, _out, err, _plan, _run = self.invoke(["--base", "nope"], plan_error=ValueError("base 不存在"))
        self.assertEqual(code, 2)
        self.assertIn("base 不存在", err)

    def test_non_positive_timeout_and_mixed_self_test_arguments_are_rejected(self):
        for argv in (["--timeout", "0"], ["--self-test", "--list"], ["--self-test", "--no-nice"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    predict.main(argv)
                self.assertEqual(caught.exception.code, 2)

    def test_the_built_in_self_test_passes_in_a_scratch_repository(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(predict.main(["--self-test"]), 0)
        self.assertIn("self-test passed", output.getvalue())

    def test_running_the_file_as_a_script_exits_with_the_verdict_code(self):
        script = ROOT / "tools" / "change_predict.py"
        output = io.StringIO()
        with mock.patch.object(sys, "argv", [str(script), "--self-test"]), redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(script), run_name="__main__")
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("self-test passed", output.getvalue())


class SelfTestHasTeeth(SFCase):
    GOOD = {"status": "planned", "modules": ["test_probe"], "files": []}

    def run_with(self, plans, outcome_code=3):
        errors = io.StringIO()
        with mock.patch.object(predict, "plan", side_effect=plans), \
                mock.patch.object(predict, "outcome", return_value={"exit_code": outcome_code}), \
                mock.patch("sys.stderr", errors), redirect_stdout(io.StringIO()):
            return predict.self_test(), errors.getvalue()

    def test_a_mapping_that_stops_working_is_caught(self):
        code, err = self.run_with([{"status": "unknown", "modules": [], "files": []}])
        self.assertEqual(code, 1)
        self.assertIn("not mapped", err)

    def test_an_unmapped_file_that_discards_the_mapped_ones_is_caught(self):
        code, err = self.run_with([self.GOOD, {"status": "partial", "modules": [], "files": []}])
        self.assertEqual(code, 1)
        self.assertIn("discard", err)

    def test_a_partial_prediction_that_exits_green_is_caught(self):
        code, err = self.run_with([self.GOOD, self.GOOD], outcome_code=0)
        self.assertEqual(code, 1)
        self.assertIn("partial prediction was allowed", err)


if __name__ == "__main__":
    unittest.main()
