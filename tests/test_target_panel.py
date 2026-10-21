# -*- coding: utf-8 -*-
"""「目标价」设置面板回归测试（v1.2.77 四维度自审修复）。

覆盖自审发现的交互/UI/逻辑/边界缺陷，均为离屏（offscreen）可跑的确定性断言：
  D1  回车添加（returnPressed 已连接）
  D2  Esc 关闭且不保存
  D3  「清空」需二次确认，误触不丢数据
  D9  错误提示写在独立状态行，不再覆盖目标价列表
  D10 列表区分"已触发"
  D13/D21 校验提示为用户语言
  D15 保存失败有提示且不关闭
  D17 生效日在"立即生效"时计算，不在添加时冻结
  D18 同方向覆盖给出"已更新"提示
  D25 股票已切换时拒绝保存
"""
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import Qt, QEvent
from PyQt6.QtWidgets import QApplication, QLabel, QSizePolicy

from sf_constants import TARGET_DIR_ABOVE, TARGET_DIR_BELOW
import target_panel
import sf_editor
import sf_ui_render


def _saved(direction, target, state="pending"):
    return {"direction": direction, "target": float(target),
            "session_date": "2026-12-31", "state": state, "last_side": None}


class TestTargetPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        # 持有引用，避免面板被 GC 后 Qt 定时器/信号报警
        self._panels = []
        self.applied = []
        self.clear_calls = []

    def _make(self, saved=(), code="600519", current_code=None,
              apply_ok=True, clear_ok=True, price=None):
        def _on_apply(alerts):
            self.applied.append(alerts)
            return apply_ok

        def _on_clear():
            self.clear_calls.append(1)
            return clear_ok

        with patch.object(target_panel, "load_alerts", return_value=list(saved)):
            p = target_panel.TargetPanel(
                code, "贵州茅台", _on_apply, _on_clear,
                current_code_fn=lambda: current_code or code,
                current_price_fn=(lambda: price) if price is not None else None)
        self._panels.append(p)
        return p

    # ---------- 输入框几何 ----------
    def test_target_panel_keeps_wide_input_area_with_narrow_main_window(self):
        """主窗缩窄时，目标价面板仍应保持可完整输入价格的最小宽度。"""
        p = self._make()
        self.assertEqual(p.width(), target_panel.PANEL_W)
        self.assertEqual(target_panel.TARGET_INPUT_W, 100)
        self.assertEqual(p.edit_up.width(), 100)
        self.assertEqual(p.edit_dn.width(), 100)

    # ---------- D13/D21 校验文案 ----------
    def test_empty_input_friendly_message(self):
        p = self._make()
        p.edit_up.setText("")
        p._add(TARGET_DIR_ABOVE)
        self.assertIn("请输入目标价", p.status_label.text())
        self.assertEqual(p._pending, [])

    def test_invalid_text_friendly_message(self):
        p = self._make()
        p.edit_up.setText("abc")
        p._add(TARGET_DIR_ABOVE)
        self.assertIn("请输入有效价格", p.status_label.text())

    def test_currency_symbol_friendly_message(self):
        p = self._make()
        p.edit_up.setText("¥1500")
        p._add(TARGET_DIR_ABOVE)
        self.assertIn("请输入有效价格", p.status_label.text())

    def test_zero_absolute_rejected(self):
        p = self._make()
        p.edit_up.setText("0")
        p._add(TARGET_DIR_ABOVE)
        self.assertIn("价格必须大于 0", p.status_label.text())

    def test_relative_without_price_rejected(self):
        """P1-3：-5 现视为相对涨跌幅，无当前价时给出友好提示而非崩溃。"""
        p = self._make()
        p.edit_up.setText("-5")
        p._add(TARGET_DIR_ABOVE)
        self.assertIn("无法获取当前价", p.status_label.text())

    def test_error_does_not_clobber_list(self):
        """D9：报错写在状态行，目标价列表保持可见。"""
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)])
        p.edit_up.setText("abc")
        p._add(TARGET_DIR_ABOVE)
        self.assertIn("涨到 1600.00", p._list_text())
        self.assertIn("请输入有效价格", p.status_label.text())

    # ---------- D1 回车添加 ----------
    def test_enter_submits(self):
        p = self._make()
        p.edit_up.setText("1600")
        p.edit_up.returnPressed.emit()
        self.assertEqual(len(p._pending), 1)
        self.assertEqual(p._pending[0]["target"], 1600.0)

    # ---------- D18 同方向：v1.2.81 起改为追加而非覆盖 ----------
    def test_same_direction_distinct_price_appends(self):
        """P1-4：同方向不同价位 → 追加为两条，不再覆盖成一条。"""
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)])
        p.edit_up.setText("1700")
        p._add(TARGET_DIR_ABOVE)
        self.assertEqual(len(p._pending), 2)
        targets = sorted(a["target"] for a in p._pending)
        self.assertEqual(targets, [1600.0, 1700.0])
        self.assertEqual(p.status_label.text(), "")

    def test_same_slot_refuses_duplicate(self):
        """同方向 + 同价位 → 拒绝重复（避免同一目标价存两份）。"""
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)])
        p.edit_up.setText("1600")
        p._add(TARGET_DIR_ABOVE)
        self.assertEqual(len(p._pending), 1)
        self.assertIn("已存在", p.status_label.text())

    def test_two_directions_coexist(self):
        p = self._make()
        p.edit_up.setText("1600")
        p._add(TARGET_DIR_ABOVE)
        p.edit_dn.setText("1400")
        p._add(TARGET_DIR_BELOW)
        self.assertEqual(len(p._pending), 2)

    def test_readding_fired_same_price_is_refused_honestly(self):
        """已触发的目标价同价位重设：不得假装成功（合并会保留 hit，永远不会再提醒）。"""
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600, state="hit")])
        p.edit_up.setText("1600")
        p._add(TARGET_DIR_ABOVE)
        self.assertIn("已触发", p.status_label.text())
        self.assertEqual(p._pending[0]["state"], "hit")   # 保持原状，未被偷偷重置
        self.assertEqual(p.edit_up.text(), "1600")        # 输入保留，用户可改价

    def test_changing_price_after_fired_works(self):
        """已触发后换个价格：应正常追加为待触发（原已触发条目保留）。"""
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600, state="hit")])
        p.edit_up.setText("1700")
        p._add(TARGET_DIR_ABOVE)
        self.assertTrue(any(a["target"] == 1700.0 and a["state"] == "pending"
                            for a in p._pending))
        self.assertTrue(any(a["target"] == 1600.0 and a["state"] == "hit"
                            for a in p._pending))

    # ---------- D3 清空二次确认 ----------
    def test_clear_requires_two_clicks(self):
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)])
        p.show()
        QApplication.processEvents()
        p._clear()
        self.assertEqual(self.clear_calls, [])          # 第一次只是预警
        self.assertIn("确认清空", p.btn_clear.text())
        self.assertTrue(p.btn_clear.property("confirmClear"))
        self.assertIn("confirmClear", p.styleSheet())
        p._clear()
        QApplication.processEvents()
        self.assertEqual(self.clear_calls, [1])         # 第二次才真正清空
        self.assertTrue(p.isVisible(), "清空成功后面板应保持打开，不可闪一下自动关闭")
        self.assertEqual(p._pending, [])
        self.assertFalse(p.btn_clear.property("confirmClear"))
        self.assertIsNone(p._close_timer)

    def test_clear_armed_resets_on_add(self):
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)])
        p._clear()
        p.edit_dn.setText("1400")
        p._add(TARGET_DIR_BELOW)
        self.assertEqual(p.btn_clear.text(), "清空")

    def test_clear_noop_when_empty(self):
        p = self._make()
        p._clear()
        self.assertEqual(self.clear_calls, [])
        self.assertEqual(p.status_label.text(), "")

    def test_clear_failure_keeps_state_and_allows_retry(self):
        """清空落盘失败：内存/界面保持原样，且保留「确认清空」态可再点重试。"""
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)], clear_ok=False)
        p._clear()                                   # 第一次：进入确认态
        p._clear()                                   # 第二次：执行但落盘失败
        self.assertIn("清空失败", p.status_label.text())
        self.assertEqual(len(p._pending), 1)         # 内存未被清空
        self.assertIn("涨到 1600.00", p._list_text())
        self.assertEqual(p.btn_clear.text(), "确认清空")   # 仍处于可重试状态

    # ---------- showEvent 路径 ----------
    def test_show_event_refreshes_list_and_focuses(self):
        """覆盖 showEvent：它负责按真实宽度重刷列表 + 设置初始焦点。

        此前所有用例都不调用 show()，该路径完全未覆盖 —— 一旦 showEvent 抛异常，
        面板在真机上会表现为"打开即崩"，而测试全绿。

        v1.2.87 补丁（S2）：焦点**始终落在输入框**，不再区分交易时段。
        原实现在非交易时段聚焦「+」按钮，按钮拿到焦点会抢走输入框的键盘通道
        （直接打字无效、空格误触发「+」），已按用户拍板改回输入框。
        """
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)])
        p.show()
        try:
            self.assertIn("1600.00", p._list_text())
            # 断言"面板内部焦点"而非 hasFocus()：离屏窗口无法成为 active window，
            # 应用级焦点恒为 None，用 hasFocus() 会假失败（平台差异，非代码缺陷）。
            self.assertIs(p.focusWidget(), p.edit_up,
                          "打开后焦点应落在「涨到」输入框")
        finally:
            p.close()

    def test_focus_never_lands_on_close_button(self):
        """S2 兜底：焦点绝不能落到右上角 ✕ —— 否则空格/回车直接关窗丢弃输入。

        构造「涨到」方向已满 3 条（该输入框被禁用）的场景，验证焦点退到
        「跌到」输入框，而不是回落到创建顺序上第一个可聚焦控件（即 ✕）。
        """
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600),
                              _saved(TARGET_DIR_ABOVE, 1610),
                              _saved(TARGET_DIR_ABOVE, 1620)])
        p.show()
        try:
            self.assertFalse(p.edit_up.isEnabled(), "涨到已满 3 条，输入框应禁用")
            fw = p.focusWidget()
            self.assertIsNot(fw, p.btn_close_x, "焦点不得落在关闭按钮上")
            self.assertIs(fw, p.edit_dn, "首选输入框不可用时焦点应退到次选输入框")
        finally:
            p.close()

    # ---------- D15 失败可见 ----------
    def test_apply_failure_reports_and_keeps_open(self):
        p = self._make(apply_ok=False)
        p.edit_up.setText("1600")
        p._add(TARGET_DIR_ABOVE)
        p._apply()
        self.assertIn("保存失败", p.status_label.text())
        self.assertEqual(len(self.applied), 1)          # 确实尝试过保存

    def test_apply_empty_pending_warns(self):
        p = self._make()
        p._apply()
        self.assertEqual(self.applied, [])
        self.assertEqual(p.status_label.text(), "")

    # ---------- D17 生效日不冻结 ----------
    def test_session_date_computed_at_apply_not_add(self):
        p = self._make()
        p.edit_up.setText("1600")
        p._add(TARGET_DIR_ABOVE)
        # 添加时不应冻结生效日
        self.assertNotIn("session_date", p._pending[0])
        p._apply()
        self.assertEqual(len(self.applied), 1)
        a = self.applied[0][0]
        self.assertTrue(a.get("session_date"))          # 生效时才生成
        self.assertEqual(a["target"], 1600.0)
        self.assertEqual(a["direction"], TARGET_DIR_ABOVE)

    # ---------- D25 切股保护 ----------
    def test_apply_blocked_when_stock_switched(self):
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)],
                       code="600519", current_code="000001")
        p._apply()
        self.assertEqual(self.applied, [])
        self.assertIn("已切换股票", p.status_label.text())

    # ---------- D6 未生效修改 ----------
    def test_has_unsaved_changes(self):
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)])
        self.assertFalse(p.has_unsaved_changes())
        p.edit_up.setText("1700")
        p._add(TARGET_DIR_ABOVE)
        self.assertTrue(p.has_unsaved_changes())

    def test_apply_syncs_saved_signature(self):
        """保存成功后同步签名：否则 320ms 关闭延迟内重开会误报"有未生效修改"。"""
        p = self._make()
        p.edit_up.setText("1600")
        p._add(TARGET_DIR_ABOVE)
        self.assertTrue(p.has_unsaved_changes())
        p._apply()
        self.assertFalse(p.has_unsaved_changes())

    def test_clear_syncs_saved_signature(self):
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)])
        p._clear()
        p._clear()
        self.assertFalse(p.has_unsaved_changes())

    # ---------- D10 已触发标记 ----------
    def test_list_marks_hit(self):
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600, state="hit")])
        self.assertIn("已触发", p._list_text())

    def test_hit_marker_survives_row_truncation(self):
        """行宽不够时先切百分比，"已触发"必须留着。

        判据用极端价位把行撑爆，而不是去改面板宽度：宽度是产品自己算的，
        测试替它预设就等于没测那一段（AGENTS §3.1 那条测法规矩）。
        这条用例的存在理由：标记原先排在行尾，2026-10-10 在云端复检机上被切成
        「已触…」，本机永远复现不了 —— 换字体度量就会露出，而它是用户判断
        "这条还要不要重设"的唯一依据。
        """
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1e308, state="hit")],
                       price=1600.0)
        text = p._list_text()  # discipline-ok(R5): 判据是"用户在列表里看到什么"，而面板对外的可读文本只有这个出口（同文件既有 D10 用例同口径）
        marker = target_panel.HIT_MARKER_TEXT
        self.assertTrue(marker, "标记文字被换成空串了 —— 下面三条断言会集体空跑")
        self.assertIn("…", text, "前提没成立：这一行根本没被截断，等于空跑")
        self.assertIn(marker, text,
                      "状态标记被省略号吃掉了 —— 用户看不出这条已经触发过")
        marker_labels = [lab for lab in p.findChildren(QLabel) if lab.text() == marker]
        self.assertEqual(len(marker_labels), 1)
        marker_labels[0].ensurePolished()
        self.assertGreaterEqual(marker_labels[0].sizeHint().width(),
                                marker_labels[0].fontMetrics().horizontalAdvance(marker))

    def test_same_direction_multiple_preserved(self):
        """P1-4：配置里同方向多条不同价位 → 全部保留（不再收敛为一条）。"""
        dup = [_saved(TARGET_DIR_ABOVE, 1500), _saved(TARGET_DIR_ABOVE, 1600),
               _saved(TARGET_DIR_BELOW, 1400), _saved(TARGET_DIR_BELOW, 1300)]
        p = self._make(saved=dup)
        self.assertEqual(len(p._pending), 4)
        ups = sorted(a["target"] for a in p._pending if a["direction"] == TARGET_DIR_ABOVE)
        dns = sorted(a["target"] for a in p._pending if a["direction"] == TARGET_DIR_BELOW)
        self.assertEqual(ups, [1500.0, 1600.0])
        self.assertEqual(dns, [1300.0, 1400.0])

    def test_exact_duplicate_slot_collapsed(self):
        """完全相同的槽位（同方向+同价位，历史/手工编辑遗留）仍应去重，避免重复通知。"""
        dup = [_saved(TARGET_DIR_ABOVE, 1600), _saved(TARGET_DIR_ABOVE, 1600)]
        p = self._make(saved=dup)
        self.assertEqual(len(p._pending), 1)

    def test_extreme_value_does_not_inflate_panel(self):
        """D11 边界：极端数值（1e308）不得把面板撑爆 —— 省略号截断且高度恒定。"""
        p = self._make()
        p.edit_up.setText("1500")
        p._add(TARGET_DIR_ABOVE)
        p.adjustSize()
        h_normal = p.height()
        p2 = self._make()
        p2.edit_up.setText("1e308")
        p2._add(TARGET_DIR_ABOVE)
        p2.adjustSize()
        self.assertEqual(p2.height(), h_normal, "极端数值不应改变面板高度")
        self.assertIn("…", p2._list_text(), "超长数值应被省略号截断")

    def test_list_plain_when_pending(self):
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)])
        self.assertNotIn("已触发", p._list_text())

    # ---------- D2 Esc 关闭不保存 ----------
    def test_escape_closes_without_saving(self):
        p = self._make()
        p.edit_up.setText("1600")
        p._add(TARGET_DIR_ABOVE)
        from PyQt6.QtGui import QKeyEvent
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                       Qt.KeyboardModifier.NoModifier)
        p.keyPressEvent(ev)
        self.assertEqual(self.applied, [])              # Esc 不触发保存
        self.assertTrue(ev.isAccepted())


