# -*- coding: utf-8 -*-
"""实时股价 v1.2.40 动画状态机纯逻辑单测（上帝类拆分第二步① sf_animator 侧验证）。

覆盖 sf_animator 的动画纯逻辑：
1. price_interp：无滚动 guard / t>=1.0 落定 / ease-out 中间值逐位一致
2. layout_anim_height：完成帧 / smoothstep 中间值高度
3. crossed_key_level：跌破 50 倍关卡触发、普通整数不触发、上涨不触发
4. quote_changed / is_tiny_move / anim_duration_ms：_apply_quote 分支守卫与参数决策
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sf_animator import (anim_duration_ms, crossed_key_level, ease_out_cubic,
                         is_tiny_move, layout_anim_height, price_interp,
                         quote_changed, smoothstep_inout_cubic)
from sf_anim_driver import AnimDriver
from sf_constants import (ANIM_LARGE_MS, ANIM_LARGE_PCT, ANIM_NORMAL_MS,
                          ANIM_TINY_PRICE)


class TestPriceTick(unittest.TestCase):
    """价格/涨跌幅插值一帧（原 _anim_tick_inner 数值分支）。"""

    def test_no_active_roll_returns_current_unchanged(self):
        # guard：from==to 且 pct 亦相等 —— 原样返回、finished=True，绝不覆盖 _disp_*
        p, pct, fin = price_interp(10.0, 0.5, 10.0, 10.0, 0.5, 0.5, 100.0, 0.2, 200.0)
        self.assertEqual((p, pct), (10.0, 0.5))
        self.assertTrue(fin)

    def test_finished_at_or_after_anim_start_plus_dur(self):
        p, pct, fin = price_interp(0.0, 0.0, 10.0, 10.5, 0.0, 0.5, 100.0, 0.2, 100.2)
        self.assertEqual((p, pct), (10.5, 0.5))
        self.assertTrue(fin)

    def test_mid_frame_equals_ease_out_cubic_formula(self):
        # 中间帧必须与公式逐位一致：from + (to-from) * (1-(1-t)^3)
        start = 100.0
        p, pct, fin = price_interp(0.0, 0.0, 10.0, 10.5, 0.0, 0.5, 100.0, 0.2, 100.1)
        t = (100.1 - 100.0) / 0.2            # 0.5
        e = ease_out_cubic(t)
        self.assertAlmostEqual(p, 10.0 + 0.5 * e)
        self.assertAlmostEqual(pct, 0.0 + 0.5 * e)
        self.assertFalse(fin)

    def test_frame_monotonic_between_from_and_to(self):
        # 中间帧永远落在 [from, to] 区间内且随 t 单调
        prev = None
        for now in (100.01, 100.05, 100.1, 100.15, 100.19):
            p, _pct, fin = price_interp(0.0, 0.0, 10.0, 12.0, 0.0, 1.0, 100.0, 0.2, now)
            self.assertGreaterEqual(p, 10.0)
            self.assertLessEqual(p, 12.0)
            if prev is not None:
                self.assertGreaterEqual(p, prev)
            prev = p

    def test_endpoint_convergence_across_frames(self):
        # 多帧推演：最终一帧恰好等于目标（无漂移）
        start, dur, to_price, to_pct = 0.0, 0.2, 12.34, 1.23
        disp_p, disp_pct = 11.0, 0.5
        for t in (0.1, 0.15, 0.19, 0.2, 0.3):
            disp_p, disp_pct, fin = price_interp(
                disp_p, disp_pct, 11.0, to_price, 0.5, to_pct, start, dur, t)
            if t >= 0.2:
                self.assertEqual(disp_p, to_price)
                self.assertEqual(disp_pct, to_pct)
                self.assertTrue(fin)
        self.assertTrue(disp_p, to_price)


class TestLayoutAnimHeight(unittest.TestCase):
    """收展布局高度插值一帧（原 _anim_tick_inner 布局分支）。"""

    def test_finished_returns_target(self):
        h, fin = layout_anim_height(300, 147, 0.0, 0.2, 0.2)
        self.assertEqual(h, 147)
        self.assertTrue(fin)

    def test_mid_frame_smoothstep_inout_cubic(self):
        h, fin = layout_anim_height(300, 147, 0.0, 0.2, 0.1)
        t = 0.1 / 0.2
        e = smoothstep_inout_cubic(t)
        self.assertEqual(h, int(300 + (147 - 300) * e))
        self.assertFalse(fin)

    def test_start_frame_is_from(self):
        h, fin = layout_anim_height(300, 147, 0.0, 0.2, 0.0)
        self.assertEqual(h, int(300))   # smoothstep(0)=0 → 高度=from
        self.assertFalse(fin)

    def test_smoothstep_endpoints_zero_velocity(self):
        # InOutCubic：端点处导数（差分）近似为 0，伸缩两端无速度突变（v1.2.19 语义）
        e0 = smoothstep_inout_cubic(0.0)
        e1 = smoothstep_inout_cubic(1.0)
        self.assertEqual(e0, 0.0)
        self.assertEqual(e1, 1.0)
        # 函数在 [0,1] 上单调递增
        prev = -1.0
        for i in range(11):
            e = smoothstep_inout_cubic(i / 10.0)
            self.assertGreaterEqual(e, prev)
            prev = e


class TestConcurrentLayoutAndPriceAnimation(unittest.TestCase):
    """第6轮：收展过程中价格动画仍须逐帧推进，不能结束后跳变。"""

    def test_price_progresses_while_layout_is_running(self):
        d = AnimDriver()
        d.begin_layout(300, 180, 0.0)
        d.anim_from_price, d.anim_to_price = 10.0, 11.0
        d.anim_from_pct, d.anim_to_pct = 0.0, 1.0
        d.anim_start, d.anim_dur = 0.0, 0.2
        actions = d.tick(False, None, 300, 200, False, False,
                         0.10, 10.0, 0.0, 0.2)
        kinds = [a[0] for a in actions]
        self.assertIn("resize", kinds, "收展中仍应更新高度")
        self.assertIn("price", kinds, "收展中价格也必须逐帧推进")
        price = next(a[1] for a in actions if a[0] == "price")
        self.assertGreater(price[0], 10.0)
        self.assertLess(price[0], 11.0)
        self.assertNotIn("price_finish", kinds)

    def test_layout_and_price_can_finish_same_frame(self):
        d = AnimDriver()
        d.begin_layout(300, 180, 0.0)
        d.anim_from_price, d.anim_to_price = 10.0, 11.0
        d.anim_from_pct, d.anim_to_pct = 0.0, 1.0
        d.anim_start, d.anim_dur = 0.0, 0.2
        actions = d.tick(False, None, 300, 200, False, False,
                         0.21, 10.0, 0.0, 0.2)
        kinds = [a[0] for a in actions]
        self.assertIn("layout_minmax", kinds)
        self.assertIn("price_finish", kinds)


class TestCrossedKeyLevel(unittest.TestCase):
    """向下跌破 50 倍整数关卡判定（原 _crossed_key_level）。"""

    def test_cross_400_triggers(self):
        self.assertTrue(crossed_key_level(404, 399.99))   # 404→399.99 跨过 400

    def test_cross_multiple_levels_triggers(self):
        self.assertTrue(crossed_key_level(500, 449.0))    # 500→449 跨过 500 与 450 两级
        self.assertTrue(crossed_key_level(499.9, 448.0))  # 跨过 450
        self.assertTrue(crossed_key_level(451, 449.0))    # 451→449 仅跨过 450

    def test_ordinary_integer_not_triggers(self):
        self.assertFalse(crossed_key_level(410, 409.99))  # 破 410（非 50 倍数）不触发

    def test_uptick_not_triggers(self):
        self.assertFalse(crossed_key_level(399.99, 404))  # 上涨不触发

    def test_below_first_level_no_level_inside(self):
        self.assertFalse(crossed_key_level(30, 29.9))     # (29.9,30] 内无 50 倍数

    def test_exact_equal_not_triggers(self):
        self.assertFalse(crossed_key_level(404, 404))     # new >= old

    def test_same_50_bin_not_triggers(self):
        self.assertFalse(crossed_key_level(455, 451))     # 都在 (450,500] 区间内


class TestApplyQuoteGuards(unittest.TestCase):
    """_apply_quote 动画分支守卫与参数决策（搬移后纯逻辑一致性）。"""

    def test_quote_changed_threshold(self):
        self.assertFalse(quote_changed(10.0, 0.5, 10.0, 0.5))
        self.assertFalse(quote_changed(10.0 + 1e-10, 0.5, 10.0, 0.5))   # 微差 < 1e-9
        self.assertTrue(quote_changed(10.01, 0.5, 10.0, 0.5))           # 价格变
        self.assertTrue(quote_changed(10.0, 0.51, 10.0, 0.5))           # 涨跌幅变

    def test_is_tiny_move(self):
        self.assertTrue(is_tiny_move(0.001, 0.002, ANIM_TINY_PRICE))
        self.assertFalse(is_tiny_move(0.02, 0.0, ANIM_TINY_PRICE))      # 价格超阈值
        self.assertFalse(is_tiny_move(0.0, 0.02, ANIM_TINY_PRICE))      # 涨跌幅超阈值
        self.assertFalse(is_tiny_move(0.02, 0.02, ANIM_TINY_PRICE))

    def test_anim_duration_ms_decision(self):
        # delta_pct >= ANIM_LARGE_PCT(3) 用大幅时长，否则常规时长
        self.assertEqual(anim_duration_ms(3.0, ANIM_LARGE_PCT, ANIM_LARGE_MS, ANIM_NORMAL_MS), ANIM_LARGE_MS)
        self.assertEqual(anim_duration_ms(5.0, ANIM_LARGE_PCT, ANIM_LARGE_MS, ANIM_NORMAL_MS), ANIM_LARGE_MS)
        self.assertEqual(anim_duration_ms(0.5, ANIM_LARGE_PCT, ANIM_LARGE_MS, ANIM_NORMAL_MS), ANIM_NORMAL_MS)
        self.assertEqual(anim_duration_ms(2.99, ANIM_LARGE_PCT, ANIM_LARGE_MS, ANIM_NORMAL_MS), ANIM_NORMAL_MS)

    def test_constants_sync(self):
        # 与主窗 _apply_quote 引用的一致性（防改名漂移）
        # v1.3.45：手感回调 —— 常规 100ms / 大幅 140ms（v1.3.43 的 80/130 偏急）
        self.assertAlmostEqual(ANIM_TINY_PRICE, 0.01)
        self.assertEqual(ANIM_LARGE_PCT, 3)
        self.assertEqual(ANIM_LARGE_MS, 140)
        self.assertEqual(ANIM_NORMAL_MS, 100)

    def test_large_move_still_slower_than_normal(self):
        """回归保护：大幅变动仍要比常规变动慢一点，避免 3% 以上跳变太突兀。"""
        self.assertGreater(ANIM_LARGE_MS, ANIM_NORMAL_MS)


if __name__ == "__main__":
    unittest.main()
