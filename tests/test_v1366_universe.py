# -*- coding: utf-8 -*-
"""V1.3.66 本地股票字典（sf_universe）防回归测试。

守护三件事：
  1. **接管范围** —— 只接管"确定无疑"的搜索（代码精确/名称精确/唯一包含），
     多结果模糊词必须交回联网，否则本地按代码序给出的首条会顶掉搜索引擎的
     相关性排序（实测：搜"贵州"本机会给贵州轮胎，搜索引擎给贵州茅台）。
  2. **市场判定** —— 920xxx 是北交所新代码段，按"9 开头=沪市"判会把贝特瑞
     (920185) 错判成 sh。这是本版探针实测踩到的坑。
  3. **测试环境默认停用** —— 字典一旦在单测里生效，会给打桩用例注入真实数据。
"""

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_quote

# 本文件用 `importlib.reload(sf_universe)` 拿"干净实例"，但 reload 返回的是**同一个
# 模块对象**，不是副本 —— 往它身上打的补丁和改的环境变量会留在整个测试进程里。
# 随机执行顺序下这已经真咬过人（v1.4.45 复核 C1）：`test_pick_host_*` 拿到的是这里
# 留下的 `lambda: "mock.example"`，红成"别的模块不稳定"。所以统一在 tearDownModule
# 里还原，别靠用例自觉。
_ORIG_UNIVERSE_DISABLE = os.environ.get("SF_UNIVERSE_DISABLE")


def tearDownModule():
    """模块跑完后把环境与模块函数交还给进程（unittest loader 会自动调它）。"""
    if _ORIG_UNIVERSE_DISABLE is None:
        os.environ.pop("SF_UNIVERSE_DISABLE", None)
    else:
        os.environ["SF_UNIVERSE_DISABLE"] = _ORIG_UNIVERSE_DISABLE
    import sf_universe
    importlib.reload(sf_universe)


def _fresh_universe(rows, enable=True, raw=None):
    """在临时目录里放一份假字典，重新加载 sf_universe 拿到干净实例。

    注意：UNIVERSE_PATH 是 sf_constants 在**首次导入时**就定好的常量，
    reload sf_universe 不会让它重读环境变量（sf_constants 不重新执行）。
    所以必须直接改模块全局 u.UNIVERSE_PATH，否则写的文件根本没被读到。
    """
    path = os.path.join(tempfile.mkdtemp(), "sec.json")
    with open(path, "w", encoding="utf-8") as f:
        if raw is not None:
            f.write(raw)
        else:
            json.dump({"ts": time.time(), "count": len(rows), "rows": rows}, f,
                      ensure_ascii=False)
    os.environ["SF_UNIVERSE_DISABLE"] = "0" if enable else "1"
    import sf_universe
    u = importlib.reload(sf_universe)
    u.UNIVERSE_PATH = path
    return u


def _big(rows):
    """load() 少于 1000 条视为残缺，测试用小字典前先垫到 1000 条。"""
    out = list(rows)
    i = 0
    while len(out) < 1000:
        out.append(["sz%06d" % (900000 + i), "占位%d" % i])
        i += 1
    return out


class TestMarketOf(unittest.TestCase):
    def test_bj_codes(self):
        u = _fresh_universe([])
        # 920xxx 是北交所新代码段：放在沪市规则之前判，否则会掉进 "9 开头=沪市"
        self.assertEqual(u._market_of("920185"), "bj")   # 贝特瑞
        self.assertEqual(u._market_of("920368"), "bj")   # 连城数控
        self.assertEqual(u._market_of("835368"), "bj")
        self.assertEqual(u._market_of("430047"), "bj")

    def test_sh_sz_codes(self):
        u = _fresh_universe([])
        self.assertEqual(u._market_of("600519"), "sh")
        self.assertEqual(u._market_of("688981"), "sh")
        self.assertEqual(u._market_of("000001"), "sz")
        self.assertEqual(u._market_of("300750"), "sz")
        self.assertEqual(u._market_of("002594"), "sz")


