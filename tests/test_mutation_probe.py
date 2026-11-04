# -*- coding: utf-8 -*-
"""`tools/mutation_probe.py` 的行为判据（2026-11-04 起）。

为什么有这枚工具：做"改坏必红"时把 `os.unlink(path)` 当锚点，文件里有 3 处，
`replace(..., 1)` 换中的是另一处，目标用例一条都没碰到它，于是"改坏之后仍然全绿"——
一次假绿。同类错误第二次出现，按项目口径改成机器强制：锚点必须恰好出现 1 次才许落盘。

这里在主进程直调 `probe()` / `main()`：工具自己的行数要计入 `.coverage`
（AGENTS §6 第 10 条，子进程不记账）。只有被测的"命令"才起真子进程。
"""
import io
import os
import py_compile
import runpy
import signal
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import mutation_probe as MP  # noqa: E402

try:
    from _base import SFCase                     # noqa: E402
except ImportError:                              # 以包形式被 discover 导入时
    from tests._base import SFCase               # noqa: E402


class Workspace(SFCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.target = self.dir / "target.py"

    def marker_command(self):
        """只要被跑过就留下一个标记文件：用来证明"被拒绝时命令根本没有执行"。"""
        self.ran = self.dir / "ran.flag"
        return [sys.executable, "-c",
                "import pathlib; pathlib.Path(%r).write_text('x')" % str(self.ran)]

    def verdict_command(self, expected):
        """真起子进程读目标文件的 VALUE：和期望不符就在 stderr 写出它并退出 1。"""
        script = ("import runpy, sys\n"
                  "value = runpy.run_path(%r)['VALUE']\n"
                  "if value != %r:\n"
                  "    sys.stderr.write('VALUE=%%r' %% (value,))\n"
                  "    sys.exit(1)\n") % (str(self.target), expected)
        return [sys.executable, "-c", script]

    def read_only_trap(self):
        """改坏后把目标文件改成只读再失败：复原那一步必然写不回去。"""
        script = ("import os, runpy, sys\n"
                  "if runpy.run_path(%r)['VALUE'] != 1:\n"
                  "    os.chmod(%r, 0o400)\n"
                  "    sys.exit(1)\n") % (str(self.target), str(self.target))
        return [sys.executable, "-c", script]


class AnchorMustAppearExactlyOnce(Workspace):
    THREE = ("def a():\n    os.unlink(path)\n\n"
             "def b():\n    os.unlink(path)\n\n"
             "def c():\n    os.unlink(path)\n")

    def test_anchor_appearing_three_times_is_refused_and_nothing_is_written(self):
        """今天踩过的原样：同一句话出现 3 次，必须拒绝，而且一个字节都不许动。"""
        self.target.write_text(self.THREE, encoding="utf-8")
        command = self.marker_command()
        result = MP.probe(self.target, "os.unlink(path)", "pass", command)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["count"], 3)
        self.assertEqual(self.target.read_text(encoding="utf-8"), self.THREE,
                         "被拒绝时不许动源码一个字节")
        self.assertFalse(self.ran.exists(), "被拒绝时连命令都不该跑")

    def test_anchor_that_is_missing_is_refused(self):
        self.target.write_text("VALUE = 1\n", encoding="utf-8")
        command = self.marker_command()
        result = MP.probe(self.target, "VALUE = 2", "VALUE = 3", command)
        self.assertEqual((result["status"], result["count"]), ("refused", 0))
        self.assertFalse(self.ran.exists())

    def test_empty_anchor_is_refused_instead_of_matching_everywhere(self):
        """空串在 Python 里 `count` 出来是 长度+1，必须在计数之前就拦住。"""
        self.target.write_text("VALUE = 1\n", encoding="utf-8")
        command = self.marker_command()
        result = MP.probe(self.target, "", "x", command)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "VALUE = 1\n")
        self.assertFalse(self.ran.exists())

    def test_cli_reports_the_count_and_exits_2_when_refused(self):
        self.target.write_text(self.THREE, encoding="utf-8")
        command = self.marker_command()
        out = io.StringIO()
        with redirect_stdout(out):
            code = MP.main([str(self.target), "os.unlink(path)", "pass", "--"] + command)
        self.assertEqual(code, 2)
        self.assertIn("3", out.getvalue(), "拒绝时要说清出现了几次")
        self.assertFalse(self.ran.exists())

    def test_cli_unknown_flag_exits_2(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            MP.main(["--no-such-flag"])
        self.assertEqual(caught.exception.code, 2)


class RealRun(Workspace):
    SOURCE = "VALUE = 1\n# anchor\n"

    def setUp(self):
        super().setUp()
        self.target.write_text(self.SOURCE, encoding="utf-8")

    def test_a_caught_mutation_is_reported_and_the_source_is_restored_byte_for_byte(self):
        result = MP.probe(self.target, "VALUE = 1", "VALUE = 2", self.verdict_command(1))
        self.assertEqual(result["status"], "caught")
        self.assertEqual(result["returncode"], 1)
        self.assertIn("VALUE=2", result["tail"], "要带回命令的输出尾部，才能看出红在哪一条")
        self.assertEqual(self.target.read_bytes(), self.SOURCE.encode("utf-8"))
        self.assertFalse(Path(result["backup"]).exists(), "复原成功后备份要删掉")

    def test_a_mutation_nobody_notices_is_reported_as_survived(self):
        """命令根本不看这处代码：改坏之后仍然全绿 = 判据没有牙，必须如实说。"""
        result = MP.probe(self.target, "# anchor", "# changed", self.verdict_command(1))
        self.assertEqual(result["status"], "survived")
        self.assertEqual(self.target.read_bytes(), self.SOURCE.encode("utf-8"))

    def test_a_command_that_already_fails_before_the_mutation_proves_nothing(self):
        """基线就红时，"改坏后红"毫无意义：不许落盘，也不许下结论。"""
        before = self.target.stat().st_mtime_ns
        result = MP.probe(self.target, "VALUE = 1", "VALUE = 2", self.verdict_command(99))
        self.assertEqual(result["status"], "control_failed")
        self.assertEqual(self.target.stat().st_mtime_ns, before, "基线就红时不许写盘")
        self.assertNotIn("backup", result)

    def test_stale_bytecode_cannot_hide_the_mutation_and_none_is_left_behind(self):
        """预先放一份"永不校验源码"的旧字节码：不清掉它，改坏就会被悄悄绕过（假绿）。"""
        py_compile.compile(str(self.target), doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        script = ("import sys\n"
                  "sys.path.insert(0, %r)\n"
                  "import target\n"
                  "sys.exit(0 if target.VALUE == 1 else 1)\n") % str(self.dir)
        result = MP.probe(self.target, "VALUE = 1", "VALUE = 2", [sys.executable, "-c", script])
        self.assertEqual(result["status"], "caught", "旧字节码没清掉，改坏就被绕过去了")
        self.assertEqual(list((self.dir / "__pycache__").glob("target.*.pyc")), [],
                         "复原之后不许留下按改坏源码编出来的字节码")

    def test_a_failed_restore_is_loud_and_keeps_the_backup(self):
        if os.geteuid() == 0:
            self.skipTest("root 不受只读位约束，造不出复原失败")
        self.addCleanup(os.chmod, self.target, 0o600)
        err = io.StringIO()
        with redirect_stderr(err):
            result = MP.probe(self.target, "VALUE = 1", "VALUE = 2", self.read_only_trap())
        self.addCleanup(Path(result["backup"]).unlink, missing_ok=True)
        self.assertEqual(result["status"], "restore_failed")
        self.assertIn(result["backup"], err.getvalue(), "复原失败要把备份路径喊出来")
        self.assertEqual(Path(result["backup"]).read_bytes(), self.SOURCE.encode("utf-8"),
                         "备份必须是原始字节，人才能据此手工复原")

    def test_a_hung_command_after_the_mutation_times_out_and_the_source_is_restored(self):
        script = ("import runpy, threading\n"
                  "if runpy.run_path(%r)['VALUE'] != 1:\n"
                  "    threading.Event().wait(60)\n") % str(self.target)
        result = MP.probe(self.target, "VALUE = 1", "VALUE = 2",
                          [sys.executable, "-c", script], timeout=5)
        self.assertEqual((result["status"], result["phase"]), ("timeout", "mutated"))
        self.assertEqual(self.target.read_bytes(), self.SOURCE.encode("utf-8"))

    def test_a_command_that_hangs_before_any_mutation_leaves_the_source_alone(self):
        script = "import threading; threading.Event().wait(60)"
        before = self.target.stat().st_mtime_ns
        result = MP.probe(self.target, "VALUE = 1", "VALUE = 2",
                          [sys.executable, "-c", script], timeout=1)
        self.assertEqual((result["status"], result["phase"]), ("timeout", "control"))
        self.assertEqual(self.target.stat().st_mtime_ns, before)

    def test_restore_does_not_trust_a_write_that_reads_back_different(self):
        """写进去 ≠ 复原成功：只写了一半（磁盘满、被打断）时，必须按失败处理。"""
        original = self.SOURCE.encode("utf-8")

        def short_write(path, data):
            with open(path, "wb") as stream:
                stream.write(data[:3])

        with mock.patch.object(MP.Path, "write_bytes", new=short_write):
            self.assertFalse(MP.restore(self.target, original))
        self.assertTrue(MP.restore(self.target, original), "正常写入读回一致才算复原")

    def test_sigterm_mid_run_still_restores_the_source_and_the_previous_handler(self):
        """工具被外部超时杀（SIGTERM）时，finally 必须跑到——否则源码停在被改坏的状态。"""
        seen = []

        def sentinel(signum, frame):
            seen.append(signum)

        previous = signal.signal(signal.SIGTERM, sentinel)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        script = ("import os, runpy, signal, threading\n"
                  "if runpy.run_path(%r)['VALUE'] != 1:\n"
                  "    os.kill(%d, signal.SIGTERM)\n"
                  "    threading.Event().wait(60)\n") % (str(self.target), os.getpid())
        with self.assertRaises(SystemExit) as caught:
            MP.probe(self.target, "VALUE = 1", "VALUE = 2", [sys.executable, "-c", script], timeout=8)
        self.assertEqual(caught.exception.code, 143)
        self.assertEqual(self.target.read_bytes(), self.SOURCE.encode("utf-8"))
        self.assertIs(signal.getsignal(signal.SIGTERM), sentinel, "要把原来的处理函数装回去")
        self.assertEqual(seen, [], "运行期间信号应该由工具接走，不是原处理函数")

    def test_probe_still_works_off_the_main_thread(self):
        """子线程里装不了信号处理函数（ValueError）：不能因此整个工具用不了。"""
        box = {}

        def work():
            box.update(MP.probe(self.target, "VALUE = 1", "VALUE = 2", self.verdict_command(1)))

        worker = threading.Thread(target=work)
        worker.start()
        worker.join(60)
        self.assertFalse(worker.is_alive())
        self.assertEqual(box.get("status"), "caught")
        self.assertEqual(self.target.read_bytes(), self.SOURCE.encode("utf-8"))


class CommandLine(Workspace):
    SOURCE = "VALUE = 1\n# anchor\n"

    def setUp(self):
        super().setUp()
        self.target.write_text(self.SOURCE, encoding="utf-8")

    def run_main(self, old, new, command, *flags):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = MP.main([str(self.target), old, new, *flags, "--", *command])
        return code, out.getvalue()

    def test_each_outcome_has_its_own_exit_code_and_a_human_sentence(self):
        hang = [sys.executable, "-c", "import threading; threading.Event().wait(60)"]
        cases = (
            ("抓住", "VALUE = 1", "VALUE = 2", self.verdict_command(1), (), 0, "抓住了"),
            ("没牙", "# anchor", "# changed", self.verdict_command(1), (), 1, "没牙"),
            ("基线红", "VALUE = 1", "VALUE = 2", self.verdict_command(99), (), 2, "改前"),
            ("超时", "VALUE = 1", "VALUE = 2", hang, ("--timeout", "1"), 2, "超时"),
        )
        for name, old, new, command, flags, want_code, want_text in cases:
            with self.subTest(name):
                code, text = self.run_main(old, new, command, *flags)
                self.assertEqual(code, want_code, text)
                self.assertIn(want_text, text)
                self.assertEqual(self.target.read_bytes(), self.SOURCE.encode("utf-8"))

    def test_failed_restore_exits_3_and_names_the_backup(self):
        if os.geteuid() == 0:
            self.skipTest("root 不受只读位约束，造不出复原失败")
        self.addCleanup(os.chmod, self.target, 0o600)
        code, text = self.run_main("VALUE = 1", "VALUE = 2", self.read_only_trap())
        self.assertEqual(code, 3, text)
        backup = [word for word in text.split() if word.endswith(".bak")]
        self.assertTrue(backup, "输出里要有备份路径：" + text)
        self.addCleanup(Path(backup[0]).unlink, missing_ok=True)

    def test_running_the_file_as_a_script_exits_with_the_status_code(self):
        """按脚本方式执行（`python tools/mutation_probe.py ...`）时，退出码要原样交给 shell。"""
        self.target.write_text("x = 1\nx = 1\nx = 1\n", encoding="utf-8")
        argv = ["mutation_probe.py", str(self.target), "x = 1", "x = 2", "--", sys.executable, "-c", "pass"]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), redirect_stdout(out), \
                self.assertRaises(SystemExit) as caught:
            runpy.run_path(str(ROOT / "tools" / "mutation_probe.py"), run_name="__main__")
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("3 次", out.getvalue())
        self.assertEqual(self.target.read_text(encoding="utf-8"), "x = 1\nx = 1\nx = 1\n")

    def test_a_missing_command_is_a_usage_error(self):
        for argv in ([str(self.target), "a", "b"], [str(self.target), "a", "b", "--"]):
            with self.subTest(argv=argv):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    MP.main(argv)
                self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
