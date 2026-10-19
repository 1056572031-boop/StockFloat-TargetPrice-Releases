# -*- coding: utf-8 -*-
"""实时股价 v1.2.48 动画驱动状态机单测（上帝类拆分第四步 sf_anim_driver 侧验证）。

覆盖 AnimDriver（由 QTimer 驱动的 UI 层动画帧逻辑，不依赖 Qt）：
1. begin_layout：收展布局动画状态登记
2. tick 拉伸分支：_resizing 期间稳定帧率单次应用目标高度
3. tick 布局动画分支：完成帧 min/max 收口 + 玻璃 mask 对齐；中间帧 resize+update
4. tick 价格 guard：无进行中滚动（from==to 且 pct 相等）直接跳过、绝不覆盖
5. tick 价格完成帧：price_finish（停表/清滚动目标由主窗执行）
6. tick 价格中间帧：price_mid（局部条带重绘 / 整窗重绘决策数据传递）
7. start_price_flash / flash_tick：破位白闪起始与相位翻转、到期复位
8. check_drop_flash：两档阈值穿越判定与独立冷却
9. start_drop_flash / drop_flash_tick：下跌白闪相位计数与用尽复位
10. reset_flash：切股复位（截止/相位/计数/冷却字典）

运行方式（在 StockFloat 目录）：
    python3 -m unittest tests/test_anim_driver.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sf_anim_driver import AnimDriver


def _priced_driver():
    """构造一台「进行中价格滚动」的 driver：from=10.0→to=12.0，pct 0.0→1.0，
    start=100.0 / dur=0.2。tick 时 now 传 100.x 即处于中间帧。"""
    d = AnimDriver()
    d.anim_from_price, d.anim_to_price = 10.0, 12.0
    d.anim_from_pct, d.anim_to_pct = 0.0, 1.0
    d.anim_start, d.anim_dur = 100.0, 0.2
    return d


class TestBeginLayout(unittest.TestCase):
    def test_registers_state(self):
        d = AnimDriver()
        d.begin_layout(300, 147, 5.0)
        self.assertEqual(d.layout_anim_from, 300)
        self.assertEqual(d.layout_anim_to, 147)
        self.assertEqual(d.layout_anim_start, 5.0)


class TestTickResizing(unittest.TestCase):
    def test_resizing_applies_target_once(self):
        d = AnimDriver()
        acts = d.tick(True, 250, 300, 600, False, False, 100.0, 0.0, 0.0)
        self.assertEqual(acts, [("resize", (600, 250)), ("update",)])

    def test_resizing_at_target_skips(self):
        d = AnimDriver()
        acts = d.tick(True, 300, 300, 600, False, False, 100.0, 0.0, 0.0)
        self.assertEqual(acts, [])


class TestTickLayoutAnim(unittest.TestCase):
    def test_layout_start_frame_is_from(self):
        d = AnimDriver()
        d.begin_layout(300, 147, 100.0)
        acts = d.tick(False, None, 300, 600, False, False, 100.0, 0.0, 0.0)
        # smoothstep(0)=0 → 高度=from=300；未完成：无 minmax/glass_mask
        self.assertEqual(acts[0], ("resize", (600, 300)))
        self.assertEqual(acts[1], ("update",))
        self.assertEqual(len(acts), 2)

    def test_layout_finish_frame_minmax_and_mask(self):
        d = AnimDriver()
        d.begin_layout(300, 147, 100.0)
        # now=100.2 恰好 = start+dur → 完成帧
        acts = d.tick(False, None, 300, 600, True, False, 100.2, 0.0, 0.0)
        kinds = [a[0] for a in acts]
        self.assertEqual(kinds, ["layout_minmax", "resize", "update", "glass_mask"])
        self.assertEqual(acts[0], ("layout_minmax", True))     # 展开态收口
        self.assertEqual(acts[1], ("resize", (600, 147)))      # 目标高度
        # 完成帧状态归零
        self.assertEqual(d.layout_anim_from, 0)
        self.assertEqual(d.layout_anim_to, 0)

    def test_layout_finish_collapsed_minmax(self):
        d = AnimDriver()
        d.begin_layout(147, 300, 100.0)
        acts = d.tick(False, None, 147, 600, False, False, 100.2, 0.0, 0.0)
        self.assertEqual(acts[0], ("layout_minmax", False))    # 收起态锁 max

    def test_layout_in_progress_also_advances_price(self):
        # v1.2.72：收展与价格动画同帧推进，避免收展完成后数字直接跳到目标。
        d = _priced_driver()
        d.begin_layout(300, 200, 100.0)
        acts = d.tick(False, None, 300, 600, True, False, 100.1, 0.0, 0.0)
        kinds = [a[0] for a in acts]
        self.assertEqual(acts[0][0], "resize")
        self.assertIn("price", kinds)
        self.assertIn("price_mid", kinds)


class TestTickPriceGuard(unittest.TestCase):
    def test_no_active_roll_returns_empty(self):
        # from==to 且 pct 相等 → 无进行中滚动，绝不产出 price 动作（v1.2.38 语义）
        d = AnimDriver()
        acts = d.tick(False, None, 300, 600, True, False, 1000.0, 10.0, 0.5)
        self.assertEqual(acts, [])


class TestTickPriceFrame(unittest.TestCase):
    def test_mid_frame_produces_price_and_price_mid(self):
        d = _priced_driver()
        acts = d.tick(False, None, 300, 600, True, True, 100.1, 0.0, 0.0)
        kinds = [a[0] for a in acts]
        self.assertEqual(kinds, ["price", "price_mid"])
        price, pct = acts[0][1]
        self.assertGreater(price, 10.0)
        self.assertLess(price, 12.0)
        self.assertEqual(acts[1], ("price_mid", (True, 600)))   # chart_changed, width

    def test_finish_frame_produces_price_finish(self):
        d = _priced_driver()
        acts = d.tick(False, None, 300, 600, True, True, 100.2, 0.0, 0.0)
        kinds = [a[0] for a in acts]
        self.assertEqual(kinds, ["price", "price_finish"])
        self.assertEqual(acts[0], ("price", (12.0, 1.0)))       # 恰好落定


class TestPriceFlash(unittest.TestCase):
    def test_start_records_until_and_on(self):
        d = AnimDriver()
        d.start_price_flash(10.0, 0.6)
        self.assertAlmostEqual(d.price_flash_until, 10.6)
        self.assertTrue(d.price_flash_on)

    def test_flash_tick_before_deadline_flips_phase(self):
        # v1.2.65：首帧必须返回「起始相位亮」—— 旧实现先翻转再返回，
        # 置亮的起始相位在第一个 tick 就被翻成灭，白闪首帧被吞（少闪一次）。
        d = AnimDriver()
        d.start_price_flash(10.0, 0.6)
        active, on = d.flash_tick(10.3)
        self.assertTrue(active)
        self.assertTrue(on)                      # 首帧 = 起始相位（亮）
        active, on = d.flash_tick(10.31)
        self.assertTrue(active)
        self.assertFalse(on)                     # 亮→灭 翻转

    def test_flash_tick_after_deadline_resets(self):
        d = AnimDriver()
        d.start_price_flash(10.0, 0.6)
        d.price_flash_on = True
        active, on = d.flash_tick(10.7)
        self.assertFalse(active)
        self.assertFalse(on)


class TestDropFlashCheck(unittest.TestCase):
    LEVELS = [
        {"key": "soft", "pct": 3.0},
        {"key": "hard", "pct": 5.0},
    ]

    def test_hit_updates_cooldown_and_returns_true(self):
        d = AnimDriver()
        hit = d.check_drop_flash(-3.5, 10.0, self.LEVELS, 30.0)
        self.assertTrue(hit)
        self.assertIn("soft", d.flash_drop_last)
        self.assertEqual(d.flash_drop_last["soft"], 10.0)

    def test_hard_level_not_hit(self):
        d = AnimDriver()
        hit = d.check_drop_flash(-3.5, 10.0, self.LEVELS, 30.0)
        self.assertTrue(hit)
        self.assertNotIn("hard", d.flash_drop_last)   # 只命中 soft 档

    def test_within_cooldown_no_hit(self):
        d = AnimDriver()
        d.flash_drop_last["soft"] = 9.0              # 9s 前触发过
        hit = d.check_drop_flash(-3.5, 10.0, self.LEVELS, 30.0)
        self.assertFalse(hit)

    def test_no_hit_no_state_change(self):
        d = AnimDriver()
        hit = d.check_drop_flash(-2.0, 10.0, self.LEVELS, 30.0)
        self.assertFalse(hit)
        self.assertEqual(d.flash_drop_last, {})


class TestDropFlashPhase(unittest.TestCase):
    def test_start_sets_on_and_zero_count(self):
        d = AnimDriver()
        d.start_drop_flash()
        self.assertTrue(d.flash_drop_on)
        self.assertEqual(d.flash_drop_count, 0)

    def test_phase_alternation_and_exhaustion(self):
        # FLASH_DROP_PHASES=6（亮-灭-亮-灭-亮-灭 = 3 个来回）：start 即亮，
        # 每 tick 翻转一次；第 6 相位用尽复位为灭并停（active=False）。
        #
        # v1.2.65 第1轮走查修复：旧实现「先翻转再返回」，起始亮相位在首 tick
        # 即被翻灭，实际只闪 2 次白；而本用例注释写的是 3 个来回 —— 注释描述
        # 了正确意图，expect 却是从错误实现反推的，等于把 BUG 固化进了测试。
        # 现按设计意图断言：3 次亮（索引 0/2/4），末相位用尽后为灭。
        d = AnimDriver()
        d.start_drop_flash()
        expect = [True, False, True, False, True, False]   # 亮→灭→亮→灭→亮→灭
        for i, exp in enumerate(expect):
            active, on = d.drop_flash_tick(6)
            if i < 5:
                self.assertTrue(active)
            self.assertEqual(on, exp)
        self.assertEqual(d.flash_drop_count, 6)
        self.assertFalse(d.flash_drop_on)

    def test_drop_flash_blinks_three_times(self):
        """白闪次数是用户可感知的视觉指标：6 相位必须恰好闪 3 次白。

        直接用「亮相位计数」锁定，避免再出现「注释写 3 个来回、断言却是 2 次」。
        """
        d = AnimDriver()
        d.start_drop_flash()
        on_count = 0
        for _ in range(6):
            _active, on = d.drop_flash_tick(6)
            if on:
                on_count += 1
        self.assertEqual(on_count, 3, "6 相位应恰好闪 3 次白")

    def test_drop_flash_color_is_white(self):
        """下跌提醒亮相色必须为白色 #ffffff，不是红色。"""
        from sf_ui_render import FLASH_DROP_COLOR, RED
        # 白色 RGB 各分量均为 255
        self.assertEqual(FLASH_DROP_COLOR.red(), 255)
        self.assertEqual(FLASH_DROP_COLOR.green(), 255)
        self.assertEqual(FLASH_DROP_COLOR.blue(), 255)
        # 明确不等于趋势红色
        self.assertNotEqual(FLASH_DROP_COLOR, RED)


