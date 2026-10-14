# -*- coding: utf-8 -*-
"""v1.2.47 右上角三按钮图标放大回归测试

需求：悬浮窗右上角三个图标按钮（编辑/最小化/关闭）图标尺寸放大"一点点"。
改动：sf_ui_render.py 中仅调整绘制常量 ——
  BTN_ICON_SZ 10→12（放大 2px，克制幅度；图标资源 20px 仍只需下采样）
  BTN_ICON_X 各 +1（edit 77→78 / min 52→53 / close 27→28）、BTN_ICON_Y 12→11，
  使放大后图标仍以按钮中心（w-BTN_X[name], BTN_CY=17）为几何中心对称扩展。
零回归：按钮热区（BTN_X/BTN_CY/HIT_DX/HIT_DY）、容器布局、间距、展开/收起逻辑均未触碰。

运行方式（在 StockFloat 目录）：
    python3 -m unittest tests.test_v147 -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sf_ui_render import (BTN_CY, BTN_ICON_SZ, BTN_ICON_X, BTN_ICON_Y, BTN_X,
                          W_NAME)
from sf_constants import APP_VERSION

NAMES = ("edit", "min", "close")




class TestIconSize(unittest.TestCase):
    """v1.2.47：图标绘制尺寸放大 2px，仍居中于按钮中心且不越界/不重叠。"""

    def test_icon_size_enlarged(self):
        # 放大后的绘制尺寸（原 10px → 12px）
        self.assertEqual(BTN_ICON_SZ, 12)

    def test_icon_vertical_center_aligned(self):
        # 图标垂直中心 = BTN_ICON_Y + SZ/2，应与按钮热区中心 BTN_CY 对齐
        self.assertEqual(BTN_ICON_Y + BTN_ICON_SZ / 2.0, BTN_CY)

    def test_icon_horizontal_center_aligned(self):
        # 图标水平中心 = w - BTN_ICON_X[name] + SZ/2，应等于按钮中心 w - BTN_X[name]
        for name in NAMES:
            self.assertEqual(
                BTN_ICON_X[name] - BTN_ICON_SZ / 2.0, BTN_X[name],
                "%s 图标水平中心与按钮中心未对齐" % name)

    def test_icons_no_overlap_and_inside_window(self):
        # 三图标按从右往左（close > min > edit）排列，相邻边缘须有间隙（不重叠）
        # 偏移量越小越靠右，故右缘 = offset - SZ 越大越靠右
        right = sorted(((BTN_ICON_X[n] - BTN_ICON_SZ, n) for n in NAMES),
                       reverse=True)
        for (edge, name), (next_edge, next_name) in zip(right, right[1:]):
            self.assertGreater(edge, next_edge,
                               "%s 与 %s 图标水平重叠" % (name, next_name))
        # 最右图标（close）右缘必须位于窗口内（offset - SZ > 0）
        self.assertGreater(min(BTN_ICON_X[n] - BTN_ICON_SZ for n in NAMES), 0)
        # 160px 窄版中名称位于按钮下方，二者垂直不重叠；只需保证最左图标仍在窗口内。
        self.assertLess(BTN_ICON_X["edit"], 160)

    def test_icon_resolution_supports_downsample(self):
        # 图标资源生成尺寸保持 20px（sf_icons.py），放大到 12px 仍由 20px 下采样，不失真
        svg_src = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "sf_icons.py")
        with open(svg_src, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("_ICON_SVG", content)
        # 资源生成尺寸仍为 20（20 资源下采样到 12 绘制）
        import re
        self.assertTrue(re.search(r"svg_pixmap\([^)]*,\s*20\)", content),
                        "sf_icons 图标资源生成尺寸应仍为 20px")


if __name__ == "__main__":
    unittest.main()
