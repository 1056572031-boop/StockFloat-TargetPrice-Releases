# -*- coding: utf-8 -*-
"""实时股价 v1.2.69 回归测试：策略层修复 + 产品决策（节假日 / 北京时间）。

覆盖：
A. 备用源代码映射 _sina_list_symbol（港股/美股/沪深北）
B. 开收盘判定改用北京时间 + 法定节假日（_beijing_now / _is_trading_time / market_status）
C. 搜索结果去重 + 截断 + 空关键词拦截（search_stocks，网络打桩）
"""
import datetime
import unittest
from unittest import mock

import sf_quote
from sf_quote import (_beijing_now, _is_trading_time, _sina_list_symbol,
                     market_status, search_stocks)


class TestSinaListSymbol(unittest.TestCase):
    """备用源新浪列表代码按市场正确转换，主源故障时美股/港股才能回退成功。"""
    def test_mapping(self):
        self.assertEqual(_sina_list_symbol("sh600519"), "sh600519")
        self.assertEqual(_sina_list_symbol("sz000001"), "sz000001")
        self.assertEqual(_sina_list_symbol("bj830700"), "bj830700")
        self.assertEqual(_sina_list_symbol("hk00700"), "hk00700")
        self.assertEqual(_sina_list_symbol("usaapl"), "gb_aapl")
        self.assertEqual(_sina_list_symbol("USGOOG"), "gb_goog")


class TestBeijingTime(unittest.TestCase):
    """开收盘固定按北京时间，且识别 2026 法定节假日。"""
    def test_beijing_now_is_utc8(self):
        now = _beijing_now()
        self.assertIsNotNone(now.utcoffset())
        self.assertEqual(now.utcoffset(), datetime.timedelta(hours=8))

    def test_trading_day(self):
        # 2026-02-24 周二 10:30，春节后首个交易日
        self.assertTrue(_is_trading_time(datetime.datetime(2026, 2, 24, 10, 30)))

    def test_weekend_closed(self):
        # 2026-02-14 周六
        self.assertFalse(_is_trading_time(datetime.datetime(2026, 2, 14, 10, 30)))

    def test_holiday_closed(self):
        # 2026-02-18 周三，春节休市（非周末）
        self.assertFalse(_is_trading_time(datetime.datetime(2026, 2, 18, 10, 30)))
        # 2026-10-05 周一，国庆休市（非周末）
        self.assertFalse(_is_trading_time(datetime.datetime(2026, 10, 5, 10, 30)))

    def test_aware_beijing_input(self):
        tz = datetime.timezone(datetime.timedelta(hours=8))
        # 北京时间 10:30 盘中
        self.assertTrue(_is_trading_time(datetime.datetime(2026, 2, 24, 10, 30, tzinfo=tz)))
        # 北京时间 12:30 午休
        self.assertFalse(_is_trading_time(datetime.datetime(2026, 2, 24, 12, 30, tzinfo=tz)))

    def test_market_status_holiday(self):
        # 节假日应显示"休市"
        self.assertEqual(market_status(datetime.datetime(2026, 2, 18, 10, 30))[0], "休市")
        # 交易日盘中返回 None（不显示休市提示）
        self.assertIsNone(market_status(datetime.datetime(2026, 2, 24, 10, 30)))


class TestSearchStocks(unittest.TestCase):
    """搜索结果去重 + 截断 + 空关键词拦截；网络与北交所兜底均打桩。"""
    @staticmethod
    def _entry(market, code, name):
        # 腾讯 smartbox 字段：0前缀 1代码 2名称 ... 4类型(GP-A)
        return "%s~%s~%s~x~GP-A~x" % (market, code, name)

    def _payload(self, n):
        items = [self._entry("sh", "%06d" % i, "n%d" % i) for i in range(n)]
        # 制造一条腾讯同源重复（与首条 sh000000 重复）
        items.append(self._entry("sh", "000000", "dup"))
        return 'v="%s";' % "^".join(items)

    def test_empty_keyword(self):
        with mock.patch("sf_quote.http_get", return_value='v="";'), \
                mock.patch("sf_quote.eastmoney_search_bj", return_value=[]):
            self.assertEqual(search_stocks("   "), [])
            self.assertEqual(search_stocks(""), [])

    def test_query_whitespace_and_case_are_normalized(self):
        """粘贴带空白/大小写混合的代码时，应按同一个查询发给搜索源。"""
        data = 'v="sh~600519~贵州茅台~x~GP-A~x";'
        with mock.patch("sf_quote.http_get", return_value=data) as http_get, \
                mock.patch("sf_quote.eastmoney_search_bj", return_value=[]):
            out = search_stocks(" SH 600519\n")
        self.assertEqual(out, [("sh600519", "贵州茅台")])
        self.assertIn("q=sh600519", http_get.call_args.args[0])

    def test_dedup_and_truncate(self):
        data = self._payload(60)  # 60 条 + 1 条重复 = 61 候选
        with mock.patch("sf_quote.http_get", return_value=data), \
                mock.patch("sf_quote.eastmoney_search_bj", return_value=[]):
            out = search_stocks("foo")
        codes = [c for c, _ in out]
        self.assertEqual(len(codes), len(set(codes)), "搜索结果不应有重复代码")
        self.assertLessEqual(len(out), 50, "搜索结果应截断到上限")
        # 重复条目被去重（首条 sh000000 只出现一次）
        self.assertEqual(codes.count("sh000000"), 1)

    def test_bj_merge(self):
        # v1.3.65：东财（北交所）请求改为与腾讯主搜索**并行**发起（省掉串行等待
        # 的 66~225ms），并行时还不知道腾讯返回几条，故按上限 SEARCH_MAX_RESULTS
        # 请求、合并时再截断到剩余名额 —— 结果与原"补差额"口径一致，仅请求参数
        # 从 45 变为 50。
        data = self._payload(5)
        with mock.patch("sf_quote.http_get", return_value=data), \
                mock.patch("sf_quote.eastmoney_search_bj",
                           return_value=[("bj123456", "北交股")]) as bj_search:
            out = search_stocks("foo")
        codes = [c for c, _ in out]
        self.assertIn("bj123456", codes, "北交所兜底条目应合并进结果")
        bj_search.assert_called_once_with("foo", count=50)

    def test_full_tencent_results_ignore_bj(self):
        """腾讯结果已满上限时，北交所候选不应再被并入（结果仍为 50 条）。

        v1.3.65 前这里断言的是"东财完全不被调用"——那依赖串行实现（等腾讯
        返回后才决定是否发起）。并行化后东财已随主搜索一起发出，无法撤回；
        但**结果**契约不变：满额时东财回包被丢弃，绝不挤占已展示的 50 条。
        代价仅是极罕见场景（腾讯返回满 50 条，实测搜索通常返回 1~9 条）
        下后台线程多一次不阻塞界面的请求。"""
        data = self._payload(50)
        with mock.patch("sf_quote.http_get", return_value=data), \
                mock.patch("sf_quote.eastmoney_search_bj",
                           return_value=[("bj123456", "北交股")]) as bj_search:
            out = search_stocks("foo")
        codes = [c for c, _ in out]
        self.assertEqual(len(out), 50, "满额时结果不应被北交所候选挤占")
        self.assertNotIn("bj123456", codes, "满额时北交所兜底条目不应并入")


if __name__ == "__main__":
    unittest.main()
