# -*- coding: utf-8 -*-
"""v1.2.95 第4轮走查：分时图对脏数据的健壮性。

背景：分时统计此前直接对原始点值 min()/max()，同文件其他统计（最高/最低）
早有 math.isfinite 保护，唯独这里漏了。实测分时数据里出现 inf 时：
  lo/hi 变成 ∓inf → 归一化与 y 坐标退化为 nan → int(nan) 抛 ValueError
  → paintEvent 兜底吞掉 → **整块分时图从界面上消失**（且每秒记数十条日志）。

本组用例锁死：无论输入多脏，分时图的所有坐标输出必须是有限数。
"""
import math
import unittest

from PyQt6.QtCore import QRect

import sf_ui_render as R


def _obj(quote=None, pts=None):
    """构造一个只带分时绘制所需属性的 RenderMixin 裸对象（不建真实窗口）。"""
    o = R.RenderMixin.__new__(R.RenderMixin)
    o.quote = quote if quote is not None else {"pre_close": 10.0}
    o.minute_points = pts if pts is not None else []
    o._trend_color = lambda: "#ff0000"
    o._line_ver = 0
    o._chart_cache_key = None
    o._chart_cache_xs = None
    o._chart_cache_np = None
    o._chart_cache_na = None
    return o


RECT = QRect(0, 0, 200, 80)

_INF = float("inf")
_NAN = float("nan")

_DIRTY_SETS = [
    ("含 inf", [("0930", 10.1), ("0931", _INF)]),
    ("含 -inf", [("0930", -_INF), ("0931", 10.1)]),
    ("含 nan", [("0930", 10.1), ("0931", _NAN)]),
    ("全 nan", [("0930", _NAN), ("0931", _NAN)]),
    ("全 inf", [("0930", _INF), ("0931", -_INF)]),
    ("非数值", [("0930", "abc"), ("0931", None)]),
    ("空列表", []),
]


class TestChartMetrics(unittest.TestCase):

    def test_range_is_finite_for_every_dirty_input(self):
        for name, pts in _DIRTY_SETS:
            o = _obj(pts=pts)
            m = R.RenderMixin._chart_metrics(o, pts, RECT)
            self.assertTrue(math.isfinite(m["lo"]) and math.isfinite(m["hi"]),
                            "%s：lo/hi 必须是有限数，实得 %r / %r" % (name, m["lo"], m["hi"]))
            self.assertGreater(m["hi"], m["lo"], "%s：区间不得退化成零宽度" % name)

    def test_good_data_unchanged(self):
        """正常数据的取值不能被防御逻辑改动。"""
        pts = [("0930", 10.0), ("0931", 11.0)]
        o = _obj(pts=pts)
        m = R.RenderMixin._chart_metrics(o, pts, RECT)
        self.assertAlmostEqual(m["lo"], 9.9, places=6)
        self.assertAlmostEqual(m["hi"], 11.1, places=6)


class TestChartY(unittest.TestCase):

    def test_y_is_finite_for_dirty_price(self):
        pts = [("0930", 10.1), ("0931", 10.2)]
        o = _obj(pts=pts)
        m = R.RenderMixin._chart_metrics(o, pts, RECT)
        for bad in (_INF, -_INF, _NAN, "abc", None):
            y = R.RenderMixin._chart_y(o, RECT, m, bad)
            self.assertTrue(math.isfinite(y),
                            "价格 %r 得到的 y=%r 不是有限数（int() 会抛异常）" % (bad, y))

    def test_y_finite_even_if_metrics_are_corrupt(self):
        """万一 lo/hi 本身也是脏的，仍不得产生非法坐标。"""
        o = _obj()
        for m in ({"lo": _NAN, "hi": _NAN, "pre_close": None},
                  {"lo": _INF, "hi": -_INF, "pre_close": None},
                  {"lo": 5.0, "hi": 5.0, "pre_close": None}):
            y = R.RenderMixin._chart_y(o, RECT, m, 10.0)
            self.assertTrue(math.isfinite(y), "指标被污染时 y 仍须有限，实得 %r" % (y,))

    def test_y_within_rect(self):
        pts = [("0930", 10.0), ("0931", 11.0)]
        o = _obj(pts=pts)
        m = R.RenderMixin._chart_metrics(o, pts, RECT)
        for price in (10.0, 10.5, 11.0, 999.0, -999.0):
            y = R.RenderMixin._chart_y(o, RECT, m, price)
            self.assertGreaterEqual(y, RECT.top() - 1e-6)
            self.assertLessEqual(y, RECT.top() + RECT.height() + 1e-6)


class TestClampAndCache(unittest.TestCase):

    def test_clamp_price(self):
        pts = [("0930", 10.0), ("0931", 11.0)]
        o = _obj(pts=pts)
        m = R.RenderMixin._chart_metrics(o, pts, RECT)
        self.assertAlmostEqual(R.RenderMixin._clamp_price(o, 10.5, m), 10.5)
        # 非有限值回落到昨收（比"贴着图框边缘"更贴近真实含义），再夹进区间
        self.assertAlmostEqual(R.RenderMixin._clamp_price(o, _INF, m), 10.0)
        self.assertAlmostEqual(R.RenderMixin._clamp_price(o, -_INF, m), 10.0)
        for bad in (_NAN, "abc", None, object()):
            v = R.RenderMixin._clamp_price(o, bad, m)
            self.assertTrue(math.isfinite(v), "脏值 %r 未被夹成有限数" % (bad,))
            self.assertGreaterEqual(v, m["lo"] - 1e-9)
            self.assertLessEqual(v, m["hi"] + 1e-9)

    def test_cache_normalized_values_are_finite(self):
        """均价为 inf 时，归一化数组不得出现 nan/inf。"""
        pts = [("0930", 10.0, _INF), ("0931", 10.5, _NAN), ("0932", 11.0, 10.4)]
        o = _obj(pts=pts)
        m = R.RenderMixin._chart_metrics(o, pts, RECT)
        xs, np_, na = R.RenderMixin._chart_pts_cache(o, RECT, m)
        for arr, label in ((xs, "xs"), (np_, "np"), (na, "na")):
            for v in arr:
                self.assertTrue(math.isfinite(v),
                                "%s 中出现非有限值 %r（会导致 int() 抛异常）" % (label, v))


if __name__ == "__main__":
    unittest.main()
