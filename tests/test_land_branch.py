"""land_branch：分支落地编排。用真实的临时 git 仓库和真实的 worktree，只把完整关卡、推送、收树换成替身。"""
import contextlib
import io
import runpy
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests._base import SFCase
from tools import gate_stamp
from tools import land_branch as land

REPO = Path(__file__).resolve().parents[1]
BASE = "mainline"
BRANCH = "task/x"
NAME = "task-x"
IDENTITY = "t <t@example.test>"


def sh(root, *args):
    """在临时仓库里跑 git，身份用 -c 单次指定（不依赖也不改全局配置）。"""
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test",
                           "-c", "commit.gpgsign=false", *args], cwd=root, env=gate_stamp.git_env(),
                          check=True, capture_output=True, text=True, timeout=60)
    return done.stdout.strip()


class FakeRunner:
    """替身：记录每条命令；关卡那条真的把暂存的合并提交出来（关卡本身不跑），其余只返回配置的退出码。"""

    def __init__(self, gate_rc=0, other_rc=0):
        self.gate_rc = gate_rc
        self.other_rc = other_rc
        self.calls = []

    def __call__(self, argv, cwd, log):
        self.calls.append((argv, cwd, log))
        if Path(argv[1]).name != "gate_queue.py":
            return self.other_rc
        if self.gate_rc == 0:
            sh(cwd, "commit", "-q", "-F", argv[argv.index("-m") + 1])
        return self.gate_rc

    def tools(self):
        return [Path(argv[1]).name + (":" + argv[2] if Path(argv[1]).name == "release_wrapup.py" else "")
                for argv, _, _ in self.calls]


class LandCase(SFCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="sf_land_test_")
        self.addCleanup(temp.cleanup)
        self.top = Path(temp.name)
        self.repo = self.top / "repo"
        self.repo.mkdir()
        sh(self.repo, "init", "-q", "-b", BASE)
        (self.repo / ".gitignore").write_text(".venv\n")
        (self.repo / "README.md").write_text("base\n")
        sh(self.repo, "add", ".gitignore", "README.md")
        sh(self.repo, "commit", "-q", "-m", "base")
        (self.repo / ".venv").mkdir()
        (self.repo / ".venv" / "marker").write_text("x")
        self.evidence = self.top / "evidence"
        self.message = self.top / "message.txt"
        self.message.write_text("merge: task x\n\nreviewed and gated.\n")
        self.tree = self.top / "task"
        sh(self.repo, "worktree", "add", "-q", "-b", BRANCH, str(self.tree))
        (self.tree / "feature.txt").write_text("feature\n")
        sh(self.tree, "add", "feature.txt")
        sh(self.tree, "commit", "-q", "-m", "feature")
        self.tip = sh(self.tree, "rev-parse", "HEAD")
        self.base_sha = sh(self.repo, "rev-parse", BASE)
        self.merge_tree = self.top / (".sf-merge-land-" + NAME)

    def run_land(self, runner=None, **overrides):
        arguments = dict(repo=self.repo, base=BASE, branch=BRANCH, tree=self.tree, reviewed=self.tip[:12],
                         merge_message=self.message, apply=True, push=False, retire=False, evidence=self.evidence,
                         runner=runner or FakeRunner())
        arguments.update(overrides)
        lines = []
        merge_sha = land.land(out=lines.append, **arguments)
        return merge_sha, lines

    def head(self, ref=BASE):
        return sh(self.repo, "rev-parse", ref)


