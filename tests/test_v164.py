# -*- coding: utf-8 -*-
"""实时股价 v1.2.64 回归测试：底部两行垂直间距统一为常量 BOTTOM_ROW_GAP = 2px。

需求（本版本的契约，由此测试锁定）：
    1. 底部数据两行（行1 最大涨幅/最大跌幅；行2 最低点/最高点）之间的垂直间距
       **唯一来源**是常量 `BOTTOM_ROW_GAP`，值为 2px。
    2. 依赖该间距的派生量（分时图底部保留区 BOTTOM_SECTION_HIDDEN、收起/展开高度）
       全部**引用常量推导**，不得再硬编码数值 —— 改间距时无需手改他处。
    3. 间距收窄后整体高度**相应收敛**（间距每减 1px，高度减 1px）。

关键几何（全部为纯算术，不经过 QFontMetrics，故与运行平台无关）：
    y_row1 = h - BOTTOM_BASE_HIDDEN - BOTTOM_ROW_H - BOTTOM_ROW_GAP
    y_row2 = h - BOTTOM_BASE_HIDDEN
    行1 字形底 = y_row1 + FONT_CODE_DESCENT
    行2 字形顶 = y_row2 - BOTTOM_ROW_ASCENT
    → 视觉净间距 = BOTTOM_ROW_H + BOTTOM_ROW_GAP - DESCENT - ASCENT
                 = 13 + 2 - 3 - 10 = 2 == BOTTOM_ROW_GAP（常量值本身）

运行方式（在 StockFloat 目录）：
    python3 -m unittest tests.test_v164 -v
"""
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sf_constants import APP_VERSION, VERSION_TEXT
import sf_ui_render as R
import stock_float_window as m
import sf_quote

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# v1.2.65：常量真实值快照 —— 下方用例会临时 patch BOTTOM_ROW_GAP，
# 复位时不能用被 patch 过的 R.BOTTOM_ROW_GAP（恒等于 6），必须在 patch 前取真值。
_REAL_GAP = R.BOTTOM_ROW_GAP


def _fake_http_get(url, **kw):
    """离线行情/分时 mock，避免测试访问网络。"""
    if "hq.sinajs.cn" in url:
        return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
    if "/appstock/app/minute/query?" in url:
        return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                '"data":["0930 1510.0 100 1000", "1000 1511.0 200 2000"]}}}}')
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


def _row_geometry(gap, h=100):
    """按给定行间距算两行几何：返回 (行1字形底, 行2字形顶, 视觉净间距)。

    纯算术，不依赖字体度量与运行平台。
    """
    y_row1 = h - R.BOTTOM_BASE_HIDDEN - R.BOTTOM_ROW_H - gap
    y_row2 = h - R.BOTTOM_BASE_HIDDEN
    row1_bottom = y_row1 + R.FONT_CODE_DESCENT
    row2_top = y_row2 - R.BOTTOM_ROW_ASCENT
    return row1_bottom, row2_top, row2_top - row1_bottom




class TestRowGapConstant(unittest.TestCase):
    """常量本身：存在、值为 2、类型为 int。"""

    def test_constant_exists_and_is_two(self):
        self.assertEqual(R.BOTTOM_ROW_GAP, 2,
                         "BOTTOM_ROW_GAP 应为 2px（用户指定）")

    def test_constant_is_int(self):
        self.assertIsInstance(R.BOTTOM_ROW_GAP, int)

    def test_row_h_and_ascent_are_positive(self):
        """推导链上的其余常量必须为正，否则净间距会算成负数。"""
        for name in ("BOTTOM_ROW_H", "BOTTOM_ROW_ASCENT", "BOTTOM_BASE_HIDDEN",
                     "FONT_CODE_DESCENT"):
            self.assertGreater(getattr(R, name), 0, "%s 必须为正" % name)

    def test_ascent_has_single_source(self):
        """ascent 只能有一份定义 —— 防止再出现同值第二份导致双份漂移。

        v1.2.64 首版新增 BOTTOM_ROW_ASCENT 时，stock_float_window 里还留着
        零引用的 FONT_CODE_ASCENT（同值 10），形成两份定义。已删除后者，此测试锁定。
        """
        self.assertTrue(hasattr(R, "BOTTOM_ROW_ASCENT"),
                        "sf_ui_render 必须定义 BOTTOM_ROW_ASCENT")
        self.assertFalse(hasattr(m, "FONT_CODE_ASCENT"),
                         "stock_float_window 不应再定义 FONT_CODE_ASCENT"
                         "（与 BOTTOM_ROW_ASCENT 重复，已删）")

    def test_ascent_matches_descent_sibling(self):
        """ascent / descent 必须成对存在（二者共同决定视觉净间距）。"""
        self.assertTrue(hasattr(R, "FONT_CODE_DESCENT"))
        self.assertEqual(R.BOTTOM_ROW_ASCENT, 10)
        self.assertEqual(R.FONT_CODE_DESCENT, 3)


