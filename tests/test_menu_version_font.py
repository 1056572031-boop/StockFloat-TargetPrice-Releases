# -*- coding: utf-8 -*-
"""右键菜单版本行改用股价同款数字字体 D-DIN（2026-10-23，用户看图选定「常规 · 14px · 字距 +0.6」）。

要守的是三件用户看得见的事，不是"代码里写了某个数"：
  1. 版本行用的是 D-DIN，而且是**真画到帧上**的 —— 菜单样式表里有 `font-size: 13px`，
     动作自己设的字体会不会被它盖掉，只有量真帧才知道（本机实测没被盖掉，这里把它钉住）；
  2. 字号、字距取自 sf_constants 的唯一来源：字距常量改了，帧上的字串宽度必须跟着变；
  3. 只动版本这一行：其余四行仍是菜单原字体；D-DIN 没解析到时版本行也保持原字体，
     不带着加了字距的兜底字体上屏。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtGui import QFont, QFontInfo  # noqa: E402

import sf_ui_render  # noqa: E402
import stock_float_window as m  # noqa: E402
from sf_constants import MENU_VERSION_FONT_PX, MENU_VERSION_LETTER_SPACING_PX  # noqa: E402

try:
    from test_menu_row_icons import _WindowCase, _row_runs         # noqa: E402
except ImportError:                                                # 以包形式被 discover 导入时
    from tests.test_menu_row_icons import _WindowCase, _row_runs   # noqa: E402

try:
    from _base import SFCase                                        # noqa: E402
except ImportError:
    from tests._base import SFCase                                  # noqa: E402


def _din_resolves():
    return QFontInfo(QFont(sf_ui_render.NUM_FONT)).family() == sf_ui_render.NUM_FONT


class TestVersionFontBuilder(SFCase):
    """字体构造函数本身：取值全部来自 sf_constants，D-DIN 缺失时返回 None。"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])
        sf_ui_render.load_bundled_fonts()
        # 度量类断言必须自己把内置字体注册上并当场验证（AGENTS §5.2 像素判据那一条）：
        # 否则这些用例是在靠"别的模块恰好先注册过"这个副作用跑。
        assert _din_resolves(), "内置 D-DIN 没解析到，下面所有字体断言都失去意义"

    def test_builder_uses_the_numeric_font_with_the_configured_size_and_spacing(self):
        f = m._menu_version_font()                  # discipline-ok(R5): 模块级私有函数，菜单真帧另有两层判据，这里只钉取值来源
        self.assertIsNotNone(f, "D-DIN 已注册，构造函数不该返回 None")
        self.assertEqual(QFontInfo(f).family(), sf_ui_render.NUM_FONT)
        self.assertEqual(f.pixelSize(), MENU_VERSION_FONT_PX,
                         "字号没取自 sf_constants.MENU_VERSION_FONT_PX")
        self.assertEqual(f.letterSpacingType(), QFont.SpacingType.AbsoluteSpacing,
                         "字距必须是绝对像素；百分比字距是另一个量纲，会随字号缩放")
        # Qt 把字距按 1/64 像素量化存储（本机实测 0.6 读回 0.59375），所以容差取一个量化步长，
        # 不能用 places=3：那是在断言 Qt 的内部精度，不是在断言取值来源。
        self.assertAlmostEqual(f.letterSpacing(), MENU_VERSION_LETTER_SPACING_PX, delta=1 / 64,
                               msg="字距没取自 sf_constants.MENU_VERSION_LETTER_SPACING_PX")
        self.assertEqual(f.weight(), QFont.Weight.Normal,
                         "用户选定的是常规字重；粗体档实测有字形粘连")

    def test_builder_returns_none_when_the_numeric_font_is_missing(self):
        """降级方向：字体解析不到就不单独设，而不是带着字距落到兜底字体上屏。"""
        with patch.object(m, "NUM_FONT", "No-Such-Family-For-Test"):
            self.assertIsNone(m._menu_version_font())      # discipline-ok(R5): 同上


