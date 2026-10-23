# -*- coding: utf-8 -*-
"""铃铛气泡的悬停节拍：划过不弹、停满才弹、滑出有缓冲、点击取消待弹、行情刷新不重复置顶。

起因：用户反馈"鼠标经过或停留时，窗口会一闪一闪，太敏感了"。探针（离屏，数 show/hide 调用）量到：
铃铛与右邻「编辑」的感应区紧挨着，划过两趟气泡出现/消失各 2 次，交界线上 ±1px 抖 40 次出现/消失各 20 次；
价格活跃时每秒 6~7 次文字变化，每次都走一遍 move+show+raise。
时间一律取产品常量（m.BELL_BUBBLE_*），不写死毫秒；"还没到点"的断言用超大常量让定时器不可能到点，
不靠"赶在 200ms 之前断言"。真窗口 + 完整隔离桩（AGENTS §6 第 14 条）。
"""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QPoint
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMenu

import sf_quote
import stock_float_window as m
from tests._base import SFCase

_QAPP = None


def _close_stray_popups():
    """关掉前面用例遗留的菜单/弹层。有活动弹层时 Qt 会把鼠标事件交给它，QTest.mouseMove 到不了主窗，
    悬停类用例就全红；完整关卡里两者中间恰好隔着别的用例把它清掉，单独或少量模块合跑才会撞上。"""
    for _ in range(10):
        popup = QApplication.activePopupWidget()
        if popup is None:
            break
        popup.close()
        QApplication.processEvents()
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, QMenu) and widget.isVisible():
            widget.close()
    QApplication.processEvents()
FAR = 600000        # 毫秒：比任何用例都长，定时器在用例内不可能到点
BELL_PRICE = {"direction": "above", "target": 1063.0, "state": "pending", "session_date": "2099-12-31"}


def _wire(host, **members):
    for name, value in members.items():
        setattr(host, name, value)


def _peek(host, *names):
    values = [getattr(host, name) for name in names]
    return values[0] if len(values) == 1 else tuple(values)


def _fake_http_get(url, **kw):
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


