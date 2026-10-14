# -*- coding: utf-8 -*-
"""样式表里的颜色必须有名字（v1.4.64，全方位复核 F6）。

AGENTS §5.2-6 那句"次要文字灰只有一个来源 `sf_constants.FG_DIM_HEX`，Qt 样式表里
不许再手写 hex"此前只做到了主窗菜单一处：实测活代码里还有 **15 处**裸 hex 写在
样式表字符串里（target_panel / sf_editor / sf_glass / 主窗菜单模板），而判据只读
`QMenu.styleSheet()` 那一处。后果不是风格问题 —— 这一族"提亮到读得清"的工作
漏掉了两处禁用态灰，它们比本轮被判不达标并删掉的旧值 **#6b7280 还暗**：

    #5a6072 对面板底 #1d1e20 = 2.66:1
    #68718a 对面板底 #1d1e20 = 3.43:1
    #6b7280（被删的旧值）   = 3.45:1
    #80899b（本轮新值）     = 4.74:1

诚实话要说全：WCAG 1.4.3 对"非活动组件"是**豁免**的，所以旧值不构成无障碍违规；
违规的是"同一个灰阶被逐处提亮、这两处漏着，而文档说只有一个来源"。

这条判据为什么做成**硬零**而不是棘轮基线：留基线等于承认"还可以有下一处"，
而这正是当初要治的东西。描边类 rgba / RGB 元组不在口径内 —— 它们承载的是"框"
不是"字"，改它们等于顺手改视觉。
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sf_constants as C  # noqa: E402
import sf_glass as G  # noqa: E402

try:
    from _base import SFCase  # noqa: E402
except ImportError:  # 以包形式被 discover 导入时
    from tests._base import SFCase  # noqa: E402

# 声明形状：`color:#ffffff` / `background-color: #4f7cff` / `selection-background-color:#4f7cff`
QSS_DECL = re.compile(r"[\w-]+\s*:\s*#[0-9a-fA-F]{3,8}\b")
HEX_ANY = re.compile(r"#[0-9a-fA-F]{3,8}\b")
# 这些目录/文件不参与：回退副本与归档件按 AGENTS §5 不许动，工具与用例不是产品视觉，
# sf_icons 是 SVG 资产数据（颜色写在自己的属性里，不是 Qt 样式表）。
SKIP_PARTS = ("backups/", "tools/archive/", ".venv/", "build/", "dist/", "tests/", "tools/")
SKIP_FILES = ("sf_icons.py",)


def _product_sources():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("sf_archive") or any(p in rel for p in SKIP_PARTS):
            continue
        if path.name in SKIP_FILES:
            continue
        yield rel, path


def stylesheet_hexes(source: str) -> list[tuple[int, str]]:
    """返回"样式表里手写的 hex"：(行号, 片段)。纯函数，便于自证可失败。"""
    hits: list[tuple[int, str]] = []
    # 语法错误**不吞**：静默返回空表会让整条判据在"解析失败"时假装通过，
    # 而种样例带缩进时就是这么骗过自己的（本轮实测踩过，见 test_criteria_can_fail）。
    tree = ast.parse(textwrap.dedent(source))
    style_calls = set()
    for node in ast.walk(tree):
        # setStyleSheet(...) 的实参整体算样式表，哪怕它跨了好几个字符串片段
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setStyleSheet"):
            for arg in node.args:
                style_calls.add(id(arg))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        text = node.value
        if id(node) in style_calls:
            if HEX_ANY.search(text):
                hits.append((node.lineno, text.strip()[:70]))
            continue
        if QSS_DECL.search(text):
            hits.append((node.lineno, text.strip()[:70]))
    return hits


def relative_luminance(rgb) -> float:
    def lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg_rgb, bg_rgb) -> float:
    a, b = relative_luminance(fg_rgb), relative_luminance(bg_rgb)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def rgb_of(hex_text: str) -> tuple[int, int, int]:
    body = hex_text.lstrip("#")
    return tuple(int(body[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


class NoBareHexInStylesheets(SFCase):
    """产品代码里不许再有第二份"手写进样式表的 hex"。"""

    def test_product_sources_are_clean(self):
        offenders = []
        for rel, path in _product_sources():
            for line, snippet in stylesheet_hexes(
                    path.read_text(encoding="utf-8", errors="replace")):
                offenders.append("%s:%d  %s" % (rel, line, snippet))
        self.assertEqual(offenders, [],
                         "样式表里出现裸 hex，颜色必须走 sf_constants 的具名 token：\n   "
                         + "\n   ".join(offenders))

    def test_unparsable_source_is_loud_not_clean(self):
        """解析失败必须炸出来，不许被当成"这文件很干净"。

        本轮实测踩过：种样例带着 8 格缩进贴进列表，ast.parse 抛 SyntaxError，
        旧实现 `except SyntaxError: return hits` 把它当成干净 —— 两条自检全绿，
        而它们一条都没真的看过。现在先 textwrap.dedent（缩进不再是坑），
        剩下真解析不了的写法一律抛出去。
        """
        with self.assertRaises(SyntaxError):
            stylesheet_hexes('        w.setStyleSheet("color: #fff;"')   # 括号没闭合

    def test_criteria_can_fail(self):
        """判据本身要能红：把本轮修掉的两种真实写法各种一处，必须都被抓到。"""
        planted = [
            '        self.hint.setStyleSheet("font-size:10px;color:#9aa3b5;'
            'background:transparent;")',
            '        w.setStyleSheet("QPushButton#ok:disabled{color:#68718a;}")',
            '        m.setStyleSheet("""QMenu { color: #ffffff; }""")',
        ]
        for src in planted:
            self.assertTrue(stylesheet_hexes(src),
                            "种了这一句裸 hex 却没报 → 这条判据是恒真的：%s" % src)

    def test_clean_writings_pass(self):
        """反向：token 插值与 rgba() 描边都不该被误伤，否则这条闸门没人能过。"""
        clean = [
            '        w.setStyleSheet("QPushButton#ok{background:%s;color:%s;}"'
            ' % (ACCENT_BLUE_HEX, FG_BRIGHT_HEX))',
            '        w.setStyleSheet("QLineEdit:disabled{background:rgba(255,255,255,'
            '0.04);color:%s;}" % FG_DISABLED_HEX)',
            '        border = QColor(90, 96, 114, 87)',
            '        p.setPen(QPen(QColor("#ffffff"), 1))',
        ]
        for src in clean:
            self.assertEqual(stylesheet_hexes(src), [], "误伤了：%s" % src)


