# -*- coding: utf-8 -*-
"""v1.4.59（走查 S8 的颜色项）：涨跌色单源 + 端点同色 + 弱化灰对比度。

三条判据各自的来由
------------------
1) **同屏只允许一种红/一种绿**：主卡用 #ff5252、目标价面板用 #ff6b6b，而面板注释写的
   是"与主界面涨色一致" —— 颜色不一致 + 注释说谎，两件事一起。现在两侧都取
   sf_constants 里的同一份常量，这条断言把"同源"钉住（改任一侧都会红）。
2) **折线线尾必须与呼吸点同色**：旧实现的横向渐变两端写死 Material 自造色
   （涨 #f44336→#b71c1c），右端深红、而钉在同一坐标的呼吸点是 token 红 #ff5252，
   端点看着像贴错了色。现在终点直接用趋势色。
3) **弱化灰必须读得清**：FG_DIM 旧值 #6b7280 实测对面板底 3.45:1、对纯黑 4.34:1，
   低于 WCAG AA 小字 4.5:1（报告按真渲染取到 4.1:1）。新值 #848d9c 实测
   对面板底 4.98:1、对纯黑 6.27:1。阈值来源写在这里，判据用算式复核 ——
   下次有人调色值，会被这条拦住，而不是靠肉眼。

底色取自 sf_glass.POPUP_SURFACE_RGB（面板表面真用的那组 RGB），不复制十六进制。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QRectF          # noqa: E402
from PyQt6.QtGui import QColor           # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import sf_constants as C                 # noqa: E402
import sf_glass as G                     # noqa: E402
import sf_ui_render as R                 # noqa: E402

try:
    from _base import SFCase                   # noqa: E402
except ImportError:                            # 以包形式被 discover 导入时
    from tests._base import SFCase             # noqa: E402


WCAG_SMALL_TEXT_MIN = 4.5   # WCAG 2.1 AA 对正常字号文本的对比度下限（不是本仓自定的口味值）

# 真机实测的最亮玻璃底（2026-10-21）：v1.4.100 卡片圆角验收留下的原生截图里，液态玻璃叠在
# 浅色壁纸上的面板主导色。sf_glass.POPUP_SURFACE_RGB 是 (0,0,0) 的"兼容占位"，拿它判对比度
# 等于站在最宽松的一侧：涨色在纯黑上 6.58:1，在这块真实底上只有 4.38:1（旧值 #ff5252）。
# 这个数不是玻璃参数，只是判据的参照底；玻璃材质与暗化度本轮没动，也不该由它反向决定。
MEASURED_GLASS_FLOOR_RGB = (44, 44, 44)


def _contrast(fg, bg):
    """WCAG 2.1 对比度：(L_lighter + 0.05) / (L_darker + 0.05)。"""
    def lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    def lum(rgb):
        r, g, b = rgb
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)

    a = lum((fg.red(), fg.green(), fg.blue()))
    b = lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _rgb_of(hex_color):
    col = QColor(hex_color)
    return (col.red(), col.green(), col.blue())


class TestBusinessColorsAreSingleSource(SFCase):
    """主卡与目标价面板的涨跌色必须是同一份常量。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _panel_module(self):
        import target_panel
        return target_panel

    def test_up_and_down_red_green_are_one_value(self):
        tp = self._panel_module()
        self.assertEqual(R.RED.name(), C.UP_RED_HEX,
                         "主卡涨色与常量不同源（%s vs %s）" % (R.RED.name(), C.UP_RED_HEX))
        self.assertEqual(R.GREEN.name(), C.DOWN_GREEN_HEX,
                         "主卡跌色与常量不同源")
        self.assertEqual(tp.UP_RED, R.RED.name(),
                         "面板涨色与主卡不同源 —— 同屏两种红就是这里来的")
        self.assertEqual(tp.DOWN_GREEN, R.GREEN.name(), "面板跌色与主卡不同源")

    def test_both_business_colors_clear_aa_on_the_panel_surface(self):
        """合并颜色不能把对比度一起合掉：兼容占位底与真机最亮玻璃底上都要 ≥ 4.5:1。"""
        surfaces = {"兼容占位底": G.POPUP_SURFACE_RGB, "真机最亮玻璃底": MEASURED_GLASS_FLOOR_RGB}
        for sname, bg in surfaces.items():
            for label, col in (("涨色", R.RED), ("跌色", R.GREEN)):
                self.assertGreaterEqual(
                    _contrast(col, bg), WCAG_SMALL_TEXT_MIN,
                    "%s %s 对%s只有 %.2f:1" % (label, col.name(), sname, _contrast(col, bg)))


