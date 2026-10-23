# -*- coding: utf-8 -*-
"""sf_menu_trace 的判据：会话记录、右键对账、应用前台追踪、原生回看、反馈开窗、导出。

主窗接线见 test_menu_trace_wiring.py。"""
from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtGui import QAction
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

import sf_menu_trace as mt
from tests._base import SFCase

_QAPP = None


class TraceBase(SFCase):
    def setUp(self):
        global _QAPP
        super().setUp()
        _QAPP = SFCase.ensure_qapp()
        mt.clear()
        self.log = Mock()
        self.track_patch(patch.object(mt, "log_line", self.log))
        self.swallowed = Mock()
        self.track_patch(patch.object(mt, "log_swallow", self.swallowed))
        self.menu = self.track_widget(QMenu())
        self.fb = self.tag("反馈问题", "feedback")
        self.diag = self.tag("导出诊断包", "diagnostics")
        self.menu.addSeparator()
        self.untagged = self.menu.addAction("没打标的")

    def tag(self, text, key):
        action = QAction(text, self.menu)
        action.setData(key)
        self.menu.addAction(action)
        return action

    def pump(self, ms=0):
        self.wait_for(lambda: (QApplication.processEvents(), False)[1], timeout=ms / 1000.0 + 0.02, tick=0.005)

    def centre(self, action):
        return self.menu.actionGeometry(action).center()

    def lines(self):
        return [c.args[0] for c in self.log.call_args_list]

    def last_session(self):
        return mt.diagnostics()["sessions"][-1]


class Keys(TraceBase):
    def test_固定名字_分隔线_没打标_没有项(self):
        self.assertEqual(mt.key_of(self.fb), "feedback")
        self.assertEqual(mt.key_of(self.menu.actions()[2]), "separator")
        self.assertEqual(mt.key_of(self.untagged), "untagged")
        self.assertEqual(mt.key_of(None), "none")

    def test_按菜单本地坐标判断落在哪一项_菜单外记_outside(self):
        self.menu.show()
        self.assertEqual(mt.action_key_at(self.menu, self.centre(self.fb)), "feedback")
        self.assertEqual(mt.action_key_at(self.menu, QPoint(-5, -5)), "outside")


class Sessions(TraceBase):
    def test_开会话记下前台状态_编号递增_环有上限(self):
        ids = [mt.begin_menu(QMenu()) for _ in range(mt.RING_SIZE + 3)]
        self.assertEqual(ids, list(range(1, mt.RING_SIZE + 4)))
        diag = mt.diagnostics()
        self.assertEqual(len(diag["sessions"]), mt.RING_SIZE)
        self.assertEqual(diag["menus_opened"], mt.RING_SIZE + 3)
        self.assertIn(diag["sessions"][-1]["app_state"], {"ApplicationActive", "ApplicationInactive",
                                                          "ApplicationHidden", "ApplicationSuspended"})

    def test_右键按下与菜单打开配对_没等到菜单的按下留在待配对里(self):
        mt.note_right_press()
        mt.begin_menu(QMenu())
        self.assertTrue(self.last_session()["after_right_press"])
        self.assertFalse(mt.diagnostics()["right_press_pending_now"])
        mt.begin_menu(QMenu())
        self.assertFalse(self.last_session()["after_right_press"], "没有新的右键按下，不该配成一对")
        mt.note_right_press()
        mt.note_right_press()
        diag = mt.diagnostics()
        self.assertEqual((diag["right_presses"], diag["menus_opened"]), (3, 2))
        self.assertTrue(diag["right_press_pending_now"])

    def test_此刻开着的会话编号_收起结算后没有(self):
        self.assertIsNone(mt.open_session_id())
        menu = QMenu()
        sid = mt.begin_menu(menu)
        self.assertEqual(mt.open_session_id(), sid)
        menu.aboutToHide.emit()
        self.pump()
        self.assertIsNone(mt.open_session_id())

    def test_宿主现场函数的返回值与出错都进记录_出错不外抛(self):
        mt.begin_menu(QMenu(), context=lambda: {"in_fullscreen_space": True})
        self.assertEqual(self.last_session()["context"], {"in_fullscreen_space": True})
        boom = Mock(side_effect=RuntimeError("x"))
        mt.begin_menu(QMenu(), context=boom)
        self.assertEqual(self.last_session()["context"], {"error": "RuntimeError"})
        self.assertEqual(self.swallowed.call_args.args[0], "sf_menu_trace.context")

    def test_建会话本身出错_返回None_不外抛(self):
        broken = Mock()
        broken.hovered.connect.side_effect = RuntimeError("boom")
        self.assertIsNone(mt.begin_menu(broken))
        self.assertEqual(self.swallowed.call_args.args[0], "sf_menu_trace.begin_menu")


