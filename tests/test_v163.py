# -*- coding: utf-8 -*-
"""实时股价 v1.2.63 回归测试：修复「涨跌幅时不时跑到下方去」。

BUG 复现与根因（量化，见 commit 说明）：
    v1.2.61 把涨跌幅移到股价右侧，判定条件是「价格文本右侧的剩余零头够不够」
    （pct_w <= pct_right_avail）。但价格字号自适应本就把预算吃满
    （price_w >= 价格文本宽），零头恒 <= -(PAD_X + PCT_GAP) = -18px，
    判定几乎必然失败 → 回落到 Y_PCT（下方）。实测回落率：
        w=200: 100%   w=206: 96%   w=212: 88%   w=230: 56%   w=240: 35%
    v1.2.62 收窄窗口后预算从 ~148 掉到 ~118，回落成为常态，表现为「时不时」。

修法（本版本的契约，由此测试锁定）：
    1. 价格行右侧边界由 W_PRICE(82) 改为 PRICE_ROW_RIGHT_PAD(PAD_X=12) ——
       价格行（Y 56~84）与顶部按钮区（Y 11~23）垂直不重叠，无需为按钮让出 82px。
    2. 布局收敛到 renderer.price_pct_layout 唯一实现，**先扣除涨跌幅预算再自适应
       价格字号** —— 价格主动让位，右侧空间恒定充足，位置由数据唯一确定。
    3. 删除「回落到 Y_PCT 下方」的分支，涨跌幅恒在价格行右侧。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sf_constants import APP_VERSION, VERSION_TEXT
import sf_ui_render as R
from renderer import price_pct_layout
from PyQt6.QtGui import QGuiApplication, QFont, QFontMetrics

# 部分布局工具需要 QGuiApplication 才能取到字体度量
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_APP = QGuiApplication.instance() or QGuiApplication(sys.argv)
R.load_bundled_fonts()


def _measure_fn(price):
    """构造 measure(size) -> 像素宽度（与 _paint_price 注入的度量一致）。"""
    def measure(size):
        return QFontMetrics(QFont(R.NUM_FONT, size, QFont.Weight.Bold)).horizontalAdvance(price)
    return measure


def _pct_w(pct):
    return QFontMetrics(QFont(R.NUM_FONT, R.FONT_PCT, QFont.Weight.Bold)).horizontalAdvance(pct)


def _badge_w():
    """徽标宽度：走 R.badge_width 唯一实现（v1.2.72）。

    此前测试直接引用常量 _badge_w()(=34)，而生产绘制用的是动态宽度
    （实测 26px）—— 测试与生产对同一物理量各持一份，测试因此**测不到**
    "布局判定比实际宽导致徽标被误丢弃"这个 BUG。改为与生产同源后，
    测试才能真正守住该不变量。
    """
    fm = QFontMetrics(QFont(R.FONT, R.BADGE_FONT_SIZE, QFont.Weight.Light))
    return R.badge_width("涨停", fm)


def _layout(w, price, pct, badge=False):
    """用真实字体度量布局（用于断言"不重叠/不越界"这类平台无关的不变量）。"""
    return price_pct_layout(w, _measure_fn(price), _pct_w(pct),
                            pad_x=R.PAD_X, right_pad=R.PRICE_ROW_RIGHT_PAD,
                            pct_gap=R.PCT_GAP,
                            badge_w=(_badge_w() if badge else 0), badge_gap=4,
                            size_max=R.FONT_PRICE_MAX, size_min=R.FONT_PRICE_MIN, step=2)


def _synth_layout(w, measure, pct_w, badge=False):
    """注入**合成度量**布局：不经过 QFontMetrics，结果与运行平台无关。

    用于测试降级链等纯算术决策（"空间是否不足"在真实字体下会随
    offscreen/cocoa 平台漂移，见 test_badge_almost_never_dropped 说明）。
    """
    return price_pct_layout(w, measure, pct_w,
                            pad_x=R.PAD_X, right_pad=R.PRICE_ROW_RIGHT_PAD,
                            pct_gap=R.PCT_GAP,
                            badge_w=(_badge_w() if badge else 0), badge_gap=4,
                            size_max=R.FONT_PRICE_MAX, size_min=R.FONT_PRICE_MIN, step=2)


# 覆盖：常见价（2 位整数/3 位整数/带小数）、极端价（5 位整数）、
#       涨跌幅（常规/两位整数/到百分位/100%）
PRICES = ["0.85", "8.50", "18.88", "38.20", "100.00", "119.00",
          "1190.00", "1234.56", "12345.67", "99999.99"]
PCTS = ["+0%", "+0.5%", "-0.05%", "-4.23%", "+10%", "-10.00%", "+100%", "-100.00%"]
WIDTHS = [180, 200, 206, 212, 220, 230, 240, 260]




class TestConstants(unittest.TestCase):
    """价格行右边界常量：放宽到 PAD_X，且不得回退到 W_PRICE。"""

    def test_price_row_right_pad_equals_pad_x(self):
        self.assertEqual(R.PRICE_ROW_RIGHT_PAD, R.PAD_X)

    def test_right_pad_is_wider_than_w_price(self):
        """右边界必须比旧 W_PRICE 宽，否则涨跌幅空间不足的 BUG 会回归。"""
        self.assertLess(R.PRICE_ROW_RIGHT_PAD, R.W_PRICE)

    def test_main_width_is_ninety_two_percent_of_original(self):
        from sf_constants import W
        self.assertEqual(W, 184)

    def test_price_row_band_does_not_overlap_button_band(self):
        """放宽右边界的前提：价格行与顶部按钮区垂直不重叠。"""
        price_top, price_bottom = R.Y_PRICE, R.Y_PRICE + R.H_PRICE
        btn_top, btn_bottom = R.BTN_ICON_Y, R.BTN_ICON_Y + R.BTN_ICON_SZ
        self.assertTrue(price_top >= btn_bottom or price_bottom <= btn_top,
                        "价格行与按钮区垂直重叠，PRICE_ROW_RIGHT_PAD 放宽不再成立")


class TestPricePctLayoutNoFallback(unittest.TestCase):
    """核心回归：涨跌幅恒在价格行右侧，绝无回落到下方。"""

    def test_always_on_right_and_never_overflow(self):
        """全组合遍历：on_right 恒 True，且右缘不越过价格行右边界。"""
        checked = 0
        for w in WIDTHS:
            for price in PRICES:
                for pct in PCTS:
                    for badge in (False, True):
                        lay = _layout(w, price, pct, badge)
                        checked += 1
                        self.assertTrue(
                            lay["on_right"],
                            "涨跌幅未落在价格行右侧: w=%s price=%s pct=%s badge=%s"
                            % (w, price, pct, badge))
                        self.assertLessEqual(
                            lay["pct_x"] + lay["pct_w"], lay["right_limit"] + 0.01,
                            "涨跌幅越过右侧安全边: w=%s price=%s pct=%s badge=%s"
                            % (w, price, pct, badge))
                        self.assertGreaterEqual(
                            lay["pct_x"], lay["price_x"],
                            "涨跌幅起点跑到价格起点左侧: w=%s price=%s pct=%s" % (w, price, pct))
        self.assertGreaterEqual(checked, 500, "组合覆盖不足")

    def test_pct_starts_after_price_text(self):
        """无徽标时涨跌幅起点 = 价格起点 + 价格文本宽 + PCT_GAP。"""
        for w in (200, 230, 260):
            for price in ("8.50", "119.00", "1234.56"):
                lay = _layout(w, price, "-4.23%")
                self.assertAlmostEqual(
                    lay["pct_x"], lay["price_x"] + lay["price_w"] + R.PCT_GAP, places=6)

    def test_pct_ignores_badge_horizontally(self):
        """v1.2.72：徽标移到涨跌幅正上方，不再占水平空间，涨跌幅起点与有无徽标无关。"""
        for price in ("8.50", "119.00", "1234.56"):
            lay_no = _layout(260, price, "-10.00%", badge=False)
            lay_yes = _layout(260, price, "-10.00%", badge=True)
            self.assertIsNotNone(lay_yes["badge_x"])
            self.assertAlmostEqual(lay_yes["pct_x"], lay_no["pct_x"], places=6,
                                   msg="涨跌幅起点不应随徽标水平移动: price=%s" % price)

    def test_badge_left_aligned_above_pct(self):
        """v1.2.72 样式调整：徽标左对齐于涨跌幅文本区域起始位置（badge_x == pct_x）。"""
        for w in WIDTHS:
            for price in PRICES:
                lay = _layout(w, price, "-10.00%", badge=True)
                if lay["badge_x"] is None:
                    continue   # 极端场景徽标被丢弃（见 test_badge_almost_never_dropped）
                # 左对齐：badge_x 必须等于 pct_x
                self.assertAlmostEqual(lay["badge_x"], lay["pct_x"], places=6,
                                       msg="徽标须左对齐涨跌幅: w=%s price=%s" % (w, price))
                # 右边缘不越出涨跌幅区域
                self.assertLessEqual(lay["badge_x"] + _badge_w(),
                                     lay["pct_x"] + lay["pct_avail"] + 0.01)


class TestNoOverlapAnySize(unittest.TestCase):
    """QC 5.7 永久硬性约束：任何窗口尺寸下信息元素都不得相互堆叠覆盖。"""

    def _rect(self, x, w):
        return (x, x + w)

    def _overlap(self, a, b):
        if a is None or b is None:
            return False
        return a[0] < b[1] and b[0] < a[1]

    def test_price_badge_pct_disjoint_in_price_axis(self):
        """价格与徽标、价格与涨跌幅 均不重叠且不越界（全组合 864 例）。

        v1.2.72：徽标移到涨跌幅正上方居中，与涨跌幅在水平方向允许重叠
        （垂直堆叠），故此处只锁定「价格」与二者的水平不重叠这一不变量；
        徽标与涨跌幅的水平居中由 test_badge_centered_above_pct 锁定。
        """
        checked = 0
        for w in WIDTHS:
            for price in PRICES:
                for pct in PCTS:
                    for badge in (False, True):
                        lay = _layout(w, price, pct, badge)
                        checked += 1
                        r_price = self._rect(lay["price_x"], lay["price_w"])
                        r_pct = self._rect(lay["pct_x"], lay["pct_w"])
                        r_badge = (self._rect(lay["badge_x"], _badge_w())
                                   if lay["badge_x"] is not None else None)
                        self.assertFalse(
                            self._overlap(r_price, r_pct),
                            "价格×涨跌幅重叠: w=%s price=%s pct=%s badge=%s"
                            % (w, price, pct, badge))
                        self.assertFalse(
                            self._overlap(r_price, r_badge),
                            "价格×徽标重叠: w=%s price=%s pct=%s badge=%s"
                            % (w, price, pct, badge))
                        # 注：徽标居中于涨跌幅正上方（v1.2.72），与涨跌幅在水平方向
                        # 允许重叠（垂直堆叠），此处不再断言两者水平不重叠。
                        for name, r in (("价格", r_price), ("徽标", r_badge),
                                        ("涨跌幅", r_pct)):
                            if r is None:
                                continue
                            self.assertGreaterEqual(r[0], R.PAD_X - 0.01,
                                                    "%s 左越界: %s" % (name, (w, price, pct, badge)))
                            self.assertLessEqual(r[1], lay["right_limit"] + 0.01,
                                                 "%s 右越界: %s" % (name, (w, price, pct, badge)))
        self.assertGreaterEqual(checked, 800, "组合覆盖不足")

    def test_badge_almost_never_dropped(self):
        """v1.2.72：徽标移到涨跌幅上方后不再占水平预算，几乎永不丢弃。

        旧的"空间不足丢弃徽标"语义已失效：徽标宽度只与涨跌幅文本区域比较，
        而涨跌幅区域宽度恒定 >= 涨跌幅文本宽（预算先扣涨跌幅的保证），
        对常规涨跌幅（宽于徽标 34px）徽标恒保留。仅当 pct 文本像素宽 < 徽标宽
        （ pathological 短串）才可能丢弃，现实行情不会出现。

        **为什么用合成度量而非真实字体**：同 test_badge_kept_and_centered，
        注入确定性度量锁定算法行为，避免 offscreen/cocoa 平台字体度量漂移。
        """
        lay = _synth_layout(w=200, measure=lambda size: size * 4, pct_w=70, badge=True)
        self.assertIsNotNone(lay["badge_x"],
                             "徽标应保留：它不再挤占价格/涨跌幅的水平空间")
        self.assertGreaterEqual(lay["pct_x"], lay["price_x"] + lay["price_w"],
                                "保留徽标后涨跌幅仍须紧贴价格且不重叠")

    def test_badge_kept_and_left_aligned(self):
        """空间充足时徽标必须保留且左对齐于涨跌幅区域（降级不得误伤），合成度量保证确定性。"""
        for w in (240, 260, 300):
            lay = _synth_layout(w=w, measure=lambda size: size * 4, pct_w=40, badge=True)
            self.assertIsNotNone(lay["badge_x"],
                                 "空间充足时不应丢弃徽标: w=%s" % w)
            self.assertFalse(
                lay["badge_x"] < lay["price_x"] + lay["price_w"] and
                lay["price_x"] < lay["badge_x"] + _badge_w(),
                "徽标不得与价格重叠: w=%s" % w)
            # 左对齐于涨跌幅区域（badge_x == pct_x）
            self.assertAlmostEqual(lay["badge_x"], lay["pct_x"], places=6,
                                   msg="徽标须左对齐涨跌幅: w=%s" % w)


class TestPricePctLayoutBudget(unittest.TestCase):
    """预算不变量：先扣涨跌幅再定字号 —— 价格字号不得突破声明下限/上限。"""

    def test_budget_reserves_pct_and_badge(self):
        w, pct = 200, "-10.00%"
        lay = _layout(w, "119.00", pct)
        expect = (w - R.PRICE_ROW_RIGHT_PAD) - R.PAD_X - (_pct_w(pct) + R.PCT_GAP)
        self.assertAlmostEqual(lay["budget"], expect, places=6)

    def test_budget_excludes_badge(self):
        """v1.2.72：徽标在涨跌幅上方，不占水平预算；带徽标与不带徽标的预算应一致。"""
        w, pct = 200, "-10.00%"
        lay_no = _layout(w, "119.00", pct, badge=False)
        lay_yes = _layout(w, "119.00", pct, badge=True)
        expect = ((w - R.PRICE_ROW_RIGHT_PAD) - R.PAD_X
                  - (_pct_w(pct) + R.PCT_GAP))
        self.assertAlmostEqual(lay_no["budget"], expect, places=6)
        self.assertAlmostEqual(lay_yes["budget"], expect, places=6,
                               msg="带徽标预算不应再扣徽标")

    def test_size_within_declared_bounds(self):
        for w in WIDTHS:
            for price in PRICES:
                for pct in PCTS:
                    for badge in (False, True):
                        lay = _layout(w, price, pct, badge)
                        self.assertGreaterEqual(lay["size"], R.FONT_PRICE_MIN)
                        self.assertLessEqual(lay["size"], R.FONT_PRICE_MAX)

    def test_price_fits_budget_then_never_overlaps(self):
        """价格在预算内时，涨跌幅与价格绝不重叠。"""
        for w in (200, 220, 240, 260):
            for price in ("8.50", "119.00", "1190.00", "1234.56"):
                for pct in PCTS:
                    lay = _layout(w, price, pct)
                    if lay["price_w"] <= lay["budget"] + 0.01:
                        self.assertGreaterEqual(
                            lay["pct_x"], lay["price_x"] + lay["price_w"],
                            "价格与涨跌幅重叠: w=%s price=%s pct=%s" % (w, price, pct))

    def test_narrow_window_bottoms_out_at_min_size(self):
        """极窄窗口 + 超长价格：字号触底且不越界（兜底夹紧）。"""
        lay = _layout(180, "12345.67", "-100.00%", badge=True)
        self.assertGreaterEqual(lay["size"], R.FONT_PRICE_MIN)
        self.assertLessEqual(lay["pct_x"] + lay["pct_w"], lay["right_limit"] + 0.01)


class TestPricePctLayoutDeterminism(unittest.TestCase):
    """决定性：同一输入必得同一输出 —— 位置不再随"零头"抖动。"""

    def test_same_input_same_output(self):
        for _ in range(3):
            a = _layout(200, "119.00", "-4.23%")
            b = _layout(200, "119.00", "-4.23%")
            self.assertEqual(a, b)

    def test_pct_x_monotonic_in_price_width(self):
        """价格文本越宽，涨跌幅起点越靠右（线性跟随，无跳变）。"""
        prev = -1.0
        for price in ("1.00", "8.50", "18.88", "119.00", "1190.00", "1234.56"):
            lay = _layout(260, price, "-4.23%")
            self.assertGreater(lay["pct_x"], prev,
                               "涨跌幅起点未随价格文本宽度单调递增: price=%s" % price)
            prev = lay["pct_x"]

    def test_pct_x_stable_across_window_widths_when_price_short(self):
        """价格短时涨跌幅位置与窗口宽度无关（贴着价格走，不随宽度漂移）。"""
        xs = [_layout(w, "8.50", "-4.23%")["pct_x"] for w in (200, 220, 240, 260)]
        self.assertTrue(all(abs(x - xs[0]) < 0.01 for x in xs),
                        "涨跌幅位置随窗口宽度漂移: %s" % (xs,))


class TestSourceContract(unittest.TestCase):
    """源码契约：_paint_price 不得再出现回落到 Y_PCT 的分支。"""

    def setUp(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "sf_ui_render.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_no_y_pct_fallback_branch(self):
        self.assertNotIn('pct_rect = QRectF(PAD_X, Y_PCT', self.src,
                         "回落到 Y_PCT 的分支不应存在 —— 涨跌幅须恒在价格行右侧")

    def test_uses_shared_layout(self):
        self.assertIn("price_pct_layout(", self.src,
                      "_paint_price 必须走 renderer.price_pct_layout 唯一实现")

    def test_no_ad_hoc_price_width(self):
        self.assertNotIn("price_w = w - W_PRICE", self.src,
                         "价格可用宽度不得再按 W_PRICE 现算（已由统一布局给出预算）")


if __name__ == "__main__":
    unittest.main()
