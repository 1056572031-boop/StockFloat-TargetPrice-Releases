# -*- coding: utf-8 -*-
"""实时股价 v1.2.68 回归测试：底部数据行统一左对齐 + 名称垂直层次。

本轮改动（由此文件锁定，防止回退）：

A. renderer._bottom_layout 统一左对齐（v1.2.68）
   移除 v1.2.66 的「宽窗两端锚定」分支（左段靠左、右段靠右）。
   现在所有窗口宽度下，底部数据行均采用紧凑左对齐：每段从 pad_x 开始逐段排布，
   段间 gap 间距。不做右对齐/两端锚定——用户要求整体靠左。
   函数签名与返回值 (row1, row2, min_width) 不变。

B. sf_ui_render 名称垂直层次（v1.2.66）
   NAME_TOP_PAD=4 把名称绘制矩形下移 4px、高度同步减 4px（矩形底不动，不侵入代码区）；
   BOTTOM_INNER_GAP 由 1 增到 3（标签-数字间距增大）。其余坐标（Y_CODE/Y_PRICE/
   Y_PCT/BTN_* 等）零改动；本版单独缩短图表与价格区间距。

运行方式（在 StockFloat 目录）：
    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_v166_layout -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import renderer
import sf_ui_render as R

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 名称绘制矩形 y / 高度常量（与 sf_ui_render._paint_header 内部一致）
NAME_DRAW_Y = R.Y_NAME + R.NAME_TOP_PAD
NAME_DRAW_H = R.H_NAME - R.NAME_TOP_PAD

# 真实调用参数（与 sf_ui_render._paint_bottom 一致）
PAD_X = 12
INNER_GAP = 3
GAP = 3
SLACK = 8
BASE = 0


class _MockFM:
    """水平度量 mock：每字符固定 10px，便于精确推导布局几何。"""

    def horizontalAdvance(self, s):
        return 10 * len(s)


class TestBottomLayoutLeftAligned(unittest.TestCase):
    """纯函数 + mock 度量，不经过真实字体，与平台无关。"""

    def _row(self, w, row1, row2=PAD_X):
        # row2 缺省构造一个 2 段行，便于统一测 min_width
        if row2 == PAD_X:
            row2 = [("最低点", "1480.0", None), ("最高点", "1520.0", None)]
        return renderer._bottom_layout(
            w, row1, row2, _MockFM(), _MockFM(),
            pad_x=PAD_X, inner_gap=INNER_GAP, gap=GAP, slack=SLACK, base=BASE)

    def test_wide_window_both_left_aligned(self):
        """宽窗（w=300）：两段均左对齐，不做右对齐/两端锚定。"""
        row1 = [("最大涨幅", "+0.60%", None), ("最大跌幅", "-1.23%", None)]
        r1, _r2, _mw = self._row(300, row1)
        self.assertEqual(r1[0]["x"], PAD_X)                       # 第一段靠左
        # 第二段也靠左（紧接第一段右缘 + gap），不是右对齐
        expected_x1 = PAD_X + r1[0]["seg_w"] + GAP
        self.assertEqual(r1[1]["x"], expected_x1)
        self.assertTrue(r1[0]["fits"])
        self.assertTrue(r1[1]["fits"])
        # 两段间距 = gap（紧凑排列）
        spacing = r1[1]["x"] - (r1[0]["x"] + r1[0]["seg_w"])
        self.assertEqual(spacing, GAP)

    def test_narrow_window_still_left_aligned(self):
        """窄窗（w=180）：余量不足，仍左对齐（第二段可能放不下）。"""
        row1 = [("最大涨幅", "+0.60%", None), ("最大跌幅", "-1.23%", None)]
        r1, _r2, _mw = self._row(180, row1)
        self.assertEqual(r1[0]["x"], PAD_X)                       # 仍左对齐
        # 第二段也是左对齐位置（紧接第一段），非右对齐
        expected_x1 = PAD_X + r1[0]["seg_w"] + GAP
        self.assertEqual(r1[1]["x"], expected_x1)
        self.assertTrue(r1[0]["fits"])
        # 窄窗下第二段可能放不下
        # (不强制断言 fits=False，因为具体取决于 mock 度量算出的宽度)

    def test_very_narrow_window_second_segment_unfit(self):
        """极窄窗（w=100）：第二段 fits=False。"""
        row1 = [("最大涨幅", "+0.60%", None), ("最大跌幅", "-1.23%", None)]
        r1, _r2, _mw = self._row(100, row1)
        self.assertFalse(r1[1]["fits"])

    def test_single_segment_left_aligned(self):
        """单段行：左对齐。"""
        row1 = [("最大涨幅", "+0.60%", None)]
        r1, _r2, _mw = self._row(300, row1)
        self.assertEqual(len(r1), 1)
        self.assertEqual(r1[0]["x"], PAD_X)
        self.assertTrue(r1[0]["fits"])

    def test_three_segments_all_left_aligned(self):
        """3+ 段行：全部左对齐，逐段紧凑排列。"""
        row1 = [("A", "+0.1%", None), ("B", "-0.2%", None), ("C", "+0.3%", None)]
        r1, _r2, _mw = self._row(300, row1)
        self.assertEqual(len(r1), 3)
        # 逐段左对齐：每段 x 递增且等于前一段右缘 + gap
        for i in range(1, len(r1)):
            self.assertEqual(r1[i]["x"], r1[i - 1]["x"] + r1[i - 1]["seg_w"] + GAP)

    def test_min_width_content_based(self):
        """min_width 仅由内容宽度决定，与窗口宽度无关。"""
        row1 = [("最大涨幅", "+0.60%", None), ("最大跌幅", "-1.23%", None)]
        _r1, _r2, mw_wide = self._row(300, row1)
        _r1, _r2, mw_narrow = self._row(180, row1)
        self.assertEqual(mw_wide, mw_narrow)
        # 直接验证公式：pad_x*2 + 每段 seg_w 和 + gap*(n-1) + slack
        seg0 = 10 * len("最大涨幅") + INNER_GAP + 10 * len("+0.60%")
        seg1 = 10 * len("最大跌幅") + INNER_GAP + 10 * len("-1.23%")
        expect = PAD_X * 2 + seg0 + seg1 + GAP + SLACK
        self.assertEqual(mw_wide, expect)


class TestNameVerticalOffset(unittest.TestCase):
    """纯常量/几何推导，不创建窗口。"""

    def test_name_top_pad_constant(self):
        self.assertEqual(R.NAME_TOP_PAD, 4)

    def test_bottom_inner_gap_increased(self):
        # v1.2.66：标签-数字间距由 1 增到 3（BOTTOM_GAP 仍为 3，不被改动）
        self.assertEqual(R.BOTTOM_INNER_GAP, 3)
        self.assertEqual(R.BOTTOM_GAP, 3)

    def test_dead_constant_removed(self):
        # 2026-09-03：清理 v1.2.62 起即无人引用的死常量 BOTTOM_GROUP_GAP，
        # 杜绝「双份漂移」隐患。锁定其已从 sf_ui_render 命名空间移除。
        self.assertFalse(hasattr(R, "BOTTOM_GROUP_GAP"))

    def test_name_draw_rect_shifted_down(self):
        # 名称绘制起点下移 NAME_TOP_PAD，但仍位于代码行之上
        self.assertEqual(NAME_DRAW_Y, R.Y_NAME + R.NAME_TOP_PAD)
        self.assertLess(NAME_DRAW_Y, R.Y_CODE)

    def test_name_bottom_preserved(self):
        # 矩形底仍在原 Y_NAME + H_NAME，不侵入代码区（与改动前一致）
        self.assertEqual(NAME_DRAW_Y + NAME_DRAW_H, R.Y_NAME + R.H_NAME)

    def test_other_elements_unmoved(self):
        # 改动 3b 不得影响其他元素坐标
        self.assertEqual(R.TOP_GAP, 28)
        self.assertEqual(R.Y_CODE, 26 + R.TOP_GAP)
        self.assertEqual(R.Y_PRICE, 46 + R.TOP_GAP)
        self.assertEqual(R.Y_PCT, 76 + R.TOP_GAP)
        self.assertEqual(R.CHART_TOP, 80 + R.TOP_GAP)
        self.assertEqual(R.CHART_TOP - (R.Y_PRICE + R.H_PRICE), 6)
        self.assertEqual(R.BTN_ICON_Y, 11)
        self.assertEqual(R.BTN_CY, 17)
        self.assertEqual(R.BTN_ICON_SZ, 12)


if __name__ == "__main__":
    unittest.main()
