# -*- coding: utf-8 -*-
"""sf_quote 补测：搜索缓存、北交所兜底解析、备用分时源的裁剪与脏数据过滤。

为什么单独补这一层（本轮为扩大自动化覆盖而加）
----------------------------------------------
搜索与分时是用户直接能看见的两条链路，坏掉的表现分别是"搜到刚才那只票"和
"分时图少一截/画出昨天的走势"。test_v1349_quote.py / test_v147_minute_stale.py
守了主源与公共入口，本文件补三块它们没走到的：
  1. 搜索缓存的 TTL / LRU / 返回副本（缓存被调用方改坏会污染后续搜索）；
  2. 东财 suggest 只认京A 的过滤逻辑（腾讯不收录北交所，这是唯一补源）；
  3. 备用分时源在"盘中/盘后 × 当日/昨日 × 脏数据"下的裁剪矩阵。

缓存与分时都涉及"现在几点"：一律经 tests/_base.py::fixed_clock 注入，
不依赖本机时区与真实时间（AGENTS.md 第 6 节第 4 条）。
"""
import datetime
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_quote              # noqa: E402
import sf_universe           # noqa: E402
from _base import SFCase, fixed_clock   # noqa: E402


class QuoteCase(SFCase):
    """所有用例默认：字典停用、缓存清空、不发真实网络请求。"""

    def setUp(self):
        super().setUp()
        self._cache = dict(sf_quote._SEARCH_CACHE)
        self._env = sf_quote._TEST_ENV
        sf_quote._SEARCH_CACHE.clear()
        self._saved_uni = (sf_universe._rows, sf_universe._by_code,
                           sf_universe._by_name, sf_universe._DISABLED)
        sf_universe._index([])
        sf_universe._DISABLED = True      # 本文件只测联网侧，字典单独有测试文件

    def tearDown(self):
        (sf_universe._rows, sf_universe._by_code, sf_universe._by_name,
         sf_universe._DISABLED) = self._saved_uni
        sf_quote._SEARCH_CACHE.clear()
        sf_quote._SEARCH_CACHE.update(self._cache)
        sf_quote._TEST_ENV = self._env
        super().tearDown()

    def _cache_on(self):
        """缓存实现按"unittest 环境一律不启用"短路，测它本身必须显式打开。

        私有开关直改原因：它是该模块唯一的"测试隔离"闸门，
        产品行为（真程序里）就是走 else 分支，只有这里能覆盖到。
        """
        sf_quote._TEST_ENV = False


# --------------------------------------------------------------------------
# _secid：备源 secid 拼接
# --------------------------------------------------------------------------

class TestSecid(SFCase):
    def test_prefixed_codes_map_to_their_own_segments(self):
        self.assertEqual(sf_quote._secid("sh600519"), "1.600519")
        self.assertEqual(sf_quote._secid("sz000001"), "0.000001")
        self.assertEqual(sf_quote._secid("bj920185"), "0.920185")

    def test_hk_and_us_get_their_segments_instead_of_the_bare_string(self):
        """v1.2.71 前港美股会穿过所有分支原样回传，备源必然取不到数据。"""
        self.assertEqual(sf_quote._secid("hk00700"), "116.00700")
        self.assertTrue(sf_quote._secid("usAAPL").startswith("105."),
                        "美股段位固定取 105（NYSE）；已知限制见函数 docstring")
        # 注意：入参会先整体 lower()，所以 usAAPL → "105.aapl"。东财美股 secid
        # 通常写作大写代码，这里的小写是否会让备源取空尚未在真机验证，
        # 本轮只守"不再原样回传裸串"这条已修的行为，不替未验证的口径背书。
        self.assertEqual(sf_quote._secid("usAAPL"), "105.aapl")

    def test_input_is_stripped_and_lowercased_first(self):
        """带空格（粘贴自选股）会让 startswith 全落空并原样回传，东财恒返回空。"""
        self.assertEqual(sf_quote._secid("  SH600519 "), "1.600519")

    def test_short_beijing_codes_are_padded_to_six_digits(self):
        self.assertEqual(sf_quote._secid("bj8307"), "0.830700")

    def test_bare_codes_follow_the_single_market_implementation(self):
        """裸码市场段位统一走 sf_utils.market_prefix：5xxxxx 是沪市基金，
        旧兜底"6/9→沪、其余→深"会把 510300 判成深市段，备源分时恒空。"""
        self.assertEqual(sf_quote._secid("510300"), "1.510300")
        self.assertEqual(sf_quote._secid("600519"), "1.600519")
        self.assertEqual(sf_quote._secid("000001"), "0.000001")
        self.assertEqual(sf_quote._secid("300750"), "0.300750")
        self.assertEqual(sf_quote._secid("920185"), "0.920185")
        self.assertEqual(sf_quote._secid("8307"), "0.830700")

    def test_unrecognised_input_is_returned_unchanged(self):
        self.assertEqual(sf_quote._secid("whatever"), "whatever")
        self.assertEqual(sf_quote._secid(None), "")
        self.assertEqual(sf_quote._secid(""), "")


