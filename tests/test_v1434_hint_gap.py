# -*- coding: utf-8 -*-
"""v1.4.34 回归：滞后提示"数据暂缺 · 重试中"不得与 0% 基准白色虚线贴近或重叠。

现场（用户 2026-09-24 截图）：昨收 143.55、当日 137~142.6（-3.71% 的下跌日），
白色虚线压在提示文字上。

实测（几何口径，窗口 184 宽，昨收高于当日最高）：
    窗口高 224 / 240 / 260 / 280 / 300 → 虚线与文字带净空 **0.0px**（穿过文字带）
    窗口高 340 / 380 / 400 / 420       → 净空 2.5 / 5.8 / 7.5 / 9.2px
根因：提示字被**固定**钉在 `rect.top() + 2`，而虚线 y 由"昨收落在当日区间的哪里"
决定 —— 昨收高于当日最高时它必然贴在图带顶部，图带一矮就撞字。

修法（sf_ui_render.stale_hint_y）：默认仍贴顶（不改上涨日的既有观感），只有顶部
这一档与虚线的净空不足 CHART_HINT_MIN_GAP_PX 时才翻到图带底部；判据按**底板外沿**
算（底板比字身大），是保守口径。另给提示字补一块只包住文字的半透明圆角底板 ——
图带就 70~266px 高，翻到底部会压到曲线（实测昨收 -1% 那档底部矩形里 70 个采样点
全在带内），底板管"压在曲线上也读得清"，位置规则管"绝不碰虚线"，各管一头。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QRectF

import sf_ui_render as sr
from sf_ui_render import (BOTTOM_SECTION_HIDDEN, CHART_TOP, CHART_HINT_MIN_GAP_PX,
                          FONT_CHART_HINT, LABEL_OUTSET_Y, PAD_X)
from tests._base import SFCase

WIN_W = 184
HINT_H = FONT_CHART_HINT + 8


def _rect(win_h):
    return QRectF(PAD_X, CHART_TOP, WIN_W - 2 * PAD_X,
                  win_h - CHART_TOP - BOTTOM_SECTION_HIDDEN)


def _baseline_y(rect, pre_close, low, high):
    """昨收线的 y —— 区间与映射都走产品自己的函数（v1.4.59 收掉 D08）。

    旧写法在这里**手抄了一遍**产品侧的公式（`pad = (hi - lo) * 0.10` 那三行）。
    后果不是"今天算错"，而是产品一改余量比例，这条用例算出来的虚线位置就是
    上一个版本的 —— 它照样绿，却已经在验一件不存在的事（会自我证明的断言）。
    现在 `prices` 仍然是用例自己造的输入，但"怎么把价格摊到矩形上"只有一份实现。
    """
    prices = [high - (high - low) * (i / 119.0) for i in range(120)] + [pre_close]
    lo, hi = sr.chart_price_span(min(prices), max(prices))
    return sr.chart_y_for_price(rect, lo, hi, pre_close)


def _gap(y, line_y):
    """提示**底板外沿**与虚线的净空；穿过底板记 0。"""
    a, b = y - LABEL_OUTSET_Y, y + HINT_H + LABEL_OUTSET_Y
    if line_y < a:
        return a - line_y
    if line_y > b:
        return line_y - b
    return 0.0


class TestStaleHintClearsBaseline(SFCase):
    def test_down_day_clears_line_at_every_window_height(self):
        """下跌日（昨收=区间上沿，虚线贴顶）：各窗口高度下净空都必须达标。"""
        worst = None
        for win_h in (224, 240, 260, 280, 300, 340, 400, 420):
            rect = _rect(win_h)
            line_y = _baseline_y(rect, 143.55, 137.0, 142.6)
            y = sr.stale_hint_y(rect.top(), rect.bottom(), HINT_H, line_y)
            gap = _gap(y, line_y)
            if worst is None or gap < worst[0]:
                worst = (gap, win_h)
            self.assertGreaterEqual(
                gap, CHART_HINT_MIN_GAP_PX,
                "窗口高 %d 时虚线压进/贴近提示底板（净空 %.1fpx）" % (win_h, gap))
        self.assertGreaterEqual(worst[0], CHART_HINT_MIN_GAP_PX)

    def test_up_day_keeps_original_top_position(self):
        """上涨日虚线在图带下方，顶部本来就有净空 —— 不该无端把字挪到底部。"""
        rect = _rect(400)
        line_y = _baseline_y(rect, 133.3, 134.0, 138.3)
        self.assertGreater(line_y, rect.center().y(), "前提：虚线应落在图带下半")
        self.assertEqual(sr.stale_hint_y(rect.top(), rect.bottom(), HINT_H, line_y),
                         rect.top() + 2)

    def test_no_baseline_falls_back_to_top(self):
        """拿不到昨收线（缺 pre_close）时退回历史行为：贴顶，绝不因此不画提示。"""
        self.assertEqual(sr.stale_hint_y(108.0, 354.0, HINT_H, None), 110.0)

    def test_tiny_band_picks_the_farther_side(self):
        """两侧都放不下时取净空较大的一侧，而不是随便钉一边。"""
        top, bottom = 108.0, 178.0
        line = sr.stale_hint_y(top, bottom, HINT_H, 160.0)
        # 虚线在底部矩形里 → 顶部反而更远，必须留顶部
        self.assertEqual(line, top + 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