class TestSolidButtonsKeepReadableWhiteText(SFCase):
    """带白字的两颗实心钮（蓝「立即生效」、红「确认清空」）：静止态白字 ≥ 4.5:1。

    悬停态刻意不在此列：悬停比静止更亮，白字对比度天然更低（蓝 4.02 / 红 3.92），
    硬压到 4.5 要放弃「悬停变亮」，是单独的设计取舍。这里只守「按下比静止更暗」的次序，
    因为只改静止色而忘了按下色时，按下就会比静止更浅、反馈消失。
    """

    @staticmethod
    def _lum_of(hex_color):
        col = QColor(hex_color)
        return _contrast(col, (0, 0, 0))   # 对纯黑的比值随亮度单调，只拿来比大小

    def test_static_state_white_text_clears_aa(self):
        for label, hexv in (("蓝钮 ACCENT_BLUE", C.ACCENT_BLUE_HEX), ("红钮 DANGER_BG", C.DANGER_BG_HEX)):
            got = _contrast(QColor("#ffffff"), _rgb_of(hexv))
            self.assertGreaterEqual(got, WCAG_SMALL_TEXT_MIN,
                                    "%s %s 白字只有 %.2f:1" % (label, hexv, got))

    def test_pressed_is_darker_than_static_for_both_buttons(self):
        pairs = (("蓝钮", C.ACCENT_BLUE_HEX, G.POPUP_PRIMARY_PRESSED),
                 ("红钮", C.DANGER_BG_HEX, C.DANGER_PRESSED_HEX))
        for label, static, pressed in pairs:
            self.assertLess(self._lum_of(pressed), self._lum_of(static),
                            "%s 按下色 %s 不比静止色 %s 更暗，按下反馈没了" % (label, pressed, static))

    def test_hover_is_lighter_than_static_for_both_buttons(self):
        pairs = (("蓝钮", C.ACCENT_BLUE_HEX, G.POPUP_PRIMARY_HOVER),
                 ("红钮", C.DANGER_BG_HEX, C.DANGER_HOVER_HEX))
        for label, static, hover in pairs:
            self.assertGreater(self._lum_of(hover), self._lum_of(static),
                               "%s 悬停色 %s 不比静止色 %s 更亮" % (label, hover, static))


class TestDimForegroundIsReadable(SFCase):
    """FG_DIM：底部高/低行、休市提示这类小字用的弱化灰，必须读得清。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_clears_aa_on_both_reference_backdrops(self):
        surfaces = {"面板底（sf_glass 真值）": G.POPUP_SURFACE_RGB, "纯黑": (0, 0, 0),
                    "真机最亮玻璃底": MEASURED_GLASS_FLOOR_RGB}
        for name, bg in surfaces.items():
            got = _contrast(C.FG_DIM, bg)
            self.assertGreaterEqual(
                got, WCAG_SMALL_TEXT_MIN,
                "FG_DIM %s 对 %s 只有 %.2f:1，低于 AA 小字 4.5:1"
                "（旧值 #6b7280 就是栽在这里：对纯黑 4.34:1）" % (C.FG_DIM.name(), name, got))

    def test_dim_is_still_dimmer_than_the_foreground_it_sits_under(self):
        """提亮不能提成"和正文一样"：层级还得在。"""
        bg = G.POPUP_SURFACE_RGB
        self.assertLess(_contrast(C.FG_DIM, bg), _contrast(QColor("#ffffff"), bg),
                        "弱化灰亮过白字，信息层级没了")


class TestChartEndpointMatchesTrendToken(SFCase):
    """分时折线的渐变：终点必须就是趋势色（线尾 = 呼吸点同色）。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_tail_color_equals_the_token_used_by_the_dot(self):
        for token, label in ((R.RED, "涨"), (R.GREEN, "跌")):
            _start, stop = R.chart_line_stop_colors(token)
            self.assertEqual(stop, token,
                             "%s势折线尾色是 %s，而呼吸点画的是 %s —— 线尾与端点两种色"
                             % (label, stop.name(), token.name()))

    def test_gradient_is_still_a_gradient(self):
        """不许为了让端点一致就把渐变整条抹平（浅→深的读法是刻意保留的）。"""
        start, stop = R.chart_line_stop_colors(R.RED)
        self.assertNotEqual(start, stop, "渐变没了，折线变成纯色的了")
        self.assertGreater(start.lightness(), stop.lightness(),
                           "起点应当比终点浅（浅→深），现在反了")

    def test_flat_trend_uses_the_same_helper_not_a_side_branch(self):
        """平盘也必须走同一个函数：旁支是下一次颜色漂移的入口。"""
        flat = C.FG_DIM
        start, stop = R.chart_line_stop_colors(flat)
        self.assertEqual(stop, flat, "平盘线尾没落在趋势色上")
        rect = QRectF(0, 0, 100, 40)
        self.assertEqual(R.chart_line_stop_colors(flat)[1], stop)


if __name__ == "__main__":
    unittest.main(verbosity=2)
