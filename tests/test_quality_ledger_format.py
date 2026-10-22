# -*- coding: utf-8 -*-
"""台账 `tools/escapes_ledger.json` 的排版必须是工具写出的规范写法。

起因（2026-10-21）：台账里有人手写的紧凑数组（`"failed_lines": ["verify", "locate"]`），
下次用 `quality_trend.py` 写入时整段被展开，同一笔提交里混进纯排版变动，内容改动被淹没，
审查的人分辨不出哪几行是真改动。判据：原文与 `_save` 的写法逐字一致。

`--ledger` 指向自建台账时不查排版（夹具是紧凑写的，且不是真账）；发版链的 `--gate`
只提示不拦截，拦截放在 `--check` 与本文件这条对真实台账的判据上。
"""
import contextlib
import io
import json
import tempfile
from pathlib import Path
from typing import cast
from unittest import mock

from tests._base import SFCase
from tools import quality_trend as qt

COMPACT = {"schema": 1, "updated": "2026-10-09", "rounds": [], "entries": []}


class TestLedgerFormat(SFCase):
    def _ledger(self, text):
        folder = tempfile.TemporaryDirectory(prefix="sf-ledger-format-")
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "ledger.json"
        path.write_text(text, encoding="utf-8")
        return path

    def _cli(self, *argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = qt.main(list(argv))
        return code, buffer.getvalue()

    def test_the_real_ledger_is_in_canonical_form(self):
        self.assertEqual(qt.format_problems(qt.LEDGER), [],
                         "台账排版不是规范写法：运行 tools/quality_trend.py --fix-format，排版变动单独提交")

    def test_hand_written_compact_array_is_reported_with_a_fix_hint(self):
        path = self._ledger('{"schema": 1, "updated": "2026-10-09", "rounds": [], "entries": []}\n')
        problems = qt.format_problems(path)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("--fix-format", problems[0])

    def test_fixing_changes_layout_only_and_is_idempotent(self):
        text = json.dumps(COMPACT, ensure_ascii=False) + "\n"
        path = self._ledger(text)
        self.assertTrue(qt.fix_format(path))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), COMPACT)
        self.assertEqual(qt.format_problems(path), [])
        self.assertFalse(qt.fix_format(path), "已是规范写法时再规范化不该报有改动")

    def test_a_missing_trailing_newline_alone_counts_as_non_canonical(self):
        path = self._ledger(qt.canonical_text(COMPACT).rstrip("\n"))
        self.assertTrue(qt.format_problems(path))

    def test_unparseable_ledger_is_an_error_not_a_pass(self):
        path = self._ledger("{ not json")
        with self.assertRaises(ValueError):
            qt.format_problems(path)

    def test_check_fails_on_the_default_ledger_when_layout_is_not_canonical(self):
        original = qt.LEDGER_PATH
        self.addCleanup(setattr, qt, "LEDGER_PATH", original)
        path = self._ledger(json.dumps(COMPACT, ensure_ascii=False) + "\n")
        qt.LEDGER_PATH = path          # 等价于默认台账换成这份；不经 --ledger，所以会查排版
        code, out = self._cli("--check")
        self.assertEqual(code, 1, out)
        self.assertIn("规范写法", out)

    def test_gate_only_warns_about_layout_and_keeps_the_gate_result(self):
        original = qt.LEDGER_PATH
        self.addCleanup(setattr, qt, "LEDGER_PATH", original)
        path = self._ledger(json.dumps(COMPACT, ensure_ascii=False) + "\n")
        qt.LEDGER_PATH = path
        code, out = self._cli("--gate", "")       # 空串 = 只校验自洽，不要求某一版的复盘声明
        self.assertEqual(code, 0, "排版问题不该让发版闸门红：" + out)
        self.assertIn("提示：", out)

    def test_custom_ledger_flag_skips_the_layout_check(self):
        path = self._ledger(json.dumps(COMPACT, ensure_ascii=False) + "\n")
        original = qt.LEDGER_PATH
        self.addCleanup(setattr, qt, "LEDGER_PATH", original)
        code, out = self._cli("--check", "--ledger", str(path))
        self.assertEqual(code, 0, out)
        self.assertNotIn("规范写法", out)

    def test_fix_format_puts_the_original_back_when_the_readback_differs(self):
        real = self._ledger(json.dumps(COMPACT, ensure_ascii=False) + "\n")
        original_text = real.read_text(encoding="utf-8")

        class TruncatingPath:
            """第一次写入只落下一截（模拟写盘出错），之后的写入——含恢复原文——正常。"""

            def __init__(self):
                self.writes = 0

            def read_text(self, encoding=None):
                return real.read_text(encoding=encoding)

            def write_text(self, text, encoding=None):
                self.writes += 1
                real.write_text(text[:8] if self.writes == 1 else text, encoding=encoding)

        with self.assertRaises(SystemExit):
            qt.fix_format(cast(Path, TruncatingPath()))
        self.assertEqual(real.read_text(encoding="utf-8"), original_text,
                         "读回对不上时必须把原文放回去，不能留下半截台账")

    def _run_self_test(self):
        original = qt.LEDGER_PATH
        self.addCleanup(setattr, qt, "LEDGER_PATH", original)   # 自检内部走 --ledger，改的是模块级全局
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = qt.self_test()
        return code, buffer.getvalue()

    def test_self_test_passes_on_the_real_implementation(self):
        code, out = self._run_self_test()
        self.assertEqual(code, 0, out)

    def test_self_test_notices_a_layout_check_that_cries_wolf(self):
        with mock.patch.object(qt, "format_problems", lambda path=None: ["always"]):
            code, out = self._run_self_test()
        self.assertEqual(code, 1, out)
        self.assertIn("误判为排版不合规", out)

    def test_self_test_notices_a_fixture_that_is_not_really_compact(self):
        flat = lambda data: json.dumps(data, ensure_ascii=False) + "\n"   # noqa: E731
        with mock.patch.object(qt, "canonical_text", flat):
            code, out = self._run_self_test()
        self.assertEqual(code, 1, out)
        self.assertIn("没造出紧凑数组", out)

    def test_self_test_notices_a_layout_check_that_lets_compact_arrays_through(self):
        with mock.patch.object(qt, "format_problems", lambda path=None: []):
            code, out = self._run_self_test()
        self.assertEqual(code, 1, out)
        self.assertIn("没被排版判据抓到", out)

    def test_self_test_notices_a_fix_that_does_nothing(self):
        with mock.patch.object(qt, "fix_format", lambda path=None: False):
            code, out = self._run_self_test()
        self.assertEqual(code, 1, out)
        self.assertIn("没把紧凑数组改回规范写法", out)

    def test_self_test_notices_a_fix_that_changes_content(self):
        def altering(path=None):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["updated"] = "1999-01-01"
            path.write_text(qt.canonical_text(data), encoding="utf-8")
            return True

        with mock.patch.object(qt, "fix_format", altering):
            code, out = self._run_self_test()
        self.assertEqual(code, 1, out)
        self.assertIn("改动了台账内容", out)

    def test_self_test_notices_a_fix_that_is_not_idempotent(self):
        real_fix = qt.fix_format

        def always_reports_change(path=None):
            real_fix(path)
            return True

        with mock.patch.object(qt, "fix_format", always_reports_change):
            code, out = self._run_self_test()
        self.assertEqual(code, 1, out)
        self.assertIn("应当幂等", out)

    def test_fix_format_flag_rewrites_the_default_ledger(self):
        original = qt.LEDGER_PATH
        self.addCleanup(setattr, qt, "LEDGER_PATH", original)
        path = self._ledger(json.dumps(COMPACT, ensure_ascii=False) + "\n")
        qt.LEDGER_PATH = path
        code, _ = self._cli("--fix-format")
        self.assertEqual(code, 0)
        self.assertEqual(qt.format_problems(path), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
