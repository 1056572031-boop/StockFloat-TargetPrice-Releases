# -*- coding: utf-8 -*-
"""v1.4.8 原生弹窗（NSAlert）桥接回归。

背景：v1.4.6 的 osascript display dialog 仍是"对话框"版式（标题栏 + 按钮横排
右下），用户反馈没有和 macOS 原生弹窗统一。v1.4.8 首选 NSAlert：无标题栏、
粗体主文案 + 副文案、按钮竖排占满底部、默认按钮蓝色。本文件用桩替换 AppKit，
断言构造参数与收口行为 —— 测试环境绝不弹真窗口。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_system_alert


class _Button:
    def __init__(self, title):
        self._title = title
        self.target = None
        self.action = None
        self.key = None

    def title(self):
        return self._title

    def setTarget_(self, target):
        self.target = target

    def setAction_(self, action):
        self.action = action

    def setKeyEquivalent_(self, key):
        self.key = key

    def setFocusRingType_(self, value):
        self.focus_ring = value


class _Window:
    def __init__(self):
        self.calls = []
        self.behavior = 0
        self.level = 0
        self.hides = True

    def collectionBehavior(self):
        return self.behavior

    def setCollectionBehavior_(self, value):
        self.behavior = value

    def setLevel_(self, value):
        self.level = value

    def setHidesOnDeactivate_(self, value):
        self.hides = value

    def isOnActiveSpace(self):
        return True

    def setReleasedWhenClosed_(self, value):
        self.calls.append(("released_when_closed", value))

    def makeKeyAndOrderFront_(self, _):
        self.calls.append(("front",))

    def orderOut_(self, _):
        self.calls.append(("order_out",))


class _Alert:
    instances = []

    @classmethod
    def alloc(cls):
        return cls()

    def __init__(self):
        self.message = ""
        self.info = ""
        self.icon = None
        self.buttons_list = []
        self.window_obj = _Window()
        self.laid_out = False
        _Alert.instances.append(self)

    def init(self):
        return self

    def setMessageText_(self, value):
        self.message = value

    def setInformativeText_(self, value):
        self.info = value

    def setIcon_(self, value):
        self.icon = value

    def addButtonWithTitle_(self, title):
        button = _Button(title)
        self.buttons_list.append(button)
        return button

    def buttons(self):
        return self.buttons_list

    def layout(self):
        self.laid_out = True

    def window(self):
        return self.window_obj


class _Image:
    @classmethod
    def alloc(cls):
        return cls()

    def initWithContentsOfFile_(self, path):
        self.path = path
        return self


class _NSApp:
    activations = []

    @staticmethod
    def activateIgnoringOtherApps_(value):
        _NSApp.activations.append(value)


class _FakeAppKit:
    NSWindowCollectionBehaviorFullScreenAuxiliary = 1 << 8
    NSAlert = _Alert
    NSImage = _Image
    NSApp = _NSApp
    NSFocusRingTypeNone = 1      # 与真 AppKit 同值：按钮不要焦点描边


class _FakeTarget:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def setOwner_(self, owner):
        self.owner = owner


class _NativeBase(unittest.TestCase):
    def setUp(self):
        _Alert.instances = []
        _NSApp.activations = []
        patchers = [
            patch.object(sf_system_alert, "AppKit", _FakeAppKit),
            patch.object(sf_system_alert, "_AlertTarget", _FakeTarget),
            patch.object(sf_system_alert, "native_available", lambda: True),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        sf_system_alert.NativeAlert._live.clear()


class TestNativeConstruction(_NativeBase):
    def test_show_alert_prefers_native_and_lays_out_contract(self):
        with tempfile.NamedTemporaryFile(suffix=".icns") as handle:
            dialog = sf_system_alert.show_alert(
                "软件更新", "合并文案兜底",
                message="发现新版本 v9.9.9。", info="更新说明",
                buttons=("暂不更新", "立即更新"), default="立即更新",
                cancel="暂不更新", icon_path=handle.name,
                on_result=lambda _b: None)
        self.assertIsInstance(dialog, sf_system_alert.NativeAlert)
        alert = _Alert.instances[0]
        self.assertEqual(alert.message, "发现新版本 v9.9.9。")
        self.assertEqual(alert.info, "更新说明")
        self.assertIsNotNone(alert.icon)
        self.assertTrue(alert.laid_out, "非模态展示必须先 layout 再取窗口")
        titles = [b.title() for b in alert.buttons_list]
        self.assertEqual(titles, ["立即更新", "暂不更新"],
                         "NSAlert 默认按钮必须第一个添加（蓝色/最上），取消最后")
        self.assertEqual(alert.buttons_list[-1].key, "\x1b", "Esc 绑定取消按钮")
        for button in alert.buttons_list:
            self.assertIsInstance(button.target, _FakeTarget)
            self.assertEqual(button.action, "buttonClicked:")
        self.assertIn(("front",), alert.window_obj.calls)
        self.assertEqual(_NSApp.activations, [True])

    def test_missing_icon_file_skips_setIcon(self):
        sf_system_alert.show_alert("t", "x", icon_path="/no/such.icns")
        self.assertIsNone(_Alert.instances[0].icon)


class TestNativeLifecycle(_NativeBase):
    def _make(self, on_result=None):
        return sf_system_alert.show_alert(
            "t", "x", buttons=("暂不更新", "好"), default="好",
            cancel="暂不更新", on_result=on_result)

    def test_click_reports_button_and_closes_window(self):
        results = []
        dialog = self._make(results.append)
        alert = _Alert.instances[0]
        alert.buttons_list[0].target.owner._handle_click("好")
        self.assertEqual(results, ["好"])
        self.assertIn(("order_out",), alert.window_obj.calls)
        self.assertEqual(sf_system_alert.NativeAlert._live, {},
                         "收口后必须撤销防 GC 引用")

    def test_close_suppresses_late_click(self):
        results = []
        dialog = self._make(results.append)
        dialog.close()
        _Alert.instances[0].buttons_list[0].target.owner._handle_click("好")
        self.assertEqual(results, [], "程序收口后不得再冒充用户选择")
        dialog.close()   # 幂等

    def test_click_survives_callback_exception(self):
        def boom(_b):
            raise RuntimeError("callback down")
        dialog = self._make(boom)
        _Alert.instances[0].buttons_list[1].target.owner._handle_click("暂不更新")
        self.assertEqual(sf_system_alert.NativeAlert._live, {})


class TestFallback(_NativeBase):
    def test_native_failure_falls_back_to_osascript(self):
        popened = []

        class _Proc:
            def communicate(self):
                return ("", "")
            def poll(self):
                return 0
            def terminate(self):
                pass

        def boom(*args, **kwargs):
            raise RuntimeError("appkit down")

        with patch.object(sf_system_alert, "NativeAlert", boom), \
             patch.object(sf_system_alert.subprocess, "Popen",
                          lambda *a, **k: popened.append(a) or _Proc()):
            dialog = sf_system_alert.show_alert("软件更新", "文本",
                                                buttons=("好",))
        self.assertIsInstance(dialog, sf_system_alert.SystemDialog)
        # 等降级线程结束（Popen 立即返回）
        dialog._thread.join(timeout=5.0)
        self.assertEqual(len(popened), 1, "原生失败必须落回 osascript")


class TestNativeAvailable(unittest.TestCase):
    def test_requires_appkit_and_main_thread(self):
        # 独立于 _NativeBase：那里把 native_available 固定 patch 掉了。
        with patch.object(sf_system_alert, "AppKit", None):
            self.assertFalse(sf_system_alert.native_available())
        with patch.object(sf_system_alert, "AppKit", _FakeAppKit):
            self.assertTrue(sf_system_alert.native_available())


if __name__ == "__main__":
    unittest.main()
