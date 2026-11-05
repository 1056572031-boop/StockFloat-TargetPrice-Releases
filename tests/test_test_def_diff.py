# -*- coding: utf-8 -*-
"""测试定义差集检查（tools/test_def_diff.py）的判据。

守的是这一类事故（同一形状出现过两次，第二次整档照绿）：往测试文件里插新用例时，
编辑锚点取了旧用例的 def 行，新内容里没重抄它，于是旧用例的函数头被顶掉、
旧用例体并进上一条用例名下、旧用例名消失。旧体仍在跑，所以整档不红，
只有拿提交前后的测试定义做差集才看得出来。

按 AGENTS §6：断言里没有版本号字面量、不读源码做字符串断言、不 sleep、
不依赖当下时钟、不访问私有成员（只调公开的 collect_ids / find_losses / main）、继承 SFCase。
"""
from __future__ import annotations

import io
import os
import pathlib
import runpy
import subprocess
import sys
import tempfile
import types
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import test_def_diff as D                    # noqa: E402

try:
    from _base import SFCase                    # noqa: E402
except ImportError:
    from tests._base import SFCase              # noqa: E402

# 子进程 git 必须洗掉 GIT_*：本文件在 pre-commit 钩子里也会跑，钩子环境把 GIT_DIR /
# GIT_INDEX_FILE 指向被提交的那棵树，临时仓库里的命令会全打到真仓库上。
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
TOOL = ROOT / "tools" / "test_def_diff.py"
FILE_A = "tests/test_alpha.py"
FIRST = FILE_A + "::Alpha.test_first"
SECOND = FILE_A + "::Alpha.test_second"
ENV.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

_HEAD = "import unittest\n\n\nclass Alpha(unittest.TestCase):\n"
_BODY = "        self.assertTrue(True)\n"
ONE = _HEAD + "    def test_first(self):\n" + _BODY
TWO = ONE + "\n    def test_second(self):\n" + _BODY
# 事故形状：test_second 的 def 行被顶掉，函数体并进 test_first 名下，文件仍能解析、仍然全绿。
EATEN = ONE + "\n" + _BODY
MOVED = _HEAD + "    def test_second(self):\n" + _BODY
NONE = _HEAD + "    pass\n"