class TestVersionRowOnScreen(_WindowCase):
    """真菜单、真帧：版本行确实是 D-DIN，字距常量确实影响帧，其余行不受牵连。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        sf_ui_render.load_bundled_fonts()
        assert _din_resolves(), "内置 D-DIN 没解析到，下面所有字体断言都失去意义"

    def version_row(self, menu):
        rows = [a for a in menu.actions() if not a.isSeparator() and not a.isEnabled()]
        self.assertEqual(len(rows), 1, "菜单里应当恰有一个禁用行（版本行），got %r"
                         % [a.text() for a in rows])
        return rows[0]

    def ink_width(self, menu, act):
        """版本行文字的墨迹宽度（像素）：该行里第一簇墨的左缘到最后一簇墨的右缘。"""
        runs = _row_runs(menu.grab().toImage(), menu.actionGeometry(act))
        self.assertTrue(runs, "版本行一个像素都没画出来")
        return runs[-1][1] - runs[0][0] + 1

    def test_version_row_is_the_numeric_font_and_other_rows_are_not(self):
        menu = self.open_menu()
        ver = self.version_row(menu)
        self.assertEqual(QFontInfo(ver.font()).family(), sf_ui_render.NUM_FONT,
                         "版本行没用上 D-DIN")
        # 动作没有单独设过字体时，QAction.font() 读回的是应用默认字体而不是菜单字体，
        # 所以"整个菜单被套上 D-DIN"只靠逐行读 action 字体是看不出来的（改坏验证 M7 实测漏过）：
        # 菜单本体的字体要单独验，其余各行的真帧另有下面一条判据。
        self.assertNotEqual(QFontInfo(menu.font()).family(), sf_ui_render.NUM_FONT,
                            "菜单本体被套上了 D-DIN：只该版本这一行用")
        for text in ("刷新行情", "检查更新", "导出诊断包", "反馈问题"):
            self.assertNotEqual(QFontInfo(self.row(menu, text).font()).family(),
                                sf_ui_render.NUM_FONT, "只改版本行，「%s」不该跟着变" % text)

    def test_other_rows_paint_exactly_as_they_did_without_the_version_font(self):
        """其余四行在帧上的墨迹宽度，与"不给版本行单独设字体"时逐行相同。
        版本行换字体不许牵连别的行（字体、字距、整菜单字体任一处误套都会让这里变宽）。"""
        rows = ("刷新行情", "检查更新", "导出诊断包", "反馈问题")
        menu = self.open_menu()
        with_din = {t: self.ink_width(menu, self.row(menu, t)) for t in rows}
        with patch.object(m, "_menu_version_font", return_value=None):
            menu2 = self.open_menu()
            baseline = {t: self.ink_width(menu2, self.row(menu2, t)) for t in rows}
        self.assertEqual(with_din, baseline, "版本行的字体设置牵连了别的行")

    def test_the_two_numbers_the_user_picked_from_the_mockup_stay_put(self):
        """字号与字距是 2026-10-23 用户看六档效果图选定的（E 档：14px、字距 +0.6），不是可随手调的实现细节。
        其余判据都按常量取值，常量被改成别的数它们照样绿（改坏验证 M8/M9 实测），
        所以这里单独钉住：想换档，是一次新的设计决定，要同时改常量、本条与效果图依据。"""
        self.assertEqual(MENU_VERSION_FONT_PX, 14)
        self.assertEqual(MENU_VERSION_LETTER_SPACING_PX, 0.6)

    def test_the_numeric_font_really_reaches_the_painted_frame(self):
        """样式表里有 `font-size: 13px`：动作字体若被它盖掉，上面的族名断言照样绿、帧上却没变。
        所以拿"不单独设字体"的同一行做对照，量帧上的字串宽度必须不同。"""
        with_din = self.ink_width(self.open_menu(), self.version_row(self.win._ctx_menu))   # discipline-ok(R5): 菜单持有成员，同 open_menu
        with patch.object(m, "_menu_version_font", return_value=None):
            baseline = self.ink_width(self.open_menu(), self.version_row(self.win._ctx_menu))   # discipline-ok(R5): 同上
        self.assertNotEqual(with_din, baseline,
                            "设了 D-DIN 与不设，帧上的版本串一样宽：字体没有真正画上屏")

    def test_letter_spacing_constant_widens_the_painted_version_string(self):
        """字距常量改了，帧上的字串宽度必须跟着变：差值约等于 (字符数-1) × 字距。"""
        menu = self.open_menu()
        ver = self.version_row(menu)
        spaced = self.ink_width(menu, ver)
        gaps = len(ver.text()) - 1                  # 字距加在每个字符之后，最后一个字符后面的那份不在墨迹里
        with patch.object(m, "MENU_VERSION_LETTER_SPACING_PX", 0.0):
            tight = self.ink_width(self.open_menu(), self.version_row(self.win._ctx_menu))   # discipline-ok(R5): 同 open_menu
        expected = gaps * MENU_VERSION_LETTER_SPACING_PX
        self.assertGreaterEqual(
            spaced - tight, expected - 2.5,
            "字距 %.1f 理应让 %d 个间隙共加宽约 %.1fpx，实际只多了 %dpx（有字距 %d / 无字距 %d）"
            % (MENU_VERSION_LETTER_SPACING_PX, gaps, expected, spaced - tight, spaced, tight))
        self.assertLessEqual(spaced - tight, expected + 2.5,
                             "加宽量远超 字距×间隙，字距可能被叠加了两次")

    def test_missing_numeric_font_leaves_the_version_row_in_the_menu_font(self):
        with patch.object(m, "NUM_FONT", "No-Such-Family-For-Test"):
            menu = self.open_menu()
            ver = self.version_row(menu)
            self.assertNotEqual(QFontInfo(ver.font()).family(), sf_ui_render.NUM_FONT)
            self.assertEqual(ver.font().letterSpacing(), 0.0,
                             "D-DIN 缺失时不该给兜底字体单独加字距")
            self.assertEqual(QFontInfo(ver.font()).family(),
                             QFontInfo(self.row(menu, "刷新行情").font()).family(),
                             "D-DIN 缺失时版本行应保持与其余行相同的菜单字体")


if __name__ == "__main__":
    unittest.main()