class TestVisualGapEqualsConstant(unittest.TestCase):
    """两行的**视觉净间距**必须严格等于 BOTTOM_ROW_GAP，不多不少。"""

    def test_visual_gap_equals_constant(self):
        _, _, gap_px = _row_geometry(R.BOTTOM_ROW_GAP)
        self.assertEqual(gap_px, R.BOTTOM_ROW_GAP,
                         "两行视觉净间距应等于 BOTTOM_ROW_GAP(%d)，实测 %d"
                         % (R.BOTTOM_ROW_GAP, gap_px))

    def test_visual_gap_tracks_constant_one_to_one(self):
        """间距常量每 +1，视觉净间距也 +1（证明二者是同一份数，不是巧合相等）。"""
        base = _row_geometry(R.BOTTOM_ROW_GAP)[2]
        for delta in (1, 2, 5):
            self.assertEqual(_row_geometry(R.BOTTOM_ROW_GAP + delta)[2], base + delta)

    def test_gap_is_consistent_at_any_window_height(self):
        """窗口高度变化不影响行间距（间距是常量，不随高度伸缩）。"""
        gaps = {_row_geometry(R.BOTTOM_ROW_GAP, h=h)[2] for h in (60, 100, 180, 260, 400)}
        self.assertEqual(gaps, {R.BOTTOM_ROW_GAP},
                         "不同窗口高度下行间距应恒为 %d" % R.BOTTOM_ROW_GAP)


class TestRowsDoNotOverlap(unittest.TestCase):
    """5.7 硬性约束：两行字形不得重叠，且净间距为正。"""

    def test_rows_have_positive_clearance(self):
        row1_bottom, row2_top, gap_px = _row_geometry(R.BOTTOM_ROW_GAP)
        self.assertGreater(gap_px, 0,
                           "行1 字形底(%d) 与行2 字形顶(%d) 重叠了" % (row1_bottom, row2_top))

    def test_row1_stays_above_row2(self):
        for gap in range(0, 13):
            row1_bottom, row2_top, _ = _row_geometry(gap)
            self.assertLessEqual(row1_bottom, row2_top,
                                 "gap=%d 时行1 压到行2 上了" % gap)

    def test_row2_within_window(self):
        """行 2 不得越出窗口底部（底部留白仍由 BOTTOM_BASE_HIDDEN 保证）。"""
        h = 100
        y_row2 = h - R.BOTTOM_BASE_HIDDEN
        row2_bottom = y_row2 + R.FONT_CODE_DESCENT
        self.assertLess(row2_bottom, h, "行2 字形已越出窗口底部")


