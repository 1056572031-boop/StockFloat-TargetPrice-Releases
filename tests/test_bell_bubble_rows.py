# -*- coding: utf-8 -*-
"""铃铛气泡行数据（targets.bell_bubble_rows）：排序、百分比取舍、小数位、坏数据过滤。

纯函数，不碰 Qt。气泡怎么画、怎么摆见 test_target_bubble_text.py，
和主窗的接线见 test_target_bubble_live.py。
"""
from __future__ import annotations

import unittest

from targets import bell_bubble_rows
from tests._base import SFCase


def _a(direction, target, state="pending"):
    return {"direction": direction, "target": target, "state": state}


class BellBubbleRowsCase(SFCase):
    def test_两组各自按价位从高到低_与保存顺序无关(self):
        alerts = [_a("below", 58.5), _a("above", 61.3), _a("above", 63.0),
                  _a("above", 60.5, "hit"), _a("below", 59.4, "hit")]
        pending, hit = bell_bubble_rows(alerts, 60.0, 2)
        self.assertEqual([r[1] for r in pending], ["63.00", "61.30", "58.50"])
        self.assertEqual([r[1] for r in hit], ["60.50", "59.40"])
        self.assertEqual(bell_bubble_rows(list(reversed(alerts)), 60.0, 2), (pending, hit),
                         "换一种保存顺序，梯子必须完全一样")

    def test_方向标记跟着条目走(self):
        pending, _hit = bell_bubble_rows([_a("above", 63.0), _a("below", 57.0)], 60.0, 2)
        self.assertEqual([(r[0], r[1]) for r in pending], [(True, "63.00"), (False, "57.00")])

    def test_待触发行带正负号的一位小数百分比_已触发行留空(self):
        alerts = [_a("above", 63.0), _a("below", 57.0), _a("above", 60.5, "hit")]
        pending, hit = bell_bubble_rows(alerts, 60.0, 2)
        self.assertEqual([r[2] for r in pending], ["+5.0%", "-5.0%"])
        self.assertEqual([r[2] for r in hit], [""], "价格已越过目标，百分比没有意义")

    def test_现价不可信时百分比留空而不是写占位(self):
        for bad in (None, 0, -1, float("nan"), float("inf"), "x"):
            with self.subTest(price=bad):
                pending, _hit = bell_bubble_rows([_a("above", 63.0)], bad, 2)
                self.assertEqual(pending, [(True, "63.00", "")])

    def test_价位按品种小数位格式化(self):
        for decimals, expected in ((2, "4.62"), (3, "4.621")):
            with self.subTest(decimals=decimals):
                pending, _hit = bell_bubble_rows([_a("above", 4.621)], 4.5, decimals)
                self.assertEqual(pending[0][1], expected)

    def test_坏条目被跳过_不拖垮整块(self):
        alerts = [None, "x", {}, {"direction": "sideways", "target": 5},
                  _a("above", 0), _a("above", -3), _a("above", float("nan")),
                  _a("above", float("inf")), {"direction": "above"}, _a("above", "abc"),
                  _a("above", 61.3)]
        pending, hit = bell_bubble_rows(alerts, 60.0, 2)
        self.assertEqual(pending, [(True, "61.30", "+2.2%")])
        self.assertEqual(hit, [])

    def test_空输入(self):
        self.assertEqual(bell_bubble_rows([], 60.0), ([], []))
        self.assertEqual(bell_bubble_rows(None, 60.0), ([], []))

    def test_同价位保持保存顺序_梯子不在刷新间抖动(self):
        pending, _hit = bell_bubble_rows([_a("below", 60.0), _a("above", 60.0)], 59.0, 2)
        self.assertEqual([r[0] for r in pending], [False, True])

    def test_没写状态的条目按待触发算(self):
        pending, hit = bell_bubble_rows([{"direction": "above", "target": 63.0}], 60.0, 2)
        self.assertEqual(len(pending), 1)
        self.assertEqual(hit, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