class DwellCase(SFCase):
    def setUp(self):
        global _QAPP
        super().setUp()
        _QAPP = SFCase.ensure_qapp()
        _close_stray_popups()
        for p in (patch.object(m, "load_config", return_value=("sh600519", 100, 100, 200, "translucent")),
                  patch.object(m, "save_config", lambda *a, **k: True),
                  patch.object(sf_quote, "http_get", _fake_http_get),
                  patch.object(m, "start_icon_worker", lambda: None),
                  patch.object(m.FloatWindow, "_force_top", lambda self: None),
                  patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
                  patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None)):
            self.track_patch(p)
        self.win = m.FloatWindow()
        self.win.running = False
        _peek(self.win, "_wake").set()
        self.win.timer.stop()
        self.win.top_timer.stop()
        self.win.show()
        QApplication.processEvents()
        _wire(self.win, _price_is_fallback=False, _real_price=1029.0, _alerts=[dict(BELL_PRICE)])
        self.shown = Mock(wraps=self.bubble().show_below)
        _wire(self.bubble(), show_below=self.shown)

    def tearDown(self):
        # 气泡是无父窗口的独立顶层窗，关主窗不会带走它；不收的话会盖在下一条用例的同一位置，
        # 让「鼠标移到哪」的前提悄悄变化（整组红、单跑绿的根因之一）。
        self.bubble().hide()
        _wire(self.win, _exit_persisted=True)   # 关窗必须发生在 patch 还活着的时候（§6 第 11 条）
        self.win.close()
        QApplication.processEvents()
        super().tearDown()

    # ---- 辅助 ----
    def bubble(self):
        return _peek(self.win, "_target_bubble")

    def timers(self):
        return _peek(self.win, "_bubble_show_timer", "_bubble_hide_timer")

    def to(self, x, y=None):
        QTest.mouseMove(self.win, QPoint(int(x), m.BTN_CY if y is None else y))
        QApplication.processEvents()

    def away(self):
        self.to(20, 50)

    def bell_x(self):
        return self.win.width() - m.BTN_X["bell"]

    def edit_x(self):
        return self.win.width() - m.BTN_X["edit"]

    def hover_bell(self):
        self.away()
        self.to(self.bell_x())

    def observe(self, seconds):
        """持续泵事件观察一段时间（wait_for 的条件永远为假，只借它的轮询节拍）。"""
        self.wait_for(lambda: (QApplication.processEvents(), False)[1], timeout=seconds, tick=0.005)

    def visible_within(self, seconds):
        def cond():
            QApplication.processEvents()
            return self.bubble().isVisible()
        return self.wait_for(cond, timeout=seconds)

    def hidden_within(self, seconds):
        def cond():
            QApplication.processEvents()
            return not self.bubble().isVisible()
        return self.wait_for(cond, timeout=seconds)

    def dwell(self):
        return m.BELL_BUBBLE_DWELL_MS / 1000.0

    def grace(self):
        return m.BELL_BUBBLE_HIDE_GRACE_MS / 1000.0

    def open_bubble(self):
        self.hover_bell()
        self.assertTrue(self.visible_within(self.dwell() + 1.5), "停满 DWELL 后应弹出气泡")

    # ---- 弹出 ----
    def test_刚进铃铛先不弹_只是排上待弹出(self):
        with patch.object(m, "BELL_BUBBLE_DWELL_MS", FAR):
            self.hover_bell()
            show_timer, _ = self.timers()
            self.assertTrue(show_timer.isActive())
            self.assertFalse(self.bubble().isVisible())
            self.shown.assert_not_called()

    def test_停满节拍才弹出(self):
        self.hover_bell()
        self.assertTrue(self.visible_within(self.dwell() + 1.5))
        self.shown.assert_called()

    def test_划过铃铛一趟_气泡一次都不弹(self):
        self.away()
        self.to(self.bell_x())
        self.to(self.edit_x())          # 马上滑到右邻按钮
        self.away()
        self.observe(self.dwell() + 0.3)
        self.shown.assert_not_called()
        self.assertFalse(self.bubble().isVisible())

    def test_铃铛与相邻按钮交界处来回抖_气泡一次都不弹(self):
        edge = self.bell_x() + m.HIT_DX
        self.away()
        for i in range(40):
            self.to(edge - 1 + (i % 2) * 2)     # edge-1 在铃铛里，edge+1 在「编辑」里
        self.away()
        self.observe(self.dwell() + 0.3)
        self.shown.assert_not_called()

    def test_待弹出期间点击铃铛_不再冒出气泡(self):
        with patch.object(m, "BELL_BUBBLE_DWELL_MS", FAR), \
                patch.object(m.FloatWindow, "_open_target_panel", Mock()):
            self.hover_bell()
            show_timer, _ = self.timers()
            self.assertTrue(show_timer.isActive())
            _peek(self.win, "_trigger_top_button")("bell")
            self.assertFalse(show_timer.isActive(), "点击是明确动作，待弹出的气泡必须取消")

    def test_待弹出到点时鼠标已不在铃铛上_不弹(self):
        # 正常路径里离开铃铛会先停掉待弹出的定时器；这里模拟「定时器还是响了」（排队的到点信号
        # 在离开之后才被派发），到点那一刻必须再核对一次，不能只信「应该已经停了」。
        self.away()
        _peek(self.win, "_on_bubble_show_due")()
        self.shown.assert_not_called()
        self.assertFalse(self.bubble().isVisible())

    def test_待弹出到点时鼠标仍在铃铛上_弹出(self):
        with patch.object(m, "BELL_BUBBLE_DWELL_MS", FAR):
            self.hover_bell()
            _peek(self.win, "_on_bubble_show_due")()
            self.assertTrue(self.bubble().isVisible())

    # ---- 收起 ----
    def test_滑出铃铛后先留着_排上待收起(self):
        self.open_bubble()
        with patch.object(m, "BELL_BUBBLE_HIDE_GRACE_MS", FAR):
            self.away()
            _, hide_timer = self.timers()
            self.assertTrue(hide_timer.isActive())
            self.assertTrue(self.bubble().isVisible(), "缓冲期内气泡还在")

    def test_缓冲到点收起(self):
        self.open_bubble()
        self.away()
        self.assertTrue(self.hidden_within(self.grace() + 1.5))

    def test_缓冲内滑回铃铛_不收起也不重新计时弹出(self):
        self.open_bubble()
        self.to(self.edit_x())
        self.to(self.bell_x())
        _, hide_timer = self.timers()
        self.assertFalse(hide_timer.isActive(), "滑回来必须取消待收起")
        self.observe(self.grace() + 0.3)
        self.assertTrue(self.bubble().isVisible())

    def test_离开整个窗口立刻收起并取消全部待办(self):
        self.open_bubble()
        self.win.leaveEvent(None)
        self.assertFalse(self.bubble().isVisible(), "离开整窗不留缓冲")
        with patch.object(m, "BELL_BUBBLE_DWELL_MS", FAR):
            self.hover_bell()
            self.win.leaveEvent(None)
            for timer in self.timers():
                self.assertFalse(timer.isActive())

    # ---- 刷新 ----
    def test_数字变了但位置没变_不再移动也不再置顶(self):
        self.open_bubble()
        before = self.bubble().pos()
        _wire(self.bubble(), move=Mock(), raise_=Mock())
        calls_before = self.shown.call_count
        _wire(self.win, _real_price=1051.4)         # 百分比 +3.3% -> +1.1%，位数相同
        _peek(self.win, "_refresh_target_bubble")()
        self.assertEqual(self.shown.call_count, calls_before + 1, "内容变了，仍要走摆放这一步")
        self.bubble().move.assert_not_called()
        self.bubble().raise_.assert_not_called()
        self.assertEqual(self.bubble().pos(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
