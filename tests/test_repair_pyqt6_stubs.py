# -*- coding: utf-8 -*-
"""tools/repair_pyqt6_stubs.py 的行为化测试（改动覆盖率门禁要求）。

覆盖点与"为什么要这么写"对齐：
  - 合法存根 → 一个字都不改写（幂等的前提）；
  - 检测到 `*args: T, /)` → 挪成合法的 `/, *args: T`，且第二次运行变成 no-op；
  - `--check` 只报告不落盘；
  - 报同一条错但正则匹配不到（无类型标注的 `*args`）→ 判失败而不是假装修好；
  - 与 PEP 570 无关的语法错误 → 跳过该文件，但最终仍让闸门失败；
  - 改写后仍不可解析 → 原文件保持不动（不留半改状态）；
  - PyQt6 不存在 → SystemExit，避免在错误环境里静默通过。
"""
from __future__ import annotations

import ast
import contextlib
import io
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import repair_pyqt6_stubs as R  # noqa: E402
from tests._base import SFCase  # noqa: E402

VALID = "x: int = 1\n"
BAD = "class C:\n    def f(self, a: int, *args: int, /) -> None: ...\n"
BAD_NO_TYPE = "def f(a, *args, /):\n    pass\n"          # 报同一条错，但正则匹配不到
BAD_UNRELATED = "def broken(:\n"                          # 与该规则无关的语法错误


def run_main(argv, stub_dir):
    """在临时存根目录上跑一次 main()，返回 (退出码, stdout)。"""
    out = io.StringIO()
    with mock.patch.object(R, "pyqt6_stub_dir", return_value=Path(stub_dir)), \
            mock.patch.object(sys, "argv", ["repair_pyqt6_stubs.py", *argv]), \
            contextlib.redirect_stdout(out):
        code = R.main()
    return code, out.getvalue()


class RepairPyQt6StubsTest(SFCase):
    def setUp(self):
        super().setUp()                       # SFCase 登记表（_sf_widgets 等）
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_valid_stub_is_untouched(self):
        path = self.write("ok.pyi", VALID)
        code, out = run_main([], self.dir)
        self.assertEqual(code, 0, out)
        self.assertEqual(path.read_text(encoding="utf-8"), VALID)
        self.assertIn("可解析", out)

    def test_repairs_pep570_pattern_and_is_idempotent(self):
        path = self.write("bad.pyi", BAD)
        code, out = run_main([], self.dir)
        self.assertEqual(code, 0, out)
        fixed = path.read_text(encoding="utf-8")
        self.assertIn("/, *args: int)", fixed)
        ast.parse(fixed)                       # 改完必须真的能被解析
        code2, out2 = run_main([], self.dir)   # 第二次必须是 no-op
        self.assertEqual(code2, 0, out2)
        self.assertIn("改写 0 处", out2)
        self.assertEqual(path.read_text(encoding="utf-8"), fixed)

    def test_check_mode_reports_without_writing(self):
        path = self.write("bad.pyi", BAD)
        code, out = run_main(["--check"], self.dir)
        self.assertEqual(code, 1, out)
        self.assertIn("待修", out)
        self.assertEqual(path.read_text(encoding="utf-8"), BAD)

    def test_matching_failure_is_reported_as_failure(self):
        path = self.write("bad.pyi", BAD_NO_TYPE)
        code, out = run_main([], self.dir)
        self.assertEqual(code, 1, out)
        self.assertIn("匹配不到", out)
        self.assertEqual(path.read_text(encoding="utf-8"), BAD_NO_TYPE)

    def test_unrelated_syntax_error_is_skipped_but_gate_fails(self):
        path = self.write("other.pyi", BAD_UNRELATED)
        code, out = run_main([], self.dir)
        self.assertEqual(code, 1, out)
        self.assertIn("跳过", out)
        self.assertIn("仍不可被 mypy 解析", out)
        self.assertEqual(path.read_text(encoding="utf-8"), BAD_UNRELATED)

    def test_patch_that_still_fails_is_rolled_back(self):
        text = BAD + BAD_NO_TYPE              # 能匹配、但改完仍有别的语法错误
        path = self.write("mixed.pyi", text)
        code, out = run_main([], self.dir)
        self.assertEqual(code, 1, out)
        self.assertIn("保持原文件不动", out)
        self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_missing_pyqt6_raises_system_exit(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            with self.assertRaises(SystemExit):
                R.pyqt6_stub_dir()

    def test_real_pyqt6_stub_dir_resolves_to_a_package(self):
        """真环境分支：找到的必须是可用的 PyQt6 包目录（含 .pyi）。"""
        root = R.pyqt6_stub_dir()
        self.assertTrue(root.is_dir(), root)
        self.assertTrue(list(root.rglob("*.pyi")), "%s 下应有 .pyi 存根" % root)

    def test_entry_point_runs_as_a_script(self):
        """覆盖 `if __name__ == "__main__":` 入口：真跑一次 --check 应 exit 0。"""
        script = ROOT / "tools" / "repair_pyqt6_stubs.py"
        proc = subprocess.run([sys.executable, str(script), "--check"],
                              cwd=str(ROOT), capture_output=True, text=True,
                              timeout=120, check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("待修", proc.stdout)        # --check 的固定输出

    def test_entry_point_in_process_returns_zero(self):
        """同一条入口的进程内版本：让覆盖率能记到 `raise SystemExit(main())` 这行。

        子进程执行不会进本进程的 coverage（没有 COVERAGE_PROCESS_START），
        改动覆盖率门禁会因此把这行记成"未覆盖"，所以这里用 runpy 在进程内跑一遍。
        """
        script = ROOT / "tools" / "repair_pyqt6_stubs.py"
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["repair_pyqt6_stubs.py", "--check"]), \
                contextlib.redirect_stdout(out), \
                self.assertRaises(SystemExit) as ctx:
            runpy.run_path(str(script), run_name="__main__")
        self.assertEqual(ctx.exception.code, 0, out.getvalue())
        self.assertIn("待修", out.getvalue())


if __name__ == "__main__":
    unittest.main()
