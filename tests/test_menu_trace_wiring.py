# -*- coding: utf-8 -*-
"""右键菜单会话记录与主窗的接线：菜单项固定名字、右键按下对账、反馈入口分段计时、主窗现场。

记录本身的逻辑见 test_menu_trace.py；这里验的是主窗把它们接上了：用真 FloatWindow 加完整隔离桩
（AGENTS §6 第 14 条）。内部状态一律走 _wire / _peek 两个入口（§6 第 12 条）。
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMenu

import sf_menu_trace as mt
import sf_quote
import stock_float_window as m
from tests._base import SFCase

_QAPP = None
EXPECTED_TAGS = {"refresh", "face_guard", "update", "diagnostics", "feedback", "version"}


def _wire(host, **members):
    for name, value in members.items():
        setattr(host, name, value)


def _peek(host, *names):
    values = [getattr(host, name) for name in names]
    return values[0] if len(values) == 1 else tuple(values)


def _fake_http_get(url, **kw):
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


class WiringCase(SFCase):
    def setUp(self):
        global _QAPP
        super().setUp()
        _QAPP = SFCase.ensure_qapp()
        mt.clear()
        self.support = tempfile.TemporaryDirectory()
        self.addCleanup(self.support.cleanup)
        for p in (patch.object(m, "load_config", return_value=("sh600519", 100, 100, 200, "translucent")),
                  patch.object(m, "save_config", lambda *a, **k: True),
                  patch.object(sf_quote, "http_get", _fake_http_get),
                  patch.object(m, "start_icon_worker", lambda: None),
                  patch.object(m.FloatWindow, "_force_top", lambda self: None),
                  patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
                  patch("sf_constants.APP_SUPPORT_DIR", self.support.name),
                  patch.object(m.QToolTip, "showText", lambda *a, **k: None),
                  patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None)):
            self.track_patch(p)
        self.win = m.FloatWindow()
        self.win.running = False
        _peek(self.win, "_wake").set()
        self.win.timer.stop()
        self.win.top_timer.stop()
        self.win.show()
        QApplication.processEvents()

    def tearDown(self):
        dialog = getattr(self.win, "_feedback_dialog", None)
        if dialog is not None:
            dialog.close()
        _wire(self.win, _exit_persisted=True)    # 关窗必须发生在 patch 还活着的时候（§6 第 11 条）
        self.win.close()
        QApplication.processEvents()
        super().tearDown()

    def open_menu(self, face_guard=True):
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(20, 20), QPoint(100, 100))
        with patch.object(m, "feature_enabled", return_value=face_guard):
            self.win.contextMenuEvent(event)
        return self.win.findChildren(QMenu)[-1]

    def attempts(self):
        return mt.diagnostics()["feedback_attempts"]

    def own_dialog(self):
        return self.track_widget(_peek(self.win, "_feedback_dialog"))


class MenuWiring(WiringCase):
    def test_每个菜单项都有固定名字(self):
        menu = self.open_menu()
        tags = {a.data() for a in menu.actions() if not a.isSeparator()}
        self.assertEqual(tags, EXPECTED_TAGS)

    def test_关掉自动隐藏窗口后少一项_其余名字不变(self):
        menu = self.open_menu(face_guard=False)
        tags = {a.data() for a in menu.actions() if not a.isSeparator()}
        self.assertEqual(tags, EXPECTED_TAGS - {"face_guard"})

    def test_打开菜单就开一条会话_带主窗现场(self):
        self.open_menu()
        session = mt.diagnostics()["sessions"][-1]
        self.assertEqual(set(session["context"]), {"main_visible", "main_active", "in_fullscreen_space"})
        self.assertTrue(session["context"]["main_visible"])

    def test_右键按下被计数_左键不计(self):
        QTest.mousePress(self.win, Qt.MouseButton.RightButton, pos=QPoint(20, 50))
        QTest.mouseRelease(self.win, Qt.MouseButton.RightButton, pos=QPoint(20, 50))
        self.assertEqual(mt.diagnostics()["right_presses"], 1)
        QTest.mouseClick(self.win, Qt.MouseButton.LeftButton, pos=QPoint(20, 50))
        self.assertEqual(mt.diagnostics()["right_presses"], 1)


class MainWindowContext(SFCase):
    def window(self, **kw):
        win = Mock()
        win.isVisible.return_value = True
        win.isActiveWindow.return_value = False
        _wire(win, _normal_space_id=kw.get("normal", 3))
        return win

    def test_非原生进程只有可见与前台两项_全屏项是未知(self):
        with patch.object(m, "native_cocoa_active", return_value=False):
            out = m._menu_context(self.window())    # discipline-ok(R5): 模块级私有函数，没有公开入口；接线由上面的真窗口用例验证
        self.assertEqual(out, {"main_visible": True, "main_active": False, "in_fullscreen_space": None})

    def test_活动桌面不是记过的普通桌面_就是在全屏桌面(self):
        for space, normal, expected in ((5, 3, True), (3, 3, False), (5, None, None), (None, 3, None)):
            with patch.object(m, "native_cocoa_active", return_value=True), \
                    patch.object(m, "active_space_id", return_value=space):
                out = m._menu_context(self.window(normal=normal))    # discipline-ok(R5): 同上
            self.assertEqual(out["in_fullscreen_space"], expected, (space, normal))

    def test_取现场出错_留一条吞异常记录_不外抛(self):
        win = self.window()
        win.isVisible.side_effect = RuntimeError("x")
        with patch.object(m, "log_swallow") as swallowed:
            out = m._menu_context(win)    # discipline-ok(R5): 同上
        self.assertEqual(out["main_visible"], None)
        self.assertEqual(swallowed.call_args.args[0], "stock_float_window._menu_context")


class FeedbackWiring(WiringCase):
    def test_点反馈问题_请求记下刚才那条菜单会话的编号(self):
        self.open_menu()
        self.win._queue_feedback_open()    # discipline-ok(R5): 菜单项的槽函数就是它，没有别的公开入口
        QApplication.processEvents()
        self.own_dialog()
        self.assertEqual(self.attempts()[-1]["session_id"], mt.diagnostics()["sessions"][-1]["id"])

    def test_上一次还没处理完又点了一次_被丢掉并记数(self):
        self.win._queue_feedback_open()    # discipline-ok(R5): 同上
        self.win._queue_feedback_open()    # discipline-ok(R5): 同上
        QApplication.processEvents()
        self.own_dialog()
        diag = mt.diagnostics()
        self.assertEqual((diag["feedback_dropped"], len(diag["feedback_attempts"])), (1, 1))

    def test_开窗记下各阶段_第二次打开记为复用(self):
        self.win._queue_feedback_open()    # discipline-ok(R5): 同上
        QApplication.processEvents()
        dialog = self.own_dialog()
        first = self.attempts()[-1]
        self.assertEqual((first["outcome"], first["reused"]), ("opened", False))
        self.assertTrue({"queued_ms", "captured_ms", "built_ms", "presented_ms", "raised_ms", "total_ms"}
                        <= set(first["marks"]))
        self.assertIsNotNone(mt.diagnostics()["frozen_at_open"])
        dialog.close()
        self.win._open_feedback()    # discipline-ok(R5): 直接走开窗这一步，验证复用路径
        second = self.attempts()[-1]
        self.assertEqual((second["outcome"], second["reused"], second["session_id"]), ("opened", True, None))

    def test_采集现场失败_记下失败阶段与错误类别(self):
        with patch("sf_feedback_capture.capture_snapshot", side_effect=RuntimeError("x")):
            self.win._queue_feedback_open()    # discipline-ok(R5): 同上
            QApplication.processEvents()
        attempt = self.attempts()[-1]
        self.assertEqual((attempt["outcome"], attempt["error"]), ("capture_failed", "RuntimeError"))
        self.assertNotIn("captured_ms", attempt["marks"])
        self.assertIsNone(mt.diagnostics()["frozen_at_open"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