# --------------------------------------------------------------------------
# 搜索结果缓存（TTL + LRU）
# --------------------------------------------------------------------------

class TestSearchCache(QuoteCase):
    def test_disabled_in_unit_test_environment_by_default(self):
        """默认必须短路：多个用例搜同一个关键词会互相顶掉结果。"""
        self.assertTrue(sf_quote._TEST_ENV)
        sf_quote._search_cache_put("foo", [("sh600000", "浦发银行")])
        self.assertIsNone(sf_quote._search_cache_get("foo"))

    def test_put_then_get_returns_the_entry(self):
        self._cache_on()
        sf_quote._search_cache_put("茅台", [("sh600519", "贵州茅台")])
        self.assertEqual(sf_quote._search_cache_get("茅台"), [("sh600519", "贵州茅台")])

    def test_miss_returns_none_not_an_empty_list(self):
        """None = 没查到，[] = 查过且确实没有。两者混用会让"空结果"永久占缓存。"""
        self._cache_on()
        self.assertIsNone(sf_quote._search_cache_get("never-searched"))

    def test_entry_expires_after_the_ttl(self):
        self._cache_on()
        sf_quote._search_cache_put("kw", [("sh600000", "x")])
        ts, res = sf_quote._SEARCH_CACHE["kw"]
        sf_quote._SEARCH_CACHE["kw"] = (ts - sf_quote.SEARCH_CACHE_TTL - 1, res)
        self.assertIsNone(sf_quote._search_cache_get("kw"))
        self.assertNotIn("kw", sf_quote._SEARCH_CACHE,
                         "过期条目要顺手摘掉，否则缓存只增不减")

    def test_cached_empty_result_is_returned_as_a_hit(self):
        self._cache_on()
        sf_quote._search_cache_put("nothing", [])
        self.assertEqual(sf_quote._search_cache_get("nothing"), [])

    def test_lru_evicts_the_least_recently_used_entry(self):
        self._cache_on()
        for i in range(sf_quote.SEARCH_CACHE_MAX + 5):
            sf_quote._search_cache_put("kw%d" % i, [])
        self.assertLessEqual(len(sf_quote._SEARCH_CACHE), sf_quote.SEARCH_CACHE_MAX)
        self.assertIsNone(sf_quote._search_cache_get("kw0"), "最久未用的先出局")
        self.assertIsNotNone(sf_quote._search_cache_get(
            "kw%d" % (sf_quote.SEARCH_CACHE_MAX + 4)))

    def test_a_read_refreshes_the_position(self):
        self._cache_on()
        for i in range(sf_quote.SEARCH_CACHE_MAX):
            sf_quote._search_cache_put("kw%d" % i, [])
        sf_quote._search_cache_get("kw0")            # 摸一下 = 变成最近使用
        sf_quote._search_cache_put("fresh", [])
        self.assertIsNotNone(sf_quote._search_cache_get("kw0"),
                            "被读到过的条目不该下一个就被挤掉")
        self.assertIsNone(sf_quote._search_cache_get("kw1"))

    def test_put_snapshots_the_input(self):
        """存进去的是快照：调用方之后修改这个列表不得改到缓存内容。"""
        self._cache_on()
        original = [("sh600519", "贵州茅台")]
        sf_quote._search_cache_put("kw", original)
        original.append(("sz000001", "调用方后加的"))
        self.assertEqual(sf_quote._search_cache_get("kw"),
                         [("sh600519", "贵州茅台")])