class TestSectionDerivedFromGap(unittest.TestCase):
    """分时图底部保留区必须由 BOTTOM_ROW_GAP 推导，不得硬编码。"""

    def test_formula_matches_constants(self):
        expected = (R.BOTTOM_BASE_HIDDEN + R.BOTTOM_ROW_H + R.BOTTOM_ROW_GAP
                    + R.BOTTOM_ROW_ASCENT + R.BOTTOM_SECTION_EXTRA)
        self.assertEqual(R.BOTTOM_SECTION_HIDDEN, expected)

    def test_section_covers_row1_top(self):
        """保留区必须盖住行1 字形顶部，否则分时图会压到数据行上（5.7）。"""
        h = 100
        row1_top = (h - R.BOTTOM_BASE_HIDDEN - R.BOTTOM_ROW_H - R.BOTTOM_ROW_GAP
                    - R.BOTTOM_ROW_ASCENT)
        section_top = h - R.BOTTOM_SECTION_HIDDEN
        self.assertLessEqual(section_top, row1_top,
                             "分时图下沿(%d) 压到行1 字形顶(%d) 之上"
                             % (section_top, row1_top))

    def test_section_shrinks_with_gap(self):
        """间距收窄，保留区同步收窄（证明是推导而非硬编码）。"""
        shrink = 6 - R.BOTTOM_ROW_GAP
        old_section = (R.BOTTOM_BASE_HIDDEN + R.BOTTOM_ROW_H + 6
                       + R.BOTTOM_ROW_ASCENT + R.BOTTOM_SECTION_EXTRA)
        self.assertEqual(old_section, 50, "改前（gap=6）保留区应为 50")
        self.assertEqual(R.BOTTOM_SECTION_HIDDEN, old_section - shrink)

    def test_no_hardcoded_literal(self):
        """源码契约：BOTTOM_SECTION_HIDDEN 不得被赋值为裸字面量。"""
        with open(os.path.join(ROOT, "sf_ui_render.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIsNone(
            re.search(r"^BOTTOM_SECTION_HIDDEN\s*=\s*\d+\s*$", src, re.M),
            "BOTTOM_SECTION_HIDDEN 被硬编码为裸数字，改间距时会漂移 —— 应引用 BOTTOM_ROW_GAP 推导")

    def test_definition_references_gap(self):
        with open(os.path.join(ROOT, "sf_ui_render.py"), encoding="utf-8") as f:
            src = f.read()
        mo = re.search(r"^BOTTOM_SECTION_HIDDEN\s*=.*?(?=\n\S)", src, re.M | re.S)
        self.assertIsNotNone(mo, "未找到 BOTTOM_SECTION_HIDDEN 定义")
        self.assertIn("BOTTOM_ROW_GAP", mo.group(0),
                      "BOTTOM_SECTION_HIDDEN 定义必须引用 BOTTOM_ROW_GAP")


class TestHeightConvergesWithGap(unittest.TestCase):
    """整体高度随间距收窄而收敛：间距每减 1px，收起/展开高度各减 1px。"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", _fake_http_get),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        self.win._expanded_h = None

    def tearDown(self):
        self.win.close()
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        for p in self._patches:
            p.stop()

    def test_collapsed_height_tracks_gap(self):
        # v1.2.65：高度公式已下沉为 sf_ui_render.collapsed_height() 唯一实现，
        # 故常量须打在真正的所有者 R 上（打在 m 上不再影响结果）。
        with patch.object(R, "BOTTOM_ROW_GAP", 6):
            h6 = self.win._height_collapsed()
        with patch.object(R, "BOTTOM_ROW_GAP", _REAL_GAP):
            h2 = self.win._height_collapsed()
        self.assertEqual(h6 - h2, 6 - _REAL_GAP,
                         "间距减少 %dpx，收起高度应同步减少 %dpx"
                         % (6 - _REAL_GAP, 6 - _REAL_GAP))

    def test_expanded_base_tracks_gap(self):
        with patch.object(R, "BOTTOM_ROW_GAP", 6):
            h6 = self.win._height_expanded()
        with patch.object(R, "BOTTOM_ROW_GAP", _REAL_GAP):
            h2 = self.win._height_expanded()
        self.assertEqual(h6 - h2, 6 - _REAL_GAP,
                         "间距减少时展开高度应同步收敛")

    def test_collapsed_height_shrank_vs_v1_2_63(self):
        """相对 v1.2.63（gap=6，实测基准）本次应收敛 4px。"""
        with patch.object(R, "BOTTOM_ROW_GAP", 6):
            old = self.win._height_collapsed()
        now = self.win._height_collapsed()
        self.assertEqual(old - now, 4, "相对 v1.2.63 收起高度应收敛 4px")

    def test_collapsed_less_than_expanded(self):
        self.assertLess(self.win._height_collapsed(), self.win._height_expanded())


class TestPaintUsesConstant(unittest.TestCase):
    """绘制路径必须引用常量，不得另写一份数字。"""

    def test_paint_references_constant(self):
        with open(os.path.join(ROOT, "sf_ui_render.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn(
            "y_row1 = self.height() - BOTTOM_BASE_HIDDEN - BOTTOM_ROW_H - BOTTOM_ROW_GAP",
            src, "y_row1 计算必须引用 BOTTOM_ROW_GAP，不得硬编码间距")

    def test_no_literal_row_gap_in_paint(self):
        """y_row1 计算里不得出现除常量外的行间距数字。"""
        with open(os.path.join(ROOT, "sf_ui_render.py"), encoding="utf-8") as f:
            src = f.read()
        mo = re.search(r"y_row1\s*=\s*self\.height\(\).*", src)
        self.assertIsNotNone(mo)
        self.assertNotIn("BOTTOM_ROW_H - 6", mo.group(0))
        self.assertNotIn("BOTTOM_ROW_H - 2", mo.group(0))

    def test_height_formula_references_constant(self):
        """高度公式必须引用常量推导，不得硬编码数值。

        v1.2.65：公式已从 stock_float_window 的两处逐字复制下沉为
        sf_ui_render.collapsed_height() 唯一实现（消除双份漂移），
        故本断言改为「唯一实现里引用了常量」。
        """
        with open(os.path.join(ROOT, "sf_ui_render.py"), encoding="utf-8") as f:
            src = f.read()
        mo = re.search(r"def collapsed_height\(\):.*?\n(?=\S)", src, re.S)
        self.assertIsNotNone(mo, "sf_ui_render 应有 collapsed_height() 唯一实现")
        body = mo.group(0)
        self.assertIn("BOTTOM_ROW_GAP", body,
                      "collapsed_height() 必须引用 BOTTOM_ROW_GAP 推导")
        self.assertIn("BOTTOM_ROW_ASCENT", body,
                      "collapsed_height() 必须用 BOTTOM_ROW_ASCENT（含汉字行的真实 ascent），"
                      "不得退回纯数字的 cap height（v1.2.65 修正的 3px 亏算）")

    def test_no_duplicate_height_formula_in_window(self):
        """主窗不得再保留第二份高度公式（改一处忘另一处是 v1.2.64 前的老病）。"""
        with open(os.path.join(ROOT, "stock_float_window.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("+ BOTTOM_ROW_H + BOTTOM_ROW_GAP", src,
                         "高度公式应只存在一份（sf_ui_render.collapsed_height），"
                         "主窗不应再拼一遍")
        for name in ("_height_collapsed", "_height_expanded"):
            mo = re.search(r"def %s\(self\):.*?\n(?=\S)" % name, src, re.S)
            self.assertIsNotNone(mo, "应有 %s" % name)
            self.assertIn("collapsed_height()", mo.group(0),
                          "%s 应调用 collapsed_height() 复用唯一实现" % name)


if __name__ == "__main__":
    unittest.main()