class DisabledGrayMeetsTheBar(SFCase):
    """禁用态灰的数值依据：过 AA 4.5，但必须仍明显比启用态暗。"""

    def test_disabled_gray_passes_the_same_aa_line_as_fg_dim(self):
        got = contrast(rgb_of(C.FG_DISABLED_HEX), G.POPUP_SURFACE_RGB)
        self.assertGreaterEqual(got, 4.5,
                                "FG_DISABLED_HEX 对弹窗面只有 %.2f:1 —— 本仓小字下限 4.5"
                                % got)

    def test_disabled_gray_is_still_visibly_dimmer_than_enabled(self):
        """提亮不能提成"禁用态和启用态一个样"：相对亮度至少低 25%。"""
        dim = relative_luminance(rgb_of(C.FG_DISABLED_HEX))
        bright = relative_luminance(rgb_of(C.FG_HINT_HEX))
        self.assertLess(dim / bright, 0.75,
                        "禁用态灰提得太多了，用户分不出「点不动」这件事")

    def test_the_two_legacy_values_were_actively_worse(self):
        """把"为什么要改"钉在数字上：旧值确实低于本仓自己定的线。"""
        for legacy in ("#5a6072", "#68718a", "#6b7280"):
            self.assertLess(contrast(rgb_of(legacy), G.POPUP_SURFACE_RGB), 4.5,
                            "%s 居然达标了？那这轮提亮的理由就不成立" % legacy)

    def test_hint_gray_is_one_source_across_the_three_files(self):
        """提示小字灰此前在三个文件各写一份字面量，现在必须同源。"""
        self.assertEqual(G.POPUP_FG_DIM, C.FG_HINT_HEX)
        self.assertEqual(rgb_of(C.FG_HINT_HEX), (154, 163, 181))