class TestSearchStocksWithCache(QuoteCase):
    # smartbox 真实字段序：市场~代码~名称(\uXXXX 转义)~拼音~类型
    TENCENT_BODY = 'var t="sh~600519~\\u8d35\\u5dde\\u8305\\u53f0~gzmt~GP-A~";'

    def _stub_network(self, results=None):
        calls = []

        def fake_http_get(url, **kw):
            calls.append(url)
            if "eastmoney" not in url:
                return self.TENCENT_BODY
            return json.dumps({"QuotationCodeTable": {"Data": []}})

        mock.patch.object(sf_quote, "http_get", side_effect=fake_http_get).start()
        mock.patch.object(sf_quote, "_eastmoney_bj_worker").start()
        self.addCleanup(mock.patch.stopall)
        return calls

    def test_second_identical_search_skips_the_network(self):
        self._cache_on()
        calls = self._stub_network()
        first = sf_quote.search_stocks("茅台")
        n = len(calls)
        second = sf_quote.search_stocks("茅台")
        self.assertEqual(second, first)
        self.assertEqual(len(calls), n, "5 分钟内重复搜索必须 0 网络往返")

    def test_cache_hit_returns_a_fresh_list(self):
        """弹窗会对结果做 in-place 处理：命中缓存时也必须拿到自己的那份。"""
        self._cache_on()
        self._stub_network()
        first = sf_quote.search_stocks("茅台")
        first.append(("sz000001", "弹窗自己加的"))
        self.assertEqual(sf_quote.search_stocks("茅台"),
                         [("sh600519", "贵州茅台")])

    def test_unicode_escape_names_are_decoded(self):
        self._cache_on()
        self._stub_network()
        self.assertEqual(sf_quote.search_stocks("茅台"),
                         [("sh600519", "贵州茅台")],
                         "接口返回 \\uXXXX 字面转义，不解码界面会显示乱码")

    def test_empty_keyword_never_touches_the_network(self):
        calls = self._stub_network()
        for kw in ("", None, "   "):
            self.assertEqual(sf_quote.search_stocks(kw), [], repr(kw))
        self.assertEqual(calls, [])

    def test_cache_key_is_the_trimmed_keyword(self):
        self._cache_on()
        calls = self._stub_network()
        sf_quote.search_stocks("  茅台 ")
        before = len(calls)
        sf_quote.search_stocks("茅台")
        self.assertEqual(len(calls), before, "首尾空格不该算成两个关键词")


# --------------------------------------------------------------------------
# 东财北交所兜底搜索
# --------------------------------------------------------------------------