class RealInteractions(TraceBase):
    def open(self):
        mt.begin_menu(self.menu)
        self.menu.popup(QPoint(40, 40))
        self.pump()

    def finish(self):
        self.pump()
        return self.last_session()

    def test_点中某项_记名字_按下松开都落在它身上_原因是选中(self):
        self.open()
        QTest.mouseClick(self.menu, Qt.MouseButton.LeftButton, pos=self.centre(self.fb))
        s = self.finish()
        self.assertEqual((s["picked"], s["first_press"], s["last_release"], s["reason"]),
                         ("feedback", "feedback", "feedback", "picked"))
        self.assertEqual((s["presses"], s["releases"]), (1, 1))
        self.assertIsNotNone(s["closed_after_ms"])
        self.assertTrue([x for x in self.lines() if x.startswith("ctx menu closed")
                         and "picked=feedback" in x and "reason=picked" in x])

    def test_按下在一项上_松开在另一项上_两个名字都留下(self):
        self.open()
        QTest.mousePress(self.menu, Qt.MouseButton.LeftButton, pos=self.centre(self.fb))
        QTest.mouseRelease(self.menu, Qt.MouseButton.LeftButton, pos=self.centre(self.diag))
        s = self.finish()
        self.assertEqual((s["first_press"], s["last_release"]), ("feedback", "diagnostics"))

    def test_Esc_收起_原因是_escape(self):
        self.open()
        QTest.keyClick(self.menu, Qt.Key.Key_Escape)
        s = self.finish()
        self.assertEqual((s["reason"], s["picked"]), ("escape", "none"))
        self.assertGreaterEqual(s["keys"], 1)

    def test_点菜单外面_原因是_outside_click(self):
        self.open()
        QTest.mouseClick(self.menu, Qt.MouseButton.LeftButton, pos=QPoint(-20, -20))
        s = self.finish()
        self.assertEqual((s["first_press"], s["reason"], s["picked"]), ("outside", "outside_click", "none"))

    def test_程序前台状态真的变成非活动_原因是_app_deactivated(self):
        app = Mock()
        app.applicationState.return_value = Qt.ApplicationState.ApplicationActive
        mt.install_app_tracker(app)
        handler = app.applicationStateChanged.connect.call_args.args[0]
        self.open()
        handler(Qt.ApplicationState.ApplicationInactive)
        self.menu.hide()
        self.assertEqual(self.finish()["reason"], "app_deactivated")

    def test_菜单窗自己的失去激活不算失焦_点菜单外仍判点外面(self):
        self.open()
        QApplication.sendEvent(self.menu, QEvent(QEvent.Type.WindowDeactivate))
        QTest.mouseClick(self.menu, Qt.MouseButton.LeftButton, pos=QPoint(-20, -20))
        self.assertEqual(self.finish()["reason"], "outside_click")

    def test_只是切到前台不算失焦_原因仍是_unknown(self):
        app = Mock()
        app.applicationState.return_value = Qt.ApplicationState.ApplicationActive
        mt.install_app_tracker(app)
        handler = app.applicationStateChanged.connect.call_args.args[0]
        self.open()
        handler(Qt.ApplicationState.ApplicationActive)      # 离屏第一次出菜单时真的会收到这一条
        self.menu.hide()
        s = self.finish()
        self.assertEqual(s["app_changes"][0]["to"], "ApplicationActive")
        self.assertEqual(s["reason"], "unknown")

    def test_没有任何线索就收起_原因是_unknown(self):
        self.open()
        self.menu.hide()
        self.assertEqual(self.finish()["reason"], "unknown")

    def test_悬停记最后一项的名字(self):
        self.open()
        self.menu.hovered.emit(self.diag)
        self.assertEqual(self.last_session()["hover"], "diagnostics")
        self.menu.hide()

    def test_上一个菜单没收就开新的_上一个记为被替换(self):
        mt.begin_menu(QMenu())
        mt.begin_menu(QMenu())
        first, second = mt.diagnostics()["sessions"]
        self.assertEqual((first["reason"], second["reason"]), ("replaced", None))
        self.assertIsNotNone(first["closed_after_ms"])

    def test_重复收起只结算一次(self):
        mt.begin_menu(self.menu)
        self.menu.aboutToHide.emit()
        self.menu.aboutToHide.emit()
        self.pump()
        self.assertEqual(len([x for x in self.lines() if x.startswith("ctx menu closed")]), 1)

    def test_第一轮事件循环耗时被记下(self):
        mt.begin_menu(self.menu)
        self.assertIsNone(self.last_session()["first_turn_ms"])
        self.pump()
        self.assertIsNotNone(self.last_session()["first_turn_ms"])


