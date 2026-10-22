# -*- coding: utf-8 -*-
"""`release_checks.txt` 的 `testref`：登记的测试必须真在源码里，且只是补充证据。

起因（2026-10-21）：v1.4.101 有五个模块只改了函数体或字面量，包内拿不出新名字，
证据只能写成清单里的注释"判据是 tests/xxx.py::test_yyy"。闸门读不到注释，测试改名或
删掉之后注释照样好看。`testref` 把它变成可读的一行，由 `tools/release_feature_check.py`
去源码里确认那个用例还在。

这里走 `main()` 而不只测函数：函数写对了、却没接进发版入口，闸门照样是摆设。
形状判据（路径逃逸、辅助函数冒充、skip 等）在闸门自己的 `--self-test` 里，这里不重复。
"""
import contextlib
import io
import pathlib
import sys
import tempfile
from unittest import mock

from tests._base import SFCase

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import release_feature_check as rfc  # noqa: E402

FAKE_VERSION = "9.9.9"     # 清单里写的、与真实 APP_VERSION 无关的占位版本

# 本文件自己的一条用例：testref 指向一个一定存在的真实测试，不依赖别的文件。
REAL_REF = "tests/test_release_feature_testref.py::TestTestrefInTheGate::test_a_real_ref_is_accepted"


class TestTestrefInTheGate(SFCase):
    def _checks(self, body):
        folder = tempfile.TemporaryDirectory(prefix="sf-testref-")
        self.addCleanup(folder.cleanup)
        path = pathlib.Path(folder.name) / "release_checks.txt"
        path.write_text(body, encoding="utf-8")
        return path

    def _main(self, path):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            # --allow-stale-feature：只跳过『新增特征』基线，不碰 git；其余判据照常。
            code = rfc.main([str(ROOT / "并不存在.app"), "--checks", str(path),
                             "--version", FAKE_VERSION, "--allow-stale-feature"])
        return code, buffer.getvalue()

    def test_a_real_ref_is_accepted(self):
        path = self._checks("version 9.9.9\ncheckfile Contents/x.sh::y\ntestref %s\n" % REAL_REF)
        _, out = self._main(path)
        self.assertIn("testref 1 条", out)
        self.assertNotIn("找不到这个用例", out)
        self.assertNotIn("没有这个测试文件", out)

    def test_a_vanished_test_blocks_the_release_through_main(self):
        path = self._checks("version 9.9.9\ncheckfile Contents/x.sh::y\n"
                            "testref tests/test_release_feature_testref.py::test_renamed_away\n")
        code, out = self._main(path)
        self.assertEqual(code, 1, out)
        self.assertIn("找不到这个用例", out)
        self.assertIn("拒绝进入产物核验", out)

    def test_a_missing_test_file_blocks_the_release_through_main(self):
        path = self._checks("version 9.9.9\ncheckfile Contents/x.sh::y\n"
                            "testref tests/test_gone_for_good.py::test_x\n")
        code, out = self._main(path)
        self.assertEqual(code, 1, out)
        self.assertIn("没有这个测试文件", out)

    def test_malformed_testref_line_is_reported_not_dropped(self):
        parsed = rfc.parse_checks(self._checks("version 9.9.9\ntestref tests/test_x.py\n"))
        self.assertEqual(parsed["testref"], [])
        self.assertTrue(any("testref" in e for e in parsed["errors"]), parsed["errors"])

    def test_testref_alone_is_not_a_feature(self):
        parsed = rfc.parse_checks(self._checks("version 9.9.9\ntestref %s\n" % REAL_REF))
        problems = rfc.gate(parsed, FAKE_VERSION)
        self.assertTrue(any("一条 check/forbid 都没有" in p for p in problems),
                        "只登记测试、没有任何 check/forbid，仍是『没有本轮特征』")

    def _run_self_test(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = rfc.self_test()
        return code, buffer.getvalue()

    def test_self_test_passes_on_the_real_implementation(self):
        code, out = self._run_self_test()
        self.assertEqual(code, 0, out)

    def test_self_test_notices_a_testref_check_that_lets_everything_through(self):
        with mock.patch.object(rfc, "testref_problems", lambda parsed, root=None: []):
            code, out = self._run_self_test()
        self.assertEqual(code, 1, out)
        self.assertIn("没被拒或理由不对", out)

    def test_self_test_notices_a_testref_check_that_rejects_everything(self):
        with mock.patch.object(rfc, "testref_problems", lambda parsed, root=None: ["always"]):
            code, out = self._run_self_test()
        self.assertEqual(code, 1, out)
        self.assertIn("被误拒", out)

    def test_self_test_notices_a_parser_that_drops_testref_lines(self):
        real_parse = rfc.parse_checks

        def drops_testref(path):
            parsed = real_parse(path)
            if pathlib.Path(path).name == "tr_checks.txt":
                parsed["testref"] = []
            return parsed

        with mock.patch.object(rfc, "parse_checks", drops_testref):
            code, out = self._run_self_test()
        self.assertEqual(code, 1, out)
        self.assertIn("testref 行的解析有误", out)

    def test_self_test_notices_a_gate_that_counts_testref_as_a_feature(self):
        real_gate = rfc.gate

        def lenient(parsed, want):
            return [] if parsed.get("testref") and not parsed["check"] else real_gate(parsed, want)

        with mock.patch.object(rfc, "gate", lenient):
            code, out = self._run_self_test()
        self.assertEqual(code, 1, out)
        self.assertIn("被当成了本轮特征", out)

    def test_self_test_notices_a_newness_check_that_counts_testref(self):
        real_judge = rfc.judge_newness

        def lenient(parsed, prev_lines, prev_sources):
            return [] if parsed.get("testref") else real_judge(parsed, prev_lines, prev_sources)

        with mock.patch.object(rfc, "judge_newness", lenient):
            code, out = self._run_self_test()
        self.assertEqual(code, 1, out)
        self.assertIn("被算进『新增特征』", out)

    def test_every_ref_the_current_checklist_cites_still_exists(self):
        """仓库里这份清单自己登记的 testref（若有）必须都在 —— 闸门自检不替真实清单把关。"""
        parsed = rfc.parse_checks(ROOT / "release_checks.txt")
        self.assertEqual(rfc.testref_problems(parsed), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