class HappyPathTests(LandCase):
    def test_dry_run_checks_everything_and_changes_nothing(self):
        runner = FakeRunner()
        _, lines = self.run_land(runner, apply=False)
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.merge_tree.exists())
        self.assertEqual(self.head(), self.base_sha)
        self.assertEqual(sh(self.repo, "branch", "--list", "merge/*"), "")
        self.assertTrue(any("预演" in line for line in lines), lines)

    def test_apply_makes_a_two_parent_merge_of_the_reviewed_tree_and_fast_forwards(self):
        runner = FakeRunner()
        merge_sha, _ = self.run_land(runner)
        self.assertEqual(self.head(), merge_sha)
        self.assertEqual(sh(self.repo, "log", "-1", "--format=%P", merge_sha).split(), [self.base_sha, self.tip])
        self.assertEqual(self.head(merge_sha + "^{tree}"), self.head(self.tip + "^{tree}"))
        self.assertEqual((self.repo / "feature.txt").read_text(), "feature\n")
        (argv, cwd, _), = runner.calls
        self.assertEqual(cwd, self.merge_tree)
        self.assertEqual(argv[argv.index("--expected-head") + 1], self.base_sha)
        self.assertEqual(argv[argv.index("--source") + 1], self.tip)
        self.assertEqual(argv[argv.index("--expected-tree") + 1], self.head(self.tip + "^{tree}"))
        self.assertEqual(argv[argv.index("--author") + 1], IDENTITY, "作者默认取任务分支末端提交的作者")
        self.assertIn("--apply", argv)
        self.assertTrue((self.merge_tree / ".venv").is_symlink(), "合并树要能用主树的环境，且符号链接不进提交")
        self.assertEqual(sh(self.merge_tree, "status", "--porcelain"), "")

    def test_nothing_is_pushed_or_removed_unless_asked(self):
        runner = FakeRunner()
        self.run_land(runner)
        self.assertEqual(runner.tools(), ["gate_queue.py"])
        self.assertTrue(self.merge_tree.exists() and self.tree.exists())

    def test_push_then_retire_run_in_that_order_after_the_fast_forward(self):
        runner = FakeRunner()
        self.run_land(runner, push=True, retire=True)
        self.assertEqual(runner.tools(), ["gate_queue.py", "release_wrapup.py:push", "release_wrapup.py:retire",
                                          "release_wrapup.py:retire"])
        retired = [argv[argv.index("--tree") + 1] for argv, _, _ in runner.calls if "retire" in argv]
        self.assertEqual(retired, [str(self.merge_tree), str(self.tree)])
        for argv, _, _ in runner.calls:
            if "retire" in argv:
                self.assertEqual(argv[argv.index("--base") + 1], BASE)
        pushed = runner.calls[1][0]
        self.assertEqual(pushed[pushed.index("--branch") + 1], BASE)
        self.assertIn("--apply", pushed)

    def failing_step(self, failing, reason):
        """让指定的一步失败，并像真工具一样把原因写进它自己的日志。"""
        real = FakeRunner()

        def picky(argv, cwd, log):
            if failing in argv:
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open("ab") as stream:
                    stream.write(("$ " + " ".join(argv) + "\n" + reason + "\n").encode())
                return 1
            return real(argv, cwd, log)

        return picky

    def test_a_failed_step_shows_why_instead_of_only_the_exit_code(self):
        reason = "refused: the tree is not safe to retire:\n  still in use: pid 4242 has its working directory"
        for failing, flags in (("retire", dict(retire=True)), ("push", dict(push=True))):
            with self.subTest(step=failing):
                self.setUp()
                with self.assertRaises(land.LandError) as caught:
                    self.run_land(self.failing_step(failing, reason), **flags)
                self.assertIn("退出码 1", str(caught.exception))
                self.assertIn("still in use: pid 4242", str(caught.exception), "失败信息里要有工具给出的真实原因")

    def test_a_failed_gate_shows_the_tail_of_its_log(self):
        with self.assertRaises(land.LandError) as caught:
            self.run_land(self.failing_step("merge-commit", "hook said: change coverage failed"))
        self.assertIn("hook said: change coverage failed", str(caught.exception))

    def test_only_the_output_of_this_attempt_is_shown_not_older_runs_in_the_same_log(self):
        log = self.evidence / "land-retire.log"
        log.parent.mkdir(parents=True)
        log.write_text("OLD-ATTEMPT-REASON\n", encoding="utf-8")
        with self.assertRaises(land.LandError) as caught:
            self.run_land(self.failing_step("retire", "NEW-ATTEMPT-REASON"), retire=True)
        self.assertIn("NEW-ATTEMPT-REASON", str(caught.exception))
        self.assertNotIn("OLD-ATTEMPT-REASON", str(caught.exception))

    def test_a_successful_step_adds_no_log_noise(self):
        runner = FakeRunner()
        self.assertEqual(land.run_step(runner, ["x", "y"], self.top, self.evidence / "never.log"), (0, ""))

    def test_a_failed_step_that_never_wrote_a_log_still_reports_cleanly(self):
        runner = FakeRunner(other_rc=3)
        rc, detail = land.run_step(runner, ["x", "y"], self.top, self.evidence / "never.log")
        self.assertEqual((rc, detail), (3, ""))
        self.assertEqual(land.with_detail("msg", ""), "msg")
        self.assertEqual(land.with_detail("msg", "  | why"), "msg\n  | why")

    def test_the_tail_is_short_and_each_line_is_cut(self):
        log = self.top / "long.log"
        lines = ["line %d" % i for i in range(land.TAIL_LINES + 8)] + ["x" * (land.TAIL_LINE_CHARS * 3)]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tail = land.log_tail(log).splitlines()
        self.assertEqual(len(tail), land.TAIL_LINES)
        self.assertTrue(all(len(line) <= land.TAIL_LINE_CHARS + 4 for line in tail), "单行要截断")
        self.assertNotIn("line 0", "\n".join(tail), "只留最后几行")
        self.assertEqual(land.log_tail(self.top / "missing.log"), "")

    def test_progress_lines_are_flushed_at_once_so_a_redirected_run_is_not_silent(self):
        with mock.patch("builtins.print") as printed:
            land.say("进度")
        printed.assert_called_once_with("进度", flush=True)

    def test_long_steps_announce_themselves_before_they_start(self):
        _, lines = self.run_land(FakeRunner(), push=True, retire=True)
        joined = "\n".join(lines)
        for announcement in ("提交合并并过完整关卡", "推送 %s" % BASE, "收树 %s" % self.merge_tree):
            self.assertIn(announcement, joined)
        self.assertLess(joined.index("提交合并并过完整关卡"), joined.index("已快进"))

    def test_a_failing_push_or_retire_is_reported_with_its_exit_code(self):
        for failing in ("push", "retire"):
            with self.subTest(step=failing):
                self.setUp()
                runner = FakeRunner()
                real = runner.__call__

                def picky(argv, cwd, log, real=real, failing=failing):
                    if failing in argv:
                        return 9
                    return real(argv, cwd, log)

                with self.assertRaisesRegex(land.LandError, "退出码 9"):
                    self.run_land(picky, push=True, retire=True)

    def test_an_already_merged_branch_is_not_merged_again_but_push_still_runs(self):
        self.run_land()
        runner = FakeRunner()
        merge_sha, lines = self.run_land(runner, push=True)
        self.assertEqual(merge_sha, "")
        self.assertEqual(runner.tools(), ["release_wrapup.py:push"])
        self.assertTrue(any("已经在" in line for line in lines), lines)
        dry, _ = self.run_land(FakeRunner(), apply=False)
        self.assertEqual(dry, "")


