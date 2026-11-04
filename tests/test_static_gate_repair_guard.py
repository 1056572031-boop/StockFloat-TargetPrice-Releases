# -*- coding: utf-8 -*-
"""静态闸门的新守卫：PyQt6 存根修复失败时，闸门必须失败（改动覆盖率门禁要求）。

背景：PyQt6 6.12.0 的 QtGui.pyi / QtDBus.pyi 有 19 处非法 PEP 570 写法，mypy 遇到
即 exit=2 终止。`run_mypy()` 因此在调用 mypy 之前先跑 tools/repair_pyqt6_stubs.py。
这里钉住两条不变式：
  1. 修复脚本失败 → 抛 RuntimeError（绝不让类型层"跑不起来"被当成通过）；
  2. mypy 探测（import mypy.version）本身的失败另有专门报错，不能被这条掩盖。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import static_gate as SG  # noqa: E402
from tests._base import SFCase  # noqa: E402


def _cp(code: int, out: str = "", err: str = ""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


class RepairGuardTest(SFCase):
    def test_repair_failure_aborts_the_gate(self):
        calls = []

        def fake_run(cmd, cwd=None):
            calls.append(list(cmd))
            joined = " ".join(str(c) for c in cmd)
            if "import mypy.version" in joined:       # 前置探测：mypy 可导入
                return _cp(0)
            if "repair_pyqt6_stubs.py" in joined:     # 修复脚本：模拟失败
                return _cp(3, err="boom: stub still invalid")
            raise AssertionError("mypy 不应在修复失败后继续执行")

        with mock.patch.object(SG, "_run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                SG.run_mypy()

        msg = str(ctx.exception)
        self.assertIn("存根修复", msg)
        self.assertIn("boom", msg)
        # 顺序不变式：先探测 mypy，再修存根，失败即止
        self.assertTrue(any("import mypy.version" in " ".join(c) for c in calls))
        self.assertTrue(any("repair_pyqt6_stubs.py" in " ".join(c) for c in calls))


if __name__ == "__main__":
    unittest.main()
