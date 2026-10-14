# -*- coding: utf-8 -*-
"""实时股价 v1.2.39 调度模块单测（上帝类拆分第一步落地的调度侧验证）。

覆盖 sf_scheduler 的轮询间隔决策：
1. compute_poll_interval：熔断正常/降级/放量放开 三态映射正确
2. 未知状态兜底 NORMAL（与拆分前 FloatWindow._poll_interval 行为一致）
3. instant_poll_interval：熔断关闭时固定高频 0.1s

运行方式（在 StockFloat 目录）：
    python3 -m unittest tests/test_scheduler.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_constants
from sf_scheduler import compute_poll_interval, instant_poll_interval
from sf_constants import (POLL_DEGRADED, POLL_INTERVAL_DEGRADED,
                          POLL_INTERVAL_NORMAL, POLL_INTERVAL_OPEN,
                          POLL_NORMAL, POLL_OPEN)


class TestComputePollInterval(unittest.TestCase):
    """熔断状态 → 交易时段轮询间隔 映射。"""

    def test_mapping(self):
        self.assertEqual(compute_poll_interval(POLL_NORMAL), POLL_INTERVAL_NORMAL)
        self.assertEqual(compute_poll_interval(POLL_DEGRADED), POLL_INTERVAL_DEGRADED)
        self.assertEqual(compute_poll_interval(POLL_OPEN), POLL_INTERVAL_OPEN)

    def test_normal_is_high_freq(self):
        # 与拆分前字面量语义一致（0.1s 高频）
        self.assertEqual(POLL_INTERVAL_NORMAL, 0.1)
        self.assertEqual(compute_poll_interval(POLL_NORMAL), 0.1)

    def test_unknown_state_falls_back_normal(self):
        self.assertEqual(compute_poll_interval(999), POLL_INTERVAL_NORMAL)


class TestInstantPollInterval(unittest.TestCase):
    """poll_backoff 关闭时的固定高频回退值（拆分前 `return 0.1` 分支）。"""

    def test_instant_high_freq(self):
        self.assertEqual(instant_poll_interval(), 0.1)

    def test_consistency_with_normal(self):
        self.assertEqual(instant_poll_interval(), POLL_INTERVAL_NORMAL)


if __name__ == "__main__":
    unittest.main()