class ResumeTests(LandCase):
    def test_a_finished_merge_commit_is_reused_after_an_interrupted_fast_forward(self):
        (self.repo / "feature.txt").write_text("someone else's uncommitted file\n")
        with self.assertRaisesRegex(land.LandError, "重叠"):
            self.run_land()
        self.assertEqual(self.head(), self.base_sha, "撞上别人的脏文件时主线一步也不动")
        merge_commit = self.head("merge/land-" + NAME)
        (self.repo / "feature.txt").unlink()
        runner = FakeRunner()
        merge_sha, lines = self.run_land(runner)
        self.assertEqual((merge_sha, runner.calls), (merge_commit, []), "已完成的合并提交不再重新过关卡")
        self.assertEqual(self.head(), merge_commit)
        self.assertTrue(any("已有完成的合并提交" in line for line in lines), lines)

    def test_dry_run_reports_an_existing_merge_commit(self):
        (self.repo / "feature.txt").write_text("x")
        with self.assertRaises(land.LandError):
            self.run_land()
        (self.repo / "feature.txt").unlink()
        merge_sha, _ = self.run_land(FakeRunner(), apply=False)
        self.assertEqual(merge_sha, self.head("merge/land-" + NAME))

    def test_a_branch_with_the_merge_name_that_is_not_this_merge_is_refused(self):
        sh(self.repo, "branch", "merge/land-" + NAME, self.tip)
        with self.assertRaisesRegex(land.LandError, "不是这次落地的合并提交"):
            self.run_land()
        self.assertEqual(self.head(), self.base_sha)

    def test_a_merge_branch_with_the_right_parents_but_a_different_tree_is_refused(self):
        # 父提交对、树不对：只查父提交的守卫会把它当成这次的合并并快进，主线就被推到一棵没审查过的树上。
        wrong = sh(self.repo, "commit-tree", self.base_sha + "^{tree}", "-p", self.base_sha, "-p", self.tip,
                   "-m", "merge with the wrong tree")
        sh(self.repo, "branch", "merge/land-" + NAME, wrong)
        runner = FakeRunner()
        with self.assertRaisesRegex(land.LandError, "不是这次落地的合并提交"):
            self.run_land(runner)
        self.assertEqual((self.head(), runner.calls), (self.base_sha, []))
        self.assertNotIn("feature.txt", sh(self.repo, "ls-tree", "--name-only", BASE))

    def test_a_pending_merge_branch_without_its_tree_is_refused(self):
        sh(self.repo, "branch", "merge/land-" + NAME, self.base_sha)
        with self.assertRaisesRegex(land.LandError, "合并树不在"):
            self.run_land()
        self.assertEqual(self.head(), self.base_sha)

    def test_a_failed_gate_leaves_a_staged_merge_that_the_same_command_finishes(self):
        with self.assertRaisesRegex(land.LandError, "退出码 1"):
            self.run_land(FakeRunner(gate_rc=1))
        self.assertTrue(self.merge_tree.exists())
        runner = FakeRunner()
        merge_sha, _ = self.run_land(runner)
        self.assertEqual(self.head(), merge_sha)
        self.assertEqual(len(runner.calls), 1, "续上的是同一棵已暂存的合并树，只重新过一次关卡")
        self.assertEqual(sh(self.repo, "log", "-1", "--format=%P", merge_sha).split(), [self.base_sha, self.tip])

    def test_an_aborted_staged_merge_is_not_resumed(self):
        with self.assertRaises(land.LandError):
            self.run_land(FakeRunner(gate_rc=1))
        sh(self.merge_tree, "merge", "--abort")
        with self.assertRaisesRegex(land.LandError, "不是可续的暂存合并"):
            self.run_land()
        self.assertEqual(self.head(), self.base_sha)

    def test_a_merge_tree_on_another_branch_or_commit_is_not_resumed(self):
        with self.assertRaises(land.LandError):
            self.run_land(FakeRunner(gate_rc=1))
        self.assertFalse(land.resumable_merge(self.merge_tree, "wrong-branch", self.base_sha, self.tip))
        self.assertFalse(land.resumable_merge(self.merge_tree, "merge/land-" + NAME, "b" * 40, self.tip))
        sh(self.merge_tree, "merge", "--abort")
        sh(self.merge_tree, "checkout", "-q", "--detach")
        self.assertFalse(land.resumable_merge(self.merge_tree, "merge/land-" + NAME, self.base_sha, self.tip))
        self.assertFalse(land.resumable_merge(self.top / "nowhere", "merge/land-" + NAME, self.base_sha, self.tip))

    def test_dirty_paths_skips_an_empty_porcelain_record(self):
        with mock.patch.object(land.subprocess, "run", return_value=SimpleNamespace(stdout=b"\0")):
            self.assertEqual(land.dirty_paths(self.repo), [])

    def test_a_leftover_merge_tree_without_a_finished_commit_is_refused(self):
        self.merge_tree.mkdir()
        with self.assertRaisesRegex(land.LandError, "已存在但没有完成的合并提交"):
            self.run_land()
        self.assertEqual(self.head(), self.base_sha)