class TestEastmoneyBeijingSearch(QuoteCase):
    def _run(self, payload, status=None):
        seen = {}

        def fake(url, **kw):
            seen["url"] = url
            seen["kw"] = kw
            if isinstance(payload, Exception):
                raise payload
            self._status["v"] = status
            return json.dumps(payload)

        with mock.patch.object(sf_quote, "http_get", side_effect=fake):
            out = sf_quote.eastmoney_search_bj("贝特瑞")
        return out, seen

    def setUp(self):
        super().setUp()
        # 状态码经 sf_network 线程本地值读取；这里换成可控替身
        self._status = {"v": None}
        self._orig_get_status = sf_quote.get_last_http_status
        sf_quote.get_last_http_status = lambda: self._status["v"]

    def tearDown(self):
        sf_quote.get_last_http_status = self._orig_get_status
        super().tearDown()

    def test_only_beijing_a_shares_are_kept(self):
        rows = [{"SecurityTypeName": "京A", "Code": "920185", "Name": "贝特瑞"},
                {"SecurityTypeName": "沪A", "Code": "600519", "Name": "贵州茅台"},
                {"MarketType": "_TB", "Code": "430047", "Name": "诺思兰德"},
                {"SecurityTypeName": "深A", "Code": "000001", "Name": "平安银行"}]
        out, _ = self._run({"QuotationCodeTable": {"Data": rows}})
        self.assertEqual(out, [("bj920185", "贝特瑞"), ("bj430047", "诺思兰德")],
                         "腾讯不收录北交所，这里必须只补京A 且统一 bj 前缀")

    def test_missing_name_falls_back_to_the_code(self):
        out, _ = self._run({"QuotationCodeTable": {"Data": [
            {"SecurityTypeName": "京A", "Code": "920185"}]}})
        self.assertEqual(out, [("bj920185", "920185")])

    def test_blank_code_rows_are_dropped(self):
        out, _ = self._run({"QuotationCodeTable": {"Data": [
            {"SecurityTypeName": "京A", "Code": "  ", "Name": "x"}]}})
        self.assertEqual(out, [])

    def test_absent_tables_are_not_an_error(self):
        for payload in ({}, {"QuotationCodeTable": None}, {"QuotationCodeTable": {}}):
            out, _ = self._run(payload)
            self.assertEqual(out, [], payload)

    def test_keyword_is_percent_encoded_into_the_url(self):
        _, seen = self._run({})
        self.assertIn("%E8%B4%9D%E7%89%B9%E7%91%9E", seen["url"],
                      "中文关键词必须百分号编码")
        self.assertEqual(seen["kw"]["retries"], 0,
                         "兜底源失败不阻断主搜索，不该长等重试")
        self.assertLessEqual(seen["kw"]["timeout"], 3.0)

    def test_http_4xx_returns_empty_without_raising(self):
        self._status["v"] = 403
        out, _ = self._run(RuntimeError("boom"))
        self.assertEqual(out, [])

    def test_network_failure_returns_empty_without_raising(self):
        self._status["v"] = None
        out, _ = self._run(OSError("connection reset"))
        self.assertEqual(out, [])

    def test_malformed_json_returns_empty(self):
        with mock.patch.object(sf_quote, "http_get", return_value="<html>502</html>"):
            self.assertEqual(sf_quote.eastmoney_search_bj("x"), [])


# --------------------------------------------------------------------------
# 备用分时源（东财 trends2）
# --------------------------------------------------------------------------