class TestEscRollback(unittest.TestCase):
    """R34-7：Esc = 关闭且不保存，必须真的丢弃未生效修改。

    原先只调用 close()，_pending 留着未生效内容，下次打开面板会把
    "已丢弃"的条目重新显示出来并提示"有未生效的修改"，与承诺矛盾。
    """
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._panels = []

    def _make(self, saved):
        # 注意：patch 必须覆盖整个用例生命周期 —— _rollback() 里的 _reload()
        # 会再次调用 load_alerts，只在构造期间 patch 会让它回落到真实磁盘。
        patcher = patch.object(target_panel, "load_alerts",
                               return_value=list(saved))
        patcher.start()
        self.addCleanup(patcher.stop)
        p = target_panel.TargetPanel("600519", "贵州茅台",
                                     lambda a: True, lambda: True,
                                     current_code_fn=lambda: "600519")
        self._panels.append(p)
        return p

    @staticmethod
    def _esc(p):
        from PyQt6.QtGui import QKeyEvent
        p.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                  Qt.KeyboardModifier.NoModifier))

    def test_esc_discards_added_target(self):
        p = self._make([_saved(TARGET_DIR_ABOVE, 1500)])
        p.edit_up.setText("1600")
        p._add(TARGET_DIR_ABOVE)
        self.assertTrue(p.has_unsaved_changes())
        self._esc(p)
        self.assertFalse(p.has_unsaved_changes(), "Esc 后不应残留未生效修改")
        self.assertEqual([a["target"] for a in p._pending], [1500.0],
                         "Esc 后应回到已保存状态")

    def test_esc_discards_removal_too(self):
        """清空内存列表但未点「立即生效」就 Esc —— 也应回滚。"""
        p = self._make([_saved(TARGET_DIR_BELOW, 1400)])
        p._pending = []
        self.assertTrue(p.has_unsaved_changes())
        self._esc(p)
        self.assertEqual([a["target"] for a in p._pending], [1400.0])
        self.assertFalse(p.has_unsaved_changes())

    def test_esc_without_changes_is_noop(self):
        p = self._make([_saved(TARGET_DIR_ABOVE, 1500)])
        self._esc(p)
        self.assertEqual([a["target"] for a in p._pending], [1500.0])

    def test_esc_disarms_clear_confirm(self):
        p = self._make([_saved(TARGET_DIR_ABOVE, 1500)])
        p._clear()                       # 进入「确认清空」态
        self.assertEqual(p.btn_clear.text(), "确认清空")
        self._esc(p)
        self.assertEqual(p.btn_clear.text(), "清空")


