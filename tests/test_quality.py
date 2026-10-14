# -*- coding: utf-8 -*-
"""实时股价 v1.2.22 质量改进回归测试（20.2 圆角抽公共 / 20.3 分时横坐标抽公共 / 一般问题-停牌 0 值误判修复）。

运行方式（在 StockFloat 目录）：
    python3 -m unittest discover -s tests -v
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sf_ui_render import _chart_minute_offset
from renderer import _squircle_se_point
import sf_quote as q


class TestChartMinuteOffset(unittest.TestCase):
    """20.3 v1.2.22：分时横坐标唯一换算函数（_chart_x 与 _chart_pts_cache 共用）"""

    def test_open_0930_is_zero(self):
        self.assertEqual(_chart_minute_offset("0930", 0, 10), 0)

    def test_close_1500_is_240(self):
        self.assertEqual(_chart_minute_offset("1500", 9, 10), 240)

    def test_morning_session_linear(self):
        # 10:00 → 距 09:30 30 分钟
        self.assertEqual(_chart_minute_offset("1000", 0, 10), 30)

    def test_afternoon_session_offset(self):
        # 14:00 → 上午 120 分钟 + 距 13:00 60 分钟
        self.assertEqual(_chart_minute_offset("1400", 0, 10), 180)

    def test_session_boundaries(self):
        self.assertEqual(_chart_minute_offset("1130", 0, 10), 120)   # 上午尾
        self.assertEqual(_chart_minute_offset("1300", 0, 10), 120)   # 下午头

    def test_invalid_hhmm_falls_back(self):
        # 非法时间串回退按序号均分（与旧实现一致）
        self.assertEqual(_chart_minute_offset("", 5, 10), 5 * 240.0 / 9)
        self.assertEqual(_chart_minute_offset("ab:cd", 5, 10), 5 * 240.0 / 9)

    def test_clamped_range(self):
        # 越界值 clamp 到 0~240
        self.assertEqual(_chart_minute_offset("0700", 0, 10), 0)
        self.assertEqual(_chart_minute_offset("2359", 0, 10), 240)


class TestQuoteStatusZeroPlaceholder(unittest.TestCase):
    """v1.2.22 修复：腾讯在非交易时段/刚开盘对 limit_up/limit_down/volume 回 '0'
    占位，旧判定把 '0' 当有效字段误报停牌；现按解析值 >0 才判定。"""

    def _now_trading(self):
        return datetime.datetime(2026, 8, 24, 10, 0)  # 周一 10:00 交易时段

    def test_zero_placeholder_not_suspend(self):
        # price=0 且 limit_up/down/volume 均为 0 → 不应误判停牌（修复前会判"停牌"）
        qq = {"price": "0", "pre_close": "9.9", "volume": "0",
              "limit_up": "0", "limit_down": "0"}
        self.assertIsNone(q.quote_status(qq, self._now_trading()))

    def test_zero_placeholder_int_not_suspend(self):
        # 数值型 0 同样不误判
        qq = {"price": 0, "pre_close": 9.9, "volume": 0,
              "limit_up": 0, "limit_down": 0}
        self.assertIsNone(q.quote_status(qq, self._now_trading()))

    def test_real_suspend_still_detected(self):
        # 真停牌：limit_up 仍为有效价 → 正常判停牌
        qq = {"price": "0", "pre_close": "9.9", "volume": "0",
              "limit_up": "10.89", "limit_down": "8.91"}
        self.assertEqual(q.quote_status(qq, self._now_trading()), "停牌")

    def test_missing_fields_not_suspend(self):
        # 备用源无 volume/limit 字段 → 不误报
        qq = {"price": "0", "pre_close": "9.9"}
        self.assertIsNone(q.quote_status(qq, self._now_trading()))

    def test_limit_up_down_still_work(self):
        now = self._now_trading()
        self.assertEqual(
            q.quote_status({"price": "11.0", "pre_close": "9.9", "volume": "10000",
                            "limit_up": "11.0", "limit_down": "8.9"}, now), "涨停")
        self.assertEqual(
            q.quote_status({"price": "8.9", "pre_close": "9.9", "volume": "10000",
                            "limit_up": "11.0", "limit_down": "8.9"}, now), "跌停")


class TestSquircleSePoint(unittest.TestCase):
    """20.2 v1.2.22：超椭圆圆角点公共函数（Qt 路径与 CGPath 共用，验证几何性质）"""

    def test_symmetric_opposite_angle(self):
        # 对称性：t 与 t+pi 的点关于圆心中心对称
        cx, cy, r = 50.0, 30.0, 8.0
        x1, y1 = _squircle_se_point(cx, cy, 0.3, r)
        x2, y2 = _squircle_se_point(cx, cy, 0.3 + 3.141592653589793, r)
        self.assertAlmostEqual(x1 + x2, 2 * cx, places=6)
        self.assertAlmostEqual(y1 + y2, 2 * cy, places=6)

    def test_axis_points_on_bbox(self):
        # 四个轴点应落在圆心 ±r 的包围盒上（超椭圆参数化存在 ~1e-7 浮点噪声，容差 1e-6）
        cx, cy, r = 100.0, 60.0, 10.0
        x, y = _squircle_se_point(cx, cy, 0.0, r)
        self.assertAlmostEqual(x, cx + r, places=6)
        self.assertAlmostEqual(y, cy, places=6)
        x, y = _squircle_se_point(cx, cy, 3.141592653589793 / 2, r)
        self.assertAlmostEqual(x, cx, places=6)
        self.assertAlmostEqual(y, cy + r, places=6)

    def test_radius_scaling(self):
        # 半径线性缩放：同角度下点距圆心距离与 r 成比例
        r1, r2 = 5.0, 10.0
        x1, y1 = _squircle_se_point(0, 0, 0.7, r1)
        x2, y2 = _squircle_se_point(0, 0, 0.7, r2)
        self.assertAlmostEqual(x2, x1 * (r2 / r1), places=9)
        self.assertAlmostEqual(y2, y1 * (r2 / r1), places=9)


if __name__ == "__main__":
    unittest.main()