class TestTargetRainbow(unittest.TestCase):
    def test_start_and_expiry(self):
        d = AnimDriver()
        d.start_rainbow(10.0, 6.0)
        self.assertTrue(d.rainbow_active(10.0))
        self.assertTrue(d.rainbow_active(15.99))
        self.assertFalse(d.rainbow_active(16.0))

    def test_phase_moves_and_wraps(self):
        d = AnimDriver()
        d.start_rainbow(10.0, 6.0)
        self.assertAlmostEqual(d.rainbow_phase(10.0), 0.0)
        self.assertAlmostEqual(d.rainbow_phase(10.75), 0.25)
        self.assertAlmostEqual(d.rainbow_phase(11.5), 0.5)
        self.assertAlmostEqual(d.rainbow_phase(13.0), 0.0)
        self.assertAlmostEqual(d.rainbow_phase(14.5), 0.5)

    def test_strength_smoothly_enters_and_leaves(self):
        d = AnimDriver()
        d.start_rainbow(10.0)
        for now, expected in [(9.0, 0.0), (10.0, 0.0), (10.18, 0.5),
                              (10.36, 1.0), (13.0, 1.0), (15.35, 1.0),
                              (15.675, 0.5), (16.0, 0.0), (17.0, 0.0)]:
            with self.subTest(now=now):
                self.assertAlmostEqual(d.rainbow_strength(now), expected)

    def test_zero_negative_and_short_duration_are_bounded(self):
        d = AnimDriver()
        for duration in (0.0, -1.0, 0.1):
            d.start_rainbow(10.0, duration)
            for now in (9.0, 10.0, 10.025, 10.05, 10.1, 11.0):
                with self.subTest(duration=duration, now=now):
                    self.assertGreaterEqual(d.rainbow_strength(now), 0.0)
                    self.assertLessEqual(d.rainbow_strength(now), 1.0)

    def test_reset_and_restart_reset_the_fade(self):
        d = AnimDriver()
        d.start_rainbow(10.0)
        self.assertEqual(d.rainbow_strength(11.0), 1.0)
        d.reset_rainbow()
        self.assertEqual(d.rainbow_strength(11.0), 0.0)
        d.start_rainbow(11.0)
        self.assertEqual(d.rainbow_strength(11.0), 0.0)
        self.assertAlmostEqual(d.rainbow_strength(11.18), 0.5)

    def test_reset_clears_rainbow(self):
        d = AnimDriver()
        d.start_rainbow(10.0, 6.0)
        d.reset_rainbow()
        self.assertFalse(d.rainbow_active(10.1))
        self.assertEqual(d.rainbow_start, 0.0)
        self.assertEqual(d.rainbow_until, 0.0)


class TestResetFlash(unittest.TestCase):
    def test_reset_clears_all_states(self):
        d = AnimDriver()
        d.start_price_flash(10.0, 0.6)
        d.start_drop_flash()
        d.check_drop_flash(-3.5, 10.0, TestDropFlashCheck.LEVELS, 30.0)
        d.reset_flash()
        self.assertEqual(d.price_flash_until, 0.0)
        self.assertFalse(d.price_flash_on)
        self.assertFalse(d.flash_drop_on)
        self.assertEqual(d.flash_drop_count, 0)
        self.assertEqual(d.flash_drop_last, {})


if __name__ == "__main__":
    unittest.main()
