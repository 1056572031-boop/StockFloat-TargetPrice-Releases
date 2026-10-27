# -*- coding: utf-8 -*-
"""实时股价 v1.2.0 核心功能一键回归冒烟测试（17.0 新增）。

覆盖：QApplication + FloatWindow 创建 → 注入合法行情 → 渲染一轮（show/update/grab）
全程无异常即通过。用于 `python3 -m unittest discover -s tests` 一条命令
快速回归"主程序能不能正常启动并渲染"，防止加新功能把主流程搞挂。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

import stock_float_window as m
import sf_quote


def _fake_quote():
    """合法行情（含 v1.2.0 的 limit_up/limit_down 字段，三态判定所需字段齐全）。"""
    return {
        "name": "贵州茅台",
        "code": "600519",
        "price": "410.00",
        "pct": "1.61",
        "change": "6.50",
        "pre_close": "403.50",
        "high": "412.00",
        "low": "400.00",
        "volume": "12345",
        "limit_up": "443.85",
        "limit_down": "363.15",
    }


class TestSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "load_api_urls", lambda: None),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", lambda *a, **k: ""),
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
        self.win._anim_timer.stop()
        self.win._flash_timer.stop()

    def tearDown(self):
        self.win.close()
        QApplication.processEvents()
        for p in self._patches:
            p.stop()

    def test_create_render_roundtrip(self):
        """创建 → 注入行情 → 渲染一轮，全程无异常即通过"""
        self.win.show()
        QApplication.processEvents()
        self.win._apply_quote(_fake_quote())
        self.win._need_redraw = True
        self.win.update()
        QApplication.processEvents()
        pm = self.win.grab()   # 真实抓帧：paintEvent 完整执行，无异常
        QApplication.processEvents()
        self.assertIsNotNone(pm)
        self.assertFalse(pm.isNull())
        self.assertIsNotNone(self.win.quote)
        self.assertEqual(self.win.quote["code"], "600519")


if __name__ == "__main__":
    unittest.main(verbosity=2)
