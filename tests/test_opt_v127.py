# -*- coding: utf-8 -*-
"""v1.2.27 优化回归测试：
- 3.1 北交所 30% / 主板 ST 5% / 科创创业 20% / 主板 10% 涨跌停阈值本地兜底（quote_status）
- 3.2 4 位北交所老代码归一化（normalize_code）
覆盖：行情源无涨跌停字段时按板块兜底、官方字段优先、停牌判定不被本地估算污染。

运行方式（在 StockFloat 目录）：
    python3 -m unittest discover -s tests -v
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_quote
import sf_utils

# 交易时段（工作日周三 10:00），保证停牌判定与涨停/跌停判定均生效
_TRADE = datetime.datetime(2026, 8, 26, 10, 0, 0)


class TestQuoteStatusBoardFallback(unittest.TestCase):
    """3.1 v1.2.27：行情源缺失 limit_up/limit_down 时按代码板块本地兜底估算涨跌停"""

    def _q(self, code, name, price, pre_close, **extra):
        q = {"name": name, "code": code, "price": price, "pre_close": pre_close,
             "change": price - pre_close, "pct": (price - pre_close) / pre_close * 100.0,
             "high": price, "low": pre_close}
        q.update(extra)
        return q

    def test_bj_limit_up_30pct(self):
        # 北交所 bj 前缀：昨收 10 → 涨停价 13.00
        q = self._q("bj830799", "某北股", 13.00, 10.0)
        self.assertEqual(sf_quote.quote_status(q, _TRADE), "涨停")

    def test_bj_limit_down_30pct(self):
        q = self._q("bj830799", "某北股", 7.00, 10.0)
        self.assertEqual(sf_quote.quote_status(q, _TRADE), "跌停")

    def test_bj_not_limit_inside_range(self):
        # 北交所价在区间内（如 +10%，未到 30%）→ 不判涨停
        q = self._q("bj830799", "某北股", 11.00, 10.0)
        self.assertIsNone(sf_quote.quote_status(q, _TRADE))

    def test_bj_920_prefix_30pct(self):
        q = self._q("920001", "北交所新", 13.00, 10.0)
        self.assertEqual(sf_quote.quote_status(q, _TRADE), "涨停")

    def test_bj_st_still_30pct(self):
        # 北交所 ST 不误判 5%：+10% 未到 30% 上限，不判涨停
        q = self._q("bj830799", "*ST某北", 11.00, 10.0)
        self.assertIsNone(sf_quote.quote_status(q, _TRADE))
        # 到 +30% 判涨停
        q2 = self._q("bj830799", "*ST某北", 13.00, 10.0)
        self.assertEqual(sf_quote.quote_status(q2, _TRADE), "涨停")

    def test_kcb_688_20pct(self):
        q = self._q("688001", "某科创", 12.00, 10.0)
        self.assertEqual(sf_quote.quote_status(q, _TRADE), "涨停")

    def test_cyb_300_20pct(self):
        q = self._q("300750", "某创业", 12.00, 10.0)
        self.assertEqual(sf_quote.quote_status(q, _TRADE), "涨停")

    def test_main_board_10pct(self):
        q = self._q("600519", "某主板", 11.00, 10.0)
        self.assertEqual(sf_quote.quote_status(q, _TRADE), "涨停")

    def test_main_board_st_5pct(self):
        q = self._q("600001", "ST某股", 10.50, 10.0)
        self.assertEqual(sf_quote.quote_status(q, _TRADE), "涨停")
        # ST 5%：+4% 未到涨停
        q2 = self._q("600001", "ST某股", 10.40, 10.0)
        self.assertIsNone(sf_quote.quote_status(q2, _TRADE))

    def test_official_fields_priority(self):
        # 行情源提供官方涨跌停价时优先用官方字段，本地估算不覆盖
        # （volume 带有效值，避免触发停牌分支，聚焦涨停/跌停判定）
        q = self._q("bj830799", "某北股", 13.00, 10.0,
                    limit_up=13.5, limit_down=6.5, volume=1000000)
        # 官方涨停价 13.5：现价 13.0 未达 → 不判涨停
        self.assertIsNone(sf_quote.quote_status(q, _TRADE))
        q2 = self._q("bj830799", "某北股", 13.50, 10.0,
                     limit_up=13.5, limit_down=6.5, volume=1000000)
        self.assertEqual(sf_quote.quote_status(q2, _TRADE), "涨停")

    def test_suspended_not_polluted_by_estimate(self):
        # 行情源无涨跌停字段 + 现价 0（交易时段）：停牌判定只用原始字段，
        # 本地估算不得让正常股票误判"停牌"；与旧版一致返回 None
        q = self._q("bj830799", "某北股", 0.0, 10.0)
        self.assertIsNone(sf_quote.quote_status(q, _TRADE))

    def test_suspended_real_with_fields(self):
        # 真停牌（官方 limit_up 有效 + volume=0）仍正确判停牌
        q = self._q("600519", "某主板", 10.0, 10.0, limit_up=11.0, limit_down=9.0, volume=0)
        self.assertEqual(sf_quote.quote_status(q, _TRADE), "停牌")

    def test_unknown_board_no_fallback(self):
        # 无法识别板块（代码非数字/空）→ 不兜底，返回 None
        q = self._q("", "未知", 11.00, 10.0)
        self.assertIsNone(sf_quote.quote_status(q, _TRADE))
        q2 = self._q("abcd", "未知", 11.00, 10.0)
        self.assertIsNone(sf_quote.quote_status(q2, _TRADE))

    def test_estimate_limit_prices(self):
        up, down = sf_quote._estimate_limit_prices({"code": "bj830799", "name": "x", "pre_close": 10.0})
        self.assertEqual(up, 13.00)
        self.assertEqual(down, 7.00)
        up, down = sf_quote._estimate_limit_prices({"code": "600001", "name": "ST某", "pre_close": 10.0})
        self.assertEqual(up, 10.50)
        up, down = sf_quote._estimate_limit_prices({"code": "688001", "name": "x", "pre_close": 10.0})
        self.assertEqual(up, 12.00)
        # 昨收非法 → 不兜底
        self.assertEqual(sf_quote._estimate_limit_prices({"code": "600001", "pre_close": 0}), (0.0, 0.0))
        self.assertEqual(sf_quote._estimate_limit_prices({"code": "600001", "pre_close": None}), (0.0, 0.0))


class TestNormalizeCodeBj4Digit(unittest.TestCase):
    """3.2 v1.2.27：4 位北交所老代码归一化（尾部补 0 到 6 位标准格式）"""

    def test_bj_4digit_8xx(self):
        self.assertEqual(sf_utils.normalize_code("8307"), "bj830700")

    def test_bj_4digit_4xx(self):
        self.assertEqual(sf_utils.normalize_code("4300"), "bj430000")

    def test_bj_6digit_unchanged(self):
        self.assertEqual(sf_utils.normalize_code("830799"), "bj830799")
        self.assertEqual(sf_utils.normalize_code("430047"), "bj430047")

    def test_bj_920_unchanged(self):
        self.assertEqual(sf_utils.normalize_code("920001"), "bj920001")

    def test_main_board_unchanged(self):
        self.assertEqual(sf_utils.normalize_code("600519"), "sh600519")
        self.assertEqual(sf_utils.normalize_code("000001"), "sz000001")

    def test_prefixed_4digit_unchanged_path(self):
        # 已带前缀直接返回（4 位场景不走归一化，保持行为一致）
        self.assertEqual(sf_utils.normalize_code("bj8307"), "bj8307")


if __name__ == "__main__":
    unittest.main()
