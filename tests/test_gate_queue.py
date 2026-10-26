"""排队提交工具：锁判定、票据排队、预演、重试边界与时间线。

锁与票据用临时目录里的符号链接/文件构造，时钟与睡眠用假的；整个文件不起回归、不碰真实锁，
也不依赖本机此刻有没有别的会话在跑关卡。
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import runpy
import subprocess
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

import gate_queue as gq


def started(_pid):
    return "started"


def unknown(_pid):
    return ""


def forbidden(*_args, **_kwargs):
    raise AssertionError("this step must not run here")


def dead_pid():
    child = subprocess.Popen(["/usr/bin/true"])
    child.wait(timeout=10)
    return child.pid


class FakeClock:
    """睡眠即前进：等待循环观察到的节奏由代码自己算出来，不靠真睡眠。"""

    def __init__(self):
        self.reading = 1000.0
        self.slept = []

    def __call__(self):
        return self.reading

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.reading += seconds

class LockReading(SFCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="sf_gq_test_")
        self.addCleanup(temp.cleanup)
        self.tmp = pathlib.Path(temp.name)
        (self.tmp / "build").mkdir()

    def test_lock_paths_prefer_the_argument_then_tmpdir_then_slash_tmp(self):
        self.assertEqual(gq.lock_paths(self.tmp, "/x")[0][1], pathlib.Path("/x/sf_run_tests.lock"))
        with mock.patch.dict(os.environ, {"TMPDIR": "/y"}):
            self.assertEqual(gq.lock_paths(self.tmp)[0][1], pathlib.Path("/y/sf_run_tests.lock"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gq.lock_paths(self.tmp)[0][1], pathlib.Path("/tmp/sf_run_tests.lock"))
        self.assertEqual(gq.lock_paths(self.tmp, "/x")[1][1], self.tmp / "build" / ".run_tests.tree.lock")

    def test_process_start_reads_a_live_pid_and_gives_nothing_for_a_dead_one(self):
        self.assertNotEqual(gq.process_start(os.getpid()), "")
        self.assertEqual(gq.process_start(dead_pid()), "")

    def test_process_start_gives_nothing_when_ps_cannot_run(self):
        with mock.patch.object(gq.subprocess, "run", side_effect=OSError("no ps")):
            self.assertEqual(gq.process_start(1), "")

    def test_malformed_holders_are_never_alive(self):
        for record in ("", "x|a|b", "0|a|b", "-3|a|b", "٣|a|b"):
            with self.subTest(record=record):
                self.assertFalse(gq.holder_alive(record, started))

    def test_start_time_decides_when_both_sides_are_known(self):
        self.assertTrue(gq.holder_alive("42|started|/w", started))
        self.assertFalse(gq.holder_alive("42|other|/w", started))

    def test_unknown_start_time_falls_back_to_whether_the_process_exists(self):
        me = "%d||/w" % os.getpid()
        self.assertTrue(gq.holder_alive(me, unknown))
        self.assertFalse(gq.holder_alive("%d||/w" % dead_pid(), unknown))
        self.assertFalse(gq.holder_alive("99999999999999999999||/w", unknown))
        with mock.patch.object(gq.os, "kill", side_effect=PermissionError):
            self.assertTrue(gq.holder_alive("42||/w", unknown))

    def test_no_lock_means_no_holders(self):
        self.assertEqual(gq.holders(self.tmp, str(self.tmp)), [])

    def test_live_stale_and_unreadable_locks_are_each_reported(self):
        shared = self.tmp / "sf_run_tests.lock"
        shared.symlink_to("%d|started|/w" % os.getpid())
        tree = self.tmp / "build" / ".run_tests.tree.lock"
        tree.symlink_to("%d|old|/w" % os.getpid())
        found = gq.holders(self.tmp, str(self.tmp), started)
        self.assertEqual([item["alive"] for item in found], [True, False])
        tree.unlink()
        tree.write_text("not a link", encoding="utf-8")
        found = gq.holders(self.tmp, str(self.tmp), started)
        self.assertEqual(found[1], {"label": "本棵工作树的 build/", "record": "", "alive": True})

    def test_descriptions_never_hide_that_a_holder_exists(self):
        self.assertIn("读不出持有者", gq.describe({"label": "L", "record": "", "alive": True}))
        full = gq.describe({"label": "L", "record": "7|Mon 10|/dir", "alive": True})
        self.assertIn("进程 7", full)
        self.assertIn("Mon 10", full)
        self.assertIn("/dir", full)
        bare = gq.describe({"label": "L", "record": "7", "alive": True})
        self.assertIn("启动于 ?", bare)
        self.assertIn("目录 ?", bare)

class Tickets(SFCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="sf_gq_tickets_")
        self.addCleanup(temp.cleanup)
        self.tmp = temp.name
        self.ticket = gq.take_ticket(self.tmp, started)
        self.addCleanup(gq.release_ticket, self.ticket)
        self.serial = int(self.ticket.name.split("-", 1)[0])

    def sibling(self, offset, text):
        path = self.ticket.parent / ("%020d-x" % (self.serial + offset))
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_ticket_records_its_owner_and_can_be_released_twice(self):
        self.assertTrue(self.ticket.read_text(encoding="utf-8").startswith("%d|started" % os.getpid()))
        gq.release_ticket(self.ticket)
        gq.release_ticket(self.ticket)
        self.assertFalse(self.ticket.exists())

    def test_only_live_tickets_ahead_of_us_block(self):
        self.sibling(-3, "42|started|/w\n")
        self.sibling(+3, "43|started|/w\n")
        self.assertEqual(len(gq.earlier_tickets(self.ticket, started)), 1)

    def test_stale_tickets_are_retired_and_hidden_files_ignored(self):
        stale = self.sibling(-5, "42|old|/w\n")
        (self.ticket.parent / ".new-1").write_text("42|started|/w\n", encoding="utf-8")
        self.assertEqual(gq.earlier_tickets(self.ticket, started), [])
        self.assertFalse(stale.exists())

    def test_an_unreadable_ticket_is_skipped_not_fatal(self):
        (self.ticket.parent / ("%020d-dir" % (self.serial - 1))).mkdir()
        self.assertEqual(gq.earlier_tickets(self.ticket, started), [])

    def test_blockers_combine_lock_holders_and_queued_tickets(self):
        self.sibling(-2, "42|started|/w\n")
        root = pathlib.Path(self.tmp) / "tree"
        (root / "build").mkdir(parents=True)
        (pathlib.Path(self.tmp) / "sf_run_tests.lock").symlink_to("%d|started|/w" % os.getpid())
        found = gq.blockers(root, self.ticket, self.tmp, started)
        self.assertEqual(len(found), 2)
        self.assertEqual(gq.blockers(root, None, self.tmp, started), found[:1])


class Waiting(SFCase):
    def run_wait(self, answers, timeout=60.0):
        clock, said = FakeClock(), []
        with mock.patch.object(gq, "blockers", side_effect=list(answers)):
            result = gq.wait_turn("root", timeout, clock=clock, sleep=clock.sleep, say=said.append)
        return result, clock, said

    def test_nobody_in_the_way_returns_at_once(self):
        result, clock, said = self.run_wait([[]])
        self.assertEqual((result, clock.slept, said), (0.0, [], []))

    def test_progress_is_reported_only_when_the_blockers_change(self):
        result, clock, said = self.run_wait([["a"], ["a"], ["b"], []])
        self.assertEqual(clock.slept, [gq.POLL_SECONDS] * 3)
        self.assertEqual(said, ["等待：a", "等待：b"])
        self.assertEqual(result, gq.POLL_SECONDS * 3)

    def test_timeout_names_who_is_still_in_the_way(self):
        clock = FakeClock()
        with mock.patch.object(gq, "blockers", return_value=["持有者甲"]):
            with self.assertRaises(TimeoutError) as caught:
                gq.wait_turn("root", 5.0, clock=clock, sleep=clock.sleep, say=lambda _text: None)
        self.assertIn("持有者甲", str(caught.exception))

class FreezeAndPrework(SFCase):
    GREEN = [{"name": "静态闸门", "script": "s", "returncode": 0, "tail": ""}]

    def test_digest_of_a_missing_file_is_a_marker_not_a_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "a.txt"
            self.assertEqual(gq.digest(target), "missing")
            target.write_text("x", encoding="utf-8")
            self.assertEqual(len(gq.digest(target)), 64)

    def test_freeze_captures_head_and_every_path(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(gq.wrapup, "git_out", return_value="head1"):
            (pathlib.Path(directory) / "a.txt").write_text("x", encoding="utf-8")
            value = gq.freeze(directory, ["a.txt", "gone.txt"])
        self.assertEqual(value["head"], "head1")
        self.assertEqual(value["files"]["gone.txt"], "missing")
        self.assertEqual(len(value["files"]["a.txt"]), 64)

    def test_cheap_checks_run_each_script_with_an_isolated_log_and_capped_output(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            code = 1 if cmd[-1].endswith("shell_cjk_scan.py") else 0
            return mock.Mock(returncode=code, stdout="o" * 900, stderr="e" * 900)

        results = gq.cheap_checks("/some/root", run=fake_run)
        self.assertEqual([item["script"] for item in results], [script for _label, script in gq.CHEAP_CHECKS])
        self.assertEqual([item["returncode"] for item in results], [0, 0, 1, 0])
        self.assertTrue(all(len(item["tail"]) == 400 for item in results))
        self.assertTrue(all(kwargs["cwd"] == "/some/root" for _cmd, kwargs in calls))
        self.assertNotEqual(calls[0][1]["env"]["SF_LOG_PATH"], os.environ.get("SF_LOG_PATH"))

    def run_prework(self, checks=None, outcome=None, do_predict=True, error=None):
        planned = {"status": "planned"}
        with mock.patch.object(gq.predict, "plan", return_value=planned, side_effect=error) as plan, \
                mock.patch.object(gq.predict, "run_plan", return_value=outcome or {"status": "pass"}):
            result = gq.prework("root", ["a.py"], "head", do_predict, 5.0, checks=lambda _root: checks or self.GREEN)
        return result, plan

    def test_clean_checks_and_a_clean_prediction_raise_no_problem_and_no_note(self):
        result, plan = self.run_prework()
        self.assertEqual((result["problems"], result["notes"], result["predict_status"]), ([], [], "pass"))
        self.assertEqual(plan.call_args.kwargs["only"], {"a.py"})

    def test_not_applicable_is_silent_too(self):
        result, _plan = self.run_prework(outcome={"status": "not_applicable"})
        self.assertEqual((result["problems"], result["notes"]), ([], []))

    def test_a_failed_cheap_check_is_a_problem_naming_check_and_code(self):
        bad = [{"name": "静态闸门", "script": "s", "returncode": 1, "tail": "boom"}]
        result, _plan = self.run_prework(checks=bad)
        self.assertIn("静态闸门未通过（退出码 1）：boom", result["problems"][0])

    def test_predicted_miss_and_failed_subset_are_problems(self):
        for status in ("predicted_miss", "tests_failed"):
            with self.subTest(status=status):
                result, _plan = self.run_prework(outcome={"status": status, "missing": {"a.py": [3]}})
                self.assertIn(status, result["problems"][0])

    def test_cannot_conclude_is_a_note_not_a_problem(self):
        result, _plan = self.run_prework(outcome={"status": "partial", "log": ""})
        self.assertEqual(result["problems"], [])
        self.assertIn("无法下结论（partial）：见预判输出", result["notes"][0])

    def test_a_hung_cheap_check_is_a_red_check_not_a_usage_error(self):
        def hangs(cmd, **kwargs):
            if cmd[-1].endswith("shell_cjk_scan.py"):
                raise subprocess.TimeoutExpired(cmd, 300)
            return mock.Mock(returncode=0, stdout="", stderr="")

        results = gq.cheap_checks("/some/root", run=hangs)
        hung = [item for item in results if item["returncode"] == 124]
        self.assertEqual([item["script"] for item in hung], ["tools/shell_cjk_scan.py"])
        self.assertIn("未结束", hung[0]["tail"])
        self.assertEqual(len(results), len(gq.CHEAP_CHECKS))

    def test_prework_reports_a_hung_check_as_a_problem_with_code_124(self):
        hung = [{"name": "shell 中文标点", "script": "s", "returncode": 124, "tail": "超过 300s 仍未结束"}]
        result, _plan = self.run_prework(checks=hung)
        self.assertIn("退出码 124", result["problems"][0])

    def test_prediction_that_cannot_even_start_is_a_note(self):
        result, _plan = self.run_prework(error=OSError("no space"))
        self.assertEqual(result["predict_status"], "unknown")
        self.assertIn("预判未能执行", result["notes"][0])

    def test_skipping_prediction_never_calls_the_planner(self):
        result, plan = self.run_prework(do_predict=False)
        self.assertEqual(result["predict_status"], "skipped")
        plan.assert_not_called()


class Support(SFCase):
    def test_stop_carries_its_exit_code(self):
        self.assertEqual(gq.Stop(4, "why").code, 4)
        self.assertEqual(str(gq.Stop(4, "why")), "why")

    def test_stamp_is_utc_iso(self):
        self.assertTrue(gq.stamp().endswith("+00:00"))

    def test_write_atomic_creates_directories_replaces_and_leaves_no_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "nested" / "t.json"
            gq.write_atomic(target, {"文": 1})
            gq.write_atomic(target, {"文": 2})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"文": 2})
            self.assertEqual([p.name for p in target.parent.iterdir()], ["t.json"])

    def test_timeline_records_wall_time_and_seconds_since_start(self):
        clock = FakeClock()
        timeline = gq.Timeline(clock)
        clock.reading += 2.5
        timeline.mark("turn", attempt=1)
        self.assertEqual(timeline.events[0]["t"], 2.5)
        self.assertEqual(timeline.events[0]["attempt"], 1)
        self.assertIn("at", timeline.events[0])

    def test_save_timeline_is_a_no_op_without_a_path_and_writes_with_one(self):
        timeline = gq.Timeline(FakeClock())
        timeline.mark("x")
        gq.save_timeline(None, timeline, {"head": "h"})
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "t.json"
            gq.save_timeline(target, timeline, {"head": "h"})
            written = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual((written["frozen"], written["events"][0]["event"]), ({"head": "h"}, "x"))

    def test_default_dependencies_are_the_real_functions(self):
        deps = gq.default_deps()
        self.assertIs(deps["commit"], gq.wrapup.commit)
        self.assertIs(deps["wait"], gq.wait_turn)
        self.assertIs(deps["take_ticket"], gq.take_ticket)
        self.assertIs(deps["prework"], gq.prework)
        self.assertIs(deps["ready"], gq.wrapup.check_ready)
        self.assertIs(deps["merge"], gq.wrapup.merge_commit)
        self.assertIs(deps["merge_ready"], gq.wrapup.check_merge_ready)
        self.assertIs(deps["merge_state"], gq.wrapup.merge_state)

class QueueCase(SFCase):
    """排队提交的编排：所有外部动作（预检、领票、等锁、提交）都换成记录型替身。"""

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="sf_gq_queue_")
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name).resolve()
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.message = self.root / "msg.txt"
        self.message.write_text("feat: x\n", encoding="utf-8")
        self.heads = ["head1"]
        patcher = mock.patch.object(gq.wrapup, "git_out", side_effect=lambda *a, **k: self.heads[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tickets = self.root / "tickets"
        self.tickets.mkdir()
        self.commits, self.said, self.readies = [], [], []

    def deps(self, **over):
        def commit(root, paths, message, author, expected, apply, **kwargs):
            self.commits.append((expected, apply, kwargs))
            return {"commit": "abc123"}

        def ready(root, paths, message, author, expected):
            self.readies.append((list(paths), expected))
            return {}

        def take():
            return gq.take_ticket(str(self.tickets), started)

        base = {"commit": commit, "ready": ready, "take_ticket": take, "say": self.said.append,
                "clock": FakeClock(),
                "prework": lambda *a, **k: {"predict_status": "pass", "problems": [], "notes": []},
                "wait": lambda *a, **k: 0.5}
        base.update(over)
        return base

    def run_queue(self, apply=True, expected_head=None, no_predict=False, timeline=None, **over):
        return gq.run_commit_queue(self.root, ["a.py"], self.message, None, expected_head, apply, 60.0, 900.0,
                                   30.0, no_predict, self.root / "hook.log", timeline, self.deps(**over))

    def stop_code(self, **kwargs):
        with self.assertRaises(gq.Stop) as caught:
            self.run_queue(**kwargs)
        return caught.exception

    def leftover_tickets(self):
        return [p for p in (self.tickets / "sf_gate_queue").glob("*")]


class QueueHappyPath(QueueCase):
    def test_dry_run_validates_the_commit_but_runs_nothing_else(self):
        result = self.run_queue(apply=False, prework=forbidden, take_ticket=forbidden, wait=forbidden)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(self.commits, [("head1", False, {})])

    def test_a_normal_run_commits_once_through_the_real_hook_wrapper(self):
        result = self.run_queue()
        self.assertEqual((result["status"], result["result"]["commit"]), ("committed", "abc123"))
        self.assertEqual(self.commits, [("head1", True, {"timeout": 900.0, "log": self.root / "hook.log"})])
        self.assertEqual(self.leftover_tickets(), [])
        events = [item["event"] for item in result["timeline"]]
        self.assertEqual(events, ["frozen", "prework", "ticket", "turn", "commit_started", "committed"])

    def test_a_matching_expected_head_is_handed_to_the_commit(self):
        self.run_queue(expected_head="head1")
        self.assertEqual(self.commits[0][0], "head1")

    def test_no_predict_flag_turns_the_prediction_off(self):
        seen = {}

        def prework(root, paths, head, do_predict, timeout):
            seen.update(do_predict=do_predict, timeout=timeout)
            return {"predict_status": "skipped", "problems": [], "notes": []}

        self.run_queue(no_predict=True, prework=prework)
        self.assertEqual(seen, {"do_predict": False, "timeout": 30.0})

    def test_notes_are_shown_but_do_not_stop_the_run(self):
        notes = {"predict_status": "unknown", "problems": [], "notes": ["无法预判"]}
        self.run_queue(prework=lambda *a, **k: notes)
        self.assertIn("提示：无法预判", self.said)

    def test_the_timeline_is_written_even_on_success(self):
        target = self.root / "t.json"
        self.run_queue(timeline=target)
        written = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(written["events"][-1]["event"], "committed")

class QueueRefusals(QueueCase):
    def events_of(self, path):
        return [item["event"] for item in json.loads(path.read_text(encoding="utf-8"))["events"]]

    def test_a_mismatching_expected_head_refuses_before_any_queueing(self):
        target = self.root / "t.json"
        stop = self.stop_code(expected_head="other", timeline=target, prework=forbidden, take_ticket=forbidden)
        self.assertEqual(stop.code, gq.EXIT_CHANGED)
        self.assertEqual(self.events_of(target), ["frozen", "expected_head_changed"])

    def test_a_missing_message_file_is_a_usage_error(self):
        self.message.unlink()
        self.assertEqual(self.stop_code().code, gq.EXIT_USAGE)

    def test_bad_paths_are_refused_by_the_shared_path_rule(self):
        with self.assertRaises(gq.wrapup.WrapupError):
            gq.run_commit_queue(self.root, ["/abs.py"], self.message, None, None, True, 60.0, 900.0, 30.0,
                                False, self.root / "hook.log", None, self.deps())

    def test_a_red_precheck_stops_before_a_ticket_is_ever_taken(self):
        target = self.root / "t.json"
        red = {"predict_status": "skipped", "problems": ["静态闸门未通过"], "notes": []}
        stop = self.stop_code(timeline=target, prework=lambda *a, **k: red, take_ticket=forbidden)
        self.assertEqual(stop.code, gq.EXIT_PRECHECK)
        self.assertIn("静态闸门未通过", str(stop))
        self.assertEqual(self.events_of(target)[-1], "prework_failed")
        self.assertEqual(self.commits, [])

    def test_a_wait_that_times_out_releases_the_ticket_and_commits_nothing(self):
        def too_long(*_args, **_kwargs):
            raise TimeoutError("等了 60s 仍被占着：甲")

        target = self.root / "t.json"
        stop = self.stop_code(timeline=target, wait=too_long)
        self.assertEqual(stop.code, gq.EXIT_TIMEOUT)
        self.assertEqual((self.commits, self.leftover_tickets()), ([], []))
        self.assertEqual(self.events_of(target)[-1], "timeout")

    def test_a_file_that_changes_while_queued_is_never_committed(self):
        def edit_then_arrive(*_args, **_kwargs):
            (self.root / "a.py").write_text("x = 2\n", encoding="utf-8")
            return 1.0

        stop = self.stop_code(wait=edit_then_arrive)
        self.assertEqual(stop.code, gq.EXIT_CHANGED)
        self.assertEqual((self.commits, self.leftover_tickets()), ([], []))

    def test_a_head_that_moves_while_queued_is_never_committed(self):
        def move_then_arrive(*_args, **_kwargs):
            self.heads[0] = "head2"
            return 1.0

        self.assertEqual(self.stop_code(wait=move_then_arrive).code, gq.EXIT_CHANGED)
        self.assertEqual(self.commits, [])


class QueueReadiness(QueueCase):
    """领票之前先问现场能不能提交：暂存区非空这类失败要立刻暴露，不是排完队才发现。"""

    def events_of(self, path):
        return [item["event"] for item in json.loads(path.read_text(encoding="utf-8"))["events"]]

    def not_ready(self, *_args):
        raise gq.wrapup.WrapupError("the index already holds staged changes; commit or unstage them first")

    def test_a_scene_that_cannot_commit_stops_before_checks_and_before_a_ticket(self):
        target = self.root / "t.json"
        stop = self.stop_code(timeline=target, ready=self.not_ready, prework=forbidden, take_ticket=forbidden,
                              wait=forbidden)
        self.assertEqual(stop.code, gq.EXIT_USAGE)
        self.assertIn("未排队", str(stop))
        self.assertIn("already holds staged changes", str(stop))
        self.assertEqual(self.events_of(target), ["frozen", "not_ready"])
        self.assertEqual((self.commits, self.leftover_tickets()), ([], []))

    def test_the_question_is_asked_with_the_paths_and_the_callers_expected_head(self):
        self.run_queue(expected_head="head1")
        self.assertEqual(self.readies, [(["a.py"], "head1")])
        self.readies.clear()
        self.run_queue()
        self.assertEqual(self.readies, [(["a.py"], None)])

    def test_it_is_asked_before_the_cheap_checks_so_a_stuck_index_costs_nothing(self):
        order = []
        self.run_queue(ready=lambda *a: order.append("ready") or {},
                       prework=lambda *a, **k: order.append("prework") or
                       {"predict_status": "pass", "problems": [], "notes": []})
        self.assertEqual(order, ["ready", "prework"])

    def test_a_dry_run_leaves_the_question_to_the_commit_function(self):
        result = self.run_queue(apply=False, ready=forbidden)
        self.assertEqual(result["status"], "dry_run")

    def test_a_mismatching_expected_head_is_refused_before_the_question(self):
        self.assertEqual(self.stop_code(expected_head="other", ready=forbidden).code, gq.EXIT_CHANGED)


class QueueRetries(QueueCase):
    def flaky(self, failures, error):
        state = {"n": 0}

        def commit(root, paths, message, author, expected, apply, **kwargs):
            state["n"] += 1
            self.commits.append(state["n"])
            if state["n"] <= failures:
                raise gq.wrapup.WrapupError(error)
            return {"commit": "abc123"}

        return commit

    def test_a_real_failure_stops_at_once_and_is_never_retried(self):
        stop = self.stop_code(commit=self.flaky(99, "git commit exited 1, nothing committed: 测试红"))
        self.assertEqual(stop.code, gq.EXIT_FAILED)
        self.assertIn("测试红", str(stop))
        self.assertEqual(self.commits, [1])

    def test_losing_the_lock_race_requeues_and_then_commits(self):
        target = self.root / "t.json"
        waits = []
        result = self.run_queue(timeline=target, commit=self.flaky(1, "..." + gq.LOST_MARKER + "占着共享临时目录"),
                                wait=lambda *a, **k: waits.append(1) or 0.5)
        self.assertEqual(result["status"], "committed")
        self.assertEqual((self.commits, len(waits)), ([1, 2], 2))
        self.assertTrue(any("输给另一轮" in line for line in self.said))
        failed = [e for e in json.loads(target.read_text(encoding="utf-8"))["events"] if e["event"] == "commit_failed"]
        self.assertTrue(failed[0]["lock_lost"])

    def test_requeueing_shares_one_wait_budget_instead_of_resetting_it(self):
        budgets = []

        def wait(root, timeout, **kwargs):
            budgets.append(timeout)
            if timeout <= 0:
                raise TimeoutError("等了 0s 仍被占着：甲")
            return min(timeout, 40.0)

        stop = self.stop_code(commit=self.flaky(99, gq.LOST_MARKER), wait=wait)
        self.assertEqual(budgets, [60.0, 20.0, 0.0])
        self.assertEqual(stop.code, gq.EXIT_TIMEOUT)
        self.assertIn("总等待预算 60s，已用 60s", str(stop))
        self.assertEqual(len(self.commits), 2)
        self.assertEqual(self.leftover_tickets(), [])

    def test_the_budget_never_goes_negative_when_a_wait_overshoots(self):
        budgets = []

        def wait(root, timeout, **kwargs):
            budgets.append(timeout)
            return 90.0

        with self.assertRaises(gq.Stop):
            self.run_queue(commit=self.flaky(99, gq.LOST_MARKER), wait=wait, expected_head=None)
        self.assertEqual(budgets[:3], [60.0, 0.0, 0.0])

    def test_progress_is_announced_before_and_after_each_phase(self):
        self.run_queue()
        text = "\n".join(self.said)
        self.assertIn("排队前检查开始", text)
        self.assertIn("排队前检查结束：预判 pass，问题 0 处", text)
        self.assertIn("已领票", text)
        self.assertIn("剩余等待预算 60s", text)

    def test_losing_every_time_gives_up_without_committing_anything(self):
        stop = self.stop_code(commit=self.flaky(99, gq.LOST_MARKER))
        self.assertEqual(stop.code, gq.EXIT_TIMEOUT)
        self.assertIn(str(gq.MAX_ATTEMPTS), str(stop))
        self.assertEqual(len(self.commits), gq.MAX_ATTEMPTS)
        self.assertEqual(self.leftover_tickets(), [])

GOOD_MERGE = {"head": "h1", "merge_heads": ["s1"], "tree": "t1", "unmerged": [], "worktree_clean": True}


class MergeQueueCase(SFCase):
    """合并提交的排队编排：读现场、就绪检查、领票、等锁、提交全部换成记录型替身。"""

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="sf_gq_mergeq_")
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name).resolve()
        self.message = self.root / "msg.txt"
        self.message.write_text("merge: x\n", encoding="utf-8")
        self.tickets = self.root / "tickets"
        self.tickets.mkdir()
        self.state = dict(GOOD_MERGE)
        self.merges, self.said, self.readies, self.preworks = [], [], [], []

    def deps(self, **over):
        def merge(root, message, author, expected_head, source, expected_tree, apply, **kwargs):
            self.merges.append((expected_head, source, expected_tree, apply, kwargs))
            return {"commit": "m123"}

        def ready(root, message, author, expected_head, source, expected_tree):
            self.readies.append((expected_head, source, expected_tree))
            return {}

        def prework(root, paths, head, do_predict, timeout):
            self.preworks.append((paths, head, do_predict))
            return {"predict_status": "skipped", "problems": [], "notes": []}

        def take():
            return gq.take_ticket(str(self.tickets), started)

        base = {"merge": merge, "merge_ready": ready, "prework": prework, "take_ticket": take,
                "merge_state": lambda _root: dict(self.state), "say": self.said.append, "clock": FakeClock(),
                "wait": lambda *a, **k: 0.5}
        base.update(over)
        return base

    def run_queue(self, apply=True, timeline=None, expected_head="h1", source="s1", expected_tree="t1", **over):
        return gq.run_merge_queue(self.root, self.message, None, expected_head, source, expected_tree, apply, 60.0,
                                  900.0, self.root / "hook.log", timeline, self.deps(**over))

    def stop_code(self, **kwargs):
        with self.assertRaises(gq.Stop) as caught:
            self.run_queue(**kwargs)
        return caught.exception

    def events_of(self, path):
        return [item["event"] for item in json.loads(path.read_text(encoding="utf-8"))["events"]]

    def leftover_tickets(self):
        return list((self.tickets / "sf_gate_queue").glob("*"))


class MergeQueueHappyPath(MergeQueueCase):
    def test_a_normal_run_commits_the_reviewed_merge_once(self):
        result = self.run_queue()
        self.assertEqual((result["status"], result["result"]["commit"]), ("committed", "m123"))
        self.assertEqual(self.merges, [("h1", "s1", "t1", True, {"timeout": 900.0, "log": self.root / "hook.log"})])
        events = [item["event"] for item in result["timeline"]]
        self.assertEqual(events, ["frozen", "prework", "ticket", "turn", "commit_started", "committed"])
        self.assertEqual(self.leftover_tickets(), [])

    def test_the_cheap_checks_run_but_the_predictor_does_not(self):
        self.run_queue()
        self.assertEqual(self.preworks, [([], "h1", False)])

    def test_readiness_is_asked_with_the_reviewed_values_before_any_ticket(self):
        self.run_queue()
        self.assertEqual(self.readies, [("h1", "s1", "t1")])

    def test_a_dry_run_validates_the_merge_but_runs_nothing_else(self):
        target = self.root / "t.json"
        result = self.run_queue(apply=False, timeline=target, prework=forbidden, take_ticket=forbidden,
                                wait=forbidden, merge_ready=forbidden)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(self.merges, [("h1", "s1", "t1", False, {})])
        self.assertEqual(self.events_of(target), ["frozen", "dry_run"])

    def test_the_timeline_is_written_even_on_success(self):
        target = self.root / "t.json"
        self.run_queue(timeline=target)
        written = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(written["events"][-1]["event"], "committed")
        self.assertEqual(written["frozen"]["merge_heads"], ["s1"])


class MergeQueueRefusals(MergeQueueCase):
    def test_a_scene_that_differs_from_the_review_is_refused_before_any_queueing(self):
        cases = (("head", dict(expected_head="other")), ("merge_heads", dict(source="other")),
                 ("tree", dict(expected_tree="other")))
        for key, override in cases:
            with self.subTest(key=key):
                target = self.root / ("t-%s.json" % key)
                stop = self.stop_code(timeline=target, prework=forbidden, take_ticket=forbidden, **override)
                self.assertEqual(stop.code, gq.EXIT_CHANGED)
                self.assertIn(key, str(stop))
                self.assertEqual(self.events_of(target), ["frozen", "expected_changed"])
        self.assertEqual(self.merges, [])

    def test_no_merge_in_progress_counts_as_a_changed_scene(self):
        self.state["merge_heads"] = []
        self.assertEqual(self.stop_code(prework=forbidden).code, gq.EXIT_CHANGED)

    def test_a_missing_message_file_is_a_usage_error(self):
        self.message.unlink()
        self.assertEqual(self.stop_code().code, gq.EXIT_USAGE)

    def test_a_merge_that_cannot_be_committed_stops_before_checks_and_before_a_ticket(self):
        def refuse(*_args):
            raise gq.wrapup.WrapupError("unresolved conflicts: base.txt")

        target = self.root / "t.json"
        stop = self.stop_code(timeline=target, merge_ready=refuse, prework=forbidden, take_ticket=forbidden)
        self.assertEqual(stop.code, gq.EXIT_USAGE)
        self.assertIn("unresolved conflicts: base.txt", str(stop))
        self.assertEqual(self.events_of(target), ["frozen", "not_ready"])
        self.assertEqual(self.merges, [])

    def test_a_red_precheck_stops_before_a_ticket_is_ever_taken(self):
        red = {"predict_status": "skipped", "problems": ["静态闸门未通过"], "notes": []}
        stop = self.stop_code(prework=lambda *a, **k: red, take_ticket=forbidden)
        self.assertEqual(stop.code, gq.EXIT_PRECHECK)
        self.assertIn("静态闸门未通过", str(stop))
        self.assertEqual(self.merges, [])

    def test_anything_the_merge_state_reports_changing_while_queued_is_never_committed(self):
        for key, value in (("tree", "t2"), ("head", "h2"), ("worktree_clean", False), ("unmerged", ["a.py"]),
                           ("merge_heads", ["s1", "s2"])):
            with self.subTest(key=key):
                self.state = dict(GOOD_MERGE)

                def change_then_arrive(*_args, _key=key, _value=value, **_kwargs):
                    self.state[_key] = _value
                    return 1.0

                self.assertEqual(self.stop_code(wait=change_then_arrive).code, gq.EXIT_CHANGED)
                self.assertEqual((self.merges, self.leftover_tickets()), ([], []))

    def test_a_wait_that_times_out_releases_the_ticket_and_commits_nothing(self):
        def too_long(*_args, **_kwargs):
            raise TimeoutError("等了 60s 仍被占着：甲")

        target = self.root / "t.json"
        stop = self.stop_code(timeline=target, wait=too_long)
        self.assertEqual(stop.code, gq.EXIT_TIMEOUT)
        self.assertEqual((self.merges, self.leftover_tickets()), ([], []))
        self.assertEqual(self.events_of(target)[-1], "timeout")


class MergeQueueRetries(MergeQueueCase):
    def failing_merge(self, failures, error):
        state = {"n": 0}

        def merge(root, message, author, expected_head, source, expected_tree, apply, **kwargs):
            state["n"] += 1
            self.merges.append(state["n"])
            if state["n"] <= failures:
                raise gq.wrapup.WrapupError(error)
            return {"commit": "m123"}

        return merge

    def test_losing_the_lock_race_requeues_the_same_merge_and_then_commits(self):
        result = self.run_queue(merge=self.failing_merge(1, "git commit exited 1\n" + gq.LOST_MARKER))
        self.assertEqual(result["result"]["commit"], "m123")
        self.assertEqual(self.merges, [1, 2])
        self.assertIn("刚好输给另一轮完整回归，保留输入不动，重新排队", self.said)
        self.assertEqual(self.leftover_tickets(), [])

    def test_a_real_failure_stops_at_once_and_is_never_retried(self):
        stop = self.stop_code(merge=self.failing_merge(5, "git commit exited 1\n静态闸门红"))
        self.assertEqual(stop.code, gq.EXIT_FAILED)
        self.assertEqual(self.merges, [1])
        self.assertIn("静态闸门红", str(stop))

    def test_losing_every_time_gives_up_without_committing_anything(self):
        stop = self.stop_code(merge=self.failing_merge(99, gq.LOST_MARKER))
        self.assertEqual(stop.code, gq.EXIT_TIMEOUT)
        self.assertEqual(len(self.merges), gq.MAX_ATTEMPTS)


class Commands(SFCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="sf_gq_commands_")
        self.addCleanup(temp.cleanup)
        self.tmp = pathlib.Path(temp.name)
        (self.tmp / "build").mkdir()

    def status(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = gq.command_status(self.tmp, str(self.tmp))
        return code, out.getvalue()

    def test_status_says_so_when_the_lock_is_free(self):
        code, text = self.status()
        self.assertEqual(code, gq.EXIT_OK)
        self.assertIn("空闲", text)

    def test_status_reports_a_live_holder_as_occupied_and_exits_nonzero(self):
        record = "%d|%s|/w" % (os.getpid(), gq.process_start(os.getpid()))
        (self.tmp / "sf_run_tests.lock").symlink_to(record)
        code, text = self.status()
        self.assertEqual(code, gq.EXIT_TIMEOUT)
        self.assertIn("占用：", text)

    def test_status_reports_a_stale_holder_but_exits_zero(self):
        (self.tmp / "sf_run_tests.lock").symlink_to("%d|not the real start time|/w" % os.getpid())
        code, text = self.status()
        self.assertEqual(code, gq.EXIT_OK)
        self.assertIn("陈旧：", text)

    def test_wait_command_reports_success_and_timeout(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gq, "wait_turn", return_value=1.5), redirect_stdout(out):
            self.assertEqual(gq.command_wait(self.tmp, 5.0, str(self.tmp)), gq.EXIT_OK)
        self.assertIn("1.5s", out.getvalue())
        with mock.patch.object(gq, "wait_turn", side_effect=TimeoutError("等了 5s")), mock.patch("sys.stderr", err):
            self.assertEqual(gq.command_wait(self.tmp, 5.0, str(self.tmp)), gq.EXIT_TIMEOUT)
        self.assertIn("!! 等了 5s", err.getvalue())

class Main(SFCase):
    REPO = pathlib.Path("/repo")

    def invoke(self, argv, queue=None, queue_error=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gq.wrapup, "repo_root", return_value=self.REPO), \
                mock.patch.object(gq, "run_commit_queue", return_value=queue, side_effect=queue_error) as run, \
                redirect_stdout(out), mock.patch("sys.stderr", err):
            code = gq.main(argv)
        return code, out.getvalue(), err.getvalue(), run

    def test_self_test_runs_without_a_repository(self):
        code, out, _err, run = self.invoke(["self-test"])
        self.assertEqual(code, 0)
        self.assertIn("self-test passed", out)
        run.assert_not_called()

    def test_status_goes_through_the_real_status_command(self):
        with tempfile.TemporaryDirectory() as directory:
            code, out, _err, _run = self.invoke(["status", "--tmpdir", directory])
        self.assertEqual(code, 0)
        self.assertIn("空闲", out)

    def test_wait_passes_root_timeout_and_tmpdir_through(self):
        with mock.patch.object(gq, "command_wait", return_value=0) as wait:
            code, _out, _err, _run = self.invoke(["wait", "--timeout", "5", "--tmpdir", "/t"])
        self.assertEqual(code, 0)
        wait.assert_called_once_with(self.REPO, 5.0, "/t")

    def test_commit_hands_every_option_to_the_queue_and_prints_the_commit(self):
        done = {"status": "committed", "result": {"commit": "abc123"}}
        code, out, _err, run = self.invoke(["commit", "--path", "a.py", "--path", "b.py", "-m", "/tmp/m.txt",
                                            "--apply", "--no-predict", "--log", "/tmp/x.log",
                                            "--wait-timeout", "9", "--author", "N <n@x.y>"], queue=done)
        args = run.call_args.args
        self.assertEqual((code, args[1], args[2], args[3], args[5]), (0, ["a.py", "b.py"],
                                                                     pathlib.Path("/tmp/m.txt"), "N <n@x.y>", True))
        self.assertEqual((args[6], args[9], args[10]), (9.0, True, pathlib.Path("/tmp/x.log")))
        self.assertIn("提交: abc123", out)

    def test_applying_without_a_log_option_creates_a_private_log_file(self):
        done = {"status": "committed", "result": {"commit": "abc123"}}
        _code, _out, _err, run = self.invoke(["commit", "--path", "a.py", "-m", "/tmp/m.txt", "--apply"],
                                             queue=done)
        log = run.call_args.args[10]
        self.addCleanup(log.unlink)
        self.assertTrue(log.is_file())
        self.assertTrue(run.call_args.args[5])

    def test_a_dry_run_leaves_no_log_file_behind(self):
        done = {"status": "dry_run"}
        _code, out, _err, run = self.invoke(["commit", "--path", "a.py", "-m", "/tmp/m.txt"], queue=done)
        self.assertIsNone(run.call_args.args[10])
        self.assertFalse(run.call_args.args[5])
        self.assertIn("dry_run", out)

    def test_non_positive_timeouts_are_rejected_by_the_parser(self):
        for flag in ("--wait-timeout", "--hook-timeout", "--predict-timeout"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit) as caught:
                self.invoke(["commit", "--path", "a.py", "-m", "/tmp/m.txt", flag, "0"])
            self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(SystemExit) as caught:
            self.invoke(["wait", "--timeout", "0"])
        self.assertEqual(caught.exception.code, 2)

    MERGE_ARGV = ["merge-commit", "-m", "/tmp/m.txt", "--expected-head", "h1", "--source", "s1",
                  "--expected-tree", "t1"]

    def invoke_merge(self, extra=(), queue=None, queue_error=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gq.wrapup, "repo_root", return_value=self.REPO), \
                mock.patch.object(gq, "run_merge_queue", return_value=queue, side_effect=queue_error) as run, \
                redirect_stdout(out), mock.patch("sys.stderr", err):
            code = gq.main(self.MERGE_ARGV + list(extra))
        return code, out.getvalue(), err.getvalue(), run

    def test_merge_commit_hands_every_option_to_the_merge_queue_and_prints_the_commit(self):
        done = {"status": "committed", "result": {"commit": "m123"}}
        code, out, _err, run = self.invoke_merge(["--apply", "--log", "/tmp/x.log", "--wait-timeout", "9",
                                                  "--hook-timeout", "7", "--author", "N <n@x.y>"], queue=done)
        root, message, author, head, source, tree, apply, wait, hook, log, timeline = run.call_args.args
        self.assertEqual((code, root, message, author), (0, self.REPO, pathlib.Path("/tmp/m.txt"), "N <n@x.y>"))
        self.assertEqual((head, source, tree, apply), ("h1", "s1", "t1", True))
        self.assertEqual((wait, hook, log, timeline), (9.0, 7.0, pathlib.Path("/tmp/x.log"), None))
        self.assertIn("提交: m123", out)

    def test_the_reviewed_values_are_required_for_a_merge(self):
        for drop in ("--expected-head", "--source", "--expected-tree"):
            argv = list(self.MERGE_ARGV)
            at = argv.index(drop)
            del argv[at:at + 2]
            with self.subTest(drop=drop), self.assertRaises(SystemExit) as caught, \
                    mock.patch("sys.stderr", io.StringIO()):
                gq.main(argv)
            self.assertEqual(caught.exception.code, 2)

    def test_applying_a_merge_without_a_log_option_creates_a_private_log_file(self):
        _code, _out, _err, run = self.invoke_merge(["--apply"], queue={"status": "committed",
                                                                       "result": {"commit": "m123"}})
        log = run.call_args.args[9]
        self.addCleanup(log.unlink)
        self.assertTrue(log.is_file())

    def test_a_merge_dry_run_leaves_no_log_file_behind(self):
        _code, out, _err, run = self.invoke_merge(queue={"status": "dry_run"})
        self.assertEqual((run.call_args.args[9], run.call_args.args[6]), (None, False))
        self.assertIn("dry_run", out)

    def test_merge_timeouts_must_be_positive_and_there_is_no_predictor_option(self):
        for flag in ("--wait-timeout", "--hook-timeout"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit) as caught, \
                    mock.patch("sys.stderr", io.StringIO()):
                self.invoke_merge([flag, "0"])
            self.assertEqual(caught.exception.code, 2)
        for flag in ("--predict-timeout", "--no-predict", "--path"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit) as caught, \
                    mock.patch("sys.stderr", io.StringIO()):
                self.invoke_merge([flag] if flag == "--no-predict" else [flag, "1"])
            self.assertEqual(caught.exception.code, 2)

    def test_a_commit_does_not_accept_the_merge_only_options(self):
        with self.assertRaises(SystemExit) as caught, mock.patch("sys.stderr", io.StringIO()):
            self.invoke(["commit", "--path", "a.py", "-m", "/tmp/m.txt", "--source", "s1"])
        self.assertEqual(caught.exception.code, 2)

    def test_a_merge_failure_maps_to_its_own_exit_code_and_message(self):
        code, _out, err, _run = self.invoke_merge(queue_error=gq.Stop(gq.EXIT_CHANGED, "现场与审查过的合并不符"))
        self.assertEqual((code, "现场与审查过的合并不符" in err), (gq.EXIT_CHANGED, True))
        code, _out, err, _run = self.invoke_merge(queue_error=gq.wrapup.WrapupError("坏说明"))
        self.assertEqual((code, "坏说明" in err), (gq.EXIT_USAGE, True))

    def test_a_waiting_queue_shows_its_first_line_while_still_running(self):
        """后台排队的真实形态：stdout 是管道。没有逐行刷出，这行要等进程结束才出现。"""
        with tempfile.TemporaryDirectory() as directory:
            tmp = pathlib.Path(directory)
            (tmp / "sf_run_tests.lock").symlink_to("%d|%s|/w" % (os.getpid(), gq.process_start(os.getpid())))
            env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
            proc = subprocess.Popen([sys.executable, str(ROOT / "tools" / "gate_queue.py"), "wait",
                                     "--timeout", "8", "--tmpdir", str(tmp)], cwd=str(ROOT), env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                first = proc.stdout.readline()
                still_running = proc.poll() is None
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                proc.stdout.close()
        self.assertIn("等待：", first)
        self.assertTrue(still_running, "第一行是进程结束后才冲出来的：输出没有逐行刷新")

    def test_say_now_flushes_every_line(self):
        class Sink(io.StringIO):
            flushes = 0

            def flush(self):
                Sink.flushes += 1
                super().flush()

        with redirect_stdout(Sink()):
            gq.say_now("一行")
        self.assertGreaterEqual(Sink.flushes, 1)

    def test_each_failure_kind_maps_to_its_own_exit_code_and_message(self):
        argv = ["commit", "--path", "a.py", "-m", "/tmp/m.txt", "--log", "/tmp/x.log"]
        code, _out, err, _run = self.invoke(argv, queue_error=gq.Stop(gq.EXIT_CHANGED, "输入变了"))
        self.assertEqual((code, "输入变了" in err), (gq.EXIT_CHANGED, True))
        code, _out, err, _run = self.invoke(argv, queue_error=OSError("磁盘满"))
        self.assertEqual((code, "gate queue rejected" in err), (gq.EXIT_USAGE, True))
        code, _out, err, _run = self.invoke(argv, queue_error=gq.wrapup.WrapupError("坏路径"))
        self.assertEqual((code, "坏路径" in err), (gq.EXIT_USAGE, True))

    def test_running_the_file_as_a_script_exits_with_the_command_result(self):
        script = ROOT / "tools" / "gate_queue.py"
        out = io.StringIO()
        with mock.patch.object(sys, "argv", [str(script), "self-test"]), redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(script), run_name="__main__")
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("self-test passed", out.getvalue())


class SelfTestHasTeeth(SFCase):
    """自检里每一处判据坏掉时都必须报红：holder_alive 的回答按调用顺序逐个喂。"""

    def run_with(self, answers):
        err = io.StringIO()
        with mock.patch.object(gq, "holder_alive", side_effect=list(answers)), \
                mock.patch("sys.stderr", err), redirect_stdout(io.StringIO()):
            return gq.self_test(), err.getvalue()

    def test_the_untouched_self_test_passes_against_the_real_judgement(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(gq.self_test(), 0)
        self.assertIn("self-test passed", out.getvalue())

    def test_a_live_lock_treated_as_stale_is_caught(self):
        code, err = self.run_with([False])
        self.assertEqual(code, 1)
        self.assertIn("live lock was treated as stale", err)

    def test_a_reused_pid_treated_as_live_is_caught(self):
        code, err = self.run_with([True, True])
        self.assertEqual(code, 1)
        self.assertIn("reused pid was treated as live", err)

    def test_a_malformed_holder_that_is_accepted_is_caught(self):
        code, err = self.run_with([True, False, True])
        self.assertEqual(code, 1)
        self.assertIn("malformed holder was accepted", err)

    def test_an_earlier_live_ticket_that_does_not_block_is_caught(self):
        code, err = self.run_with([True, False, False, False])
        self.assertEqual(code, 1)
        self.assertIn("earlier live ticket did not block", err)

    def test_a_stale_ticket_that_is_not_retired_is_caught(self):
        code, err = self.run_with([True, False, False, True, True])
        self.assertEqual(code, 1)
        self.assertIn("stale ticket was not retired", err)


if __name__ == "__main__":
    unittest.main()
