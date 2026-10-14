# -*- coding: utf-8 -*-
"""实时股价 v1.2.74 休市提示左对齐回归测试。

需求：午间休市/休市/已收盘等市场状态提示（顶部栏）**左边缘与股票名称左边缘对齐**
（仅水平左移，垂直位置不变）；宽度取文字自身宽度，不拉伸、不居中。

布局决策已下沉纯函数 sf_ui_render.market_status_rect（可脱离 Qt 单测）：
- v1.2.74 起：无论窗口宽度/名称长短，提示左边缘恒等于 PAD_X（名称左边缘）
- 仅输出水平布局 (rx, rw)，垂直位置由绘制侧 text_cy 决定，本函数不参与

运行方式（在 StockFloat 目录）：
    python3 -m unittest tests.test_v153 -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sf_ui_render import market_status_rect, PAD_X, BTN_X, BTN_ICON_SZ
from sf_constants import APP_VERSION, VERSION_TEXT




class TestMarketStatusRect(unittest.TestCase):
    """market_status_rect 布局决策（纯函数）：v1.2.74 起左边缘对齐股票名称(PAD_X)。"""

    def test_left_edge_aligns_name(self):
        """任意窗口宽度下，提示左边缘恒等于名称左边缘 PAD_X（仅水平左移）"""
        for w in (200, 220, 260, 300):
            rx, rw = market_status_rect(w, lab_w=36, name_w=36)
            self.assertAlmostEqual(rx, PAD_X, places=6,
                                   msg="休市提示左边缘须与股票名称左边缘(PAD_X)对齐")
            self.assertEqual(rw, 36, "宽度取文字自身宽度，保持不变")

    def test_left_edge_independent_of_name_width(self):
        """名称长短不影响左边缘（始终 PAD_X），仅水平位置、垂直由绘制侧决定"""
        for name_w in (0, 36, 90, 120):
            rx, rw = market_status_rect(230, lab_w=30, name_w=name_w)
            self.assertEqual(rx, PAD_X)
            self.assertEqual(rw, 30)

    def test_only_horizontal_output(self):
        """函数只输出水平布局（矩形两分量），垂直位置由绘制侧保持不变 —— 不参与决策"""
        res = market_status_rect(260, lab_w=36, name_w=36)
        self.assertEqual(len(res), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