class RefusalTests(LandCase):
    def refused(self, pattern, **overrides):
        runner = overrides.pop("runner", FakeRunner())
        with self.assertRaisesRegex(land.LandError, pattern):
            self.run_land(runner, **overrides)
        self.assertEqual(self.head(), self.base_sha, "拒绝时主线不动")
        self.assertFalse(self.merge_tree.exists(), "拒绝发生在建树之前")
        self.assertEqual(runner.calls, [])

    def test_the_main_tree_must_be_on_the_base_branch(self):
        sh(self.repo, "checkout", "-q", "-b", "elsewhere")
        self.refused("主树当前不在")

    def test_the_task_tree_must_be_on_the_named_branch(self):
        self.refused("任务树 .* 当前不在", branch="task/y")

    def test_a_dirty_task_tree_is_refused(self):
        (self.tree / "unfinished.txt").write_text("x")
        self.refused("未提交的改动")

    def test_the_reviewed_sha_must_match_the_branch_tip(self):
        for given in ("deadbeefdead", self.tip[:6], ""):
            with self.subTest(reviewed=given):
                self.refused("审批绑定固定提交", reviewed=given)
        (self.tree / "more.txt").write_text("later work")
        sh(self.tree, "add", "more.txt")
        sh(self.tree, "commit", "-q", "-m", "later")
        self.refused("审批绑定固定提交", reviewed=self.tip[:12])

    def test_a_mainline_that_moved_after_the_branch_is_refused(self):
        (self.repo / "other.txt").write_text("other")
        sh(self.repo, "add", "other.txt")
        sh(self.repo, "commit", "-q", "-m", "mainline moved")
        self.base_sha = self.head()
        self.refused("前进了")

    def test_a_missing_merge_message_is_refused_before_anything_is_built(self):
        self.refused("合并说明文件不存在", merge_message=self.top / "nope.txt")

    def test_dry_run_does_not_need_the_merge_message(self):
        merge_sha, _ = self.run_land(apply=False, merge_message=self.top / "nope.txt")
        self.assertEqual(merge_sha, "")

    def test_a_failing_gate_leaves_the_mainline_alone_and_names_the_tree(self):
        runner = FakeRunner(gate_rc=1)
        with self.assertRaisesRegex(land.LandError, "退出码 1"):
            self.run_land(runner)
        self.assertEqual(self.head(), self.base_sha)
        self.assertTrue(self.merge_tree.exists(), "关卡没过时合并树留着，方便看现场")

    def test_a_gate_that_reports_success_without_a_merge_commit_is_not_believed(self):
        def silent(argv, cwd, log):
            return 0

        with self.assertRaisesRegex(land.LandError, "没有生成合并提交"):
            self.run_land(silent)
        self.assertEqual(self.head(), self.base_sha)

    def test_a_staged_tree_that_differs_from_the_reviewed_tree_is_never_committed(self):
        # 主线是祖先时暂存树按理必然等于分支树；这条是防“合并驱动、属性文件之类让结果悄悄不同”的最后一道，
        # 所以只能让 write-tree 的答案变一下来证明它真的会拦。
        real = land.git

        def drifted(root, *args, **kwargs):
            if args[:1] == ("write-tree",):
                return 0, "0" * 40
            return real(root, *args, **kwargs)

        runner = FakeRunner()
        with mock.patch.object(land, "git", drifted), self.assertRaisesRegex(land.LandError, "不一致"):
            self.run_land(runner)
        self.assertEqual((runner.calls, self.head()), ([], self.base_sha))

    def test_fast_forward_refuses_when_the_main_tree_was_switched_during_the_gate(self):
        def switching(argv, cwd, log):
            rc = FakeRunner()(argv, cwd, log)
            sh(self.repo, "checkout", "-q", "-b", "elsewhere", self.base_sha)
            return rc

        with self.assertRaisesRegex(land.LandError, "已切到别的分支"):
            self.run_land(switching)
        self.assertEqual(sh(self.repo, "rev-parse", BASE), self.base_sha)
        self.assertEqual(sh(self.repo, "rev-parse", "elsewhere"), self.base_sha, "被切到的分支也没被推进")

    def test_fast_forward_overlap_check_handles_names_with_spaces(self):
        (self.tree / "with space.txt").write_text("feature\n")
        sh(self.tree, "add", "with space.txt")
        sh(self.tree, "commit", "-q", "-m", "spaced")
        self.tip = sh(self.tree, "rev-parse", "HEAD")
        (self.repo / "with space.txt").write_text("someone else's file\n")
        with self.assertRaisesRegex(land.LandError, "with space.txt"):
            self.run_land()
        self.assertEqual(self.head(), self.base_sha)

    def test_fast_forward_refuses_a_mainline_that_moved_during_the_gate(self):
        merge_sha, _ = self.run_land()
        with self.assertRaisesRegex(land.LandError, "前进了"):
            land.fast_forward(self.repo, BASE, self.base_sha, merge_sha)


