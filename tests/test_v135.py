# -*- coding: utf-8 -*-
"""实时股价 v1.2.35 回归：右键菜单精简 + 毛玻璃暗化加深。

覆盖：
- contextMenuEvent 生成的菜单不再含「替换股票」「目标价」「最小化到 Dock」「退出」项
  （底层功能保留，仅 UI 层不可见），保留「立即刷新」「导出诊断包」与版本行；
- 毛玻璃暗化 alpha 加深：暗色系统 >100、浅色系统 >=170（修复用户"太透光"反馈）。

运行方式（在 StockFloat 目录）：
    python3 -m unittest discover -s tests -v
"""
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtWidgets import QApplication

import stock_float_window as m
import sf_quote
from sf_constants import BG_GLASS_DIM, BG_GLASS_DIM_LIGHT


def _fake_http_get(url, **kw):
    if "hq.sinajs.cn" in url:
        return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
    if "web.ifzq.gtimg.cn" in url:
        return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                '"data":["0930 1510.0 100 1000", "1000 1511.0 200 2000"]}}}}')
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


class TestV135(unittest.TestCase):
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

    def test_context_menu_hides_three_items(self):
        """右键菜单移除「替换股票」「最小化到 Dock」「退出」（功能保留，仅 UI 隐藏）"""
        ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(10, 10), QPoint(10, 10))
        self.win.contextMenuEvent(ev)
        menu = getattr(self.win, "_ctx_menu", None)
        self.assertIsNotNone(menu, "menu should have been created")
        texts = [a.text() for a in menu.actions()]
        self.assertNotIn("替换股票", texts)
        self.assertNotIn("最小化到 Dock", texts)
        self.assertNotIn("退出", texts)
        self.assertFalse(any(t.startswith("目标价") for t in texts),
                         "目标价功能保留，但右键菜单入口必须隐藏")
        self.assertIn("立即刷新", texts)
        self.assertIn("导出诊断包", texts)
        self.assertIn("检查更新", texts)
        # v1.3.41：用户明确要求右键菜单不放卸载入口（危险操作不该出现在主菜单）
        self.assertFalse(any(t.startswith("彻底卸载") for t in texts),
                         "右键菜单不得出现卸载入口，got %r" % texts)
        # 版本行仍在（灰色禁用项）。v1.4.53 起去掉「版本」两个字，直接是 `v1.4.53 Beta`，
        # 所以判据改成认"以 v + 数字.数字 开头"这一形状 —— 刻意**不**写死版本号本身
        # （AGENTS §6 第 1 条：版本唯一来源是 sf_constants.APP_VERSION，同源性由发版链守）。
        self.assertTrue(any(re.match(r"^v\d+\.\d+", t) for t in texts),
                        "version item should remain, got %r" % texts)
        self.assertFalse(any(t.startswith("版本") for t in texts),
                         "菜单里不该再有「版本」这两个字：" + repr(texts))

    def test_glass_dim_deepened(self):
        """毛玻璃暗化加深：暗色系统 alpha>100、浅色系统 >=170（此前 80/160 太透光）"""
        self.assertGreater(BG_GLASS_DIM, 100)
        self.assertGreaterEqual(BG_GLASS_DIM_LIGHT, 170)


if __name__ == "__main__":
    unittest.main()