class AppTracker(TraceBase):
    def fake_app(self, state):
        app = Mock()
        app.applicationState.return_value = state
        return app

    def test_同一个应用只接一次_空应用不接(self):
        app = self.fake_app(Qt.ApplicationState.ApplicationInactive)
        self.assertTrue(mt.install_app_tracker(app))
        self.assertFalse(mt.install_app_tracker(app))
        self.assertFalse(mt.install_app_tracker(None))
        app.applicationStateChanged.connect.assert_called_once()

    def test_装的时候已在前台_记下不晚于此刻_否则没有(self):
        mt.install_app_tracker(self.fake_app(Qt.ApplicationState.ApplicationActive))
        self.assertIsNotNone(mt.diagnostics()["app_active_age_s"])
        mt.clear()
        mt.install_app_tracker(self.fake_app(Qt.ApplicationState.ApplicationInactive))
        self.assertIsNone(mt.diagnostics()["app_active_age_s"])

    def test_状态变化记时刻_菜单开着时还记进会话_最多八条(self):
        app = self.fake_app(Qt.ApplicationState.ApplicationActive)
        mt.install_app_tracker(app)
        handler = app.applicationStateChanged.connect.call_args.args[0]
        handler(Qt.ApplicationState.ApplicationInactive)
        diag = mt.diagnostics()
        self.assertIsNotNone(diag["app_inactive_age_s"])
        mt.begin_menu(QMenu())
        for _ in range(10):
            handler(Qt.ApplicationState.ApplicationActive)
        self.assertEqual(len(self.last_session()["app_changes"]), 8)
        self.assertEqual(self.last_session()["app_changes"][0]["to"], "ApplicationActive")


class NativeReadback(TraceBase):
    def widget(self, visible=True):
        w = Mock()
        w.isVisible.return_value = visible
        w.isActiveWindow.return_value = True
        return w

    def fake_window(self, occlusion=2):
        nsw = Mock()
        nsw.isOnActiveSpace.return_value = True
        nsw.level.return_value = 1000
        nsw.collectionBehavior.return_value = 273
        nsw.occlusionState.return_value = occlusion
        return nsw

    def test_不是_cocoa_不碰原生(self):
        with patch.object(mt, "native_cocoa_active", return_value=False):
            out = mt.native_readback(self.widget())
        self.assertEqual(out, {"visible": True, "active": True, "native": "unavailable:not_cocoa"})

    def test_没显示不碰原生(self):
        with patch.object(mt, "native_cocoa_active", return_value=True):
            self.assertEqual(mt.native_readback(self.widget(False))["native"], "unavailable:not_visible")

    def test_取不到原生窗口(self):
        with patch.object(mt, "native_cocoa_active", return_value=True), \
                patch.object(mt, "content_ns_window", return_value=None):
            self.assertEqual(mt.native_readback(self.widget())["native"], "unavailable:no_nswindow")

    def test_原生字段读回_屏幕有无与遮挡位(self):
        for screen, occlusion in ((object(), 2), (None, 0)):
            with patch.object(mt, "native_cocoa_active", return_value=True), \
                    patch.object(mt, "content_ns_window", return_value=self.fake_window(occlusion)), \
                    patch.object(mt, "QGuiApplication") as gui:
                gui.screenAt.return_value = screen
                out = mt.native_readback(self.widget())
            self.assertEqual((out["native"], out["on_active_space"], out["level"], out["behavior"]),
                             ("ok", True, 1000, 273))
            self.assertEqual((out["on_a_screen"], out["occlusion_visible"]), (screen is not None, occlusion == 2))

    def test_读原生出错_留名字_不外抛(self):
        with patch.object(mt, "native_cocoa_active", return_value=True), \
                patch.object(mt, "content_ns_window", side_effect=RuntimeError("x")):
            out = mt.native_readback(self.widget())
        self.assertEqual(out["native"], "error:RuntimeError")
        self.assertEqual(self.swallowed.call_args.args[0], "sf_menu_trace.native_readback")

    def test_按时刻回看_窗口没了记_gone_读回出错留痕(self):
        with patch.object(mt, "READBACK_FIRST_MS", 0):
            gone = QMenu()
            mt.begin_menu(gone)
            sip_delete = __import__("PyQt6.sip", fromlist=["delete"]).delete
            sip_delete(gone)
            self.pump(30)
            self.assertEqual(self.last_session()["readbacks"]["t300"], {"gone": True})
            alive = self.track_widget(QMenu())
            mt.begin_menu(alive)
            self.pump(30)
            self.assertIn("visible", self.last_session()["readbacks"]["t300"])
            with patch.object(mt, "native_readback", side_effect=RuntimeError("x")):
                mt.begin_menu(alive)
                self.pump(30)
            self.assertEqual(self.swallowed.call_args.args[0], "sf_menu_trace.readback")


