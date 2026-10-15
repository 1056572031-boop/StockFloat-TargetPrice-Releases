# -*- coding: utf-8 -*-
"""v1.2.72 第6轮走查：行情降级、极端布局、诊断脱敏回归。"""
import datetime
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_config
import sf_quote
from renderer import price_pct_layout


class TestMinuteFallbackEntry(unittest.TestCase):
    def test_empty_primary_during_trading_uses_fallback(self):
        """盘中主源结构性空数据时，必须尝试备用源而不是给界面空图。"""
        pts = [("10:00", 10.0, 10.0)]
        with patch.object(sf_quote, "_fetch_minute_primary", return_value=[]), \
             patch.object(sf_quote, "fetch_minute_fallback", return_value=pts) as fallback, \
             patch.object(sf_quote, "_is_trading_time", return_value=True):
            self.assertEqual(sf_quote.fetch_minute("sh600519"), pts)
        fallback.assert_called_once_with("sh600519")

    def test_empty_primary_off_market_does_not_unnecessarily_fallback(self):
        with patch.object(sf_quote, "_fetch_minute_primary", return_value=[]), \
             patch.object(sf_quote, "fetch_minute_fallback") as fallback, \
             patch.object(sf_quote, "_is_trading_time", return_value=False):
            self.assertEqual(sf_quote.fetch_minute("sh600519"), [])
        fallback.assert_not_called()


class TestExtremePriceLayout(unittest.TestCase):
    def test_extreme_price_hides_pct_instead_of_overlapping(self):
        """最窄窗 + 超长价格：宁可隐藏辅助涨跌幅，也不能数字压在一起。"""
        lay = price_pct_layout(
            180, lambda size: 101 if size == 16 else 160, 70,
            pad_x=12, right_pad=12, pct_gap=6,
            size_max=27, size_min=16, step=2)
        self.assertFalse(lay["show_pct"])
        self.assertIsNone(lay["pct_x"])
        self.assertFalse(lay["on_right"])
        self.assertLessEqual(lay["price_x"] + lay["price_w"], lay["right_limit"])


class TestDiagnosticCredentialCoverage(unittest.TestCase):
    def test_userinfo_and_extended_query_credentials_are_redacted(self):
        raw = ("https://name:PASS@api.example.com/q?session=SESSION&jwt=JWT"
               "&access_token=ACCESS&refresh_token=REFRESH&normal=ok")
        out = sf_config.redact_config({"api_urls": {"quote": raw}})
        text = json.dumps(out, ensure_ascii=False)
        for secret in ("name", "PASS", "SESSION", "JWT", "ACCESS", "REFRESH"):
            self.assertNotIn(secret, text)
        self.assertIn("https://api.example.com/", out["api_urls"]["quote"])
        self.assertNotIn("normal=ok", out["api_urls"]["quote"])


if __name__ == "__main__":
    unittest.main()
