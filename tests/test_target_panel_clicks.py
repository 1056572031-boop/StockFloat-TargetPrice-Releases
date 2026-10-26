# -*- coding: utf-8 -*-
"""目标价面板：每一枚可点件都**真点一次**（2026-10-23，"发布流程补真实点击" A 档之一）。

由来：`test_target_panel.py` 那 104 条把添加 / 生效 / 清空全写成 `p._add(...)`、
`p._apply()` 直调 —— 它证明**槽函数逻辑对**，但没证明**用户点那枚按钮真的走到那个槽**。
`clicked` / `returnPressed` 的接线一旦漏连或被改名（本仓真实缺陷形态是 AttributeError
被兜异常吞掉，界面照样把按钮画成可点），逻辑照样全绿而用户点下去没反应。

本文件只做一件事：面板上的可点件与输入框全部走 `QTest` 真事件，断言只看可观测输出 ——
列表行的标签文案、输入框是否被清空、状态行写了什么、回调收到的参数。

离屏（offscreen）可跑，不需要真屏幕，因此进全量回归与提交钩子。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QLabel, QPushButton

import target_panel
from sf_constants import TARGET_DIR_ABOVE, TARGET_DIR_BELOW
from tests._base import SFCase

# 用例内的样本价位：涨到必须高于现价，跌到必须低于现价（v1.2.84 那条硬规则）
CODE = "600519"
CURRENT_PRICE = 1500.0
ABOVE_TEXT = "1600"
BELOW_TEXT = "1400"
BELOW_OR_EQUAL_TEXT = "1400"
# 面板按 price_decimals(code) 出两位小数，行内标签的 toolTip 存的是未省略的原文
ABOVE_LABEL = "1600.00"
BELOW_LABEL = "1400.00"


def _saved(direction, target):
    """一条已落盘形状的目标价（与 test_target_panel.py 的 _saved 同一形状）。"""
    return {"direction": direction, "target": float(target),
            "session_date": "2026-12-31", "state": "pending", "last_side": None}


class TargetPanelClicks(SFCase):
    def setUp(self):
        super().setUp()
        self.app = self.ensure_qapp()
        self.applied = []
        self.clear_calls = []
        self._open_panels = []

    def tearDown(self):
        # §6.11：关窗必须发生在 patch 还活着的时候，所以先关自己开的窗再交给基类
        for p in self._open_panels:
            try:
                p.close()
            except RuntimeError:
                pass
        self._open_panels = []
        super().tearDown()

    def _make(self, saved=(), price=None):
        def _on_apply(alerts):
            self.applied.append(alerts)
            return True

        def _on_clear():
            self.clear_calls.append(1)
            return True

        self.track_patch(patch.object(target_panel, "load_alerts",
                                      return_value=list(saved)))
        p = target_panel.TargetPanel(
            CODE, "贵州茅台", _on_apply, _on_clear,
            current_code_fn=lambda: CODE,
            current_price_fn=(lambda: price) if price is not None else None)
        p.show()
        self._open_panels.append(p)
        return p

    @staticmethod
    def _rows(p):
        """按视觉顺序取列表行（方向标签文案, 价位标签原文）。

        只读行内标签：价位标签显示时可能被省略号截断，未截断的原文在它 toolTip 里
        （`_target_row` 就是这么写的），断言 toolTip 比断言 text 稳。
        """
        out = []
        for i in range(p.list_box.count()):
            row = p.list_box.itemAt(i).widget()
            out.append((row.findChild(QLabel, "targetDirection").text(),
                        row.findChild(QLabel, "targetPrice").toolTip()))
        return out

    # ---------- 添加 ----------
    def test_真敲进输入框再点加号_行才进列表(self):
        """接线核对：`btn_up.clicked` → `_add(涨到)`，且添加后只改内存不落盘。"""
        p = self._make()
        QTest.keyClicks(p.edit_up, ABOVE_TEXT)
        self.assertEqual(p.edit_up.text(), ABOVE_TEXT, "真按键没进框，说明输入被过滤器拦了")
        QTest.mouseClick(p.btn_up, Qt.MouseButton.LeftButton)
        self.assertEqual(self._rows(p), [("涨到", ABOVE_LABEL)])
        self.assertEqual(p.edit_up.text(), "", "添加成功后输入框应被清空")
        self.assertEqual(self.applied, [], "添加只改内存，落盘要等「立即生效」")

    def test_跌到那一行真按回车等于点加号(self):
        """接线核对：`edit_dn.returnPressed` → `_add(跌到)`（D1）。"""
        p = self._make()
        QTest.keyClicks(p.edit_dn, BELOW_TEXT)
        QTest.keyClick(p.edit_dn, Qt.Key.Key_Return)
        self.assertEqual(self._rows(p), [("跌到", BELOW_LABEL)])

    def test_价不高于现价时点加号只报错且不吞掉用户输入(self):
        """v1.2.84 那条拒绝逻辑必须**点得出来**：拒绝时输入留在框里，用户能改。"""
        p = self._make(price=CURRENT_PRICE)
        QTest.keyClicks(p.edit_up, BELOW_OR_EQUAL_TEXT)
        QTest.mouseClick(p.btn_up, Qt.MouseButton.LeftButton)
        self.assertEqual(self._rows(p), [])
        self.assertIn("需高于当前价", p.status_label.text())
        self.assertEqual(p.edit_up.text(), BELOW_OR_EQUAL_TEXT)

    # ---------- 立即生效 ----------
    def test_点立即生效_回调收到刚点进去的那条(self):
        p = self._make()
        QTest.keyClicks(p.edit_up, ABOVE_TEXT)
        QTest.mouseClick(p.btn_up, Qt.MouseButton.LeftButton)
        QTest.mouseClick(p.btn_apply, Qt.MouseButton.LeftButton)
        self.assertEqual(len(self.applied), 1)
        entry = self.applied[0][0]
        self.assertEqual((entry["direction"], entry["target"]),
                         (TARGET_DIR_ABOVE, float(ABOVE_TEXT)))
        self.assertTrue(entry.get("session_date"), "生效日应在点下「立即生效」这一刻生成（D17）")

    def test_列表为空时那两枚按钮真的点不动(self):
        """禁用态必须由**真点击**证伪：光断言 isEnabled() 测不出"禁用了还照样落盘"。"""
        p = self._make()
        for button in (p.btn_apply, p.btn_clear):
            self.assertFalse(button.isEnabled(), "空列表时底部按钮必须禁用")
        QTest.mouseClick(p.btn_apply, Qt.MouseButton.LeftButton)
        QTest.mouseClick(p.btn_clear, Qt.MouseButton.LeftButton)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.clear_calls, [])

    # ---------- 清空（两段式）----------
    def test_清空要真点两下_第一下一行都不少(self):
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, ABOVE_TEXT),
                               _saved(TARGET_DIR_BELOW, BELOW_TEXT)])
        QTest.mouseClick(p.btn_clear, Qt.MouseButton.LeftButton)
        self.assertEqual(p.btn_clear.text(), "确认清空")
        self.assertEqual(self.clear_calls, [], "第一下只是预警，不许落盘（D3）")
        self.assertEqual(len(self._rows(p)), 2)
        QTest.mouseClick(p.btn_clear, Qt.MouseButton.LeftButton)
        self.assertEqual(self.clear_calls, [1])
        self.assertEqual(self._rows(p), [])

    def test_点某一行的叉只删掉那一行(self):
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, ABOVE_TEXT),
                               _saved(TARGET_DIR_BELOW, BELOW_TEXT)])
        QTest.mouseClick(p.findChildren(QPushButton, "del")[0], Qt.MouseButton.LeftButton)
        self.assertEqual(self._rows(p), [("跌到", BELOW_LABEL)])
        self.assertEqual(self.applied, [], "删单行同样要等「立即生效」才落盘")

    # ---------- 关闭 ----------
    def test_点右上角关闭既不保存也不清空(self):
        """✕ 与 Esc 等价（D2）：这条以前只由直调 `_on_close_x` 与 `_esc` 覆盖。

        前提必须让「已保存的一条 + 新加的一条」同时存在：若只测空列表，回滚后
        `_pending` 本就为空，即便 ✕ 被改成「回滚完顺手保存一次」也照样看不出 ——
        实测种这一处错时旧写法判绿，所以留了已落盘的那条当判别样本。
        """
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, ABOVE_TEXT)])
        QTest.keyClicks(p.edit_dn, BELOW_TEXT)
        QTest.mouseClick(p.btn_dn, Qt.MouseButton.LeftButton)
        self.assertEqual(len(self._rows(p)), 2)
        QTest.mouseClick(p.btn_close_x, Qt.MouseButton.LeftButton)
        self.assertEqual(self.applied, [], "点 ✕ 不许落盘")
        self.assertEqual(self.clear_calls, [], "点 ✕ 不许清空已保存的目标价")
        self.assertFalse(p.isVisible())


if __name__ == "__main__":
    unittest.main()