class TestApplyClosesImmediately(unittest.TestCase):
    """保存成功后立即关闭，不显示成功或通知授权状态提示。"""
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._panels = []
        self.applied = []

    def _make(self):
        with patch.object(target_panel, "load_alerts", return_value=[]):
            p = target_panel.TargetPanel(
                "600519", "贵州茅台", lambda a: self.applied.append(a) or True,
                lambda: True, current_code_fn=lambda: "600519")
        self._panels.append(p)
        return p

    def test_closes_immediately_when_notifications_denied(self):
        p = self._make()
        p.show()
        p.edit_up.setText("1600")
        p._add(TARGET_DIR_ABOVE)
        with patch("sf_notify.authorization_status", return_value="denied"):
            p._apply()
        self.assertEqual(len(self.applied), 1)
        self.assertFalse(p.isVisible())
        self.assertEqual(p.status_label.text(), "")

    def test_closes_immediately_when_notifications_authorized(self):
        p = self._make()
        p.show()
        p.edit_up.setText("1600")
        p._add(TARGET_DIR_ABOVE)
        with patch("sf_notify.authorization_status", return_value="authorized"):
            p._apply()
        self.assertEqual(len(self.applied), 1)
        self.assertFalse(p.isVisible())
        self.assertEqual(p.status_label.text(), "")