class FeedbackFlow(TraceBase):
    def dialog(self):
        return self.track_widget(QWidget())

    def run_attempt(self, outcome="opened", dialog=None, reused=False, session_id=None, error=None):
        attempt = mt.feedback_request(session_id)
        mt.feedback_begin()
        mt.feedback_mark(attempt, "captured_ms")
        mt.feedback_finish(attempt, outcome, dialog, reused, error)
        return attempt

    def test_重复请求被丢掉_只记数不新建(self):
        self.assertIsNone(mt.feedback_request(dropped=True))
        diag = mt.diagnostics()
        self.assertEqual((diag["feedback_dropped"], diag["feedback_attempts"]), (1, []))

    def test_一次完整开窗_带菜单会话编号_各阶段毫秒_总耗时(self):
        self.run_attempt(dialog=self.dialog(), session_id=7)
        a = mt.diagnostics()["feedback_attempts"][-1]
        self.assertEqual((a["session_id"], a["outcome"], a["reused"]), (7, "opened", False))
        self.assertTrue({"queued_ms", "captured_ms", "total_ms"} <= set(a["marks"]))

    def test_没有排队记录的直接调用也补一条(self):
        attempt = mt.feedback_begin()
        self.assertIsNone(attempt["session_id"])
        self.assertEqual(len(mt.diagnostics()["feedback_attempts"]), 1)

    def test_新建的窗冻结当时的菜单记录_复用旧窗不覆盖冻结(self):
        mt.note_right_press()
        mt.begin_menu(QMenu())
        first = self.run_attempt(dialog=self.dialog())
        frozen = mt.diagnostics()["frozen_at_open"]
        self.assertEqual((frozen["at_attempt"], frozen["right_presses"], frozen["menus_opened"]),
                         (first["id"], 1, 1))
        self.assertEqual(len(frozen["sessions"]), 1)
        mt.begin_menu(QMenu())
        self.run_attempt(dialog=self.dialog(), reused=True)
        again = mt.diagnostics()["frozen_at_open"]
        self.assertEqual((again["at_attempt"], len(again["sessions"])), (first["id"], 1))

    def test_采集失败_记原因_不冻结也不回看(self):
        self.run_attempt("capture_failed", error="RuntimeError")
        diag = mt.diagnostics()
        a = diag["feedback_attempts"][-1]
        self.assertEqual((a["outcome"], a["error"], a["readbacks"]), ("capture_failed", "RuntimeError", {}))
        self.assertIsNone(diag["frozen_at_open"])

    def test_开窗后按两个时刻回看(self):
        with patch.object(mt, "READBACK_FIRST_MS", 0), patch.object(mt, "READBACK_SECOND_MS", 10):
            self.run_attempt(dialog=self.dialog())
            self.pump(60)
        self.assertEqual(set(mt.diagnostics()["feedback_attempts"][-1]["readbacks"]), {"t300", "t1500"})

    def test_尝试环有上限(self):
        for _ in range(mt.RING_SIZE + 2):
            self.run_attempt("capture_failed")
        self.assertEqual(len(mt.diagnostics()["feedback_attempts"]), mt.RING_SIZE)


class Export(TraceBase):
    def test_导出可序列化_不带内部键_改返回值不影响内部记录(self):
        mt.begin_menu(self.menu)
        diag = mt.diagnostics()
        json.dumps(diag, allow_nan=False)
        self.assertEqual((diag["schema"], diag["available"]), ("menu_trace.v1", True))
        self.assertFalse([k for k in diag["sessions"][0] if k.startswith("_")])
        diag["sessions"][0]["picked"] = "tampered"
        diag["sessions"][0]["app_changes"].append({"to": "tampered"})       # 嵌套的也不能连到内部记录
        diag["sessions"][0]["readbacks"]["t300"] = {"tampered": True}
        again = mt.diagnostics()["sessions"][0]
        self.assertEqual((again["picked"], again["app_changes"], again["readbacks"]), ("none", [], {}))

    def test_回看时刻的来源常量(self):
        from sf_glass import POPUP_SPACE_MAX_TICKS, POPUP_SPACE_TICK_MS
        self.assertEqual(mt.READBACK_SECOND_MS, POPUP_SPACE_MAX_TICKS * POPUP_SPACE_TICK_MS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