class TestMinuteFallback(QuoteCase):
    """备用源的裁剪矩阵：盘中/盘后 × 当日/昨日 × 脏数据。"""

    def _run(self, rows, at, trading=True):
        body = json.dumps({"data": {"trends": rows}})
        with mock.patch.object(sf_quote, "http_get", return_value=body) as hg:
            with fixed_clock(at):
                with mock.patch.object(sf_quote, "_is_trading_time",
                                       return_value=trading):
                    return sf_quote.fetch_minute_fallback("sh600519"), hg

    def test_url_carries_the_secid_of_the_requested_stock(self):
        _, hg = self._run([], datetime.datetime(2026, 2, 18, 10, 0))
        self.assertIn("1.600519", hg.call_args.args[0])

    def test_valid_rows_keep_the_time_price_and_average(self):
        rows = ["2026-02-18 09:31,1,2,3,4,5,6,7.5",
                "2026-02-18 09:32,1,2.5,3,4,5,6,7.6"]
        pts, _ = self._run(rows, datetime.datetime(2026, 2, 18, 9, 35))
        self.assertEqual(pts, [("09:31", 2.0, 7.5), ("09:32", 2.5, 7.6)],
                         "收=parts[2]、均价=parts[7]，时间保持 HH:MM（渲染层依赖此形状）")

    def test_rows_shorter_than_eight_fields_are_dropped(self):
        pts, _ = self._run(["2026-02-18 09:31,1,2"],
                           datetime.datetime(2026, 2, 18, 9, 35))
        self.assertEqual(pts, [])

    def test_yesterday_is_during_trading_hours_but_not_after_the_close(self):
        """盘中裁剪必须以北京时间为准：错判会把昨日全量画成「今日走势」。"""
        rows = ["2026-02-17 09:31,1,2,3,4,5,6,7.5"]
        pts, _ = self._run(rows, datetime.datetime(2026, 2, 18, 9, 35))
        self.assertEqual(pts, [], "交易时段内的非当日数据必须丢弃")
        pts2, _ = self._run(rows, datetime.datetime(2026, 2, 18, 20, 0),
                            trading=False)
        self.assertEqual(pts2, [("09:31", 2.0, 7.5)],
                         "休市时接受最近交易日完整分时")

    def test_points_after_the_current_minute_are_cut_off(self):
        rows = ["2026-02-18 09:31,1,2,3,4,5,6,7.5",
                "2026-02-18 09:40,1,2,3,4,5,6,7.5",
                "2026-02-18 10:00,1,2,3,4,5,6,7.5"]
        pts, _ = self._run(rows, datetime.datetime(2026, 2, 18, 9, 45))
        self.assertEqual([t for t, _, _ in pts], ["09:31", "09:40"],
                         "盘中上限 = min(当前分钟, 15:00)")

    def test_after_hours_points_are_capped_at_the_close(self):
        rows = ["2026-02-18 %s,1,2,3,4,5,6,7.5" % hm
                for hm in ("09:30", "15:00", "15:06", "15:30")]
        pts, _ = self._run(rows, datetime.datetime(2026, 2, 18, 15, 50),
                           trading=False)
        self.assertEqual([t for t, _, _ in pts], ["09:30", "15:00"],
                         "15:00 后的盘后点若保留，会被 clamp 到同一 x 叠成竖线")

    def test_pre_open_points_are_dropped(self):
        pts, _ = self._run(["2026-02-18 09:15,1,2,3,4,5,6,7.5"],
                           datetime.datetime(2026, 2, 18, 20, 0), trading=False)
        self.assertEqual(pts, [])

    def test_time_only_form_is_accepted(self):
        pts, _ = self._run(["09:31,1,2,3,4,5,6,7.5"],
                           datetime.datetime(2026, 2, 18, 9, 35))
        self.assertEqual(pts, [("09:31", 2.0, 7.5)],
                         "接口只给 HH:MM 时也必须能画")

    def test_unparseable_time_is_skipped_not_fatal(self):
        rows = ["2026-02-18 ab:cd,1,2,3,4,5,6,7.5",
                "2026-02-18 0931,1,2,3,4,5,6,7.5"]
        pts, _ = self._run(rows, datetime.datetime(2026, 2, 18, 9, 35))
        self.assertEqual([t for t, _, _ in pts], ["0931"])

    def test_non_finite_price_drops_the_point_and_the_whole_row_survives(self):
        rows = ["2026-02-18 09:31,1,nan,3,4,5,6,7.5",
                "2026-02-18 09:32,1,inf,3,4,5,6,7.5",
                "2026-02-18 09:33,1,2,3,4,5,6,7.5"]
        pts, _ = self._run(rows, datetime.datetime(2026, 2, 18, 9, 35))
        self.assertEqual(pts, [("09:33", 2.0, 7.5)],
                         "NaN/inf 会让折线整段消失，必须逐点剔除")

    def test_non_numeric_price_is_skipped(self):
        pts, _ = self._run(["2026-02-18 09:31,1,--,3,4,5,6,7.5"],
                           datetime.datetime(2026, 2, 18, 9, 35))
        self.assertEqual(pts, [])

    def test_blank_average_becomes_none_rather_than_zero(self):
        """均价缺失必须是 None：写成 0 会让「均价线」贴到 0 刻度上。"""
        pts, _ = self._run(["2026-02-18 09:31,1,2,3,4,5,6,",
                            "2026-02-18 09:32,1,2,3,4,5,6,nan"],
                           datetime.datetime(2026, 2, 18, 9, 35))
        self.assertEqual(pts, [("09:31", 2.0, None), ("09:32", 2.0, None)])

    def test_empty_response_is_an_empty_point_set_not_an_error(self):
        pts, _ = self._run([], datetime.datetime(2026, 2, 18, 9, 35))
        self.assertEqual(pts, [])
        with mock.patch.object(sf_quote, "http_get", return_value='{"data":null}'):
            self.assertEqual(sf_quote.fetch_minute_fallback("sh600519"), [])


