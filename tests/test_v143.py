# -*- coding: utf-8 -*-
"""v1.2.43 高帧率稳定性四项优化回归测试

覆盖：
  1) PreciseTimer 精确模式：源码级校验 4 个高频/动画 QTimer（_anim_timer /
     _flash_timer / _flash_drop_timer / 50ms 行情轮询 timer）均已设
     Qt.TimerType.PreciseTimer，防 CoarseTimer 5% 容差在 120Hz 高刷屏吞帧回归；
  2) 价格动画局部重绘：price_update_rect 脏区计算（价格+涨跌幅条带、不侵入分时区）
     与 is_price_partial_redraw 损坏区精确匹配判定（不误伤整窗重绘/不残留）；
  3) 玻璃遮罩缓存键：should_rebuild_glass_mask 目标尺寸缓存复用 + 节流合并判定；
  4) 动画期图表是否需重画判定：should_draw_chart（动画期数据/几何未变→不重画图表，
     数据/几何有变或动画结束→重画图表）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sf_glass import GLASS_MASK_THROTTLE, should_rebuild_glass_mask
from sf_ui_render import (CHART_TOP, H_PCT, H_PRICE, PRICE_PARTIAL_PAD, Y_PCT, Y_PRICE,
                          is_price_partial_redraw, price_update_rect,
                          should_draw_chart)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW_SRC = os.path.join(REPO, "stock_float_window.py")


class TestPreciseTimer(unittest.TestCase):
    """第1项：动画/高频 QTimer 均设为精确模式（源码级校验，防吞帧回归）"""

    def test_four_highfreq_timers_set_precise(self):
        with open(WINDOW_SRC, encoding="utf-8") as f:
            src = f.read()
        # _anim_timer / _flash_timer / _flash_drop_timer / 50ms 行情轮询 timer
        self.assertGreaterEqual(src.count("Qt.TimerType.PreciseTimer"), 4)


class TestPriceUpdateRect(unittest.TestCase):
    """第2项：价格动画局部重绘脏区计算"""

    def test_covers_price_and_pct_rows_full_width(self):
        w = 286
        x, y, rw, rh = price_update_rect(w)
        self.assertEqual(x, 0)
        self.assertEqual(rw, w)
        # 条带从价格行上安全区开始
        self.assertEqual(y, Y_PRICE - PRICE_PARTIAL_PAD)
        # v1.2.61：涨跌幅已移至股价右侧同一水平带，脏区覆盖价格行整高即可
        self.assertLessEqual(y, Y_PRICE)
        self.assertGreaterEqual(y + rh, Y_PRICE + H_PRICE)
        self.assertGreater(rh, 0)

    def test_does_not_intrude_into_chart_area(self):
        # 底部 ≤ 分时图顶部：动画期间局部重绘绝不覆盖图表区首行
        x, y, rw, rh = price_update_rect(286)
        self.assertLessEqual(y + rh, CHART_TOP)

    def test_width_scales_with_window(self):
        # 条带宽度随窗口宽度等比变化（整窗宽），保证状态徽标/滚动滑出字符都覆盖
        _, _, rw_a, _ = price_update_rect(200)
        _, _, rw_b, _ = price_update_rect(400)
        self.assertEqual(rw_a, 200)
        self.assertEqual(rw_b, 400)


class TestIsPricePartialRedraw(unittest.TestCase):
    """第2项：损坏区==局部脏区 的精确匹配判定"""

    def setUp(self):
        self.partial = price_update_rect(286)

    def test_exact_match(self):
        self.assertTrue(is_price_partial_redraw(tuple(self.partial), self.partial))

    def test_within_tolerance(self):
        # Retina 整/半像素取整容差 1px 内仍判为局部重绘
        shifted = tuple(v + 0 for v in self.partial)
        self.assertTrue(is_price_partial_redraw(shifted, self.partial))
        near = (self.partial[0] + 1, self.partial[1], self.partial[2], self.partial[3])
        self.assertTrue(is_price_partial_redraw(near, self.partial))

    def test_whole_window_damage_never_matches(self):
        # 整窗重绘的损坏区 ≠ 局部脏区 → 必须走完整绘制，防悬挂局部脏区吞掉整窗重绘
        w = 286
        whole = (0, 0, w, 400)
        self.assertFalse(is_price_partial_redraw(whole, self.partial))
        # 高度差超容差（如局部区外还有其它悬停/闪烁脏区）→ 完整绘制
        taller = (0, self.partial[1], self.partial[2], self.partial[3] + 30)
        self.assertFalse(is_price_partial_redraw(taller, self.partial))

    def test_none_guard(self):
        self.assertFalse(is_price_partial_redraw(None, self.partial))
        self.assertFalse(is_price_partial_redraw(tuple(self.partial), None))


class TestShouldDrawChart(unittest.TestCase):
    """第4项：价格动画 tick 期间 图表是否需重画 判定"""

    def test_not_animating_always_draw_chart(self):
        self.assertTrue(should_draw_chart(False, False))
        self.assertTrue(should_draw_chart(False, True))

    def test_animating_without_change_skip_chart(self):
        # 动画进行中且图表数据/几何均未变化 → 分时图零重绘（本帧只局部重绘价格）
        self.assertFalse(should_draw_chart(True, False))

    def test_animating_with_data_change_draw_chart(self):
        # 动画进行中但行情数据/窗口几何已更新 → 整窗重绘一次，图表与价格不错帧
        self.assertTrue(should_draw_chart(True, True))


class TestShouldRebuildGlassMask(unittest.TestCase):
    """第3项：玻璃遮罩缓存键 —— 目标尺寸缓存复用 + 节流合并"""

    def _min_ts_diff(self):
        return GLASS_MASK_THROTTLE + 0.001

    def test_force_mask_always_rebuild(self):
        self.assertTrue(should_rebuild_glass_mask(286, 250, 286, 250, 1.0, 1.0,
                                                  force_mask=True))
        self.assertTrue(should_rebuild_glass_mask(1, 1, 200, 200, 0.0, 0.0,
                                                  force_mask=True))

    def test_same_size_cache_reuse_skips(self):
        # 目标尺寸与上次重建一致（≤1px）→ 直接跳过重建（动画/拉伸中间帧不每帧重建）
        now = 100.0
        self.assertFalse(should_rebuild_glass_mask(286, 250, 286, 250, now - 5.0, now))
        # ≤1px 抖动视作同尺寸
        self.assertFalse(should_rebuild_glass_mask(287, 250, 286, 250, now - 5.0, now))

    def test_changed_size_throttle_merges(self):
        # 尺寸变化但在节流窗内 → 合并等待到节流到期（不逐帧重建）
        now = 1.0
        self.assertFalse(should_rebuild_glass_mask(
            300, 250, 286, 250, now - (GLASS_MASK_THROTTLE / 2), now))
        # 节流窗已过 → 以最新尺寸重建一次（合并重建）
        self.assertTrue(should_rebuild_glass_mask(
            300, 250, 286, 250, now - self._min_ts_diff(), now))

    def test_first_rebuild_without_history(self):
        # 无历史记录（last_w/last_h=0）→ 只要距上次重建超过节流即重建
        now = 100.0
        self.assertTrue(should_rebuild_glass_mask(286, 250, 0, 0, 0.0, now))
        # force_mask 命中时即使无历史也强制重建（不应走 False 短路）
        self.assertTrue(should_rebuild_glass_mask(286, 250, 0, 0, 0.0, now,
                                                  force_mask=True))


if __name__ == "__main__":
    unittest.main()
