# -*- coding: utf-8 -*-
"""实时股价 v1.2.37 回归：收起态系统级高度锁死。

背景：v1.2.35/36 仅在「启动即为收起态」时把系统级最大高度锁到收起高度；
但运行中「展开→收起」的 _toggle_chart 收起分支只 setMinimumHeight(target)，
max 停留在 MAX_H，macOS 系统级垂直缩放把手（绕过 Qt 事件门控）仍可从下沿把
收起态拉高（真机 145→205 复现）。本版本收起分支与动画完成帧统一收口。

覆盖：
- 启动即收起态：min == max == _height_collapsed()（恢复既有 v1.2.36 行为）；
- 展开后收起（_toggle_chart）：min == max == _height_collapsed()，系统缩放把手锁死；
- 再展开：max 恢复 MAX_H、min 恢复 MIN_H（拉伸能力不丢失）。
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

import stock_float_window as m
import sf_constants
import sf_quote


def _fake_http_get(url, **kw):
    if "hq.sinajs.cn" in url:
        return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
    if "/appstock/app/minute/query?" in url:
        return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                '"data":["0930 1510.0 100 1000", "1000 1511.0 200 2000"]}}}}')
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


class TestV137(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        # 预置为收起态，验证启动即收起时的锁高（v1.2.36 已有行为回归）
        QSettings(sf_constants.BUNDLE_ID, "StockFloat").setValue("chartExpanded", False)
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 145, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
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

    def tearDown(self):
        self.win._minute_thread.join(0.3)
        self.win._quote_thread.join(0.3)
        for p in self._patches:
            p.stop()
        QSettings(sf_constants.BUNDLE_ID, "StockFloat").remove("chartExpanded")

    def _drive_layout_done(self):
        """驱动 180ms 布局动画到完成帧（复用真实定时器路径，非手推）"""
        deadline = time.monotonic() + 1.0
        while getattr(self.win, "_layout_anim_to", 0) != 0 and deadline > time.monotonic():
            QApplication.processEvents()
            time.sleep(0.01)
        QApplication.processEvents()

    def test_init_always_expands_even_if_last_run_collapsed(self):
        """即使上次保存为收起，本次启动也必须默认展开分时图。"""
        w = self.win
        self.assertTrue(w._chart_expanded)
        collapsed = w._height_collapsed()
        self.assertGreaterEqual(w.height(), collapsed + m.CHART_MIN_H)
        self.assertEqual(w.minimumHeight(), m.MIN_H)
        self.assertEqual(w.maximumHeight(), m.MAX_H)

    def test_toggle_collapse_relocks_max(self):
        """展开→收起后：min == max == 收起高度（v1.2.37 核心修复）"""
        w = self.win
        collapsed = w._height_collapsed()
        self.assertTrue(w._chart_expanded)
        self.assertEqual(w.maximumHeight(), m.MAX_H, "展开态系统级 max 应为 MAX_H")
        self.assertEqual(w.minimumHeight(), m.MIN_H)
        # 手动收起 —— 收起后 max 必须锁住，运行中交互仍保留
        w._toggle_chart()
        self._drive_layout_done()
        QApplication.processEvents()
        self.assertFalse(w._chart_expanded)
        self.assertEqual(w.minimumHeight(), collapsed)
        self.assertEqual(w.maximumHeight(), collapsed,
                         "收起后系统级 max 必须重新锁回收起高度，防止下沿把手复活")
        self.assertEqual(w.height(), collapsed)

    def test_toggle_expand_restores_stretch(self):
        """收起后再展开：min==MIN_H / max==MAX_H，拉伸能力恢复"""
        w = self.win
        # 启动默认展开 → 手动收起 → 再展开
        self.assertTrue(w._chart_expanded)
        w._toggle_chart(); self._drive_layout_done()
        QApplication.processEvents()
        self.assertFalse(w._chart_expanded)
        w._toggle_chart(); self._drive_layout_done()
        QApplication.processEvents()
        self.assertTrue(w._chart_expanded)
        self.assertEqual(w.maximumHeight(), m.MAX_H)
        self.assertEqual(w.minimumHeight(), m.MIN_H)


if __name__ == "__main__":
    unittest.main()