class HelperTests(LandCase):
    def test_git_failures_are_refusals_not_tracebacks(self):
        with self.assertRaisesRegex(land.LandError, "git rev-parse"):
            land.git(self.top, "rev-parse", "HEAD")

    def test_dirty_paths_include_untracked_renamed_and_non_ascii_names_but_not_ignored_ones(self):
        (self.repo / "a.txt").write_text("a")
        sh(self.repo, "add", "a.txt")
        sh(self.repo, "commit", "-q", "-m", "a")
        sh(self.repo, "mv", "a.txt", "b.txt")
        (self.repo / "中文.txt").write_text("x")
        (self.repo / "with space.txt").write_text("x")
        self.assertEqual(sorted(land.dirty_paths(self.repo)), ["a.txt", "b.txt", "with space.txt", "中文.txt"])

    def test_run_logged_reports_success_missing_tools_and_timeouts_without_raising(self):
        log = self.top / "logs" / "run.log"
        self.assertEqual(land.run_logged(["/usr/bin/true"], self.top, log), 0)
        self.assertEqual(land.run_logged(["/nonexistent/tool"], self.top, log), 127)

        def too_slow(*args, **kwargs):
            raise subprocess.TimeoutExpired("x", 1)

        proxy = SimpleNamespace(run=too_slow, STDOUT=subprocess.STDOUT, TimeoutExpired=subprocess.TimeoutExpired)
        with mock.patch.object(land, "subprocess", proxy):
            self.assertEqual(land.run_logged(["/usr/bin/true"], self.top, log), 127)
        text = log.read_text(encoding="utf-8")
        self.assertEqual(text.count("$ "), 3)
        self.assertEqual(text.count("无法执行"), 2)


