# -*- coding: utf-8 -*-
"""v1.3.52 行情源两个缺陷的回归测试（sf_quote 单模块，无需 Qt）。

缺陷 A：北交所老代码在东财备源取不到数据
    _secid("bj8307") 旧实现返回 "0.8307" —— 东财 secid 要的是 6 位数字
    ("0.830700")，4 位码恒返回空。同文件 _tencent_code 已对 bj 补零，
    _secid 漏了这一手，两边口径不一致。修：bj 分支数字部分不足 6 位右侧补 "0"。

缺陷 B：新浪备源昨收为 0 时涨跌幅恒显示 0.00%（假数据）
    0.000 是合法有限浮点数，能通过校验；pct 被算成 0.0，界面显示 0.00%，
    资深股民会误判成"平盘"。修：pre <= 0 时 change/pct 置空串（下游显示 "--"），
    记 WARNING 日志，但现价/名称/高低照旧返回（整条行情不丢）。

"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_quote


def _sina_payload(name, open_, pre, price, high, low):
    """拼一条新浪 hq.sinajs.cn 响应体（字段：0名称 1今开 2昨收 3现价 4最高 5最低）。"""
    return 'var hq_str_test="%s,%s,%s,%s,%s,%s,0,0,0,0";\n' % (
        name, open_, pre, price, high, low)


class TestSecid(unittest.TestCase):
    """东财 secid 拼接（缺陷 A）。"""

    def test_bj_4digit_pads_to_six(self):
        """核心修复：北交所 4 位老代码补零到 6 位。"""
        self.assertEqual(sf_quote._secid("bj8307"), "0.830700")

    def test_bj_already_six_unchanged(self):
        """已满 6 位的 bj 代码不受影响。"""
        self.assertEqual(sf_quote._secid("bj830700"), "0.830700")

    def test_bj_5digit_pads_one_zero(self):
        self.assertEqual(sf_quote._secid("bj43001"), "0.430010")

    def test_bj_920_new_segment_unchanged(self):
        self.assertEqual(sf_quote._secid("bj920001"), "0.920001")

    def test_sh_regression(self):
        self.assertEqual(sf_quote._secid("sh600519"), "1.600519")

    def test_sz_regression(self):
        self.assertEqual(sf_quote._secid("sz000001"), "0.000001")

    def test_bare_digit_regression(self):
        """裸数字分支未在本次改动范围内，保持原样。"""
        self.assertEqual(sf_quote._secid("600519"), "1.600519")
        self.assertEqual(sf_quote._secid("000001"), "0.000001")
        self.assertEqual(sf_quote._secid("920001"), "0.920001")

    def test_hk_us_regression(self):
        self.assertEqual(sf_quote._secid("hk00700"), "116.00700")
        self.assertEqual(sf_quote._secid("usaapl"), "105.aapl")

    def test_bj_with_spaces(self):
        """带空格入参（粘贴自选）也应 strip 后正确补零。"""
        self.assertEqual(sf_quote._secid(" BJ8307 "), "0.830700")

    def test_bj_matches_tencent_code_caliber(self):
        """与 _tencent_code 同口径：两者数字部分必须一致。"""
        for raw in ("bj8307", "bj830700", " BJ8307 "):
            with self.subTest(raw=raw):
                self.assertEqual(sf_quote._secid(raw).split(".", 1)[1],
                                 sf_quote._tencent_code(raw)[2:])


class TestFallbackPreCloseZero(unittest.TestCase):
    """新浪备源昨收为 0（缺陷 B）。"""

    def _run(self, payload, code="bj830700"):
        with patch.object(sf_quote, "http_get", return_value=payload) as g, \
             patch.object(sf_quote, "log_line") as lg:
            out = sf_quote.fetch_quote_fallback(code)
        return out, g, lg

    def test_pre_close_zero_marks_unknown(self):
        """昨收 0.000：change/pct 置空串，现价照旧返回，整条不丢。"""
        out, _, _ = self._run(_sina_payload("某某新股", "0.000", "0.000", "12.34", "12.50", "12.00"))
        self.assertEqual(out["pct"], "")
        self.assertEqual(out["change"], "")
        self.assertEqual(out["price"], "12.34")
        self.assertEqual(float(out["price"]), 12.34)
        self.assertEqual(out["name"], "某某新股")
        self.assertEqual(out["high"], "12.50")
        self.assertEqual(out["low"], "12.00")
        self.assertEqual(out["code"], "bj830700")

    def test_pre_close_zero_reported_as_zero(self):
        """昨收如实回 0，不伪造。"""
        out, _, _ = self._run(_sina_payload("某某新股", "0.000", "0.000", "12.34", "12.50", "12.00"))
        self.assertEqual(float(out["pre_close"]), 0.0)

    def test_pre_close_zero_logs_warning(self):
        """必须留下 WARNING 日志，含 fallback pre_close<=0 和代码。"""
        _, _, lg = self._run(_sina_payload("某某新股", "0.000", "0.000", "12.34", "12.50", "12.00"))
        hits = [c for c in lg.call_args_list
                if len(c[0]) >= 1 and "fallback pre_close<=0" in str(c[0][0])
                and "bj830700" in str(c[0][0])]
        self.assertTrue(hits, "缺少含 'fallback pre_close<=0' 与代码的日志")
        self.assertTrue(any((c[1].get("level") or (c[0][1] if len(c[0]) > 1 else "")) == "WARNING"
                            for c in hits), "日志级别应为 WARNING")

    def test_normal_pre_close_still_computes_pct(self):
        """正常路径未改坏：昨收 10.00、现价 11.00 → pct 10.0。"""
        out, _, lg = self._run(_sina_payload("正常股", "10.00", "10.00", "11.00", "11.20", "9.80"),
                               code="sh600519")
        self.assertEqual(float(out["pct"]), 10.0)
        self.assertEqual(float(out["change"]), 1.0)
        self.assertEqual(out["price"], "11.00")
        for c in lg.call_args_list:
            self.assertNotIn("fallback pre_close<=0", str(c[0][0]) if c[0] else "")

    def test_negative_pct_normal_path(self):
        out, _, _ = self._run(_sina_payload("下跌股", "10.00", "10.00", "9.00", "10.10", "8.80"),
                              code="sh600519")
        self.assertEqual(float(out["pct"]), -10.0)
        self.assertEqual(float(out["change"]), -1.0)

    def test_parse_error_path_keeps_zero(self):
        """解析异常路径（非昨收为 0）保持 0.0 不动，下游需要数值。"""
        bad = 'var hq_str_test="名称,1.0,abc,3.0,4.0,5.0";\n'
        with patch.object(sf_quote, "http_get", return_value=bad):
            with self.assertRaises(sf_quote.QuoteDataError):
                sf_quote.fetch_quote_fallback("sh600519")


if __name__ == "__main__":
    unittest.main()
