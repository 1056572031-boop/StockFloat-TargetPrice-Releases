# -*- coding: utf-8 -*-
"""v1.3.41：Cmd+W 缩小窗口到 Dock 程序坞的键盘快捷操作回归测试。

覆盖：
1. 主窗聚焦时按 Cmd+W → 走缩小入口（与顶栏缩小按钮同一路径）；
2. 无修饰符的普通 W 不触发缩小（不误伤输入）。

"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QEvent
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication

import stock_float_window as m


class _WinCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._save_patch = patch.object(m, "save_config", lambda *a, **k: None)
        self._save_patch.start()
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()
        self.win.timer.stop()

    def tearDown(self):
        try:
            self.win._minute_thread.join(0.3)
            self.win._quote_thread.join(0.3)
        except Exception:
            pass
        try:
            self.win.close()
        except Exception:
            pass
        self._save_patch.stop()

    def _press(self, key, modifier):
        ev = QKeyEvent(QEvent.Type.KeyPress, key, modifier)
        self.win.keyPressEvent(ev)


class TestCmdWMinimize(_WinCase):

    def test_cmd_w_triggers_minimize(self):
        """主窗聚焦时 Cmd+W 必须缩小到 Dock（与顶栏缩小按钮同一入口）。"""
        with patch.object(self.win, "_trigger_top_button", return_value=True) as trig:
            self._press(Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
            trig.assert_called_once_with("min")

    def test_plain_w_does_not_minimize(self):
        """没有 Command 修饰符的普通 W 不应触发任何缩小动作。"""
        with patch.object(self.win, "_trigger_top_button") as trig:
            self._press(Qt.Key.Key_W, Qt.KeyboardModifier.NoModifier)
            trig.assert_not_called()

    def test_shift_w_does_not_minimize(self):
        """Shift+W 等其它修饰组合不触发缩小，避免误伤。"""
        with patch.object(self.win, "_trigger_top_button") as trig:
            self._press(Qt.Key.Key_W, Qt.KeyboardModifier.ShiftModifier)
            trig.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
