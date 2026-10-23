# -*- coding: utf-8 -*-
"""实时股价 v1.1.0 UI 交互回归测试（16.2 补充，QTest 应用内注入，可离线运行）。

覆盖：顶部按钮命中（编辑/关闭）、窗口拖拽、底部高度缩放、右键菜单。
方法：QTest 在应用内注入 mousePress/MouseMove/MouseRelease/右键事件，
替代系统级鼠标模拟，确定性验证 PyQt6 下坐标命中与交互逻辑。

运行方式（在 StockFloat 目录）：
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import inspect
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import stock_float_window as m
import sf_quote


def _fake_http_get(url, **kw):
    """离线行情/分时 mock：主源腾讯、备用源新浪/东财均返回合法数据。"""
    if "hq.sinajs.cn" in url:
        return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
    if "/appstock/app/minute/query?" in url:
        return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                '"data":["0930 1510.0 100 1000", "1000 1511.0 200 2000"]}}}}')
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


class TestUIInteraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", _fake_http_get),
            patch.object(m, "start_icon_worker", lambda: None),
            # 私有 API / AppKit / 原生菜单不进入测试（有 try/except 兜底，这里显式隔离）
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        # 停掉后台行情/分时线程与定时器，保证测试进程可退出、无真实网络
        self.win.running = False
        self.win._wake.set()
        self.win.timer.stop()
        self.win.top_timer.stop()
        self.win.show()
        QApplication.processEvents()

    def tearDown(self):
        self.win.close()
        QApplication.processEvents()
        for p in self._patches:
            p.stop()

    def _btn_center(self, name):
        return QPoint(m.W - m.BTN_X[name], m.BTN_CY)

    def test_bell_tooltip_uses_current_code_price_precision(self):
        self.win.code = "sh510300"
        self.win._alerts = [{"direction": "above", "target": 4.621,
                             "session_date": "2099-12-31", "state": "pending"}]
        self.assertEqual(self.win._target_bubble_rows()[0][0][1], "4.621")
        self.win.code = "sh600519"
        self.assertEqual(self.win._target_bubble_rows()[0][0][1], "4.62",
                         "换品种后小数位必须跟着变，不能带着上一只的精度")

    def test_bell_tooltip_anchor_is_below_bell(self):
        """气泡本体中心与小三角都对准铃铛正下方，不把铃铛中心误当气泡左上角。"""
        self.win._alerts = [{"direction": "above", "target": 65.10, "state": "hit"}]
        bubble = self.win._target_bubble
        # 本条验的是摆放位置，不是悬停节拍（节拍见 test_target_bubble_dwell.py），所以直接走立即显示这一步。
        self.win._show_target_bubble_now()
        expected = self.win.mapToGlobal(
            QPoint(self.win.width() - m.BTN_X["bell"], m.BTN_ICON_Y + m.BTN_ICON_SZ))
        body = bubble.body_rect_global()
        self.assertLessEqual(abs((body.left() + body.width() / 2.0) - expected.x()), 0.5)
        self.assertLessEqual(abs(bubble.caret_center_global_x() - expected.x()), 0.5)
        self.assertEqual(body.top(), expected.y() + m.TargetBubble.GAP_TO_ICON)
        self.assertFalse(bubble.is_flipped(), "屏幕上方空间充足时不该翻转")
        self.assertTrue(bubble.isVisible())
        self.assertNotIn("QToolTip", inspect.getsource(self.win._update_target_tooltip))

    def test_bell_tooltip_contains_fired_and_pending_targets(self):
        """铃铛气泡在卡片外承载目标提示，包含已触发和待触发，待触发组在上。"""
        self.win._alerts = [
            {"direction": "above", "target": 65.10, "state": "hit"},
            {"direction": "below", "target": 62.50, "state": "pending"},
        ]
        pending, hit = self.win._target_bubble_rows()
        self.assertEqual([(r[0], r[1]) for r in pending], [(False, "62.50")])
        self.assertEqual([(r[0], r[1]) for r in hit], [(True, "65.10")])
        self.assertEqual(hit[0][2], "", "已触发行不带百分比")
        self.win._show_target_bubble_now()
        self.assertEqual(self.win._target_bubble.rows()["pending"], pending)
        self.assertEqual(self.win._target_bubble.rows()["hit"], hit)

    def test_bell_prefers_pending_yellow_when_states_mixed(self):
        self.win._alerts = [
            {"direction": "above", "target": 65.10, "state": "hit"},
            {"direction": "below", "target": 62.50, "state": "pending"},
        ]
        self.assertEqual(self.win._bell_state(), "pending")

    def test_target_bubble_uses_divided_sections(self):
        bubble = m.TargetBubble()
        self.addCleanup(bubble.close)
        bubble.setRows([(True, "61.30", "+2.2%")], [])
        one_group = bubble.body_rect_global().height()
        bubble.setRows([(True, "61.30", "+2.2%")], [(True, "61.25", "")])
        two_groups = bubble.body_rect_global().height()
        # 再加一个已触发组 = 多一个标题 + 一行 + 一道细线占位
        self.assertEqual(two_groups - one_group,
                         m.TargetBubble.HEADER_H + m.TargetBubble.HEADER_GAP + m.TargetBubble.ROW_H
                         + m.TargetBubble.SEP_GAP_TOP + 1 + m.TargetBubble.SEP_GAP_BOTTOM)
        self.assertEqual(bubble.rows()["pending"], [(True, "61.30", "+2.2%")])
        self.assertEqual(bubble.rows()["hit"], [(True, "61.25", "")])
        # 组标题圆点与铃铛的两种颜色同源：待触发金黄、已触发灰
        self.assertEqual(bubble.PENDING_DOT_COLOR.name(), "#f5c542")
        self.assertEqual(bubble.HIT_DOT_COLOR.name(), "#9aa2b8")

    def test_fired_target_still_allows_opening_panel(self):
        """目标到达后铃铛仍必须可点，允许继续新设或修改目标价。"""
        self.win._alerts = [{"direction": "above", "target": 65.10, "state": "hit"}]
        self.assertEqual(self.win._bell_state(), "fired")
        self.assertFalse(self.win._is_btn_disabled("bell"),
                         "已触发状态不等于入口禁用")
        self.win._open_target_panel()
        self.assertIsNotNone(self.win._target_panel)
        self.assertTrue(self.win._target_panel.isVisible())

    def test_target_panel_is_independent_tool_window(self):
        """目标价面板必须独立于主行情窗口，关闭面板不可连带关闭主窗。"""
        self.win._open_target_panel()
        panel = self.win._target_panel
        self.assertIsNotNone(panel)
        self.assertIsNone(panel.parentWidget(), "面板必须没有主窗父级")
        self.assertTrue(bool(panel.windowFlags() & Qt.WindowType.Tool),
                        "面板必须作为独立工具窗显示")
        self.assertFalse(QApplication.instance().quitOnLastWindowClosed(),
                        "工具窗关闭不能触发应用自动退出")
        panel.close()
        QApplication.processEvents()
        self.assertTrue(self.win.isVisible(), "关闭目标价面板后主行情窗口必须仍然可见")

    def test_second_bell_click_closes_clean_target_panel(self):
        """未输入、未添加、未保存时，再点铃铛应直接收起目标价面板。"""
        self.win._open_target_panel()
        panel = self.win._target_panel
        self.assertTrue(panel.isVisible())
        self.assertFalse(panel.has_unsaved_changes())
        self.win._open_target_panel()
        QApplication.processEvents()
        self.assertIsNone(self.win._target_panel)
        self.assertTrue(self.win.isVisible(), "收起面板不能连带关闭主窗")

    def test_second_bell_click_keeps_dirty_target_panel_open(self):
        """有未生效输入时，再点铃铛必须保留面板和草稿。"""
        self.win._open_target_panel()
        panel = self.win._target_panel
        panel.edit_up.setText("1600")
        panel._add("above")
        self.assertTrue(panel.has_unsaved_changes())
        self.win._open_target_panel()
        self.assertIs(self.win._target_panel, panel)
        self.assertTrue(panel.isVisible())
        self.assertTrue(panel.has_unsaved_changes())

    def test_target_hit_save_failure_keeps_pending_state(self):
        """命中落盘失败时不能提前写入内存或发通知。"""
        self.win._alerts = [{"direction": "below", "target": 64.50, "state": "pending"}]
        fired = [{"direction": "below", "target": 64.50, "text": "跌到 64.50"}]
        with patch.object(m, "evaluate_tick", return_value=(
                [{"direction": "below", "target": 64.50, "state": "hit"}], fired)), \
             patch.object(m, "save_alerts", return_value=False), \
             patch.object(m, "_notify_deliver") as notify:
            self.win._evaluate_targets(64.50)
        self.assertEqual(self.win._alerts[0]["state"], "pending")
        notify.assert_not_called()

    def test_target_notification_uses_stock_name_for_live_trigger(self):
        """实时越过目标价时，通知标题必须展示股票名称而非单独代码。"""
        self.win.code = "sz002837"
        self.win.quote = {"name": "英雄克", "code": "002837"}
        self.win._alerts = [{"direction": "below", "target": 64.50, "state": "pending"}]
        fired = [{"direction": "below", "target": 64.50, "text": "跌到 64.50"}]
        with patch.object(m, "evaluate_tick", return_value=([], fired)), \
             patch.object(m, "save_alerts", return_value=True), \
             patch.object(m, "_notify_deliver") as notify:
            self.win._evaluate_targets(64.50)
        self.assertEqual(notify.call_args.args[0], "英雄克 目标价")
        self.assertIn("002837", notify.call_args.args[3])

    def test_target_hit_starts_rainbow_after_successful_save(self):
        """目标价命中且状态落盘成功后，必须恢复流动彩虹；保存失败不播放。"""
        self.win._alerts = [{"direction": "below", "target": 64.50, "state": "pending"}]
        fired = [{"direction": "below", "target": 64.50, "text": "跌到 64.50"}]
        with patch.object(m, "evaluate_tick", return_value=(
                [{"direction": "below", "target": 64.50, "state": "hit"}], fired)), \
             patch.object(m, "save_alerts", return_value=True), \
             patch.object(self.win, "_start_rainbow") as start_rainbow, \
             patch.object(m, "_notify_deliver"):
            self.win._evaluate_targets(64.50)
        start_rainbow.assert_called_once()

    def test_target_hit_save_failure_does_not_start_rainbow(self):
        self.win._alerts = [{"direction": "below", "target": 64.50, "state": "pending"}]
        fired = [{"direction": "below", "target": 64.50, "text": "跌到 64.50"}]
        with patch.object(m, "evaluate_tick", return_value=(
                [{"direction": "below", "target": 64.50, "state": "hit"}], fired)), \
             patch.object(m, "save_alerts", return_value=False), \
             patch.object(self.win, "_start_rainbow") as start_rainbow, \
             patch.object(m, "_notify_deliver"):
            self.win._evaluate_targets(64.50)
        start_rainbow.assert_not_called()

    def test_target_notification_uses_stock_name_on_startup_recovery(self):
        """启动恢复时补发的通知也必须展示股票名称。"""
        self.win.code = "sz002837"
        self.win.quote = {"name": "英雄克", "code": "002837"}
        raw = [{"direction": "below", "target": 64.50, "state": "pending"}]
        reminder = {"direction": "below", "target": 64.50, "text": "跌到 64.50"}
        with patch.object(m, "load_alerts", return_value=raw), \
             patch.object(m, "recover_alerts", return_value=(raw, [reminder])), \
             patch.object(m, "_notify_deliver") as notify:
            self.win._init_targets()
        self.assertEqual(notify.call_args.args[0], "英雄克 目标价")
        self.assertIn("002837", notify.call_args.args[3])

    def test_current_price_prefers_real_quote_over_animation(self):
        """R47：判定必须基于真实成交价，不能用滚动动画的中间帧。

        价格 64.00 → 65.00 时动画会路过 64.50；用动画值判定会让「跌到 64.50」
        被一个从未成交过的价格触发。
        """
        self.win.quote = {"price": 65.0}
        self.win._disp_price = 64.50      # 动画中途
        self.assertEqual(self.win._current_price(), 65.0)

    def test_current_price_falls_back_to_display_when_quote_missing(self):
        self.win.quote = None
        self.win._disp_price = 64.50
        self.assertEqual(self.win._current_price(), 64.50)

    def test_current_price_skips_non_positive_quote(self):
        """行情价 0（停牌/未就绪）时不得返回 0，应退化为显示值。"""
        self.win.quote = {"price": 0}
        self.win._disp_price = 64.50
        self.assertEqual(self.win._current_price(), 64.50)

    def test_fallback_price_does_not_trigger_targets(self):
        """R48：非交易时段的兜底价（上一交易日收盘价）不得参与触发判定。

        盘前腾讯接口返回 0，兜底逻辑会用上周五收盘价填充展示价；拿它判定会让
        「跌到 X」一开盘前就被误触发。没有真实成交价时应返回 0（不下结论）。
        """
        self.win.quote = {"price": 9.40}      # 兜底改写后的展示价
        self.win._disp_price = 9.40
        self.win._real_price = 0.0            # 真实行情价缺失
        self.win._price_is_fallback = True
        self.assertEqual(self.win._current_price(), 0.0)

    def test_real_price_still_used_during_fallback(self):
        """有真实成交价时（如午休），兜底不影响判定。"""
        self.win.quote = {"price": 9.40}
        self.win._real_price = 9.60
        self.win._price_is_fallback = True
        self.assertEqual(self.win._current_price(), 9.60)

    def test_clear_expired_keeps_next_day_targets(self):
        """15:00:30 的过期清理不得连「次日生效」的目标价一起清掉。"""
        self.win._alerts = [
            {"direction": "above", "target": 70.0, "session_date": "2020-01-01",
             "state": "pending", "last_side": None},
            {"direction": "above", "target": 71.0, "session_date": "2099-12-31",
             "state": "pending", "last_side": None},
        ]
        with patch.object(m, "save_alerts", return_value=True):
            self.win._clear_expired()
        self.assertEqual(len(self.win._alerts), 1)
        self.assertEqual(self.win._alerts[0]["session_date"], "2099-12-31")

    def test_open_target_panel_blocked_when_editor_open(self):
        """M1：编辑器开启时，任何入口都不得再打开目标价面板。

        右键菜单此前未做互斥，可绕出「编辑器 + 面板」双弹窗并存；而面板一出现，
        编辑图标就被置灰，用户反而关不掉编辑器。互斥已下沉到入口内部。
        """
        class _FakeEditor:
            def isVisible(self):
                return True
        self.win._editor = _FakeEditor()
        try:
            self.win._open_target_panel()
            self.assertIsNone(getattr(self.win, "_target_panel", None),
                              "编辑器开启时，面板入口应被拦截（禁止双弹窗并存）")
        finally:
            self.win._editor = None

    def test_minimize_to_dock_survives_deleted_editor(self):
        """编辑器 C++ 对象已销毁时，缩小主窗仍必须继续执行，不能表现为按钮无响应。"""
        class _DeletedEditor:
            def isVisible(self):
                raise RuntimeError("wrapped C/C++ object of type EditorWindow has been deleted")
        self.win._editor = _DeletedEditor()
        with patch.object(m, "show_dock"), \
             patch.object(m.FloatWindow, "_close_target_panel"), \
             patch.object(m.FloatWindow, "_update_nap_policy"):
            self.win.minimize_to_dock()
        self.assertTrue(self.win._minimized)
        self.assertFalse(self.win.isVisible())
        self.assertIsNone(self.win._editor)

    def test_app_activation_does_not_trigger_restore(self):
        """应用激活不是 Dock 恢复指令，不能制造无效恢复回调。"""
        with patch.object(self.win, "_restore_window") as restore:
            self.win._on_app_state(Qt.ApplicationState.ApplicationActive)
        restore.assert_not_called()

    def test_minimize_to_dock_closes_target_panel(self):
        """M3：最小化到 Dock 必须一并收掉目标价面板。

        原实现只关编辑器，面板会孤零零留在屏幕上，且仍能输入价格并「立即生效」
        写回已隐藏的主窗。
        """
        closed = {"n": 0}

        class _FakePanel:
            def isVisible(self):
                return True

            def close(self):
                closed["n"] += 1

        self.win._target_panel = _FakePanel()
        try:
            self.win.minimize_to_dock()
        except Exception:
            pass    # 只关心面板是否被收口，Dock 相关副作用不影响本断言
        self.assertGreaterEqual(closed["n"], 1,
                                "最小化时目标价面板应被关闭")

    def test_edit_button_hit(self):
        """顶部编辑按钮：按下+松开同一点触发 open_editor"""
        self.win.open_editor = lambda: setattr(self.win, "_opened", True)
        pos = self._btn_center("edit")
        QTest.mousePress(self.win, Qt.MouseButton.LeftButton, pos=pos)
        QTest.mouseRelease(self.win, Qt.MouseButton.LeftButton, pos=pos)
        self.assertTrue(self.win._opened)

    def test_top_buttons_have_complete_expanded_hit_areas(self):
        """v1.3.58：四枚按钮中心/垂直边缘可点，相邻 22px 热区间不留死区。"""
        centers = []
        names = ("bell", "edit", "min", "close")
        for name in names:
            center = self._btn_center(name)
            centers.append(center.x())
            self.assertEqual(self.win._hit_btn(center), name)
            self.assertEqual(self.win._hit_btn(QPoint(center.x(), center.y() + m.HIT_DY)), name)
        gaps = [right - left for left, right in zip(sorted(centers), sorted(centers)[1:])]
        self.assertEqual(gaps, [22, 22, 22])
        for left, right in (("bell", "edit"), ("edit", "min"), ("min", "close")):
            x = (self._btn_center(left).x() + self._btn_center(right).x()) // 2
            self.assertIn(self.win._hit_btn(QPoint(x, m.BTN_CY)), (left, right))
        self.assertIsNone(self.win._hit_btn(QPoint(0, 0)))

    def test_top_button_accepts_six_px_release_drift(self):
        """v1.3.58：目标价按钮在 6px 触控板释放漂移下仍必须触发。"""
        calls = []
        self.win._open_target_panel = lambda: calls.append("bell")
        press = self._btn_center("bell")
        release = QPoint(press.x() + m.PRESS_RELEASE_SLOP, press.y() + m.PRESS_RELEASE_SLOP)
        QTest.mousePress(self.win, Qt.MouseButton.LeftButton, pos=press)
        self.assertEqual(self.win._press_btn, "bell", "按下时必须记录按钮，供界面画出按下态")
        QTest.mouseRelease(self.win, Qt.MouseButton.LeftButton, pos=release)
        self.assertEqual(calls, ["bell"])
        self.assertIsNone(self.win._press_btn, "松开后必须清除按下态")

    def test_top_button_hit_areas_have_no_horizontal_dead_zone(self):
        """v1.3.58：相邻中心的中点必须命中其中最近按钮，不能留下点击空档。"""
        for left, right in (("bell", "edit"), ("edit", "min"), ("min", "close")):
            x = (self._btn_center(left).x() + self._btn_center(right).x()) // 2
            hit = self.win._hit_btn(QPoint(x, m.BTN_CY))
            self.assertIn(hit, (left, right), "%s/%s 中间不得是死区" % (left, right))

    def test_leaving_pressed_button_clears_stale_highlight(self):
        """鼠标离开缩小按钮后必须清除按下态，不能残留悬浮底板。"""
        pos = self._btn_center("min")
        self.win.minimize_to_dock = lambda: None
        QTest.mousePress(self.win, Qt.MouseButton.LeftButton, pos=pos)
        self.assertEqual(self.win._press_btn, "min")
        QTest.mouseMove(self.win, QPoint(20, 50))
        self.assertIsNone(self.win._press_btn)
        self.assertIsNone(self.win._hover_btn)

    def test_leaving_window_clears_stale_button_states(self):
        """跨出主窗时即使系统未继续投递移动/松开事件，也必须撤销灰色底板。"""
        self.win._hover_btn = "min"
        self.win._press_btn = "min"
        self.win._press_btn_actioned = True
        self.win.leaveEvent(None)
        self.assertIsNone(self.win._hover_btn)
        self.assertIsNone(self.win._press_btn)
        self.assertFalse(self.win._press_btn_actioned)

    def test_blocked_buttons_show_explicit_feedback(self):
        """互斥入口不可用时必须告知用户，而非静默无响应。"""
        with patch.object(m.QToolTip, "showText") as show_tip, \
             patch.object(m.QTimer, "singleShot"):
            self.win._show_btn_blocked_feedback("edit")
            self.win._show_btn_blocked_feedback("bell")
        self.assertEqual(show_tip.call_count, 2)
        self.assertEqual(show_tip.call_args_list[0].args[1], "请先关闭目标价窗口")
        self.assertEqual(show_tip.call_args_list[1].args[1], "请先关闭替换股票窗口")

    def test_target_bubble_is_mouse_transparent(self):
        """铃铛提示气泡只能展示信息，不能抢走顶部按钮点击。"""
        bubble = self.win._target_bubble
        self.assertTrue(bubble.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))

    def test_sync_open_tool_windows_follows_position_without_raising(self):
        """v1.4.28：主窗移动时工具窗只跟位，不再每拍抬层（抬层归 _force_top）。

        改前这一步挂在同一个方法里，于是拖动的每一拍都要替面板做一遍
        `winId()` + `setLevel_` + `orderFrontRegardless`，实测让面板开着时的拖动
        每拍从 0.44ms 涨到 1.6~2.2ms（最差 6.9ms）。真正需要抬层的时刻只有
        "主窗刚被前置"，那条路径单独测（见下条）。
        """
        class _FakePanel:
            def __init__(self):
                self.raised = 0

            def isVisible(self):
                return True

            def raise_(self):
                self.raised += 1

        panel = _FakePanel()
        self.win._target_panel = panel
        with patch.object(m.FloatWindow, "_place_panel") as place, \
             patch.object(m.FloatWindow, "_raise_tool_window") as raise_tool:
            self.win._sync_open_tool_windows()
        place.assert_called_once_with(panel)
        raise_tool.assert_not_called()
        self.win._target_panel = None

    def test_raise_open_tool_windows_raises_only_visible_tools(self):
        """主窗被前置之后，可见的工具窗必须抬回它上面；隐藏的不动。"""
        class _FakeTool:
            def __init__(self, visible):
                self.visible = visible
                self.raised = 0

            def isVisible(self):
                return self.visible

            def raise_(self):
                self.raised += 1

        panel, editor = _FakeTool(True), _FakeTool(False)
        self.win._target_panel, self.win._editor = panel, editor
        with patch.object(m.FloatWindow, "_raise_tool_window",
                          side_effect=lambda tool: tool.raise_()) as raise_tool:
            self.win._raise_open_tool_windows()
        self.assertEqual(panel.raised, 1, "可见面板必须抬层")
        self.assertEqual(editor.raised, 0, "隐藏编辑器不应抬层")
        self.win._target_panel = None
        self.win._editor = None

    def test_close_button_calls_quit(self):
        """顶部关闭按钮触发 quit()"""
        self.win.quit = lambda: setattr(self.win, "_quit_called", True)
        pos = self._btn_center("close")
        QTest.mousePress(self.win, Qt.MouseButton.LeftButton, pos=pos)
        QTest.mouseRelease(self.win, Qt.MouseButton.LeftButton, pos=pos)
        QApplication.processEvents()
        self.assertTrue(self.win._quit_called)

    def test_min_and_close_execute_on_press_when_release_is_lost(self):
        """macOS 丢失松开事件时，缩小/关闭仍必须在按下命中后立即生效。"""
        calls = []
        self.win.minimize_to_dock = lambda: calls.append("min")
        self.win.quit = lambda: calls.append("close")
        for name in ("min", "close"):
            QTest.mousePress(self.win, Qt.MouseButton.LeftButton, pos=self._btn_center(name))
            QApplication.processEvents()
            self.assertIn(name, calls)
            self.assertTrue(self.win._press_btn_actioned)
            # 故意不发送 mouseRelease，模拟跨 Space/置顶时系统丢失松开事件。
            self.win._press_btn = None
            self.win._press_btn_actioned = False

    def test_min_and_close_queue_actions_after_mouse_event(self):
        """缩小/关闭必须由主窗命中后排队执行，不再增加会干扰 Dock 生命周期的子按钮层。"""
        self.assertFalse(hasattr(self.win, "_native_top_buttons"))
        calls = []
        self.win.minimize_to_dock = lambda: calls.append("min")
        self.win.quit = lambda: calls.append("close")
        with patch.object(m.QTimer, "singleShot", side_effect=lambda _ms, cb: cb()) as queue:
            self.assertTrue(self.win._trigger_top_button("min"))
            self.assertTrue(self.win._trigger_top_button("close"))
        self.assertEqual(calls, ["min", "close"])
        self.assertEqual(queue.call_count, 2)

    def test_restore_ignores_stale_callback_when_window_is_not_minimized(self):
        """普通激活或过期 Dock 回调不得重复恢复窗口，避免主窗闪现后再次消失。"""
        self.win._runtime.exit_dock()
        with patch.object(self.win, "show") as show:
            self.win._restore_window()
        show.assert_not_called()

    def test_drag_moves_window(self):
        """拖拽：按下-移动-松开，窗口位移等于光标位移"""
        x0, y0 = self.win.pos().x(), self.win.pos().y()
        p_press = QPoint(30, 40)          # 非按钮、非底部缩放区
        p_move = QPoint(60, 60)
        QTest.mousePress(self.win, Qt.MouseButton.LeftButton, pos=p_press)
        QTest.mouseMove(self.win, p_move)
        QTest.mouseRelease(self.win, Qt.MouseButton.LeftButton, pos=p_move)
        dx, dy = p_move.x() - p_press.x(), p_move.y() - p_press.y()
        self.assertEqual(self.win.pos().x(), x0 + dx)
        self.assertEqual(self.win.pos().y(), y0 + dy)

    def test_bottom_resize(self):
        """底部缩放：在底部边缘按下并下移，窗口高度同步增加（仅展开态）"""
        self.win._chart_expanded = True
        # v1.2.36：展开态需同步系统级高度上下限 —— 收起态会把 max 锁到收起高度
        # （min==max），仅置 _chart_expanded 标志不足以代表真实展开态（真实展开
        # 必经 _toggle_chart 恢复 max），此处显式同步以匹配实现语义
        self.win.setMinimumHeight(m.MIN_H)
        self.win.setMaximumHeight(m.MAX_H)
        QApplication.processEvents()
        h0 = self.win.height()
        press = QPoint(m.W // 2, h0 - 2)
        move = QPoint(m.W // 2, h0 - 2 + 20)
        QTest.mousePress(self.win, Qt.MouseButton.LeftButton, pos=press)
        QTest.mouseMove(self.win, move)
        QTest.mouseRelease(self.win, Qt.MouseButton.LeftButton, pos=move)
        self.assertEqual(self.win.height(), h0 + 20)

    def test_bottom_resize_collapsed_locked(self):
        """v1.2.28 收起态锁定高度：底部边缘按下不进入 _resizing，高度保持不变"""
        self.win._chart_expanded = False
        ch = self.win._height_collapsed()
        self.win.setMinimumHeight(ch)
        self.win.resize(self.win.width(), ch)
        QApplication.processEvents()
        h0 = self.win.height()
        press = QPoint(m.W // 2, h0 - 2)
        move = QPoint(m.W // 2, h0 - 2 + 20)
        QTest.mousePress(self.win, Qt.MouseButton.LeftButton, pos=press)
        self.assertFalse(self.win._resizing)      # 收起态底部不进入调整高度模式
        QTest.mouseMove(self.win, move)
        QTest.mouseRelease(self.win, Qt.MouseButton.LeftButton, pos=move)
        self.assertEqual(self.win.height(), h0)  # 高度锁定，未被拉伸

    def test_height_collapsed_math(self):
        """v1.2.28: _height_collapsed 数学自洽 —— 固定值、为正、小于展开高度"""
        ch = self.win._height_collapsed()
        self.assertEqual(ch, self.win._height_collapsed())
        self.assertGreater(ch, 0)
        self.assertLess(ch, self.win._height_expanded())

    def test_context_menu(self):
        """右键触发 contextMenuEvent 并持有上下文菜单（非阻塞 popup）"""
        ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                               QPoint(40, 40), QPoint(140, 140))
        QApplication.sendEvent(self.win, ev)
        QApplication.processEvents()
        self.assertIsNotNone(self.win._ctx_menu)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTargetSaveConsistency(unittest.TestCase):
    """R46 第 1 轮走查：目标价「先落盘成功、再动内存」不变量（窗口层）。

    命中 / 过期清理 / 面板保存 / 清空 / 切股 五条写路径都必须满足：
    save_alerts 返回 False 时内存保持原值，杜绝「界面已生效、磁盘没写入」——
    否则重启后旧目标复活、或已触发状态回退成待触发导致重复提醒。
    """

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: True),
            patch.object(sf_quote, "http_get", _fake_http_get),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()
        self.win.timer.stop()
        self.win.top_timer.stop()

    def tearDown(self):
        self.win.close()
        QApplication.processEvents()
        for p in self._patches:
            p.stop()

    @staticmethod
    def _alert(target=70.0, state="pending", sd="2099-12-31"):
        return {"direction": "above", "target": target,
                "session_date": sd, "state": state, "last_side": None}

    def test_set_targets_failure_keeps_memory(self):
        old = [self._alert(70.0)]
        self.win._alerts = [dict(a) for a in old]
        with patch.object(m, "save_alerts", return_value=False), \
             patch.object(m.FloatWindow, "_after_save_config", lambda self, ok: None):
            ok = self.win._set_targets([self._alert(71.0)])
        self.assertFalse(ok)
        self.assertEqual(self.win._alerts, old,
                         "落盘失败时内存不得提前切换到新目标价")

    def test_set_targets_success_updates_memory(self):
        self.win._alerts = [self._alert(70.0)]
        with patch.object(m, "save_alerts", return_value=True), \
             patch.object(m.FloatWindow, "_after_save_config", lambda self, ok: None), \
             patch.object(m.FloatWindow, "_request_notify_auth", lambda self: None):
            ok = self.win._set_targets([self._alert(71.0)])
        self.assertTrue(ok)
        self.assertEqual(self.win._alerts[0]["target"], 71.0)

    def test_clear_targets_failure_keeps_memory(self):
        old = [self._alert(70.0)]
        self.win._alerts = [dict(a) for a in old]
        with patch.object(m, "save_alerts", return_value=False), \
             patch.object(m.FloatWindow, "_after_save_config", lambda self, ok: None):
            ok = self.win._clear_targets()
        self.assertFalse(ok)
        self.assertEqual(self.win._alerts, old)

    def test_switch_stock_still_proceeds_when_the_store_is_unwritable(self):
        """v1.4.58 的旧契约（切股先清空，清空失败就中止切股）已经被彻底解取代：
        切股不再删除任何目标价，所以「清空落盘失败」这件事本身不存在了。

        留这一条是为了钉住"旧行为不许回来"：存储写不动也必须照常切股，且切过去之后
        内存里是**这只股票自己的**条目（而不是上一只的，也不是被清空的）。
        正向流程与"别股条目不被带走"由 tests/test_v1459_targets_per_code.py 逐条覆盖。
        """
        old = [self._alert(70.0)]
        self.win._alerts = [dict(a) for a in old]
        with patch.object(m, "save_alerts", return_value=False) as save, \
             patch.object(m.FloatWindow, "_after_save_config", lambda self, ok: None), \
             patch.object(m, "load_alerts", return_value=[]), \
             patch.object(m.FloatWindow, "_force_fetch") as fetch, \
             patch.object(m.FloatWindow, "_close_target_panel") as close_panel:
            ok = self.win.apply_stock("sz000001")
        self.assertTrue(ok, "切股被一次与本股无关的写盘失败拦住了 —— 旧契约回来了")
        self.assertEqual(self.win.code, "sz000001")
        self.assertEqual(self.win._alerts, [], "切过去应当接管这只股票自己的条目")
        save.assert_not_called()
        fetch.assert_called_once()
        close_panel.assert_called_once()

    def test_switch_stock_without_targets_does_not_write_empty_alerts(self):
        """没有旧目标价时，切股不得依赖 alerts 配置文件可写。"""
        self.win._alerts = []
        with patch.object(m, "save_alerts", return_value=False) as save, \
             patch.object(m.FloatWindow, "_force_fetch"), \
             patch.object(m.FloatWindow, "_close_target_panel"):
            ok = self.win.apply_stock("sz000001")
        self.assertTrue(ok)
        save.assert_not_called()
        self.assertEqual(self.win.code, "sz000001")

    def test_roll_failure_keeps_old_memory_and_skips_evaluation(self):
        old = [self._alert(70.0, sd="2026-09-06")]
        rolled = [self._alert(70.0, sd="2026-09-07")]
        self.win._alerts = old
        with patch.object(m, "roll_alerts_to_trading_day", return_value=(rolled, True)), \
             patch.object(m, "save_alerts", return_value=False), \
             patch.object(m, "evaluate_tick") as evaluate, \
             patch.object(m.FloatWindow, "_after_save_config") as visible:
            self.win._evaluate_targets(71.0)
        self.assertIs(self.win._alerts, old)
        evaluate.assert_not_called()
        visible.assert_called_once_with(False)

    def test_roll_success_commits_memory_before_evaluation(self):
        old = [self._alert(70.0, sd="2026-09-06")]
        rolled = [self._alert(70.0, sd="2026-09-07")]
        self.win._alerts = old
        with patch.object(m, "roll_alerts_to_trading_day", return_value=(rolled, True)), \
             patch.object(m, "save_alerts", return_value=True), \
             patch.object(m, "evaluate_tick", return_value=(rolled, [])) as evaluate, \
             patch.object(m.FloatWindow, "_after_save_config"):
            self.win._evaluate_targets(69.0)
        self.assertEqual(self.win._alerts, rolled)
        evaluate.assert_called_once_with(rolled, 69.0, decimals=2)

    def test_arm_timer_clears_when_all_expired(self):
        """R46：所有条目都已过清空点时，武装定时器必须顺手清掉死条目。

        原实现发现没有"未来时刻"就直接 return，死条目会挂在列表里、铃铛常亮
        直到重启（定时器漏触发 / 休眠跨过 15:00:30 时必现）。
        """
        self.win._alerts = [self._alert(70.0, sd="2020-01-01")]
        with patch.object(m, "save_alerts", return_value=True) as save, \
             patch.object(m.FloatWindow, "_after_save_config", lambda self, ok: None):
            self.win._arm_clear_timer()
        self.assertEqual(self.win._alerts, [])
        # v1.4.59（S4 彻底解）：写盘统一走 _persist_alerts，必须带上当前股票 code，
        # 否则这一笔会把别的股票的条目一起换掉。
        save.assert_called_once_with([], code=self.win.code)

    def test_init_targets_failure_drops_expired_from_memory(self):
        """R46：启动恢复落盘失败时，内存回退的是"去掉过期项"的原始数据。

        直接回退 raw 会把本该丢弃的过期条目留在列表里，而 _arm_clear_timer
        只武装未来时刻，它们永远不会被清掉。
        """
        stale = self._alert(70.0, sd="2020-01-01")
        alive = self._alert(71.0, sd="2099-12-31")
        raw = [stale, alive]
        with patch.object(m, "load_alerts", return_value=raw), \
             patch.object(m, "save_alerts", return_value=False), \
             patch.object(m, "recover_alerts", return_value=([alive], [])):
            self.win._init_targets()
        self.assertEqual(self.win._alerts, [alive])