class TestMatchConfident(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = _big([
            ["sh600519", "贵州茅台"],
            ["sz000001", "平安银行"],
            ["sh601318", "中国平安"],
            ["sz300750", "宁德时代"],
            ["bj920185", "贝特瑞"],
            ["sz000589", "贵州轮胎"],
        ])
        cls.u = _fresh_universe(rows)
        cls.u.load(force=True)
        cls.n = len(cls.u._rows)

    def test_dict_loaded(self):
        self.assertGreaterEqual(self.n, 1000)

    def test_code_exact(self):
        self.assertEqual(self.u.match_confident("600519")[0][0], "sh600519")
        self.assertEqual(self.u.match_confident("sh600519")[0][0], "sh600519")
        self.assertEqual(self.u.match_confident("920185")[0][0], "bj920185")

    def test_name_exact(self):
        self.assertEqual(self.u.match_confident("贵州茅台")[0][0], "sh600519")
        self.assertEqual(self.u.match_confident("宁德时代")[0][0], "sz300750")

    def test_unique_contains(self):
        # 全表只有"贵州茅台"含"茅台" -> 唯一，可安全接管
        self.assertEqual(self.u.match_confident("茅台")[0][0], "sh600519")
        self.assertEqual(self.u.match_confident("宁德")[0][0], "sz300750")

    def test_ambiguous_not_taken(self):
        # "贵州" 命中 2 条（贵州茅台/贵州轮胎）—— 谁排第一是相关性问题，
        # 必须交回联网，不能让本地的代码序说了算。
        self.assertEqual(self.u.match_confident("贵州"), [])
        self.assertEqual(len(self.u.match_fuzzy("贵州")), 2)

    def test_no_match(self):
        self.assertEqual(self.u.match_confident("绝无此股xyz"), [])
        self.assertEqual(self.u.match_confident(""), [])
        self.assertEqual(self.u.match_confident("   "), [])


class TestNameNormalization(unittest.TestCase):
    """东财部分名称带空格（实测返回 "五 粮 液"），不归一化会漏掉一整类股票。"""

    def setUp(self):
        rows = _big([["sz000858", "五 粮 液"], ["sh600519", "贵州茅台"]])
        self.u = _fresh_universe(rows)
        self.u.load(force=True)

    def test_strip_spaces_in_dict(self):
        self.assertEqual(self.u.match_confident("五粮液")[0][0], "sz000858")
        # 返回给上游的名称也必须是不带空格的，否则界面上会显示成 "五 粮 液"
        self.assertEqual(self.u.match_confident("五粮液")[0][1], "五粮液")

    def test_keyword_with_spaces(self):
        self.assertEqual(self.u.match_confident("五 粮 液")[0][0], "sz000858")

    def test_code_unaffected(self):
        self.assertEqual(self.u.match_confident("000858")[0][0], "sz000858")


class TestLoadGuard(unittest.TestCase):
    def test_reject_truncated_dict(self):
        u = _fresh_universe([["sh600519", "贵州茅台"]])
        # 只有 1 条 -> 视为残缺，宁可退回联网也不用半张表
        self.assertFalse(u.load(force=True))
        self.assertEqual(u.match_confident("600519"), [])

    def test_reject_broken_file(self):
        u = _fresh_universe([], raw="{ this is not json")
        self.assertFalse(u.load(force=True))


class TestRefreshWrite(unittest.TestCase):
    """刷新落盘的临时文件必须独立，双实例不能互相踩掉对方文件。"""

    def test_refresh_uses_unique_temp_name(self):
        u = _fresh_universe([])
        u.UNIVERSE_MIN_ROWS = 1
        u.UNIVERSE_MAX_PAGES = 1
        u.UNIVERSE_WORKERS = 1
        u.UNIVERSE_PAGE_GAP = 0
        u.UNIVERSE_PAGE_RETRIES = 0
        calls = []
        original_replace = u.os.replace

        def capture_replace(src, dst):
            calls.append((src, dst))
            return original_replace(src, dst)

        # 一律用 patch.object 而不是 `u.xxx = lambda...`：reload 拿到的是同一个模块，
        # 直接赋值会把桩留在进程里，随机序后面的模块就会拿到 "mock.example"（复核 C1）。
        with mock.patch.object(u, "_pick_host", lambda: "mock.example"), \
             mock.patch.object(u, "_fetch_page",
                               lambda host, pn, pz=1:
                               [{"f12": "600519", "f14": "贵州茅台"}]), \
             mock.patch.object(u.os, "replace", capture_replace):
            self.assertTrue(u._refresh_once())
        self.assertEqual(len(calls), 1)
        src, dst = calls[0]
        self.assertEqual(dst, u.UNIVERSE_PATH)
        self.assertTrue(src.startswith(u.UNIVERSE_PATH + "."))
        self.assertTrue(src.endswith(".tmp"))
        self.assertNotEqual(src, u.UNIVERSE_PATH + ".tmp",
                            "固定 .tmp 会在多实例同时刷新时互相踩踏")
        self.assertFalse(os.path.exists(src), "replace 后不得残留临时文件")


class TestSearchIntegration(unittest.TestCase):
    """字典命中时不得再发网络请求；未命中时行为与旧版一致。"""

    def setUp(self):
        self._orig = sf_quote.universe
        self._orig_cache = dict(sf_quote._SEARCH_CACHE)
        sf_quote._SEARCH_CACHE.clear()

    def tearDown(self):
        sf_quote.universe = self._orig
        sf_quote._SEARCH_CACHE.clear()
        sf_quote._SEARCH_CACHE.update(self._orig_cache)

    def test_hit_skips_network(self):
        class FakeU(object):
            @staticmethod
            def match_confident(kw):
                return [("sh600519", "贵州茅台")] if kw == "600519" else []

            @staticmethod
            def match_fuzzy(kw, limit=50):
                return []

            @staticmethod
            def ensure_ready():
                raise AssertionError("命中本地字典时不应触发后台刷新")

        sf_quote.universe = FakeU
        calls = []

        def fake_http_get(url, *a, **kw):
            calls.append(url)
            return ""

        orig = sf_quote.http_get
        sf_quote.http_get = fake_http_get
        try:
            out = sf_quote.search_stocks("600519")
        finally:
            sf_quote.http_get = orig
        self.assertEqual(out, [("sh600519", "贵州茅台")])
        self.assertEqual(calls, [], "本地命中时不应有任何网络请求")

    def test_miss_falls_back_to_network(self):
        class FakeU(object):
            @staticmethod
            def match_confident(kw):
                return []

            @staticmethod
            def match_fuzzy(kw, limit=50):
                return []

            @staticmethod
            def ensure_ready():
                return None

        sf_quote.universe = FakeU
        orig = sf_quote.http_get
        # 打桩返回一条腾讯结果，验证联网路径照旧
        sf_quote.http_get = lambda url, *a, **kw: (
            'v_hint="sh~600519~贵州茅台~~GP-A~;"' if "smartbox" in url else "")
        try:
            out = sf_quote.search_stocks("茅台")
        finally:
            sf_quote.http_get = orig
        self.assertEqual(out, [("sh600519", "贵州茅台")])

    def test_network_empty_uses_local_fuzzy(self):
        class FakeU(object):
            @staticmethod
            def match_confident(kw):
                return []

            @staticmethod
            def match_fuzzy(kw, limit=50):
                return [("bj920185", "贝特瑞")]

            @staticmethod
            def ensure_ready():
                return None

        sf_quote.universe = FakeU
        orig = sf_quote.http_get
        sf_quote.http_get = lambda url, *a, **kw: ""
        try:
            out = sf_quote.search_stocks("贝特瑞")
        finally:
            sf_quote.http_get = orig
        # 联网 0 结果（腾讯不覆盖北交所）-> 本地字典救场
        self.assertEqual(out, [("bj920185", "贝特瑞")])

    def test_universe_error_never_breaks_search(self):
        class BoomU(object):
            @staticmethod
            def match_confident(kw):
                raise RuntimeError("boom")

            @staticmethod
            def match_fuzzy(kw, limit=50):
                raise RuntimeError("boom")

            @staticmethod
            def ensure_ready():
                raise RuntimeError("boom")

        sf_quote.universe = BoomU
        orig = sf_quote.http_get
        sf_quote.http_get = lambda url, *a, **kw: (
            'v_hint="sz~000001~平安银行~~GP-A~;"' if "smartbox" in url else "")
        try:
            out = sf_quote.search_stocks("平安银行")
        finally:
            sf_quote.http_get = orig
        self.assertEqual(out, [("sz000001", "平安银行")])


class TestDisabledUnderUnittest(unittest.TestCase):
    def test_default_disabled_in_tests(self):
        # 单测环境下必须默认停用：否则真实字典会污染其它用例的打桩结果
        import sf_universe
        u = importlib.reload(sf_universe)
        self.assertTrue(u._DISABLED)


if __name__ == "__main__":
    unittest.main()
