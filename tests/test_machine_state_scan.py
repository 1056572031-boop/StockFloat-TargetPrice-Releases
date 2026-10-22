#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/machine_state_scan.py` 的判据（2026-10-22 补，T-15 的「还有几处」）。

为什么这道要进链而不是进文档：2026-10-21 那 26 条误红修掉之后，剩下的问题是
「同一族形状还有几处」——那是清单答不了的问题，只能每次提交现扫。本文件把闸门
钉在测试链里（`tests/` 每轮全量都跑），所以它不需要另外的接线就已经在链上。

三条硬要求（AGENTS §6 第 2 条与「判据必须能红」）：
- 每条断言都要有反向对照：种错必须被抓到；
- 每条判据都要有**正向对照**：正当写法（桩、假对象、字符串里出现探针名）不许报，
  否则这把尺子会因为报假警而被下一轮拿去开豁免；
- 空输入必须拒答（零输入当通过是本仓台账 G24 那一族）。
"""
from __future__ import annotations

import contextlib
import io
import pathlib
import runpy
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "machine_state_scan.py"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import machine_state_scan as gate        # noqa: E402  链上工具不在包内，只能按路径取
from tests._base import SFCase            # noqa: E402  §6.7：新用例走登记式基类


def _repo_with(src: str, name: str = "test_probe_fixture.py") -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp(prefix="sf_mss_fixture_"))
    tdir = root / "tests"
    tdir.mkdir(parents=True, exist_ok=True)
    for i in range(gate.MIN_TEST_FILES):      # 先喂饱范围红线，别让每种样例都红在范围上
        (tdir / f"test_filler_{i}.py").write_text("def x():\n    pass\n", encoding="utf-8")
    (tdir / name).write_text(src, encoding="utf-8")
    return root


class RealRepoIsClean(SFCase):
    def test_current_repo_has_no_unguarded_machine_state_premises(self):
        """现状必须 0 处：这一条是「别新增」的棘轮，也是本轮量出来的答案。"""
        bad, n = gate.scan(ROOT)
        self.assertGreaterEqual(n, gate.MIN_TEST_FILES,
                               "扫到的用例文件少于下限＝范围失效，这条断言什么也没验")
        self.assertEqual(bad, [], "用例把本机瞬时状态当隐式前提：\n" +
                         "\n".join(f"  {f}:{ln} {fn}() {what}" for f, fn, ln, what in bad))

    def test_gate_exit_code_is_zero_on_the_repo(self):
        self.assertEqual(gate.main(["--dir", str(ROOT)]), 0)


class PlantedErrorsAreCaught(SFCase):
    def test_direct_probe_call_without_stub_is_named(self):
        root = _repo_with('''
import sf_presence
class T:
    def test_reads_screen_state(self):
        self.assertTrue(sf_presence.screen_unavailable())
''')
        bad, _ = gate.scan(root)
        self.assertEqual(len(bad), 1, "直调探针却没被判违规")
        self.assertIn("屏幕锁定/显示器休眠", bad[0][3])

    def test_stub_in_another_case_does_not_cover_this_one(self):
        """桩必须与被测那一例同体：另一个用例里 patch 过，不等于这一例也不读真实值。"""
        root = _repo_with('''
import sf_presence
from unittest import mock
class T:
    def test_stubbed(self):
        with mock.patch.object(sf_presence, "screen_unavailable", lambda: False):
            pass
    def test_reads_it_for_real(self):
        self.assertTrue(sf_presence.screen_unavailable())
''')
        bad, _ = gate.scan(root)
        self.assertEqual([b[1] for b in bad], ["test_reads_it_for_real"],
                         "同一文件里另一例的桩把这条盖住了")

    def test_declaration_marker_without_reason_is_refused(self):
        root = _repo_with('''
import sf_presence
class T:
    def test_declared_but_empty(self):
        sf_presence.screen_unavailable()  # machine-state-ok:
''')
        bad, _ = gate.scan(root)
        self.assertEqual(len(bad), 1, "空理由的申报标记被当成了正当写法")

    def test_active_space_probe_called_for_real_is_named(self):
        root = _repo_with('''
import sf_glass
class T:
    def test_asks_the_system(self):
        self.assertEqual(sf_glass.CGSGetActiveSpace(), 1)
''')
        bad, _ = gate.scan(root)
        self.assertEqual(len(bad), 1, "活动 Space 的探针没进视野")


class LegitimateShapesAreNotFlagged(SFCase):
    """正向对照：尺子过宽就等于没有尺子，这三条必须**不报**。"""

    def test_stubbed_probe_is_clean(self):
        root = _repo_with('''
import sf_presence
from unittest import mock
class T:
    def test_stubbed(self):
        with mock.patch.object(sf_presence, "screen_unavailable", lambda: False):
            self.assertFalse(sf_presence.screen_unavailable())
''')
        self.assertEqual(gate.scan(root)[0], [])

    def test_fake_object_premise_is_clean(self):
        root = _repo_with('''
class _FakeWin:
    def __init__(self, on_active_space): self._v = on_active_space
    def isOnActiveSpace(self): return self._v
class T:
    def test_fake_driven(self):
        self.assertFalse(_FakeWin(on_active_space=False).isOnActiveSpace())
''')
        self.assertEqual(gate.scan(root)[0], [])

    def test_name_only_in_a_string_assertion_is_clean(self):
        """源码文本里出现探针名属 §6 第 2 条那一族，不许混进这把尺子。"""
        root = _repo_with('''
class T:
    def test_reads_source(self):
        src = "CGSGetActiveSpace and screen_unavailable"
        self.assertIn("CGSGetActiveSpace", src)
''')
        self.assertEqual(gate.scan(root)[0], [])

    def test_declared_with_a_real_reason_is_clean(self):
        root = _repo_with('''
import sf_presence
class T:
    def test_evidence_needs_the_machine(self):
        self.assertIsInstance(sf_presence.screen_unavailable(), bool)  # machine-state-ok: 取证用例，量的就是本机答复
''')
        self.assertEqual(gate.scan(root)[0], [])


class GateRefusesWhenItCannotSee(SFCase):
    def test_empty_scan_refuses_to_conclude(self):
        empty = pathlib.Path(tempfile.mkdtemp(prefix="sf_mss_empty_"))
        (empty / "tests").mkdir()
        self.assertEqual(gate.main(["--dir", str(empty)]), 2,
                         "零输入被当成整关通过")

    def test_self_test_passes(self):
        self.assertEqual(gate.self_test(), 0, "--self-test 抓不到自己种的错")

    def test_unknown_flag_is_a_usage_error(self):
        """AGENTS §3.1：未知参数不许落到默认动作（否则「默认写盘/默认放行」会被误当成判定）。"""
        with self.assertRaises(SystemExit) as ctx:
            gate.main(["--definitely-not-an-option"])
        self.assertEqual(ctx.exception.code, 2)


class UnreadableFilesAndReporting(SFCase):
    """读不动的用例文件不许被当成干净；违规清单必须点名到文件与行。"""

    def test_syntax_error_file_is_reported_not_skipped(self):
        root = _repo_with("def broken(:\n", name="test_broken_syntax.py")
        bad, _ = gate.scan(root)
        self.assertEqual([b[0] for b in bad], ["test_broken_syntax.py"])
        self.assertIn("解析失败", bad[0][3])

    def test_undecodable_file_is_reported_not_skipped(self):
        root = _repo_with("x = 1\n", name="test_bad_bytes.py")
        (root / "tests" / "test_bad_bytes.py").write_bytes(b"\xff\xfe\x00bad")
        bad, _ = gate.scan(root)
        self.assertEqual([b[0] for b in bad], ["test_bad_bytes.py"])

    def test_violation_listing_names_file_line_and_remedy(self):
        root = _repo_with('''
import sf_presence
class T:
    def test_reads_screen_state(self):
        self.assertTrue(sf_presence.screen_unavailable())
''')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gate.report(root)
        self.assertEqual(rc, 1)
        text = out.getvalue()
        self.assertIn("test_probe_fixture.py:5", text)
        self.assertIn("machine-state-ok", text, "清单必须告诉读的人三种合规改法")


class SelfTestCanFail(SFCase):
    """自检本身必须能红：把关卡换成「瞎」的或「过宽」的替身，自检都得判失败。"""

    def _run_selftest(self, fake_scan):
        err = io.StringIO()
        with mock.patch.object(gate, "scan", fake_scan), contextlib.redirect_stderr(err):
            rc = gate.self_test()
        return rc, err.getvalue()

    def test_blind_gate_fails_the_self_test(self):
        rc, err = self._run_selftest(lambda root: ([], gate.MIN_TEST_FILES + 10))
        self.assertEqual(rc, 1)
        self.assertIn("种了错却没抓到", err)
        self.assertIn("范围红线", err)

    def test_over_wide_gate_fails_the_self_test(self):
        def everything_is_bad(root):
            files = sorted((root / "tests").glob("test_zz_*.py"))
            return [(f.name, "t", 1, "x") for f in files], gate.MIN_TEST_FILES + 10
        rc, err = self._run_selftest(everything_is_bad)
        self.assertEqual(rc, 1)
        self.assertIn("正当写法被判违规", err)


class MainDispatch(SFCase):
    def test_report_and_self_test_together_is_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = gate.main(["--report", "--self-test"])
        self.assertEqual(rc, 2)
        self.assertIn("互斥", err.getvalue())

    def test_self_test_flag_runs_the_self_test(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gate.main(["--self-test"])
        self.assertEqual(rc, 0)
        self.assertIn("自检通过", out.getvalue())

    def test_default_root_is_the_repository(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gate.main([])
        self.assertEqual(rc, 0, out.getvalue())


class ScriptEntryPoint(SFCase):
    def test_running_the_file_as_a_script_exits_with_the_gate_verdict(self):
        """直接 `python tools/machine_state_scan.py` 走的是文件末尾的入口，不是 main()。

        变更行门禁不读 .coveragerc 的排除规则，新文件的入口行也要被真跑过；这里用空目录
        喂它，期望按范围红线退 2（零输入不当通过），同时证明入口把 main() 的返回码原样交出去。
        """
        empty = pathlib.Path(tempfile.mkdtemp(prefix="sf_mss_entry_"))
        (empty / "tests").mkdir()
        err = io.StringIO()
        with mock.patch.object(sys, "argv", [str(SCRIPT), "--dir", str(empty)]), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("范围失效", err.getvalue())


if __name__ == "__main__":
    unittest.main()
