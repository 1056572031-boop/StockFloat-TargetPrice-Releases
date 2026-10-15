# -*- coding: utf-8 -*-
"""实时股价 v1.1.1 核心逻辑单元测试（16.1 随 PyQt6 迁移）。

运行方式（在 StockFloat 目录）：
    python3 -m unittest discover -s tests -v
依赖：本机需能 import PyQt6（源码模块顶层 import PyQt6）。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stock_float_window as m
import sf_config
import sf_constants
import sf_network
import sf_quote
import sf_utils

_ORIG_CONFIG_PATH = m.CONFIG_PATH
_ORIG_CONFIG_PATH_SFC = sf_config.CONFIG_PATH
_ORIG_HTTP_GET = sf_quote.http_get  # 供 TestFetchQuoteFallback 等 tearDown 还原


class TestPureFuncs(unittest.TestCase):
    """2.x / 纯函数：fmt_pct / normalize_code 边界"""

    def test_fmt_pct(self):
        # v1.2.66：固定两位小数，不再去末尾 0
        self.assertEqual(m.fmt_pct(0.50), "+0.50%")
        self.assertEqual(m.fmt_pct(0.0), "0.00%")
        self.assertEqual(m.fmt_pct(-1.234), "-1.23%")
        self.assertEqual(m.fmt_pct(10.0), "+10.00%")
        # 非有限数（inf/-inf/nan）边界防御：归零，不渲染畸形文本
        self.assertEqual(m.fmt_pct(float("inf")), "0.00%")
        self.assertEqual(m.fmt_pct(float("-inf")), "0.00%")
        self.assertEqual(m.fmt_pct(float("nan")), "0.00%")

    def test_fmt_pct_rounding(self):
        # %.2f 四舍五入，固定两位小数
        self.assertEqual(m.fmt_pct(0.999), "+1.00%")
        self.assertEqual(m.fmt_pct(1.005), "+1.00%")

    def test_fmt_pct_tiny(self):
        # v1.2.12：微小涨跌幅（|v|<0.005）四舍五入归零前直接判平，
        # 避免输出畸形 "+%"/"-%"（如 +0.002%）
        self.assertEqual(m.fmt_pct(0.0), "0.00%")
        self.assertEqual(m.fmt_pct(0.002), "0.00%")
        self.assertEqual(m.fmt_pct(-0.002), "0.00%")
        self.assertEqual(m.fmt_pct(0.0049), "0.00%")
        self.assertEqual(m.fmt_pct(-0.0049), "0.00%")
        self.assertEqual(m.fmt_pct(0.0001), "0.00%")
        # 临界值：0.005 起应正常显示，不得输出 "+%"/"-%"
        self.assertIn(m.fmt_pct(0.005), ("+0.01%", "+0.00%"))
        self.assertIn(m.fmt_pct(-0.005), ("-0.01%", "-0.00%"))
        # 边界外正常路径不受影响
        self.assertEqual(m.fmt_pct(0.0051), "+0.01%")
        self.assertEqual(m.fmt_pct(-0.0051), "-0.01%")

    def test_normalize_code(self):
        self.assertEqual(sf_utils.normalize_code("600519"), "sh600519")
        self.assertEqual(sf_utils.normalize_code("300750"), "sz300750")
        self.assertEqual(sf_utils.normalize_code("688981"), "sh688981")
        self.assertEqual(sf_utils.normalize_code("920002"), "bj920002")
        self.assertEqual(sf_utils.normalize_code("bj920002"), "bj920002")
        self.assertEqual(sf_utils.normalize_code("sh600519"), "sh600519")

    def test_normalize_code_rejects_illegal_prefix(self):
        # 2.1 建议项：非法输入仅 strip+lower 后原样返回，不误加前缀
        self.assertEqual(sf_utils.normalize_code("usAAPL"), "usaapl")
        self.assertEqual(sf_utils.normalize_code("hk00700"), "hk00700")
        self.assertEqual(sf_utils.normalize_code("xx123456"), "xx123456")
        self.assertEqual(sf_utils.normalize_code(""), "")


class TestFetchQuoteTolerance(unittest.TestCase):
    """3.1 / 4.1：北交所等板块字段容错与数据源语义自检（mock http_get）"""

    def tearDown(self):
        sf_quote.http_get = _ORIG_HTTP_GET

    def _mk(self, fields):
        payload = "~".join(str(x) for x in fields)

        def hg(code, **kw):
            return 'v_sh600519="%s";' % payload
        sf_quote.http_get = hg
        return m.fetch_quote("sh600519")

    def _base(self):
        f = ["1", "贵州茅台", "600519", "10.0", "9.9", "9.95"]
        f += [""] * 25      # 索引 6..30
        f += ["", "", "", ""]  # 索引 31..34
        return f

    def test_short_fields_bj(self):
        # 北交所/短字段：仅头部必填，扩展字段缺失返回空串
        q = self._mk(["1", "贵州茅台", "600519", "10.0", "9.9", "9.95"])
        self.assertEqual(q["name"], "贵州茅台")
        self.assertEqual(q["code"], "600519")
        # v1.2.65：出口处按品种归一小数位（股票 2 位）—— mock 里写的 "10.0" 是
        # 1 位，真实接口对股票恒返回 2 位（如 "12.10"），故归一后为 "10.00"。
        # 归一目的是让主源与备用源返回同一宽度，避免切源时字号抖动。
        self.assertEqual(q["price"], "10.00")
        self.assertEqual(q["pre_close"], "9.9")
        self.assertEqual(q["change"], "")
        self.assertEqual(q["high"], "")

    def test_full_fields(self):
        f = self._base()
        f[31], f[32], f[33], f[34] = "0.1", "1.01", "10.5", "9.8"
        q = self._mk(f)
        self.assertEqual(q["change"], "0.1")
        self.assertEqual(q["pct"], "1.01")
        self.assertEqual(q["high"], "10.5")
        self.assertEqual(q["low"], "9.8")

    def test_invalid_extension_fields_blank(self):
        f = self._base()
        f[31], f[32], f[33] = "abc", "nan", "10.5"
        q = self._mk(f)
        self.assertEqual(q["change"], "")
        self.assertEqual(q["pct"], "")
        self.assertEqual(q["high"], "10.5")

    def test_missing_code_raises(self):
        with self.assertRaises(m.QuoteDataError):
            self._mk(["1", "贵州茅台", "", "10.0", "9.9"])

    def test_too_few_fields_raises(self):
        with self.assertRaises(m.QuoteDataError):
            self._mk(["1", "贵州茅台", "600519"])

    def test_bad_price_raises(self):
        with self.assertRaises(m.QuoteDataError):
            self._mk(["1", "贵州茅台", "600519", "abc", "9.9"])

    def test_malformed_payload_raises(self):
        def hg(code, **kw):
            return "no equals here"
        sf_quote.http_get = hg
        with self.assertRaises(m.QuoteDataError):
            m.fetch_quote("sh600519")


class TestTokenConfig(unittest.TestCase):
    """v1.2.28：设置东财 TOKEN 功能已移除 —— Keychain 存取/迁移入口与封禁跟踪全删除。

    本轮（清理批）另删掉遗留空壳 `sf_keychain.py`：它的 `_load_token()` 自 v1.2.28 起恒定返回
    内置默认常量，而产品码从 v1.2.39 之后一次也没调用过它（`sf_quote.py:845` 直接从
    `sf_constants` 取 `EASTMONEY_TOKEN_DEFAULT`）。留着它只带来一个坏处：模块 docstring 还在
    声称"东财搜索经本模块取 token"，下一个人会照着这句话去找一条不存在的调用链。
    token 消费点本身由下面两条行为用例守着。
    """

    def test_default_token_constant(self):
        self.assertIsInstance(sf_constants.EASTMONEY_TOKEN_DEFAULT, str)
        self.assertTrue(sf_constants.EASTMONEY_TOKEN_DEFAULT)

    def test_window_no_keychain_imports(self):
        # stock_float_window 顶层不再引入 keychain token 符号（无残留引用）
        for sym in ("_load_token", "_keychain_token_set", "_keychain_token_get",
                    "_migrate_token_to_keychain"):
            self.assertFalse(hasattr(m, sym))

    def test_eastmoney_search_uses_builtin_token(self):
        # eastmoney 北交所搜索兜底不再依赖 _load_token，直接用内置默认 token 构造 URL
        self.assertFalse(hasattr(sf_quote, "_load_token"))
        self.assertFalse(hasattr(sf_quote, "mark_fallback_token_banned"))

    def test_eastmoney_url_carries_builtin_token(self):
        """行为锚点：北交所搜索真的用内置默认 token 拼出 URL 并解析出京A结果。

        取代原先"`sf_keychain._load_token()` 等于 `EASTMONEY_TOKEN_DEFAULT`"那条自指断言 ——
        它只证明一个函数返回了它自己要返回的常量，把消费点整个删掉它照样绿。
        """
        seen = {}

        def fake(url, **kw):
            seen["url"] = url
            return json.dumps({"QuotationCodeTable": {"Data": [
                {"Code": "835185", "Name": "贝特瑞", "SecurityTypeName": "京A"},
                {"Code": "600519", "Name": "贵州茅台", "MarketType": "A股"},
            ]}})

        sf_quote.http_get = fake
        try:
            out = sf_quote.eastmoney_search_bj("贝特瑞")
        finally:
            sf_quote.http_get = _ORIG_HTTP_GET
        # 只留京A/_TB：600519 那条属上交所，必须被过滤掉
        self.assertEqual(out, [("bj835185", "贝特瑞")])
        self.assertIn(sf_constants.EASTMONEY_TOKEN_DEFAULT, seen["url"])

    def test_eastmoney_url_follows_the_constant(self):
        """换掉模块上的 token 值，URL 必须跟着变 —— 证明那串是从常量取的而非写死。

        上一条按值断言有一个测不出的洞：若哪天有人把 token 字面量直接抄进 URL 拼装，
        两条的值仍然相等、用例仍绿。这条用哨兵值把"取常量"与"写死同一个值"分开。
        """
        seen = {}
        sentinel = "SENTINEL-TOKEN-FOR-TEST"
        orig = sf_quote.EASTMONEY_TOKEN_DEFAULT

        def fake(url, **kw):
            seen["url"] = url
            return json.dumps({"QuotationCodeTable": {"Data": []}})

        sf_quote.http_get = fake
        sf_quote.EASTMONEY_TOKEN_DEFAULT = sentinel
        try:
            sf_quote.eastmoney_search_bj("x")
        finally:
            sf_quote.EASTMONEY_TOKEN_DEFAULT = orig
            sf_quote.http_get = _ORIG_HTTP_GET
        self.assertIn(sentinel, seen["url"],
                      "URL 里的 token 不随 sf_quote.EASTMONEY_TOKEN_DEFAULT 变化，"
                      "说明拼装处写死了字面量")
        self.assertNotIn(orig, seen["url"])


class TestMarketTime(unittest.TestCase):
    """16.x：交易时段/休市状态边界（模块级单一实现，消除双份逻辑）"""

    def test_is_trading_time_weekend(self):
        self.assertFalse(m._is_trading_time(m.datetime.datetime(2026, 8, 23, 10, 0)))  # 周日

    def test_is_trading_time_boundaries(self):
        # 开盘竞价开始后保持实时行情刷新。
        self.assertTrue(m._is_trading_time(m.datetime.datetime(2026, 8, 24, 9, 30)))
        self.assertTrue(m._is_trading_time(m.datetime.datetime(2026, 8, 24, 9, 29)))
        # 11:30 上午收盘边界为交易中，11:31 午休
        self.assertTrue(m._is_trading_time(m.datetime.datetime(2026, 8, 24, 11, 30)))
        self.assertFalse(m._is_trading_time(m.datetime.datetime(2026, 8, 24, 11, 31)))
        # 13:00 下午开盘，15:00 收盘边界为交易中，15:01 已收盘
        self.assertTrue(m._is_trading_time(m.datetime.datetime(2026, 8, 24, 13, 0)))
        self.assertTrue(m._is_trading_time(m.datetime.datetime(2026, 8, 24, 15, 0)))
        self.assertFalse(m._is_trading_time(m.datetime.datetime(2026, 8, 24, 15, 1)))

    def test_market_status_weekend(self):
        self.assertEqual(m.market_status(m.datetime.datetime(2026, 8, 22, 10, 0)),
                         ("休市", m.FG_DIM))

    def test_market_status_trading_returns_none(self):
        self.assertIsNone(m.market_status(m.datetime.datetime(2026, 8, 24, 10, 0)))
        self.assertIsNone(m.market_status(m.datetime.datetime(2026, 8, 24, 15, 0)))

    def test_market_status_phases(self):
        self.assertEqual(m.market_status(m.datetime.datetime(2026, 8, 24, 9, 0)),
                         ("未开盘", m.FG_DIM))
        self.assertEqual(m.market_status(m.datetime.datetime(2026, 8, 24, 12, 0)),
                         ("午间休市", m.FG_DIM))
        self.assertEqual(m.market_status(m.datetime.datetime(2026, 8, 24, 15, 1)),
                         ("已收盘", m.FG_DIM))


class TestVersion(unittest.TestCase):
    """版本号三处一致性 —— 全仓**唯一**一处版本断言。

    v1.3.68 收敛：此前 12 个文件各硬编码一份 `assertEqual(APP_VERSION, "1.3.67")`，
    每次发版都要批量改 19 处，漏改即红，而失败与真实缺陷无关。
    版本号的真源只有 `sf_constants.APP_VERSION` 一处，这里守的是**三处必须一致**：
      ① APP_VERSION 本身格式合法（纯数字三段，不含 v 前缀或后缀脏字符）
      ② VERSION_TEXT 必须由它派生（很多界面文案直接拼这个串）
      ③ 打包产物 Info.plist 必须与源码版本同源（dist 存在才校验，未打包则跳过）
    任何一处要改版本，只改 sf_constants.APP_VERSION，这里会自动跟上。
    """

    def test_version_format(self):
        import re
        self.assertRegex(m.APP_VERSION, r"^\d+\.\d+\.\d+$",
                         "APP_VERSION 必须是纯三段数字（例如 1.3.68），不得含 v 前缀或后缀")

    def test_version_text_derived(self):
        from sf_constants import VERSION_TEXT
        self.assertEqual(VERSION_TEXT, "v" + m.APP_VERSION,
                         "VERSION_TEXT 必须由 APP_VERSION 派生，否则界面版本文案会与实际版本分叉")

    # 说明：产物 Info.plist 与源码版本是否同源，不由这里校验 ——
    # 正常发版流程是"先改代码跑回归、后打包"，此刻 dist/ 必然还是上一版，
    # 在这里断言只会让每次发版第一步就红。这道闸门由 build_release.sh 调用
    # tools/dist_version_check.py 承担（拦的是"拿旧 dist 打当前版本的包"）。


class TestQuoteStatus(unittest.TestCase):
    """1.1 v1.2.0：涨停/跌停/停牌状态判定（纯函数）"""

    def _q(self, price="10.0", pre="9.9", vol="10000", up="11.0", down="8.9"):
        q = {"price": price, "pre_close": pre, "volume": vol,
             "limit_up": up, "limit_down": down}
        return q

    def test_limit_up(self):
        # 交易时段 price>=limit_up → 涨停
        now = m.datetime.datetime(2026, 8, 24, 10, 0)  # 周一 10:00
        self.assertEqual(sf_quote.quote_status(self._q(price="11.0"), now), "涨停")

    def test_limit_down(self):
        now = m.datetime.datetime(2026, 8, 24, 10, 0)
        self.assertEqual(sf_quote.quote_status(self._q(price="8.9"), now), "跌停")

    def test_normal(self):
        now = m.datetime.datetime(2026, 8, 24, 10, 0)
        self.assertIsNone(sf_quote.quote_status(self._q(price="10.3"), now))

    def test_suspend_trading_hour(self):
        # 交易时段 price=0 → 停牌
        now = m.datetime.datetime(2026, 8, 24, 10, 0)
        self.assertEqual(sf_quote.quote_status(self._q(price="0", vol="0"), now), "停牌")

    def test_price_zero_outside_trading_not_suspend(self):
        # 非交易时段 price=0 属收盘态，不误报停牌（也无涨跌停字段）
        now = m.datetime.datetime(2026, 8, 24, 16, 0)  # 周一 16:00 已收盘
        q = self._q(price="0", pre="9.9", vol="0")
        # limit_up/volume 字段存在但非交易时段 → 停牌判定跳过，返回 None
        self.assertIsNone(sf_quote.quote_status(q, now))

    def test_missing_limit_fields_skips(self):
        # 备用源（新浪）无 limit_up/limit_down/volume 字段 → 不误报停牌/涨跌停
        now = m.datetime.datetime(2026, 8, 24, 10, 0)
        q = {"price": "0", "pre_close": "9.9"}  # 无 volume/limit_up/limit_down
        self.assertIsNone(sf_quote.quote_status(q, now))
        q2 = {"price": "11.0", "pre_close": "9.9"}  # 无涨跌停价
        self.assertIsNone(sf_quote.quote_status(q2, now))

    def test_non_dict(self):
        self.assertIsNone(sf_quote.quote_status(None))
        self.assertIsNone(sf_quote.quote_status("not-a-dict"))

    def test_bad_numeric(self):
        now = m.datetime.datetime(2026, 8, 24, 10, 0)
        q = self._q(price="abc", up="xx")
        self.assertIsNone(sf_quote.quote_status(q, now))


class TestFetchQuoteLimitFields(unittest.TestCase):
    """1.1 v1.2.0：_fetch_quote_primary 解析 volume/limit_up/limit_down（含缺失容错）"""

    def tearDown(self):
        sf_quote.http_get = _ORIG_HTTP_GET

    def _mk(self, fields):
        payload = "~".join(str(x) for x in fields)

        def hg(code, **kw):
            return 'v_sh600519="%s";' % payload
        sf_quote.http_get = hg
        return m.fetch_quote("sh600519")

    def _base(self):
        f = ["1", "贵州茅台", "600519", "10.0", "9.9", "9.95"]
        f += [""] * 30   # 索引 6..35
        f += [""] * 14   # 索引 36..49（含涨停 47 / 跌停 48）
        return f

    def test_limit_fields_parsed(self):
        f = self._base()
        f[36], f[47], f[48] = "340767", "11.0", "8.9"
        q = self._mk(f)
        self.assertEqual(q["volume"], "340767")
        self.assertEqual(q["limit_up"], "11.0")
        self.assertEqual(q["limit_down"], "8.9")

    def test_limit_fields_missing_blank(self):
        # 短字段（新浪风格）缺失 → 返回空串
        f = self._base()
        q = self._mk(f)
        self.assertEqual(q["volume"], "")
        self.assertEqual(q["limit_up"], "")
        self.assertEqual(q["limit_down"], "")

    def test_invalid_limit_fields_blank(self):
        f = self._base()
        f[36], f[47] = "nan", "abc"
        q = self._mk(f)
        self.assertEqual(q["volume"], "")
        self.assertEqual(q["limit_up"], "")


class TestFetchQuoteFallback(unittest.TestCase):
    """4.1 v1.2.0：主源失败自动切备用源；主备都失败抛异常"""

    def tearDown(self):
        sf_quote.http_get = _ORIG_HTTP_GET

    def test_primary_ok_uses_tencent(self):
        # 主源返回合法数据 → 直接成功，数据源 tencent
        sf_quote.http_get = lambda code, **kw: 'v_sh600519="%s";' % "~".join(
            ["1", "贵州茅台", "600519", "10.0", "9.9", "9.95"])
        sf_quote._LAST_QUOTE_SOURCE = None
        q = m.fetch_quote("sh600519")
        self.assertEqual(q["name"], "贵州茅台")
        self.assertEqual(sf_quote._LAST_QUOTE_SOURCE, "tencent")

    def test_primary_raises_network_falls_back(self):
        # 主源抛网络异常（非 QuoteDataError）→ 走备用源
        def hg(url, **kw):
            if "qt.gtimg.cn" in url:
                raise OSError("network down")
            # 新浪备用源格式：0名称 1今开 2昨收 3现价 4最高 5最低（需≥6字段）
            return 'var hq_str_sh600519="贵州茅台,10.1,9.9,10.0,10.5,9.8";'
        sf_quote.http_get = hg
        sf_quote._LAST_QUOTE_SOURCE = None
        q = m.fetch_quote("sh600519")
        self.assertEqual(q["name"], "贵州茅台")
        self.assertEqual(sf_quote._LAST_QUOTE_SOURCE, "sina")

    def test_both_fail_raises(self):
        def hg(code, **kw):
            raise OSError("all down")
        sf_quote.http_get = hg
        with self.assertRaises(OSError):
            m.fetch_quote("sh600519")


class TestHttpGetStatus(unittest.TestCase):
    """6.0 v1.2.0：http_get 记录最近 HTTP 状态码；异常抛出"""

    def tearDown(self):
        sf_quote.http_get = _ORIG_HTTP_GET

    def test_status_recorded(self):
        import urllib.request

        class _FakeResp:
            status = 200

            def read(self, count=-1):
                return b"ok"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        orig_urlopen = sf_network.safe_urlopen
        try:
            sf_network.safe_urlopen = lambda req, timeout=4: _FakeResp()
            sf_network._LAST_HTTP_STATUS = None
            self.assertEqual(sf_quote.http_get("https://x", retries=0), "ok")
            self.assertEqual(sf_network._LAST_HTTP_STATUS, 200)
        finally:
            sf_network.safe_urlopen = orig_urlopen

    def test_timeout_raises(self):
        import urllib.request
        orig_urlopen = sf_network.safe_urlopen
        try:
            def boom(req, timeout=4):
                raise urllib.error.URLError("timeout")
            sf_network.safe_urlopen = boom
            with self.assertRaises(Exception):
                sf_quote.http_get("https://x", retries=0)
        finally:
            sf_network.safe_urlopen = orig_urlopen


class TestConfigSchemaVersion(unittest.TestCase):
    """5.0/1.3 v1.2.0：config 结构版本号兼容 + api_urls 保留与覆盖"""

    def setUp(self):
        self._orig_path = m.CONFIG_PATH
        self._orig_path_sfc = sf_config.CONFIG_PATH
        self._orig_path_quote = sf_quote.CONFIG_PATH
        self._dir = tempfile.mkdtemp()

    def tearDown(self):
        m.CONFIG_PATH = self._orig_path
        sf_config.CONFIG_PATH = self._orig_path_sfc
        sf_quote.CONFIG_PATH = self._orig_path_quote

    def _write(self, d):
        p = os.path.join(self._dir, "config.json")
        m.CONFIG_PATH = p
        sf_config.CONFIG_PATH = p
        sf_quote.CONFIG_PATH = p
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f)

    def test_load_config_no_version_ok(self):
        # 旧配置无 version → 可正常加载（视为 v1）
        self._write({"code": "sh600000", "x": 1, "y": 2, "h": 240, "bg": "glass"})
        code, x, y, h, bg = m.load_config()
        self.assertEqual((code, x, y, h, bg), ("sh600000", 1, 2, 240, "glass"))

    def test_save_config_preserves_version_and_api_urls(self):
        self._write({"code": "sh600000", "x": 1, "y": 2, "h": 240, "bg": "glass",
                     "version": 1,
                     "api_urls": {"quote": "https://custom.example/q=%s"}})
        m.save_config("sh600000", 1, 2, 240, "glass")
        with open(m.CONFIG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["version"], sf_constants.CONFIG_SCHEMA_VERSION)
        self.assertEqual(d["api_urls"]["quote"], "https://custom.example/q=%s")
        self.assertEqual(d["code"], "sh600000")

    def test_load_api_urls_overrides_globals(self):
        self._write({"code": "sh600000",
                     "api_urls": {"quote": "https://custom.example/q=%s",
                                  "sina_referer": "https://custom.referer/"}})
        from tests.test_quality_credentials import MemoryKeychain
        with unittest.mock.patch("sf_credentials.MacKeychain", return_value=MemoryKeychain()):
            changed = m.load_api_urls()
        self.assertIn("quote", changed)
        self.assertEqual(sf_quote.QUOTE_URL, "https://custom.example/q=%s")
        self.assertEqual(sf_quote.SINA_REFERER, "https://custom.referer/")
        # 还原全局常量，避免影响后续用例
        sf_quote.QUOTE_URL = sf_constants.API_URLS_DEFAULT["quote"]
        sf_quote.SINA_REFERER = sf_constants.API_URLS_DEFAULT["sina_referer"]

    def test_load_api_urls_no_config_silent(self):
        sf_quote.CONFIG_PATH = os.path.join(self._dir, "missing.json")
        self.assertEqual(m.load_api_urls(), [])


class TestDropFlash(unittest.TestCase):
    """v1.2.11 下跌闪烁触发判定（纯函数 _drop_flash_due）"""

    def test_pct4_trigger(self):
        # 跌幅 -4.0 首次到达 4% 档，触发
        self.assertTrue(m._drop_flash_due(-4.0, 4.0, None, 100.0, 10.0))

    def test_pct4_below_threshold_no_trigger(self):
        # -3.99 未达 4%，不触发
        self.assertFalse(m._drop_flash_due(-3.99, 4.0, None, 100.0, 10.0))

    def test_pct75_trigger(self):
        # 跌幅 -7.5 到达 7.5% 档，触发（4% 档与 7.5% 档独立）
        self.assertTrue(m._drop_flash_due(-7.5, 7.5, None, 100.0, 10.0))

    def test_pct75_below_threshold_no_trigger(self):
        # -7.4 未达 7.5%，不触发
        self.assertFalse(m._drop_flash_due(-7.4, 7.5, None, 100.0, 10.0))

    def test_cooldown_suppress_repeat(self):
        # 同档触发后 10s 内（last=95, now=100, 差 5s）不重复
        self.assertFalse(m._drop_flash_due(-8.0, 4.0, 95.0, 100.0, 10.0))

    def test_retrigger_after_cooldown(self):
        # 冷却期过后（last=89.9, now=100, 差 10.1s）可再次触发
        self.assertTrue(m._drop_flash_due(-8.0, 4.0, 89.9, 100.0, 10.0))

    def test_boundary_cooldown_exact(self):
        # 恰好满 10s（差 = 10.0）不视为冷却期内，可触发
        self.assertTrue(m._drop_flash_due(-8.0, 4.0, 90.0, 100.0, 10.0))

    def test_cross_threshold_no_false_trigger(self):
        # 跌到 -6% 仅穿越 4% 档；7.5% 档判定不误触发
        self.assertFalse(m._drop_flash_due(-6.0, 7.5, None, 100.0, 10.0))
        self.assertTrue(m._drop_flash_due(-6.0, 4.0, None, 100.0, 10.0))

    def test_rise_never_triggers(self):
        # 2026-09-03 用户确认：只有下跌才闪。上涨/平盘一律不触发（对照：下跌仍触发）
        self.assertFalse(m._drop_flash_due(4.0, 4.0, None, 100.0, 10.0))   # 涨 4% 不触发
        self.assertFalse(m._drop_flash_due(7.5, 7.5, None, 100.0, 10.0))  # 涨 7.5% 不触发
        self.assertFalse(m._drop_flash_due(0.0, 4.0, None, 100.0, 10.0))  # 平盘不触发
        self.assertFalse(m._drop_flash_due(0.0, 7.5, None, 100.0, 10.0))  # 平盘不触发
        self.assertTrue(m._drop_flash_due(-4.0, 4.0, None, 100.0, 10.0))  # 下跌仍触发（对照）

    def test_two_levels_independent_cooldown(self):
        # 两档独立冷却：4% 档刚触发冷却中，7.5% 档首次仍应触发
        last = {"pct4": 95.0}
        self.assertFalse(m._drop_flash_due(-8.0, 4.0, last.get("pct4"), 100.0, 10.0))
        self.assertTrue(m._drop_flash_due(-8.0, 7.5, last.get("pct75"), 100.0, 10.0))

    def test_bad_input_safe(self):
        # 异常/非数值输入安全返回 False，不抛异常
        self.assertFalse(m._drop_flash_due(None, 4.0, None, 100.0, 10.0))
        self.assertFalse(m._drop_flash_due("bad", 4.0, None, 100.0, 10.0))


class TestUrlSanitize(unittest.TestCase):
    """v1.2.12：日志/监控埋点 URL 脱敏（剥离 token 等敏感参数）"""

    def test_strip_eastmoney_token(self):
        url = ("https://searchapi.eastmoney.com/api/suggest/get?"
               "input=%E8%8C%85%E5%8F%B0&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=10")
        out = sf_network.sanitize_url_for_log(url)
        self.assertNotIn("D43BF722C8E33BDC906FB84D85E326E8", out)
        self.assertNotIn("token=", out)
        # 非敏感参数保留，便于诊断
        self.assertIn("input=", out)
        self.assertIn("count=10", out)
        self.assertTrue(out.startswith("https://searchapi.eastmoney.com/api/suggest/get"))

    def test_keep_innocuous_params(self):
        url = "https://qt.gtimg.cn/q=sh600519&invt=2&fltt=1"
        out = sf_network.sanitize_url_for_log(url)
        self.assertIn("sh600519", out)
        self.assertIn("invt=2", out)
        self.assertIn("fltt=1", out)

    def test_strip_userinfo_and_fragment(self):
        url = "https://user:secret@host.example/path?key=abc#frag"
        out = sf_network.sanitize_url_for_log(url)
        self.assertNotIn("secret", out)
        self.assertNotIn("key=abc", out)
        self.assertNotIn("frag", out)
        self.assertTrue(out.startswith("https://host.example/path"))

    def test_blank_query_gives_host_path(self):
        out = sf_network.sanitize_url_for_log("https://push2.eastmoney.com/api/qt/stock/get")
        self.assertEqual(out, "https://push2.eastmoney.com/api/qt/stock/get")

    def test_garbage_falls_back_without_crash(self):
        # 非 URL 输入：无法解析出 host+path，保底不抛异常、不崩溃
        out = sf_network.sanitize_url_for_log("not a url")
        self.assertIsInstance(out, str)
        # 完整 URL 的敏感 token 一律被剥离（见 test_strip_eastmoney_token），
        # 此处仅验证无 scheme 输入不会导致崩溃
        self.assertEqual(sf_network.sanitize_url_for_log(""), "")


class TestQuitWaitTimeout(unittest.TestCase):
    """v1.2.12：退出时等待搜索线程设置超时上限，网络卡住也不阻塞退出"""

    def test_wait_timeout_does_not_block_forever(self):
        import time

        class _FakeSelf:
            pass

        class _SlowThread:
            def wait(self, ms):
                time.sleep(0.1)
                return True

        fake = _FakeSelf()
        t0 = time.monotonic()
        m.FloatWindow._quit_after_threads(fake, [_SlowThread()])
        dt = time.monotonic() - t0
        # 在超时上限（2s）内放行、不抛异常
        self.assertLess(dt, 1.5)


class TestFetchQuoteFallbackFiniteHighLow(unittest.TestCase):
    """改动2：新浪备用源 high/low 非有限值（nan/inf）必须归空串，与主源防御语义一致。

    主源（腾讯）扩展字段已做 isfinite 校验，备用源此前直接透传 f[4]/f[5]，
    NaN/inf 可能进入状态判定。修复后备用源 high/low 与缺失一致返回 ''，下游隐藏该值。
    """

    def tearDown(self):
        sf_quote.http_get = _ORIG_HTTP_GET

    def _fetch(self, high, low):
        payload = 'var hq_str_sh600519="贵州茅台,10.1,9.9,10.0,%s,%s";' % (high, low)

        def hg(url, **kw):
            return payload
        sf_quote.http_get = hg
        return sf_quote.fetch_quote_fallback("sh600519")

    def test_nan_high_low_blank(self):
        q = self._fetch("nan", "inf")
        self.assertEqual(q["high"], "")
        self.assertEqual(q["low"], "")

    def test_inf_high_low_blank(self):
        q = self._fetch("inf", "nan")
        self.assertEqual(q["high"], "")
        self.assertEqual(q["low"], "")

    def test_normal_high_low_passthrough(self):
        q = self._fetch("10.5", "9.8")
        self.assertEqual(q["high"], "10.5")
        self.assertEqual(q["low"], "9.8")

    def test_isfinite_helper(self):
        # 新增辅助函数 _is_finite_str 与各分支语义自检
        self.assertTrue(sf_quote._is_finite_str("10.5"))
        self.assertTrue(sf_quote._is_finite_str("-3.14"))
        self.assertFalse(sf_quote._is_finite_str("nan"))
        self.assertFalse(sf_quote._is_finite_str("inf"))
        self.assertFalse(sf_quote._is_finite_str(""))
        self.assertFalse(sf_quote._is_finite_str("abc"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