class TestSignatureRobustness(unittest.TestCase):
    """R34-9：_signature 不得因 direction 为 None 抛 TypeError。"""
    def test_none_direction_does_not_crash(self):
        sig = target_panel.TargetPanel._signature(
            [{"direction": None, "target": 1.0},
             {"direction": TARGET_DIR_ABOVE, "target": 2.0}])
        self.assertEqual(len(sig), 2)

    def test_junk_entries_skipped(self):
        self.assertEqual(target_panel.TargetPanel._signature(
            [{"direction": TARGET_DIR_ABOVE, "target": "abc"}, "junk", None]), ())

    def test_signature_is_order_independent(self):
        a = [{"direction": TARGET_DIR_ABOVE, "target": 1.0},
             {"direction": TARGET_DIR_BELOW, "target": 2.0}]
        b = list(reversed(a))
        self.assertEqual(target_panel.TargetPanel._signature(a),
                         target_panel.TargetPanel._signature(b))


class TestLiveDistancePct(unittest.TestCase):
    """P1-2：列表实时显示距目标价差%。"""
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _make(self, price):
        with patch.object(target_panel, "load_alerts", return_value=[]):
            return target_panel.TargetPanel(
                "600519", "贵州茅台", lambda a: True, lambda: True,
                current_code_fn=lambda: "600519",
                current_price_fn=lambda: price)

    def test_up_target_shows_positive_pct(self):
        p = self._make(1500.0)
        p.edit_up.setText("1600")
        p._add(TARGET_DIR_ABOVE)
        self.assertIn("+6.7%", p._list_text())

    def test_down_target_shows_negative_pct(self):
        p = self._make(1500.0)
        p.edit_dn.setText("1400")
        p._add(TARGET_DIR_BELOW)
        self.assertIn("-6.7%", p._list_text())

    def test_no_price_hides_pct(self):
        p = self._make(None)
        p.edit_up.setText("1600")
        p._add(TARGET_DIR_ABOVE)
        self.assertNotIn("%", p._list_text())


class TestPercentInput(unittest.TestCase):
    """P1-3：支持百分比 / 相对涨跌幅快捷输入。"""
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _make(self, price):
        with patch.object(target_panel, "load_alerts", return_value=[]):
            return target_panel.TargetPanel(
                "600519", "贵州茅台", lambda a: True, lambda: True,
                current_code_fn=lambda: "600519",
                current_price_fn=lambda: price)

    def test_plus_pct(self):
        p = self._make(1000.0)
        t, err = p._resolve_target(TARGET_DIR_ABOVE, "+5%")
        self.assertEqual(t, 1050.0)
        self.assertEqual(err, "")

    def test_minus_pct(self):
        p = self._make(1000.0)
        t, err = p._resolve_target(TARGET_DIR_BELOW, "-3%")
        self.assertEqual(t, 970.0)

    def test_bare_pct_is_positive(self):
        p = self._make(1000.0)
        t, _ = p._resolve_target(TARGET_DIR_ABOVE, "5%")
        self.assertEqual(t, 1050.0)

    def test_symbol_prefix_relative(self):
        p = self._make(200.0)
        t, _ = p._resolve_target(TARGET_DIR_ABOVE, "+10")
        self.assertEqual(t, 220.0)

    def test_absolute_still_works(self):
        p = self._make(1000.0)
        t, _ = p._resolve_target(TARGET_DIR_ABOVE, "1500")
        self.assertEqual(t, 1500.0)

    def test_pct_without_price_errors(self):
        p = self._make(None)
        t, err = p._resolve_target(TARGET_DIR_ABOVE, "+5%")
        self.assertIsNone(t)
        self.assertIn("当前价", err)