# --------------------------------------------------------------------------
# fetch_minute 的主备切换（补公共入口未覆盖的组合）
# --------------------------------------------------------------------------

class TestMinutePrimaryToFallback(QuoteCase):
    def test_empty_primary_during_trading_hours_falls_back(self):
        """盘中空分时不是可展示的成功结果：备用源有数据就得切过去。"""
        with mock.patch.object(sf_quote, "_fetch_minute_primary", return_value=[]), \
                mock.patch.object(sf_quote, "_is_trading_time", return_value=True), \
                mock.patch.object(sf_quote, "fetch_minute_fallback",
                                  return_value=[("0931", 1.0, None)]) as fb, \
                mock.patch.object(sf_quote, "log_line") as lg:
            self.assertEqual(sf_quote.fetch_minute("sh600519"),
                             [("0931", 1.0, None)])
        self.assertEqual(fb.call_count, 1)
        self.assertIn("tencent->eastmoney", lg.call_args.args[0])

    def test_empty_primary_outside_trading_hours_is_a_legitimate_result(self):
        with mock.patch.object(sf_quote, "_fetch_minute_primary", return_value=[]), \
                mock.patch.object(sf_quote, "_is_trading_time", return_value=False), \
                mock.patch.object(sf_quote, "fetch_minute_fallback") as fb:
            self.assertEqual(sf_quote.fetch_minute("sh600519"), [])
        self.assertEqual(fb.call_count, 0, "休市空点集不该额外发一次网络请求")

    def test_fallback_failure_reports_the_primary_error(self):
        """两个源都失败时抛出的是主源异常 —— 诊断里看到的是第一现场。"""
        boom = ValueError("primary broken")
        with mock.patch.object(sf_quote, "_fetch_minute_primary", side_effect=boom), \
                mock.patch.object(sf_quote, "fetch_minute_fallback",
                                  side_effect=OSError("fallback down")), \
                mock.patch.object(sf_quote, "_bump_format_error") as bump, \
                mock.patch.object(sf_quote, "log_line"):
            with self.assertRaises(ValueError) as cm:
                sf_quote.fetch_minute("sh600519")
        self.assertIs(cm.exception, boom)
        self.assertEqual(bump.call_count, 1)

    def test_ordinary_empty_result_resets_the_format_error_counter(self):
        """备用源正常时不能继续累加"接口格式变更"，否则日志长期谎报格式坏了。"""
        with mock.patch.object(sf_quote, "_fetch_minute_primary", return_value=[]), \
                mock.patch.object(sf_quote, "_is_trading_time", return_value=True), \
                mock.patch.object(sf_quote, "fetch_minute_fallback", return_value=[]), \
                mock.patch.object(sf_quote, "_bump_format_error") as bump, \
                mock.patch.object(sf_quote, "_reset_format_error") as reset, \
                mock.patch.object(sf_quote, "log_line"):
            sf_quote.fetch_minute("sh600519")
        self.assertEqual(bump.call_count, 0, "主源空点集不算格式异常")
        reset.assert_any_call("minute")


if __name__ == "__main__":
    unittest.main()