class _Repo(SFCase):
    """带一次初始提交的临时仓库：tests/test_alpha.py 有两条用例，notes.txt 是普通文件。"""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="sf_def_diff_test_")
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        self.git("init", "-q")
        self.git("config", "user.email", "gate@example.invalid")
        self.git("config", "user.name", "gate")
        self.git("config", "commit.gpgsign", "false")
        self.write(FILE_A, TWO)
        self.write("notes.txt", "n\n")
        self.commit_all("init")
        self.git("branch", "-M", "trunk")

    def git(self, *args, check=True):
        done = subprocess.run(["git", *args], cwd=self.repo, env=ENV, capture_output=True, text=True)
        if check:
            self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def stage(self, rel, text):
        self.write(rel, text)
        self.git("add", "--", rel)

    def commit_all(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def losses(self):
        return D.find_losses(self.repo, env=ENV)


class TestCollectIds(SFCase):
    def test_module_class_nested_and_async_tests_are_collected(self):
        source = ("import unittest\n\n"
                  "def test_top():\n    pass\n\n"
                  "async def test_async_top():\n    pass\n\n"
                  "class A(unittest.TestCase):\n"
                  "    def test_one(self):\n        pass\n\n"
                  "    class Inner:\n"
                  "        def test_deep(self):\n            pass\n")
        self.assertEqual(D.collect_ids(source),
                         {"test_top", "test_async_top", "A.test_one", "A.Inner.test_deep"})

    def test_helpers_and_nested_functions_are_not_tests(self):
        source = ("class A:\n"
                  "    def helper(self):\n        pass\n\n"
                  "    def _test_hidden(self):\n        pass\n\n"
                  "    def test_real(self):\n"
                  "        def test_inner():\n            pass\n")
        self.assertEqual(D.collect_ids(source), {"A.test_real"})

    def test_the_accident_shape_is_what_the_fixtures_say_it_is(self):
        self.assertEqual(D.collect_ids(TWO), {"Alpha.test_first", "Alpha.test_second"})
        self.assertEqual(D.collect_ids(EATEN), {"Alpha.test_first"})

    def test_unparsable_source_has_no_ids(self):
        self.assertIsNone(D.collect_ids("def broken(:\n"))


class TestStagedLosses(_Repo):
    def test_untouched_tree_is_ok(self):
        report = self.losses()
        self.assertEqual((report.status, report.lost), ("ok", ()))

    def test_adding_tests_is_ok(self):
        self.stage(FILE_A, TWO + "\n    def test_third(self):\n" + _BODY)
        self.assertEqual(self.losses().lost, ())

    def test_a_swallowed_def_line_is_reported_by_name(self):
        self.stage(FILE_A, EATEN)
        report = self.losses()
        self.assertEqual((report.status, report.lost), ("fail", (SECOND,)))

    def test_deleting_a_whole_test_file_loses_all_its_tests(self):
        self.git("rm", "-q", "--", FILE_A)
        self.assertEqual(self.losses().lost, (FIRST, SECOND))

    def test_moving_a_test_to_another_file_still_needs_a_declaration(self):
        self.stage(FILE_A, ONE)
        self.stage("tests/test_beta.py", MOVED)
        self.assertEqual(self.losses().lost, (SECOND,))

    def test_renaming_a_test_needs_a_declaration(self):
        self.stage(FILE_A, TWO.replace("test_second", "test_second_renamed"))
        self.assertEqual(self.losses().lost, (SECOND,))

    def test_unstaged_deletion_is_not_this_commits_business(self):
        self.write(FILE_A, ONE)
        self.assertEqual(self.losses().lost, ())

    def test_files_outside_the_tests_directory_are_ignored(self):
        self.stage("docs/test_outside.py", "def test_other():\n    pass\n")
        self.commit_all("add outside")
        self.git("rm", "-q", "--", "docs/test_outside.py")
        self.assertEqual(self.losses().lost, ())

    def test_staged_syntax_error_is_a_problem_not_a_loss(self):
        self.stage(FILE_A, "def broken(:\n")
        report = self.losses()
        self.assertEqual((report.status, report.lost), ("fail", ()))
        self.assertTrue(any(FILE_A in line for line in report.problems))

    def test_an_unparsable_baseline_does_not_block_the_fix(self):
        self.stage(FILE_A, "def broken(:\n")
        self.commit_all("broken")
        self.stage(FILE_A, TWO)
        self.assertEqual(self.losses().status, "ok")


class TestDeclarations(_Repo):
    REASON = "用例已并入 test_first，不再单独保留"

    def declare(self, text):
        self.stage(D.DECLARATION_FILE, text)

    def test_a_declaration_with_a_reason_releases_the_loss(self):
        self.stage(FILE_A, ONE)
        self.declare("%s | %s\n" % (SECOND, self.REASON))
        report = self.losses()
        self.assertEqual((report.status, report.lost, report.released), ("ok", (), (SECOND,)))

    def test_wildcard_declaration_covers_a_whole_file(self):
        self.git("rm", "-q", "--", FILE_A)
        self.declare("%s::* | 整个文件拆分到别处，逐条核对过\n" % FILE_A)
        report = self.losses()
        self.assertEqual((report.status, report.lost, report.released), ("ok", (), (FIRST, SECOND)))

    def test_malformed_declarations_are_problems_and_release_nothing(self):
        self.stage(FILE_A, ONE)
        for line in ("%s |\n" % SECOND, "%s | x\n" % SECOND, "%s 没有分隔符的一句说明\n" % SECOND):
            with self.subTest(line=line):
                self.declare(line)
                report = self.losses()
                self.assertEqual((report.status, report.lost), ("fail", (SECOND,)))
                self.assertTrue(report.problems)

    def test_an_old_declaration_does_not_excuse_a_new_loss(self):
        self.declare("%s | %s\n" % (SECOND, self.REASON))
        self.commit_all("declare early")
        self.stage(FILE_A, ONE)
        report = self.losses()
        self.assertEqual((report.status, report.lost), ("fail", (SECOND,)))

    def test_comments_and_blank_lines_in_the_declaration_file_are_fine(self):
        self.stage(FILE_A, ONE)
        self.declare("# 说明\n\n%s | %s\n" % (SECOND, self.REASON))
        self.assertEqual(self.losses().status, "ok")


class TestMerge(_Repo):
    def merge_in_progress(self):
        self.git("checkout", "-q", "-b", "side")
        self.stage(FILE_A, ONE)
        self.commit_all("side drops test_second")
        self.git("checkout", "-q", "trunk")
        self.stage("tests/test_beta.py", MOVED.replace("Alpha", "Beta"))
        self.commit_all("trunk adds a file")
        self.git("merge", "-q", "--no-commit", "--no-ff", "side")

    def test_a_removal_only_one_parent_made_is_not_the_merges_loss(self):
        self.merge_in_progress()
        report = self.losses()
        self.assertEqual((report.status, report.lost), ("ok", ()))

    def test_a_test_both_parents_had_must_survive_the_merge(self):
        self.merge_in_progress()
        self.stage(FILE_A, NONE)
        report = self.losses()
        self.assertEqual((report.status, report.lost), ("fail", (FIRST,)))


class TestRepositoryStates(SFCase):
    def fresh_dir(self):
        tmp = tempfile.TemporaryDirectory(prefix="sf_def_diff_state_")
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def init_in(self, path):
        done = subprocess.run(["git", "init", "-q"], cwd=path, env=ENV, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_repository_without_commits_is_ok(self):
        root = self.fresh_dir()
        self.init_in(root)
        self.assertEqual(D.find_losses(root, env=ENV).status, "ok")

    def test_a_directory_that_is_not_a_repository_is_skipped(self):
        report = D.find_losses(self.fresh_dir(), env=ENV)
        self.assertEqual(report.status, "skipped")
        self.assertTrue(report.note)

    def test_a_broken_git_directory_fails_instead_of_passing(self):
        root = self.fresh_dir()
        os.mkdir(os.path.join(root, ".git"))
        self.assertEqual(D.find_losses(root, env=ENV).status, "fail")

    def test_a_head_naming_a_commit_that_does_not_exist_fails_instead_of_passing(self):
        root = self.fresh_dir()
        self.init_in(root)
        with open(os.path.join(root, ".git", "HEAD"), "w", encoding="utf-8") as handle:
            handle.write("0" * 39 + "1\n")
        self.assertEqual(D.find_losses(root, env=ENV).status, "fail")

    def test_an_unreadable_index_fails_instead_of_passing(self):
        root = self.fresh_dir()
        self.init_in(root)
        subprocess.run(["git", "-c", "user.email=g@example.invalid", "-c", "user.name=g",
                        "commit", "-q", "--allow-empty", "-m", "init"], cwd=root, env=ENV, check=True)
        with open(os.path.join(root, ".git", "index"), "w", encoding="utf-8") as handle:
            handle.write("garbage")
        report = D.find_losses(root, env=ENV)
        self.assertEqual(report.status, "fail")
        self.assertTrue(report.problems)

    def test_a_branch_that_has_no_commit_yet_is_not_a_broken_head(self):
        root = self.fresh_dir()
        self.init_in(root)
        with open(os.path.join(root, ".git", "HEAD"), "w", encoding="utf-8") as handle:
            handle.write("ref: refs/heads/not-born-yet\n")
        self.assertEqual(D.find_losses(root, env=ENV).status, "ok")

    def test_a_missing_git_program_fails_instead_of_passing(self):
        root = self.fresh_dir()
        self.init_in(root)
        report = D.find_losses(root, env=dict(ENV, PATH="/nonexistent"))
        self.assertEqual(report.status, "fail")
        self.assertTrue(report.problems)


class TestCommandLine(_Repo):
    def run_main(self, *argv, root=None):
        out = io.StringIO()
        code = D.main(["--root", root or self.repo, *argv], out=out, env=ENV)
        return code, out.getvalue()

    def test_a_clean_tree_exits_zero(self):
        code, text = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("通过", text)

    def test_a_loss_exits_one_and_names_the_test_and_the_declaration_file(self):
        self.stage(FILE_A, EATEN)
        code, text = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(SECOND, text)
        self.assertIn(D.DECLARATION_FILE, text)

    def test_a_declared_removal_exits_zero_and_says_so(self):
        self.stage(FILE_A, ONE)
        self.stage(D.DECLARATION_FILE, "%s | %s\n" % (SECOND, TestDeclarations.REASON))
        code, text = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("声明", text)

    def test_a_problem_without_any_loss_still_exits_one(self):
        self.stage(FILE_A, "def broken(:\n")
        code, text = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(FILE_A, text)

    def test_not_a_repository_is_skipped_with_exit_zero(self):
        tmp = tempfile.TemporaryDirectory(prefix="sf_def_diff_cli_")
        self.addCleanup(tmp.cleanup)
        code, text = self.run_main(root=tmp.name)
        self.assertEqual(code, 0)
        self.assertIn("跳过", text)

    def test_unknown_argument_exits_two(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            D.main(["--nope"], env=ENV)
        self.assertEqual(caught.exception.code, 2)

    def test_help_exits_zero(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            D.main(["-h"], env=ENV)
        self.assertEqual(caught.exception.code, 0)

    def test_running_the_file_as_a_script_exits_with_the_report_code(self):
        self.stage(FILE_A, EATEN)
        with mock.patch.object(sys, "argv", [str(TOOL), "--root", self.repo]), \
                mock.patch.dict(os.environ, ENV, clear=True), \
                redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            runpy.run_path(str(TOOL), run_name="__main__")
        self.assertEqual(caught.exception.code, 1)


class TestInsideARealHook(_Repo):
    """钩子里必须读 git 此刻真正要提交的那份索引（继承钩子环境），而不是磁盘上的 .git/index。
    洗掉 GIT_* 的读法在这两种提交上会错：commit -a 漏报，路径提交多报（探针实测 6/8）。"""

    def setUp(self):
        super().setUp()
        hook = os.path.join(self.repo, ".git", "hooks", "pre-commit")
        with open(hook, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\nexec "%s" "%s" --root "$PWD"\n' % (sys.executable, TOOL))
        os.chmod(hook, 0o755)

    def test_commit_a_cannot_slip_a_loss_past_the_hook(self):
        self.write(FILE_A, EATEN)
        done = self.git("commit", "-a", "-qm", "sneaky", check=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(SECOND, done.stdout + done.stderr)

    def test_a_path_only_commit_is_judged_on_what_it_commits(self):
        self.stage(FILE_A, EATEN)
        self.write("notes.txt", "changed\n")
        done = self.git("commit", "-qm", "notes only", "--", "notes.txt", check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class TestSelfTest(SFCase):
    """发版链上的工具都要带 --self-test（test_release_gates 起子进程要求退 0），
    而且自检本身必须会红：检查哪天放过了事故形状，它要当场报出来。"""

    def test_self_test_passes_on_the_real_implementation(self):
        out = io.StringIO()
        self.assertEqual(D.self_test(out=out), 0, out.getvalue())
        self.assertIn("自检通过", out.getvalue())

    def test_self_test_goes_red_when_the_check_stops_catching_the_accident(self):
        out = io.StringIO()
        with mock.patch.object(D, "find_losses", return_value=D.Report("ok")):
            self.assertEqual(D.self_test(out=out), 1)
        self.assertIn("自检未通过", out.getvalue())

    def test_a_git_failure_inside_the_self_test_is_a_failure_not_a_pass(self):
        def boom(*args, **kwargs):
            raise OSError("no git")

        proxy = types.SimpleNamespace(run=boom, SubprocessError=subprocess.SubprocessError)
        out = io.StringIO()
        with mock.patch.object(D, "subprocess", proxy):
            self.assertEqual(D.self_test(out=out), 1)
        self.assertIn("自检未通过", out.getvalue())

    def test_the_command_line_dispatches_to_the_self_test(self):
        with mock.patch.object(D, "self_test", return_value=7) as called:
            self.assertEqual(D.main(["--self-test"]), 7)
        self.assertEqual(called.call_count, 1)


class TestDeclarationStaysInternal(SFCase):
    """声明里写的是自由文本理由，属于内部材料：公开快照导出时必须丢弃它，而不是靠人记得。"""

    def test_the_public_export_drops_the_declaration_file(self):
        import public_release
        self.assertTrue(public_release.export_excluded(D.DECLARATION_FILE), D.DECLARATION_FILE)


class TestThisRepository(SFCase):
    def test_the_current_checkout_has_no_undeclared_test_loss(self):
        report = D.find_losses(str(ROOT))
        if report.status == "skipped":
            self.skipTest(report.note)
        self.assertEqual((report.status, report.lost, report.problems), ("ok", (), ()))