class TestInstrumentPricePrecision(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _make(self, code, price, saved=()):
        with patch.object(target_panel, "load_alerts", return_value=list(saved)):
            return target_panel.TargetPanel(
                code, "测试标的", lambda a: True, lambda: True,
                current_code_fn=lambda: code, current_price_fn=lambda: price)

    def test_stock_keeps_two_decimals(self):
        p = self._make("sh600519", 4.620)
        self.assertEqual(p._resolve_target(TARGET_DIR_ABOVE, "4.621")[0], 4.62)
        self.assertEqual(p._price_example(), "1500.00")

    def test_etf_absolute_targets_keep_three_decimals(self):
        p = self._make("sh510300", 4.620)
        p.edit_up.setText("4.621")
        p._add(TARGET_DIR_ABOVE)
        p.edit_dn.setText("4.619")
        p._add(TARGET_DIR_BELOW)
        self.assertEqual([a["target"] for a in p._pending], [4.621, 4.619])
        self.assertIn("涨到 4.621", p._list_text())
        self.assertIn("跌到 4.619", p._list_text())

    def test_hk_and_convertible_bond_use_three_decimals(self):
        for code in ("hk00700", "sh113001", "sz123001", "sz160001"):
            p = self._make(code, 4.620)
            self.assertEqual(p._resolve_target(TARGET_DIR_ABOVE, "4.621")[0], 4.621)

    def test_percent_conversion_uses_instrument_precision(self):
        p = self._make("sh510300", 4.620)
        target, err = p._resolve_target(TARGET_DIR_ABOVE, "+0.02%")
        self.assertEqual(err, "")
        self.assertEqual(target, 4.621)

    def test_dedup_key_uses_three_decimal_precision(self):
        p = self._make("sh510300", 4.620,
                       saved=[_saved(TARGET_DIR_ABOVE, 4.621)])
        p.edit_up.setText("4.6211")
        p._add(TARGET_DIR_ABOVE)
        self.assertEqual(len(p._pending), 1)
        self.assertIn("已存在", p.status_label.text())

    def test_error_message_uses_three_decimal_example(self):
        p = self._make("sh510300", 4.620)
        p.edit_up.setText("abc")
        p._add(TARGET_DIR_ABOVE)
        self.assertIn("1500.000", p.status_label.text())


class TestDeleteOne(unittest.TestCase):
    """P2-1：逐条删除单条目标价。"""
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _make(self, saved):
        with patch.object(target_panel, "load_alerts", return_value=list(saved)):
            return target_panel.TargetPanel(
                "600519", "贵州茅台", lambda a: True, lambda: True,
                current_code_fn=lambda: "600519")

    def test_remove_one_keeps_other(self):
        p = self._make([_saved(TARGET_DIR_ABOVE, 1600), _saved(TARGET_DIR_ABOVE, 1700)])
        self.assertEqual(len(p._pending), 2)
        p._remove_one(TARGET_DIR_ABOVE, 1600.0)
        self.assertEqual(len(p._pending), 1)
        self.assertEqual(p._pending[0]["target"], 1700.0)
        self.assertNotIn("1600.00", p._list_text())

    def test_remove_nonexistent_is_noop(self):
        p = self._make([_saved(TARGET_DIR_ABOVE, 1600)])
        p._remove_one(TARGET_DIR_ABOVE, 9999.0)
        self.assertEqual(len(p._pending), 1)




class TestMaxAndValidationAndButtons(unittest.TestCase):
    """v1.2.84：每方向最多 3 条、涨到/跌到价相对现价校验、空列表时底部按钮禁用。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _make(self, price=None, saved=()):
        def _on_apply(alerts):
            return True

        def _on_clear():
            return True

        with patch.object(target_panel, "load_alerts", return_value=list(saved)):
            return target_panel.TargetPanel(
                "600519", "贵州茅台", _on_apply, _on_clear,
                current_code_fn=lambda: "600519",
                current_price_fn=(lambda: price) if price is not None else None)

    def test_trigger_highlight_is_runtime_only(self):
        """彩虹由主窗在命中时播放，面板不再提供持久化开关。"""
        p = self._make()
        self.assertFalse(hasattr(p, "flash_check"))
        self.assertNotIn("触发时高亮窗口", p.styleSheet())

    def test_buttons_disabled_when_empty(self):
        """没有目标价时，「清空」与「立即生效」均置灰。"""
        p = self._make()
        self.assertFalse(p.btn_clear.isEnabled())
        self.assertFalse(p.btn_apply.isEnabled())

    def test_buttons_enabled_after_add(self):
        """添加一条后，两个底部按钮恢复可点击。"""
        p = self._make(price=1500.0)
        p.edit_up.setText("1600")
        p._add(TARGET_DIR_ABOVE)
        self.assertTrue(p.btn_clear.isEnabled())
        self.assertTrue(p.btn_apply.isEnabled())

    def test_above_must_be_higher_than_current(self):
        """涨到价必须高于当前价，否则禁止添加并提示。"""
        p = self._make(price=1500.0)
        p.edit_up.setText("1400")
        p._add(TARGET_DIR_ABOVE)
        self.assertEqual(len(p._pending), 0)
        self.assertIn("高于当前价", p.status_label.text())

    def test_below_must_be_lower_than_current(self):
        """跌到价必须低于当前价，否则禁止添加并提示。"""
        p = self._make(price=1500.0)
        p.edit_dn.setText("1600")
        p._add(TARGET_DIR_BELOW)
        self.assertEqual(len(p._pending), 0)
        self.assertIn("低于当前价", p.status_label.text())

    def test_below_price_between_current_and_limit_down_can_apply(self):
        """截图回归：64.86 的股票设置「跌到 64.50」应允许添加并保存。"""
        saved = []
        with patch.object(target_panel, "load_alerts", return_value=[]):
            p = target_panel.TargetPanel(
                "002837", "英雄克", lambda alerts: saved.extend(alerts) or True, lambda: True,
                current_code_fn=lambda: "002837", current_price_fn=lambda: 64.86,
                limits_fn=lambda: (65.16, 62.63))
        p.edit_dn.setText("64.50")
        p._add(TARGET_DIR_BELOW)
        self.assertEqual(p.status_label.text(), "")
        self.assertEqual(len(p._pending), 1)
        p._apply()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["direction"], TARGET_DIR_BELOW)
        self.assertEqual(saved[0]["target"], 64.50)

    def test_below_price_with_fullwidth_input_can_apply(self):
        """中文输入法的全角 ６４．５０ 也必须视为有效的跌到价格。"""
        p = self._make(price=64.86)
        p._limits_fn = lambda: (65.16, 62.63)
        p.edit_dn.setText("６４．５０")
        p._add(TARGET_DIR_BELOW)
        self.assertEqual(len(p._pending), 1)
        self.assertEqual(p._pending[0]["target"], 64.50)

    def test_fullwidth_percent_and_sign_inputs_can_apply(self):
        """全角 ＋５％ / －３％ / －３ 同样按原有相对价格规则解析。"""
        p = self._make(price=100.0)
        for direction, text, expected in (
                (TARGET_DIR_ABOVE, "＋５％", 105.0),
                (TARGET_DIR_BELOW, "－３％", 97.0),
                (TARGET_DIR_BELOW, "－３", 97.0)):
            target, err = p._resolve_target(direction, text)
            self.assertEqual(err, "")
            self.assertEqual(target, expected)

    def test_max_three_per_direction_blocks_and_hides_input(self):
        """每个方向达到 3 条后，该输入框禁用、+ 按钮隐藏。"""
        p = self._make(price=1000.0)
        for target in [1100, 1200, 1300]:
            p.edit_up.setText(str(target))
            p._add(TARGET_DIR_ABOVE)
        self.assertEqual(len(p._pending), 3)
        # 第 4 条应被拒绝
        p.edit_up.setText("1400")
        p._add(TARGET_DIR_ABOVE)
        self.assertEqual(len(p._pending), 3)
        self.assertIn("最多 3 个", p.status_label.text())
        self.assertFalse(p.edit_up.isEnabled())
        self.assertTrue(p.btn_up.isHidden())
        # 跌到方向仍可继续
        self.assertTrue(p.edit_dn.isEnabled())
        self.assertFalse(p.btn_dn.isHidden())

    def test_delete_one_re_enables_input(self):
        """删除一条使数量低于 3 后，对应输入框恢复。"""
        p = self._make(price=1000.0)
        for target in [1100, 1200, 1300]:
            p.edit_up.setText(str(target))
            p._add(TARGET_DIR_ABOVE)
        self.assertFalse(p.edit_up.isEnabled())
        p._remove_one(TARGET_DIR_ABOVE, 1300.0)
        self.assertTrue(p.edit_up.isEnabled())
        self.assertFalse(p.btn_up.isHidden())

    def test_validation_allows_exactly_at_boundary(self):
        """涨到 > 现价、跌到 < 现价是开区间，等于现价仍拒绝。"""
        p = self._make(price=1500.0)
        p.edit_up.setText("1500")
        p._add(TARGET_DIR_ABOVE)
        self.assertEqual(len(p._pending), 0)
        p.edit_dn.setText("1500")
        p._add(TARGET_DIR_BELOW)
        self.assertEqual(len(p._pending), 0)


if __name__ == "__main__":
    unittest.main()


class TestV1287Features(unittest.TestCase):
    """v1.2.87：涨停/跌停越界拦截、铃铛已提醒禁用「立即生效」、
    列表按方向红绿着色、X 关闭丢弃草稿（重开为全新面板）。"""
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _make(self, saved=(), price=None, apply_blocked=False, limits=None):
        with patch.object(target_panel, "load_alerts", return_value=list(saved)):
            return target_panel.TargetPanel(
                "600519", "贵州茅台", lambda a: True, lambda: True,
                current_code_fn=lambda: "600519",
                current_price_fn=(lambda: price) if price is not None else None,
                apply_blocked_fn=(lambda: apply_blocked) if apply_blocked else None,
                limits_fn=(lambda: limits) if limits is not None else None)

    def test_above_cannot_exceed_limit_up(self):
        p = self._make(price=1500.0, limits=(1600.0, 1400.0))
        p.edit_up.setText("1700")          # 超过涨停价 1600
        p._add(TARGET_DIR_ABOVE)
        self.assertEqual(len(p._pending), 0)
        self.assertIn("涨停价", p.status_label.text())

    def test_below_cannot_below_limit_down(self):
        p = self._make(price=1500.0, limits=(1600.0, 1400.0))
        p.edit_dn.setText("1300")          # 低于跌停价 1400
        p._add(TARGET_DIR_BELOW)
        self.assertEqual(len(p._pending), 0)
        self.assertIn("跌停价", p.status_label.text())

    def test_within_limits_ok(self):
        p = self._make(price=1500.0, limits=(1600.0, 1400.0))
        p.edit_up.setText("1550")
        p._add(TARGET_DIR_ABOVE)
        self.assertEqual(len(p._pending), 1)

    def test_fired_target_still_allows_apply_button(self):
        """已触发只是状态提示，不能阻止用户继续新增或修改目标价。"""
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)], apply_blocked=True)
        p._update_buttons()
        self.assertTrue(p.btn_apply.isEnabled())
        self.assertTrue(p.btn_clear.isEnabled())

    def test_list_up_label_red_down_label_green(self):
        """列表涨相关文本红色、跌相关文本绿色，且数字用 D-DIN。"""
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600),
                             _saved(TARGET_DIR_BELOW, 1400)])
        styles = " ".join(l.styleSheet() for l in self._collect_labels(p))
        # v1.4.59（走查 S8）：断言改成"与主卡同一份涨跌色 token"，不再钉十六进制字面量 ——
        # 原来这里钉的是 #ff6b6b，而主卡是 #ff5252，同屏两种红正是那条走查项。
        import sf_ui_render as sr
        self.assertIn(sr.RED.name(), styles, "涨相关文本应与主卡涨色同源")
        self.assertIn(sr.GREEN.name(), styles, "跌相关文本应与主卡跌色同源")
        self.assertIn("D-DIN", styles, "数字应使用 D-DIN 字体")

    def test_x_close_discards_draft(self):
        """右上角 X 关闭（未点立即生效）应丢弃草稿，重开即全新面板。"""
        p = self._make()
        p.edit_up.setText("1600")
        p._add(TARGET_DIR_ABOVE)
        self.assertTrue(p.has_unsaved_changes())
        # 关闭回滚必须以磁盘已保存内容为准；构造时的 load_alerts mock 已退出，
        # 这里再次固定为空，避免本机真实配置污染离屏测试。
        with patch.object(target_panel, "load_alerts", return_value=[]):
            p._on_close_x()
        self.assertFalse(p.has_unsaved_changes(), "X 关闭后不应残留未生效修改")
        self.assertEqual([a["target"] for a in p._pending], [])

    def test_editor_input_fills_popup_content_width(self):
        """替换股票输入框占满编辑弹窗可用宽度，高度仍与目标价一致。"""
        from PyQt6.QtCore import QPoint
        import sf_glass
        ed = sf_editor.EditorWindow("600519", lambda c: True, QPoint(100, 100), 268)
        left, _top, right, _bottom = ed.layout().getContentsMargins()
        self.assertEqual(ed.entry.width(), ed.width() - left - right)
        self.assertEqual(ed.entry.height(), sf_glass.POPUP_INPUT_H)
        self.assertEqual(ed.entry.height(), 36)

    def test_popup_input_focus_border_is_subtle_cool_gray(self):
        """聚焦边框可略提亮以呼应玻璃受光，但不能抢过输入文字。"""
        import sf_glass
        rgb = sf_glass.POPUP_INPUT_FOCUS_RGB
        self.assertEqual(len(rgb), 4)
        self.assertLess(rgb[3], 200)
        self.assertLess(max(rgb[:3]), 220)
        self.assertIn("174,190,212", sf_glass.POPUP_INPUT_FOCUS_BORDER)

    def test_close_button_hover_matches_main_window(self):
        """两弹窗关闭键使用主界面同款自绘细线叉号与悬浮底板。"""
        p = self._make()
        self.assertIsInstance(p.btn_close_x, sf_editor.MainCloseButton)
        s = p.btn_close_x.styleSheet()
        self.assertNotIn(":hover", s, "悬浮底板应抗锯齿自绘，不能叠加样式表边框")
        self.assertTrue(p.btn_close_x.testAttribute(Qt.WidgetAttribute.WA_Hover))
        self.assertEqual(p.btn_close_x.text(), "", "关闭图标不能使用字体字符")
        self.assertIsNotNone(p.btn_close_x._close_img, "必须加载主界面正在使用的 close SVG")
        self.assertEqual(p.btn_close_x._close_img.width(), 24)
        self.assertEqual(p.btn_close_x._close_img.height(), 24)
        self.assertEqual(p.btn_close_x.width(), 20)
        self.assertEqual(p.btn_close_x.height(), 20)

    def test_disabled_style_rules_exist_for_all_buttons(self):
        """每个按钮都必须有 :disabled 样式，否则禁用时「看起来还能点」。

        真实事故：只有 #ok 有 :disabled，#cancel（清空）缺 —— 禁用后背景仍是
        #262b3a，与可点状态完全一样，用户反馈「没有置灰」。

        为什么必须**自动扫描所有 ID**而不是列举 ok/cancel/plus：
        通用 QPushButton:disabled 兜底**救不了具名按钮** —— CSS 特异性
        ID(1,0,1) > 类型+伪类(0,1,1)，实测 #del 禁用前后同为 #9aa2b8（未置灰），
        而无 ID 按钮兜底生效。此前只测三个硬编码名字，给了错误的安全感。
        """
        s = self._make().styleSheet()
        ids = set(re.findall(r"QPushButton#(\w+)\s*\{", s))
        self.assertTrue(ids, "未提取到任何按钮 ID，正则可能已失效（需同步修测试）")
        for name in sorted(ids):
            self.assertIn("#%s:disabled" % name, s,
                          "按钮 #%s 缺少 :disabled 置灰样式"
                          "（通用兜底对具名按钮无效，必须逐个声明）" % name)

    def test_clear_button_renders_differently_when_disabled(self):
        """像素级：清空按钮禁用态与启用态渲染出的颜色必须不同。

        只断言样式表字符串不够 —— 规则可能因选择器冲突/解析失败而不生效
        （sf_editor 就曾因样式表里混入 '#' 注释导致整段规则被丢弃），
        故直接采样渲染结果的中心像素。
        """
        p = self._make()
        p.show()

        def center_pixel(w):
            QApplication.processEvents()
            img = p.grab().toImage()
            point = w.mapTo(p, w.rect().center())
            scale = img.devicePixelRatio()
            return img.pixelColor(round(point.x() * scale), round(point.y() * scale)).name()

        p.btn_clear.setEnabled(False)
        off = center_pixel(p.btn_clear)
        p.btn_clear.setEnabled(True)
        p.repaint()
        on = center_pixel(p.btn_clear)
        self.assertNotEqual(off, on,
                            "清空按钮禁用态与启用态渲染同色，说明置灰未生效")
        p.close()

    def test_fired_state_never_locks_apply(self):
        """已触发后仍可保存新的目标价，不再把状态提示误当成编辑权限。"""
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)])
        state = {"fired": False}
        p._apply_blocked_fn = lambda: state["fired"]
        p._update_buttons()
        self.assertTrue(p.btn_apply.isEnabled())
        state["fired"] = True
        p._update_buttons()
        self.assertTrue(p.btn_apply.isEnabled(),
                        "目标到达后必须仍可新增或修改目标价")

    def test_successful_apply_closes_without_delayed_lock(self):
        """保存成功即关闭，不再进入延迟停留或锁定态。"""
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)])
        p.show()
        p._apply()
        self.assertFalse(p.isVisible(), "保存成功后应直接关闭目标价面板")
        self.assertFalse(p._locked)
        self.assertIsNone(p._close_timer)

    def test_nan_and_inf_inputs_rejected(self):
        """M7：nan/inf 不得绕过校验 —— 落盘成 NaN 会变成永不触发的永久脏数据。

        nan <= 0 恒为 False，旧的 `target <= 0` 判断拦不住它们。
        """
        p = self._make(price=1500.0)
        for bad in ("nan%", "inf%", "+nan", "-inf%", "1e309%"):
            p._pending = []
            p.edit_up.setText(bad)
            p._add(TARGET_DIR_ABOVE)
            self.assertEqual(len(p._pending), 0, "%r 不应被接受" % bad)

    def test_close_glass_resets_pending_flag(self):
        """M9：关闭毛玻璃后必须复位 _glass_pending，否则复用实例永久丢毛玻璃。"""
        p = self._make()
        p._close_glass()
        self.assertTrue(p._glass_pending,
                        "关闭后应复位为待注入状态，复用时才能重新注入毛玻璃")
        self.assertEqual(p._glass_mask_w, 0, "mask 尺寸缓存应一并复位")

    def test_child_window_glass_injection_uses_stable_qt_fallback(self):
        """崩溃/空壳回归：子窗不得再创建独立 NSVisualEffectView。"""
        p = self._make()
        p._inject_glass()
        self.assertFalse(p._bg_effect)
        self.assertIsNone(p._glass_window)
        self.assertIsNone(p._glass_view)

    def test_close_glass_only_orders_out_native_window(self):
        """兼容收尾：若历史路径残留原生背景窗，也只能隐藏，不能手动 close。"""
        class _NativeWindow:
            def __init__(self):
                self.hidden = 0
                self.closed = 0

            def orderOut_(self, _sender):
                self.hidden += 1

            def close(self):
                self.closed += 1

        p = self._make()
        native = _NativeWindow()
        p._glass_window = native
        p._close_glass()
        self.assertEqual(native.hidden, 1)
        self.assertEqual(native.closed, 0,
                         "调用 NSWindow.close 会与 PyObjC 自动释放形成双重 release")

    def test_plus_buttons_are_gray_self_drawn_and_percent_input_is_discoverable(self):
        """加号为灰底自绘图标；输入框明确提示可直接按百分比设价。"""
        p = self._make()
        self.assertIsInstance(p.btn_up, target_panel.AddIconButton)
        self.assertIsInstance(p.btn_dn, target_panel.AddIconButton)
        self.assertEqual(p.btn_up.text(), "")
        self.assertIn("QPushButton#plus{background:rgba(255,255,255,0.10)", p.styleSheet())
        self.assertIn("+5%", p.edit_up.placeholderText())
        self.assertIn("-5%", p.edit_dn.placeholderText())

    def test_compact_inputs_match_square_plus_buttons(self):
        """两行输入框固定为紧凑高度；右侧加号必须同高同宽。"""
        import sf_glass
        p = self._make()
        self.assertEqual(sf_glass.POPUP_INPUT_H, 36)
        for edit, plus in ((p.edit_up, p.btn_up), (p.edit_dn, p.btn_dn)):
            self.assertEqual(edit.minimumHeight(), target_panel.TARGET_INPUT_H)
            self.assertEqual(edit.maximumHeight(), target_panel.TARGET_INPUT_H)
            self.assertEqual(plus.width(), target_panel.TARGET_INPUT_H)
            self.assertEqual(plus.height(), target_panel.TARGET_INPUT_H)
            self.assertEqual(plus.width(), plus.height())

    def test_target_panel_has_no_hard_region_mask(self):
        """圆角只由抗锯齿路径绘制，不再用整数 QRegion 造成锯齿。"""
        p = self._make()
        p._update_mask()
        self.assertTrue(p.mask().isEmpty())

    def test_selected_area_vertical_spacing_is_roomier(self):
        """框选区只拉开纵向间距：列表行 7px，列表与提示语 10px。"""
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600),
                              _saved(TARGET_DIR_BELOW, 1400)])
        self.assertEqual(p.list_box.spacing(), 0)
        self.assertEqual(target_panel.LIST_ROW_GAP, 0)
        self.assertEqual(target_panel.LIST_STATUS_GAP, 6)
        self.assertLess(target_panel.LIST_ROW_GAP, 7)
        self.assertLess(target_panel.LIST_STATUS_GAP, 10)

    def test_empty_target_list_has_no_placeholder_text(self):
        """用户要求：没有目标价时列表区域保持为空，不显示冗余提示。"""
        p = self._make(saved=[])
        texts = [lab.text() for lab in self._collect_labels(p)]
        self.assertNotIn("（暂无目标价）", texts)
        self.assertEqual(texts, [])

    def test_redundant_status_copy_is_absent(self):
        """成功结果由界面状态直接体现，只保留错误/确认/通知受限等必要反馈。"""
        forbidden = ("当前没有目标价", "已清空", "已保存：", "点「立即生效」保存")
        from pathlib import Path
        src = Path(target_panel.__file__).read_text(encoding="utf-8")
        for text in forbidden:
            self.assertNotIn(text, src)

    def test_popup_visual_tokens_are_shared(self):
        """两个弹窗必须使用同一套面板材质与控件状态，禁止再次各写一份。"""
        import sf_glass
        import inspect
        editor_src = inspect.getsource(sf_editor.EditorWindow)
        panel_src = inspect.getsource(target_panel.TargetPanel)
        self.assertIn("popup_stylesheet", editor_src)
        self.assertIn("popup_stylesheet", panel_src)
        self.assertIn("paint_popup_surface", editor_src)
        self.assertIn("paint_popup_surface", panel_src)
        self.assertIn("prepare_surface", editor_src)
        self.assertIn("prepare_surface", panel_src)
        css = sf_glass.popup_stylesheet()
        for state in ("QLineEdit:focus", "#ok:hover", "#ok:pressed",
                      "#ok:disabled", "#cancel:hover", "#cancel:pressed",
                      "#cancel:disabled"):
            self.assertIn(state, css)
        self.assertAlmostEqual(sf_glass.POPUP_BG_ALPHA, sf_ui_render.BG_ALPHA_DEFAULT)
        from pathlib import Path
        glass_src = Path(sf_glass.__file__).read_text(encoding="utf-8")
        self.assertNotIn("grabWindow", glass_src)
        self.assertNotIn("capture_blurred_background", glass_src)
        self.assertEqual(sf_glass.POPUP_INPUT_BG, "rgba(255,255,255,0.08)")
        self.assertEqual(sf_glass.POPUP_SURFACE_RGB, (0, 0, 0))
        self.assertEqual(int(sf_glass.POPUP_BG_ALPHA * 255),
                         int(sf_ui_render.BG_ALPHA_DEFAULT * 255))
        self.assertIn("setHasShadow_", inspect.getsource(sf_glass.disable_native_window_shadow))

    def test_status_text_uses_only_white_or_gray(self):
        """面板提示语只允许白色/灰色，不使用红绿黄等业务颜色。"""
        s = " ".join(target_panel.STATUS_STYLE.values()).lower()
        for forbidden in ("#ff5252", "#ff5858", "#4caf50", "#f5a623", "#6fe06f"):
            self.assertNotIn(forbidden, s)
        self.assertIn("#ffffff", s)
        self.assertIn("#9aa2b8", s)

    def test_bottom_buttons_match_editor_alignment(self):
        """底部按钮等宽并撑满面板内容区。"""
        p = self._make(saved=[_saved(TARGET_DIR_ABOVE, 1600)])
        self.assertEqual(p.btn_clear.sizePolicy().horizontalPolicy(),
                         QSizePolicy.Policy.Expanding)
        self.assertEqual(p.btn_apply.sizePolicy().horizontalPolicy(),
                         QSizePolicy.Policy.Expanding)
        p.show()
        QApplication.processEvents()
        self.assertEqual(p.btn_clear.width(), p.btn_apply.width())
        self.assertEqual(p.btn_apply.geometry().right(), p.width() - target_panel.MARGIN_X - 1)
        p.close()

    @staticmethod
    def _collect_labels(p):
        return [label for i in range(p.list_box.count())
                for label in p.list_box.itemAt(i).widget().findChildren(QLabel)]


class TestRuntimeSyncAndLock(unittest.TestCase):
    """R46 第 2 轮走查：面板与后台状态同步、延迟关闭期的完整只读锁定。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._panels = []

    def _make(self, saved, price=1600.0):
        with patch.object(target_panel, "load_alerts", return_value=list(saved)):
            p = target_panel.TargetPanel(
                "600519", "贵州茅台", lambda a: True, lambda: True,
                current_code_fn=lambda: "600519",
                current_price_fn=lambda: price)
        self._panels.append(p)
        return p

    def tearDown(self):
        for p in self._panels:
            try:
                p.close()
            except Exception:
                pass

    def test_sync_alerts_reflects_backend_hit(self):
        """面板打开期间后台触发了目标价，列表必须显示"已触发"。"""
        p = self._make([_saved(TARGET_DIR_ABOVE, 1600)])
        self.assertNotIn("已触发", p._list_text())
        p.sync_alerts([_saved(TARGET_DIR_ABOVE, 1600, state="hit")])
        self.assertIn("已触发", p._list_text(),
                      "后台已触发时面板仍显示待触发，用户会重复添加同价位目标")

    def test_sync_alerts_does_not_drop_user_pending_edits(self):
        """同步只回写状态，不得删掉用户新增但尚未生效的条目。"""
        p = self._make([_saved(TARGET_DIR_ABOVE, 1600)])
        p.edit_dn.setText("1400")
        p.btn_dn.click()
        p.sync_alerts([_saved(TARGET_DIR_ABOVE, 1600, state="hit")])
        self.assertEqual(len(p._pending), 2)

    def test_sync_alerts_ignored_when_locked(self):
        p = self._make([_saved(TARGET_DIR_ABOVE, 1600)])
        p._finish_and_close(60_000)
        p.sync_alerts([_saved(TARGET_DIR_ABOVE, 1600, state="hit")])
        self.assertNotIn("已触发", p._list_text())
        p._stop_close_timer()

    def test_lock_disables_row_delete_buttons(self):
        """延迟关闭期必须连列表行内的 ✕ 一起禁用。

        只禁用主按钮时，点行内 ✕ 会走 _update_buttons() 把「立即生效」重新
        启用，用户得以二次落盘，把刚确认的内容再写一遍。
        """
        from PyQt6.QtWidgets import QPushButton
        p = self._make([_saved(TARGET_DIR_ABOVE, 1600)])
        btns = p.findChildren(QPushButton)
        self.assertGreaterEqual(len(btns), 2, "应包含主按钮与行内删除按钮")
        p._finish_and_close(60_000)
        self.assertTrue(all(not b.isEnabled() for b in p.findChildren(QPushButton)),
                        "锁定态下所有按钮（含行内 ✕）都必须禁用")
        self.assertFalse(p.edit_up.isEnabled())
        self.assertFalse(p.edit_dn.isEnabled())
        p._stop_close_timer()

    def test_lock_blocks_add_remove_apply_clear(self):
        p = self._make([_saved(TARGET_DIR_ABOVE, 1600)])
        p._finish_and_close(60_000)
        before = [dict(a) for a in p._pending]
        p.edit_dn.setText("1400")
        p._add(TARGET_DIR_BELOW)
        p._remove_one(TARGET_DIR_ABOVE, 1600.0)
        p._apply()
        p._clear()
        self.assertEqual([dict(a) for a in p._pending], before,
                         "锁定态下任何增删/保存/清空都必须无效")
        p._stop_close_timer()

class TestInputNormalization(unittest.TestCase):
    """R46：全角 / 数学符号输入必须能解析，不能被当成非法输入。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._panels = []

    def _make(self, price=100.0):
        with patch.object(target_panel, "load_alerts", return_value=[]):
            p = target_panel.TargetPanel(
                "600519", "贵州茅台", lambda a: True, lambda: True,
                current_code_fn=lambda: "600519",
                current_price_fn=lambda: price)
        self._panels.append(p)
        return p

    def tearDown(self):
        for p in self._panels:
            try:
                p.close()
            except Exception:
                pass

    def test_fullwidth_digits(self):
        p = self._make()
        t, err = p._resolve_target(TARGET_DIR_ABOVE, "６４．５０")
        self.assertFalse(err, "全角数字应能解析：%r" % (err,))
        self.assertAlmostEqual(t, 64.50, places=2)

    def test_math_minus_sign_percent(self):
        """从网页复制的 −5%（U+2212）必须能解析。"""
        p = self._make(price=100.0)
        t, err = p._resolve_target(TARGET_DIR_BELOW, "\u22125%")
        self.assertFalse(err, "数学减号 U+2212 应能解析：%r" % (err,))
        self.assertAlmostEqual(t, 95.0, places=2)

    def test_whitespace_only_is_friendly_error(self):
        p = self._make()
        t, err = p._resolve_target(TARGET_DIR_ABOVE, "   ")
        self.assertIsNone(t)
        self.assertTrue(err)