class RenderedDisabledTextIsTheToken(SFCase):
    """真渲染：样式表里的 token 要真的落到像素上。

    为什么不停在"字符串里含有正确颜色"：Qt 的 :disabled 选择器、优先级与父窗
    样式继承都可能让那句话不生效 —— 上一轮"菜单禁用项还写着旧值"就是这么漏的。
    判据用"画出来的字是什么颜色"这种可观测事实，而不是亮度排名：抗锯齿会让
    边缘像素混向底色，但字的核心像素就是声明色本身（本机实测 #80899b 原色出现 13 次）。
    """

    def setUp(self):
        from PyQt6.QtWidgets import QApplication
        self._app = QApplication.instance() or QApplication([])
        self.widgets = []

    def tearDown(self):
        for w in self.widgets:
            w.close()
        self.widgets = []

    def _render_disabled_del(self, color_hex):
        """用面板真正那条 QSS 画一颗禁用的删除钮，返回它自己的 QImage。"""
        from unittest.mock import patch

        from PyQt6.QtWidgets import QPushButton

        import target_panel
        with patch.object(target_panel, "load_alerts", return_value=[]):
            panel = target_panel.TargetPanel("600519", "贵州茅台",
                                             lambda _a: True, lambda: True)
        qss = panel.styleSheet()          # 公开输出：面板实际用的那一条样式表
        if color_hex != C.FG_DISABLED_HEX:
            qss = qss.replace(C.FG_DISABLED_HEX, color_hex)
        btn = QPushButton("删")
        btn.setObjectName("del")
        btn.setFixedSize(26, 18)
        btn.setEnabled(False)
        btn.setStyleSheet(qss)
        self.widgets.append(btn)
        return btn.grab().toImage()

    def _glyph_pixels(self, image):
        """按钮上离白底足够远的像素 —— 也就是画出来的字。"""
        out = []
        for y in range(image.height()):
            for x in range(image.width()):
                c = image.pixelColor(x, y)
                rgb = (c.red(), c.green(), c.blue())
                if sum(255 - v for v in rgb) > 60:
                    out.append(rgb)
        return out

    def _modal_glyph_color(self, image):
        import collections
        pixels = self._glyph_pixels(image)
        if not pixels:
            return None, 0
        (color, n), = collections.Counter(pixels).most_common(1)
        return color, n

    def test_something_is_actually_painted(self):
        _color, n = self._modal_glyph_color(
            self._render_disabled_del(C.FG_DISABLED_HEX))
        self.assertGreaterEqual(n, 5,
                               "禁用态只画出 %d 个像素 —— 样式表把内容吞了" % n)

    def test_painted_color_is_exactly_the_token(self):
        color, _n = self._modal_glyph_color(
            self._render_disabled_del(C.FG_DISABLED_HEX))
        self.assertEqual(color, rgb_of(C.FG_DISABLED_HEX),
                         "画出来的字不是 token 那个颜色 —— QSS 被别的规则盖掉了")

    def test_new_value_renders_brighter_than_the_legacy_one(self):
        """相对结论：同一颗钮、同一条 QSS，只换颜色，新值明显更亮。"""
        new, _ = self._modal_glyph_color(self._render_disabled_del(C.FG_DISABLED_HEX))
        old, _ = self._modal_glyph_color(self._render_disabled_del("#5a6072"))
        ratio = relative_luminance(new) / max(relative_luminance(old), 1e-6)
        self.assertGreater(ratio, 1.5,
                           "渲染出来只亮了 %.2f 倍，与算式（4.74 vs 2.66）不符" % ratio)
