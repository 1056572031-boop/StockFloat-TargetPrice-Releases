# -*- coding: utf-8 -*-
"""30 轮运行现状走查 · 第 9 轮：ETF/基金/债券代码必须还能录入。

为什么单独守这一条（不是把数字改好看的补测，是**用户当天就用不了**的回归）：
v1.4.18 建议#5 给"裸 6 位码直接应用"加了本地字典存在性校验，方向是对的（误输
123456 会把当前股票覆盖成 "--"、顺带清空其目标价，坏码还写进配置）。但本地字典
按东财 `fs` 段拉取，本机实测 5917 行只含股票段（2 位前缀恰好只有 60/68/00/30/92），
基金/ETF、可转债、国债、B 股**一段都没有** —— 于是"字典里查不到"被当成
"代码不存在"：

    输 510300 → 提示「未找到该代码，请核对后重试」
    而 fetch_quote('sh510300') 实测正常返回 沪深300ETF华泰柏瑞 4.326

即"能报价、不能录入"。v1.4.18 之前裸码是直通的，ETF 本来就加得进去，
所以这是回归，不是旧限制。

本文件守的是**行为**：桩只替换外部世界（本地字典 / 行情接口），判定仍走
sf_editor 的真实分支与 sf_universe 的真实判据；不读源码字符串（AGENTS §6.2）。
"""
import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QApplication

import sf_editor                                     # noqa: E402
import sf_universe                                   # noqa: E402
from sf_quote import QuoteDataError                  # noqa: E402

try:
    from _base import SFCase, wait_for
except ImportError:                                   # pragma: no cover - 包模式
    from tests._base import SFCase, wait_for


@contextlib.contextmanager
def fake_universe(rows):
    """把本地字典钉成给定内容。

    为什么必须碰私有成员（discipline-ok(R5) 见下）：sf_universe 的字典是模块级
    私有状态，没有任何公开写入口，而"这个代码段收录没收录"正是被测判据本身 ——
    不打桩就只能依赖真实 5917 行网络缓存，用例会在字典下次刷新后随机变绿变红。
    被测逻辑（covers_segment / do_search 的分支）一行都没换。
    """
    rows_patch = mock.patch.object(sf_universe, "_rows", list(rows))      # discipline-ok(R5)
    code_patch = mock.patch.object(sf_universe, "_by_code", dict(rows))   # discipline-ok(R5)
    with rows_patch, code_patch:
        yield


class TestCoversSegment(SFCase):
    """字典只对"自己确实收录的段"发言。"""

    ROWS = [("sh600519", "贵州茅台"), ("sz000001", "平安银行"),
            ("sh688001", "华兴源创"), ("sz300750", "宁德时代")]

    def test_stock_segments_are_covered(self):
        with fake_universe(self.ROWS):
            self.assertTrue(sf_universe.covers_segment("600519"))
            self.assertTrue(sf_universe.covers_segment("000001"))
            self.assertTrue(sf_universe.covers_segment("688001"))

    def test_fund_and_bond_segments_are_not_covered(self):
        # 51/15/58=ETF、11=可转债、01=国债：字典一段都没有，不能据此判"代码不存在"
        with fake_universe(self.ROWS):
            for code in ("510300", "159915", "588000", "110011", "019547"):
                self.assertFalse(sf_universe.covers_segment(code),
                                 "%s 不该由本地字典判存在性" % code)

    def test_empty_dict_covers_nothing(self):
        # 字典未装载时对所有段都没有发言权（否则首启/离线会把正常码全拦死）
        with fake_universe([]):
            self.assertFalse(sf_universe.covers_segment("600519"))

    def test_non_six_digit_input_not_covered(self):
        with fake_universe(self.ROWS):
            self.assertFalse(sf_universe.covers_segment("贵州茅台"))
            self.assertFalse(sf_universe.covers_segment("12345"))
            self.assertFalse(sf_universe.covers_segment(""))


class TestEditorBareCodeEntry(SFCase):
    """录入分支：股票段仍受字典保护；非股票段向行情要权威答案。"""

    # QApplication 必须由用例自己持有引用（tests/_base.ensure_qapp），否则构造
    # QWidget 时进程直接 abort。
    _app = None

    @classmethod
    def setUpClass(cls):
        cls._app = cls.ensure_qapp()

    def _run(self, text, rows, quote=None, raises=None):
        """走真实的 do_search 分支，返回 (应用的代码列表, 提示文案)。"""
        applied = []
        win = sf_editor.EditorWindow("sh600519", applied.append, QPoint(0, 0), 10)
        win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        win.entry.setText(text)
        side = mock.Mock()
        if raises is not None:
            side.fetch_quote.side_effect = raises
        else:
            side.fetch_quote.return_value = quote
        try:
            with fake_universe(rows), \
                    mock.patch.object(sf_universe, "ensure_ready", lambda *a: None), \
                    mock.patch("sf_quote.fetch_quote", side.fetch_quote):
                win.do_search()
                def complete():
                    QApplication.processEvents()
                    return bool(applied) or win.hint.text() != "搜索中..."
                self.assertTrue(wait_for(complete), "后台代码核验未结束")
        finally:
            win.close()
            win.deleteLater()
        return list(applied), win.hint.text()

    def test_etf_code_is_applied_when_quote_confirms_it(self):
        applied, _hint = self._run(
            "510300", [("sh600519", "贵州茅台")],
            quote={"name": "沪深300ETF华泰柏瑞", "code": "sh510300", "price": "4.326"})
        self.assertEqual(applied, ["sh510300"],
                         "行情确认存在的 ETF 必须照常录入（这就是回归的用户可见后果）")

    def test_nonexistent_code_is_rejected_without_applying(self):
        applied, hint = self._run("123456", [("sh600519", "贵州茅台")],
                                  raises=QuoteDataError("field count too short"))
        self.assertEqual(applied, [], "查无此码不得写进配置")
        self.assertIn("未找到", hint)

    def test_network_failure_does_not_lock_the_user_out(self):
        # 探测本身失败（断网/超时）时沿用"可用性优先"放行：
        # 绝不因为校验器拿不到答案就把正常录入堵死，也不让编辑窗卡死。
        applied, _hint = self._run("510300", [("sh600519", "贵州茅台")],
                                   raises=OSError("network down"))
        self.assertEqual(applied, ["sh510300"])

    def test_wrong_stock_response_is_still_rejected(self):
        # 字典未收录改用网络核验，但另一个代码的响应不能冒充当前输入。
        applied, hint = self._run("600999", [("sh600519", "贵州茅台")],
                                  quote={"name": "贵州茅台", "code": "sh600519"})
        self.assertEqual(applied, [])
        self.assertIn("未找到", hint)

    def test_empty_input_applies_nothing(self):
        applied, _hint = self._run("", [("sh600519", "贵州茅台")],
                                   quote={"name": "贵州茅台"})
        self.assertEqual(applied, [])