class CommandLineTests(LandCase):
    def argv(self, *extra, reviewed=None):
        return ["--repo", str(self.repo), "--base", BASE, "--branch", BRANCH, "--tree", str(self.tree),
                "--reviewed-sha", reviewed or self.tip[:12], "--merge-message", str(self.message), *extra]

    def call(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = land.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_exits_zero_and_creates_nothing(self):
        code, out, _ = self.call(self.argv())
        self.assertEqual(code, 0)
        self.assertIn("预演", out)
        self.assertFalse((self.repo / "build").exists())

    def test_a_refusal_exits_one_and_says_why(self):
        code, _, err = self.call(self.argv(reviewed="deadbeefdead"))
        self.assertEqual(code, 1)
        self.assertIn("land_branch:", err)
        self.assertIn("审批绑定固定提交", err)

    def test_apply_goes_through_the_module_level_runner_and_defaults_the_evidence_folder(self):
        runner = FakeRunner()
        with mock.patch.object(land, "run_logged", runner):
            code, out, _ = self.call(self.argv("--apply", "--push"))
        self.assertEqual(code, 0, out)
        self.assertEqual(runner.tools(), ["gate_queue.py", "release_wrapup.py:push"])
        self.assertEqual(runner.calls[0][2], self.repo / "build" / "land" / NAME / "land-gate.log")

    def test_missing_arguments_are_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
            land.main(["--branch", BRANCH])
        self.assertEqual(stop.exception.code, 2)

    def test_an_unusable_tree_path_is_a_refusal_not_a_traceback(self):
        argv = ["land_branch.py", *self.argv()[:6], "--tree", str(self.top / "missing"), "--reviewed-sha", "abc123abc123",
                "--merge-message", str(self.message)]
        with mock.patch("sys.argv", argv), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as stop:
            runpy.run_path(str(REPO / "tools" / "land_branch.py"), run_name="__main__")
        self.assertEqual(stop.exception.code, 1)
