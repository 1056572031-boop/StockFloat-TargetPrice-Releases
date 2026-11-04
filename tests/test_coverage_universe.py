# -*- coding: utf-8 -*-
"""sf_universe（本地股票字典）补测：装载、匹配范围、翻页刷新与后台刷新调度。

为什么单独补这一层（本轮为扩大自动化覆盖而加）
----------------------------------------------
字典决定"搜索是本地 0.5ms 还是联网 56ms"，也决定断网时还能不能搜到股票。
test_v1366_universe.py 已守"接管范围只接确定无疑的搜索"这条主线，本文件补它
没走到的分支：装载的残缺判定、刷新链路的翻页/重试/去重/缺页不写盘、
后台刷新的幂等与最小间隔、以及写盘失败时不留坏文件。

实现口径：不改 SF_UNIVERSE_DISABLE 也不 reload 模块（reload 会让 sf_quote 手里
的引用换一份，跨用例串味），而是**保存-改-还原**模块全局。
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_universe as u          # noqa: E402

try:
    from _base import SFCase, wait_for
except ImportError:               # pragma: no cover - 包模式导入
    from tests._base import SFCase, wait_for

_SAVED_GLOBALS = (
    "_DISABLED", "UNIVERSE_PATH", "_rows", "_by_code", "_by_name", "_loaded",
    "_refresh_started", "_last_attempt", "_last_error", "UNIVERSE_MAX_PAGES",
    "UNIVERSE_PAGE_GAP", "UNIVERSE_WORKERS", "UNIVERSE_MIN_ROWS",
    "UNIVERSE_PAGE_RETRIES", "UNIVERSE_TTL", "UNIVERSE_MIN_INTERVAL", "_server_total",
)


class UniverseCase(SFCase):
    """干净的字典状态：每个用例一套全局，tearDown 原样还原。"""

    def setUp(self):
        super().setUp()
        self._saved = {k: getattr(u, k) for k in _SAVED_GLOBALS}
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "securities.json")
        u._DISABLED = False
        u.UNIVERSE_PATH = self.path
        u._rows, u._by_code, u._by_name = [], {}, {}
        u._loaded = False
        u._refresh_started = False
        u._last_attempt = 0.0
        u._last_error = None
        # 刷新默认参数：测试里不要真的等页间隔、不要重试 4 次
        u.UNIVERSE_MAX_PAGES = 3
        u.UNIVERSE_WORKERS = 2
        u.UNIVERSE_PAGE_GAP = 0
        u.UNIVERSE_PAGE_RETRIES = 0
        u.UNIVERSE_MIN_ROWS = 1
        u.UNIVERSE_MIN_INTERVAL = 0

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(u, k, v)
        super().tearDown()

    def _seed(self, rows):
        u._index(rows)

    def _write_rows(self, rows, wrap=True, count=None):
        payload = {"ts": time.time(), "count": count if count is not None else len(rows),
                   "rows": [[c, n] for c, n in rows]}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(payload if wrap else rows, f, ensure_ascii=False)


# --------------------------------------------------------------------------
# 纯函数：市场判定 / 名称归一 / URL
# --------------------------------------------------------------------------

class TestMarketAndNaming(UniverseCase):
    def test_beijing_new_code_range_wins_over_the_shanghai_nine_rule(self):
        """920xxx 是北交所新代码段 —— 按"9 开头=沪市"判会把贝特瑞错判成 sh。"""
        self.assertEqual(u._market_of("920185"), "bj")
        self.assertEqual(u._market_of("900901"), "sh")      # 沪市 B 股
        self.assertEqual(u._market_of("200011"), "sz")      # 深市 B 股
        for code in ("430047", "830799", "873122", "880001"):
            self.assertEqual(u._market_of(code), "bj", code)
        for code in ("600519", "688981"):
            self.assertEqual(u._market_of(code), "sh", code)
        for code in ("000001", "001979", "002594", "003816", "300750", "301236"):
            self.assertEqual(u._market_of(code), "sz", code)

    def test_unknown_ranges_return_empty_so_refresh_can_drop_them(self):
        for code in ("", "12345", "abcdef", "500001"):
            self.assertEqual(u._market_of(code), "", code)

    def test_names_with_embedded_spaces_are_normalised(self):
        """东财实测返回 "五 粮 液"，不去空格则精确/包含匹配双双落空。"""
        self.assertEqual(u._norm_name("五 粮 液"), "五粮液")
        self.assertEqual(u._norm_name(" 贵州\t茅台\n"), "贵州茅台")
        self.assertEqual(u._norm_name(None), "")

    def test_index_normalises_names_and_keeps_the_first_on_duplicates(self):
        u._index([("sz000001", "平安 银行"), ("sh600000", "浦发银行"),
                  ("sz000002", "平安银行")])
        self.assertEqual(u._by_code["sz000001"], "平安银行")
        self.assertEqual(u._by_name["平安银行"], "sz000001",
                         "重名保留先出现的，后写的不得覆盖")

    def test_page_url_sorts_by_code_not_by_change(self):
        """按涨跌幅排序翻页会漂移（实测 60 页只捞回 3013/5913 条）。"""
        url = u._page_url("push2delay.eastmoney.com", 7, 50)
        self.assertIn("fid=f12", url)
        self.assertNotIn("fid=f3", url)
        self.assertIn("push2delay.eastmoney.com", url)
        self.assertIn("pn=7", url)
        self.assertIn("pz=50", url)


# --------------------------------------------------------------------------
# 磁盘装载
# --------------------------------------------------------------------------

class TestLoad(UniverseCase):
    def _pad(self, rows, to=1005):
        out = list(rows)
        i = 0
        while len(out) < to:
            i += 1
            out.append(("sz00%04d" % i, "填充%d" % i))
        return out

    def test_valid_file_is_loaded_and_indexed(self):
        self._write_rows(self._pad([("sh600519", "贵州茅台")]))
        self.assertTrue(u.load())
        self.assertEqual(u._by_code["sh600519"], "贵州茅台")
        self.assertTrue(u.index_ready())

    def test_bare_list_payload_is_also_accepted(self):
        """兼容只写数组不带 {"rows": ...} 包装的产物。"""
        self._write_rows(self._pad([("sh600519", "贵州茅台")]), wrap=False)
        self.assertTrue(u.load())

    def test_short_table_is_refused_so_a_half_table_is_never_used(self):
        self._write_rows([("sh600519", "贵州茅台")])
        self.assertFalse(u.load())
        self.assertFalse(u.index_ready())

    def test_corrupt_json_is_refused_without_raising(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertFalse(u.load())

    def test_many_invalid_rows_are_rejected_with_diagnostic(self):
        with open(self.path, "w", encoding="utf-8") as stream:
            json.dump({"rows": [[None, None]] * 1000}, stream)
        self.assertFalse(u.load())
        self.assertIn("valid rows=0", u.stats()["last_error"])

    def test_unreadable_existing_count_is_logged(self):
        expected_count = 0
        with mock.patch("builtins.open", side_effect=OSError("unreadable")), \
                mock.patch.object(u, "log_swallow") as log:
            self.assertEqual(u._existing_row_count(), expected_count)  # discipline-ok(R5): 验证刷新前的磁盘计数失败路径。
        self.assertIn("_existing_row_count", log.call_args.args[0])

    def test_missing_file_is_refused(self):
        self.assertFalse(u.load())

    def test_junk_rows_are_skipped_but_good_ones_survive(self):
        rows = self._pad([("sh600519", "贵州茅台")])
        raw = [["sh600519", "贵州茅台"], [None, None], ["x"], 3,
               ["sz000001", 5]] + [[c, n] for c, n in rows]
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"rows": raw}, f, ensure_ascii=False)
        self.assertTrue(u.load())
        self.assertEqual(u._by_code["sh600519"], "贵州茅台")
        self.assertNotIn("None", u._by_code)

    def test_second_load_is_cached_until_forced(self):
        self._write_rows(self._pad([("sh600519", "贵州茅台")]))
        self.assertTrue(u.load())
        os.remove(self.path)
        self.assertTrue(u.load(), "已加载过就该用内存里的，不该因为文件没了清空")
        self.assertFalse(u.load(force=True), "文件已不存在，force 必须重读")
        # 重读失败只报 False，绝不能顺手把可用表清空 —— 那会让搜索当场退化
        self.assertTrue(u.index_ready(), "刷新失败不得清空已在内存里可用的字典")

    def test_disabled_environment_short_circuits_everything(self):
        u._DISABLED = True
        self.assertFalse(u.load())
        self.assertTrue(u.is_stale(), "停用时永远报过期，别让调用方以为字典可用")
        self.assertEqual(u.match_confident("600519"), [])
        self.assertEqual(u.match_fuzzy("茅台"), [])
        self.assertFalse(u.refresh_async())
        u.ensure_ready()


class TestIsStale(UniverseCase):
    def test_empty_index_is_always_stale(self):
        self.assertTrue(u.is_stale())

    def test_fresh_index_is_not_stale(self):
        self._seed([("sh600519", "贵州茅台")])
        self._write_rows([("sh600519", "贵州茅台")])
        self.assertFalse(u.is_stale())

    def test_index_older_than_ttl_is_stale(self):
        self._seed([("sh600519", "贵州茅台")])
        self._write_rows([("sh600519", "贵州茅台")])
        old = time.time() - u.UNIVERSE_TTL - 10
        os.utime(self.path, (old, old))
        self.assertTrue(u.is_stale())

    def test_index_in_memory_but_file_gone_is_stale(self):
        """文件被清理掉时要报过期，好让后台刷新把表补回来。"""
        self._seed([("sh600519", "贵州茅台")])
        self.assertTrue(u.is_stale())


# --------------------------------------------------------------------------
# 匹配范围（本地只接"确定无疑"）
# --------------------------------------------------------------------------

class TestMatching(UniverseCase):
    def setUp(self):
        super().setUp()
        self._seed([("sh600519", "贵州茅台"), ("sz300750", "宁德时代"),
                    ("bj920185", "贝特瑞"), ("sz000651", "格力电器"),
                    ("sh600000", "浦发银行"), ("sz002142", "宁波银行")])

    def test_exact_code_in_every_spelling(self):
        for kw in ("600519", "sh600519", "SH600519", " 600519 "):
            self.assertEqual(u.match_confident(kw), [("sh600519", "贵州茅台")], kw)
        self.assertEqual(u.match_confident("920185"), [("bj920185", "贝特瑞")])

    def test_exact_name_matches_after_whitespace_stripping(self):
        self.assertEqual(u.match_confident("贵州茅台"), [("sh600519", "贵州茅台")])

    def test_unique_containment_is_taken_over_locally(self):
        self.assertEqual(u.match_confident("茅台"), [("sh600519", "贵州茅台")])
        self.assertEqual(u.match_confident("宁德"), [("sz300750", "宁德时代")])

    def test_ambiguous_word_is_handed_back_to_the_network(self):
        """多结果的价值在相关性排序，本地按代码序会给错首条 —— 一律不接管。"""
        self.assertEqual(u.match_confident("银行"), [])
        self.assertEqual(u.match_confident("00"), [])

    def test_unknown_inputs_are_not_matched(self):
        for kw in ("", None, "   ", "不存在的名字", "123456"):
            self.assertEqual(u.match_confident(kw), [], repr(kw))

    def test_empty_index_returns_empty_rather_than_erroring(self):
        u._index([])
        self.assertEqual(u.match_confident("600519"), [])
        self.assertEqual(u.match_fuzzy("600519"), [])
        self.assertFalse(u.index_ready())

    def test_fuzzy_prefers_prefix_hits_then_falls_back_to_containment(self):
        self.assertEqual(u.match_fuzzy("宁德"), [("sz300750", "宁德时代")])
        self.assertEqual(u.match_fuzzy("州"), [("sh600519", "贵州茅台")])

    def test_fuzzy_returns_everything_matching_up_to_the_limit(self):
        got = u.match_fuzzy("银行")
        self.assertEqual({c for c, _ in got}, {"sh600000", "sz002142"})
        self.assertEqual(u.match_fuzzy("银行", limit=1).__len__(), 1,
                         "limit 必须生效，弹窗放不下无限条")

    def test_digits_helper_accepts_prefixed_and_bare_forms(self):
        self.assertEqual(u._digits_of("sh600519"), "600519")
        self.assertEqual(u._digits_of("600519"), "600519")
        self.assertEqual(u._digits_of("600519SH"), "")
        self.assertEqual(u._digits_of("茅台"), "")


# --------------------------------------------------------------------------
# 刷新链路
# --------------------------------------------------------------------------

class TestRefresh(UniverseCase):
    def _page(self, *pairs):
        return [{"f12": c, "f13": 1, "f14": n} for c, n in pairs]

    def test_fetch_page_parses_the_payload_and_survives_missing_keys(self):
        """_fetch_page 内部 `from sf_network import http_get`，所以要桩在 sf_network 上。"""
        import sf_network
        with mock.patch.object(sf_network, "http_get",
                               return_value='{"data":{"diff":[{"f12":"600519"}]}}'):
            self.assertEqual(u._fetch_page("h", 1, 5), [{"f12": "600519"}])
        for body in ('{"data":null}', "{}", '{"data":{}}', "null"):
            with mock.patch.object(sf_network, "http_get", return_value=body):
                self.assertEqual(u._fetch_page("h", 1), [], body)

    def test_pick_host_falls_through_to_the_delayed_domain(self):
        seen = []

        def fake(host, pn, pz=1):
            seen.append(host)
            if host == u._HOSTS[0]:
                raise OSError("RemoteDisconnected")
            return [{"f12": "600519"}]

        with mock.patch.object(u, "_fetch_page", side_effect=fake):
            self.assertEqual(u._pick_host(), u._HOSTS[1])
        self.assertEqual(seen, list(u._HOSTS))

    def test_pick_host_returns_none_when_every_domain_fails(self):
        with mock.patch.object(u, "_fetch_page", side_effect=OSError("down")):
            self.assertIsNone(u._pick_host())

    def test_refresh_without_a_usable_host_writes_nothing(self):
        with mock.patch.object(u, "_pick_host", return_value=None):
            self.assertFalse(u._refresh_once())
        self.assertEqual(u._last_error, "no usable host")
        self.assertFalse(os.path.exists(self.path))

    def test_refresh_merges_pages_dedupes_and_maps_markets(self):
        pages = {
            1: self._page(("600519", "贵州茅台"), ("300750", "宁德时代")),
            2: self._page(("920185", "贝特瑞"), ("600519", "贵州茅台"),
                          ("510300", "上证指数")),     # 无法判定市场 → 丢弃
            3: self._page(("000651", "格 力 电 器")),
        }
        with mock.patch.object(u, "_pick_host", return_value=u._HOSTS[0]), \
                mock.patch.object(u, "_fetch_page",
                                  side_effect=lambda h, pn, pz=u.UNIVERSE_PAGE_SIZE:
                                  pages[pn]):
            self.assertTrue(u._refresh_once())
        codes = [c for c, _ in u._rows]
        self.assertEqual(sorted(codes),
                         sorted(["sh600519", "sz300750", "bj920185", "sz000651"]),
                         "跨页重复必须去掉，未知市场必须丢弃")
        self.assertEqual(u._by_code["sz000651"], "格力电器", "落盘的名称要归一")
        with open(self.path, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["count"], 4)

    def test_every_page_is_fetched_exactly_once(self):
        called = []
        with mock.patch.object(u, "_pick_host", return_value="h"), \
                mock.patch.object(u, "_fetch_page",
                                  side_effect=lambda h, pn, pz=1: called.append(pn)
                                  or self._page(("60%04d" % pn, "股%d" % pn))):
            u.UNIVERSE_MAX_PAGES = 5
            u._refresh_once()
        self.assertEqual(sorted(called), [1, 2, 3, 4, 5],
                         "并发 worker 不得重复抓同一页，也不能漏页")

    def test_single_page_failure_is_retried_then_parked_as_empty(self):
        u.UNIVERSE_PAGE_RETRIES = 2
        attempts = []

        def fake(host, pn, pz=1):
            attempts.append(pn)
            if pn == 2:
                raise OSError("timeout")
            return self._page(("60%04d" % pn, "股%d" % pn))

        with mock.patch.object(u, "_pick_host", return_value="h"), \
                mock.patch.object(u, "_fetch_page", side_effect=fake), \
                mock.patch.object(u.time, "sleep"):
            u._refresh_once()
        self.assertEqual(attempts.count(2), 3, "单页要按 UNIVERSE_PAGE_RETRIES 重试")
        self.assertEqual(u._rows, [], "缺页不得把残缺表升级成权威名单")
        self.assertIn("incomplete pages", u._last_error)  # discipline-ok(R5): the refresh failure reason has no public getter

    def test_missing_pages_refuse_to_overwrite_a_good_table(self):
        """缺页不写盘：宁可用旧字典，也不能把残缺表当完整表用。"""
        self._seed([("sh600519", "贵州茅台")])
        u.UNIVERSE_MIN_ROWS = 100
        with mock.patch.object(u, "_pick_host", return_value="h"), \
                mock.patch.object(u, "_fetch_page",
                                  return_value=self._page(("600519", "贵州茅台"))):
            self.assertFalse(u._refresh_once())
        self.assertFalse(os.path.exists(self.path))
        self.assertIn("too few rows", u._last_error)
        self.assertEqual(u._rows, [("sh600519", "贵州茅台")], "内存里的旧表不能被清掉")

    def test_unwritable_target_leaves_no_temp_file_behind(self):
        blocker = os.path.join(self.d, "blocked")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("我占着这个位置，父目录建不出来")
        with mock.patch.object(u, "_pick_host", return_value="h"), \
                mock.patch.object(u, "_fetch_page",
                                  return_value=self._page(("600519", "贵州茅台"))), \
                mock.patch.object(u, "UNIVERSE_PATH",
                                  os.path.join(blocker, "nested", "s.json")):
            self.assertFalse(u._refresh_once())
        self.assertIn("write failed", u._last_error)
        left = [n for n in os.listdir(self.d) if n.endswith(".tmp")]
        self.assertEqual(left, [], "临时文件必须清掉：%s" % left)

    def test_two_concurrent_writers_cannot_produce_a_half_written_table(self):
        """临时名每次唯一（pid + time_ns）：固定的 ".tmp" 会让两个实例互相截断。"""
        u.UNIVERSE_MIN_ROWS = 1
        seq = [self._page(("600519", "贵州茅台"), ("300750", "宁德时代")),
               self._page(("000651", "格力电器"), ("601318", "中国平安"))]
        lock = threading.Lock()

        def fake(host, pn, pz=1):
            return [] if pn > 2 else seq[min(pn - 1, 1)]

        with mock.patch.object(u, "_pick_host", return_value="h"), \
                mock.patch.object(u, "_fetch_page", side_effect=fake):
            a = threading.Thread(target=u._refresh_once)
            b = threading.Thread(target=u._refresh_once)
            a.start(); b.start(); a.join(); b.join()
        with open(self.path, encoding="utf-8") as f:
            obj = json.load(f)                      # 半截文件会在这里炸
        names = {n for _, n in obj["rows"]}
        self.assertTrue(names & {"贵州茅台", "格力电器"}, obj)
        self.assertEqual([n for n in os.listdir(self.d) if n.endswith(".tmp")], [],
                         "写完后不得留下临时文件")
        del lock


class TestLoadLeavesATrace(UniverseCase):
    """2026-11-02 走查 F-A-06：字典缓存存在却读不出来，不许零痕迹。

    首次启动没有缓存文件是正常的（静默）；文件在但损坏/读不了，用户只会看到
    "搜索永远走联网"，日志与诊断包里什么都没有。
    """

    def setUp(self):
        super().setUp()
        self.swallowed = []
        self.track_patch(mock.patch.object(
            u, "log_swallow", side_effect=lambda where, exc, *a, **k: self.swallowed.append((where, exc))))

    def test_missing_cache_file_is_silent(self):
        self.assertFalse(u.load(force=True))
        self.assertEqual(self.swallowed, [], "首次启动没有缓存是正常情况")
        self.assertIsNone(u._last_error)  # discipline-ok(R5): the load failure reason has no public getter

    def test_corrupt_cache_file_is_logged_and_recorded(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        self.assertFalse(u.load(force=True))
        self.assertEqual([w for w, _ in self.swallowed], ["sf_universe.load"])
        self.assertIn("load failed", u._last_error)  # discipline-ok(R5): the load failure reason has no public getter
        self.assertIn("load failed", u.stats()["last_error"], "诊断包读的就是 stats()")

    def test_too_small_cache_is_recorded_without_pretending_to_be_an_exception(self):
        self._write_rows([("sh600519", "贵州茅台")])
        self.assertFalse(u.load(force=True))
        self.assertIn("cache unusable", u._last_error)  # discipline-ok(R5): the load failure reason has no public getter
        self.assertEqual(self.swallowed, [])

    def test_successful_forced_load_clears_previous_load_error(self):
        self._write_rows([])
        self.assertFalse(u.load(force=True))
        self.assertIn("cache unusable", u.stats()["last_error"])
        self._write_rows([("sz%06d" % i, "股%d" % i) for i in range(1000)])
        self.assertTrue(u.load(force=True))
        self.assertIsNone(u.stats()["last_error"])


class TestRefreshPageCap(UniverseCase):
    """2026-11-02 走查 F-A-04：翻页触顶（服务端条数超过 UNIVERSE_MAX_PAGES 页）必须留痕，
    且被截断的数据不得覆盖条数更多的旧缓存。

    旧行为：全 A 超过 60 页（6000 条）后，按代码排序最靠后的 920xxx 北交所整段被悄悄截掉，
    `_last_error` 仍是 None、日志没有一行，诊断包里也看不出字典是残的。
    """

    def setUp(self):
        super().setUp()
        self.logs = []
        self.track_patch(mock.patch.object(
            u, "log_line_throttled",
            side_effect=lambda key, msg, level="WARNING", every=1: self.logs.append((level, msg))))

    def _codes(self, n):
        return ["60%04d" % i for i in range(n)]

    def _server(self, codes):
        """按页切片的假服务端；`_fetch_page` 返回 diff 行列表，与真实形状一致。"""
        def fake(host, pn, pz=u.UNIVERSE_PAGE_SIZE):
            return [{"f12": c, "f14": "股" + c} for c in codes[(pn - 1) * pz: pn * pz]]
        return fake

    def test_full_last_page_is_recorded_as_a_page_cap(self):
        codes = self._codes(350)                     # 3 页上限只够取 300 条
        with mock.patch.object(u, "_pick_host", return_value="h"), \
                mock.patch.object(u, "_fetch_page", side_effect=self._server(codes)):
            self.assertTrue(u._refresh_once(), "没有旧缓存时，截断的新表仍比没有强，照常落盘")  # discipline-ok(R5): 同步执行真实刷新以验证落盘
        self.assertIn("page cap", u._last_error)  # discipline-ok(R5): the refresh failure reason has no public getter
        self.assertEqual(u.stats()["count"], 300)
        self.assertTrue([m for lvl, m in self.logs if lvl == "WARNING" and "page cap" in m],
                        "触顶必须有一行 WARNING：%r" % (self.logs,))

    def test_partial_last_page_is_not_flagged(self):
        codes = self._codes(250)                     # 第 3 页只有 50 条 = 取完了
        with mock.patch.object(u, "_pick_host", return_value="h"), \
                mock.patch.object(u, "_fetch_page", side_effect=self._server(codes)):
            self.assertTrue(u._refresh_once())  # discipline-ok(R5): 同步执行真实刷新以验证完整页
        self.assertIsNone(u._last_error)  # discipline-ok(R5): the refresh failure reason has no public getter
        self.assertEqual(self.logs, [])
        self.assertEqual(u.stats()["count"], 250)

    def test_capped_refresh_never_overwrites_a_larger_cache(self):
        old_rows = [("sh" + c, "旧" + c) for c in self._codes(350)]
        self._seed(old_rows)
        self._write_rows(old_rows)
        with open(self.path, "rb") as f:
            before = f.read()
        with mock.patch.object(u, "_pick_host", return_value="h"), \
                mock.patch.object(u, "_fetch_page", side_effect=self._server(self._codes(400))):
            self.assertFalse(u._refresh_once(), "截断的 300 条不得顶掉完整的 350 条")  # discipline-ok(R5): 同步执行真实刷新以验证旧缓存保留
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before, "旧缓存文件必须原样保留")
        self.assertEqual(u.stats()["count"], 350, "内存里的旧表也不能被换成半张表")
        self.assertIn("page cap", u._last_error)  # discipline-ok(R5): the refresh failure reason has no public getter

    def test_capped_larger_refresh_preserves_old_security(self):
        old_rows = [("bj920185", "贝特瑞")]
        self._seed(old_rows)
        self._write_rows(old_rows)
        with open(self.path, "rb") as f:
            before = f.read()
        with mock.patch.object(u, "_pick_host", return_value="h"), \
                mock.patch.object(u, "_fetch_page", side_effect=self._server(self._codes(400))):
            self.assertFalse(u._refresh_once())  # discipline-ok(R5): 同步执行真实刷新以核对缓存
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(u.match_confident("920185"), old_rows)
        self.assertIn("page cap", u.stats()["last_error"])

    def test_capped_refresh_preserves_disk_cache_when_not_loaded(self):
        old_rows = [("bj920185", "贝特瑞")]
        self._write_rows(old_rows)
        with open(self.path, "rb") as f:
            before = f.read()
        with mock.patch.object(u, "_pick_host", return_value="h"), \
                mock.patch.object(u, "_fetch_page", side_effect=self._server(self._codes(400))):
            self.assertFalse(u._refresh_once())  # discipline-ok(R5): 同步执行真实刷新以核对磁盘
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(u.index_ready())

    def test_complete_refresh_clears_previous_error(self):
        u._last_error = "old error"  # discipline-ok(R5): seed a previous failure in diagnostics
        with mock.patch.object(u, "_pick_host", return_value="h"), \
                mock.patch.object(u, "_fetch_page", side_effect=self._server(self._codes(250))):
            self.assertTrue(u._refresh_once())  # discipline-ok(R5): 同步执行刷新以检查恢复后的诊断
        self.assertIsNone(u.stats()["last_error"])

    def test_concurrent_server_totals_keep_the_maximum(self):
        smaller_comparing = threading.Event()
        larger_finished = threading.Event()

        class InitialTotal(int):
            def __lt__(self, other):
                if other == 100:
                    smaller_comparing.set()
                    larger_finished.wait(0.5)
                return super().__lt__(other)

        u._server_total = InitialTotal(0)  # discipline-ok(R5): 注入比较对象以强制线程交错

        def record_larger():
            try:
                u._note_server_total(200)  # discipline-ok(R5): 直接驱动并发总数更新
            finally:
                larger_finished.set()

        smaller = threading.Thread(target=u._note_server_total, args=(100,))  # discipline-ok(R5): 直接驱动并发总数更新
        larger = threading.Thread(target=record_larger)
        smaller.start()
        try:
            self.assertTrue(smaller_comparing.wait(2))
            larger.start()
        finally:
            smaller.join(3)
            if larger.ident is not None:
                larger.join(3)
        self.assertFalse(smaller.is_alive())
        self.assertFalse(larger.is_alive())
        self.assertEqual(u._server_total, 200)  # discipline-ok(R5): 检查并发最大值的内部结果

    def test_server_total_larger_than_fetched_rows_is_flagged_too(self):
        """服务端 data.total 比实取条数大 = 没取全（页大小/页数变了也能抓到）。"""
        import json
        import sf_network
        codes = self._codes(260)

        def fake_http(url, **kwargs):
            pn = int(url.split("pn=")[1].split("&")[0])
            pz = int(url.split("pz=")[1].split("&")[0])
            diff = [{"f12": c, "f14": "股" + c} for c in codes[(pn - 1) * pz: pn * pz]]
            return json.dumps({"data": {"total": 500, "diff": diff}})

        with mock.patch.object(sf_network, "http_get", side_effect=fake_http):
            u._refresh_once()  # discipline-ok(R5): 同步执行真实分页以验证服务端总数
        self.assertIn("page cap", u._last_error)  # discipline-ok(R5): the refresh failure reason has no public getter
        self.assertIn("500", u._last_error)  # discipline-ok(R5): the refresh failure reason has no public getter


class TestRefreshAsync(UniverseCase):
    def test_only_one_refresh_runs_at_a_time(self):
        u._refresh_started = True
        self.assertFalse(u.refresh_async(), "已有刷新在跑时不得再起一个")

    def test_min_interval_protects_against_a_tight_loop(self):
        u.UNIVERSE_MIN_INTERVAL = 3600
        u._last_attempt = time.time()
        self.assertFalse(u.refresh_async())

    def test_successful_dispatch_releases_the_flag_when_done(self):
        done = threading.Event()

        def slow():
            done.wait(2.0)

        with mock.patch.object(u, "_refresh_once", side_effect=slow):
            self.assertTrue(u.refresh_async())
            self.assertTrue(u._refresh_started)
            self.assertFalse(u.refresh_async(), "线程还没结束，第二次必须被拒")
            done.set()
            self.assertTrue(wait_for(lambda: not u._refresh_started,
                                     timeout=3.0, msg="刷新结束后必须解锁"))

    def test_a_crashing_refresh_still_releases_the_flag(self):
        """刷新炸了也不能把字典永久锁死 —— 那会让后续所有搜索再也刷不新。"""
        with mock.patch.object(u, "_refresh_once",
                               side_effect=RuntimeError("boom")):
            self.assertTrue(u.refresh_async())
            self.assertTrue(wait_for(lambda: not u._refresh_started,
                                     timeout=3.0, msg="异常路径必须复位"))

    def test_refresh_result_is_visible_to_the_caller_state(self):
        with mock.patch.object(u, "_refresh_once",
                               side_effect=lambda: u._index([("sh600519", "贵州茅台")])):
            u.refresh_async()
            self.assertTrue(wait_for(lambda: u.index_ready(), timeout=3.0))


class TestEnsureReady(UniverseCase):
    def test_loads_then_refreshes_only_when_stale(self):
        calls = []
        with mock.patch.object(u, "load", side_effect=lambda *a: calls.append("load")), \
                mock.patch.object(u, "refresh_async",
                                  side_effect=lambda: calls.append("refresh")):
            u.ensure_ready()
        self.assertEqual(calls, ["load", "refresh"],
                         "字典缺失/过期时才刷，且必须先尝试装载")

    def test_fresh_index_is_left_alone(self):
        self._seed([("sh600519", "贵州茅台")])
        self._write_rows([("sh600519", "贵州茅台")])
        u._loaded = True
        with mock.patch.object(u, "refresh_async") as ra:
            u.ensure_ready()
        self.assertEqual(ra.call_count, 0, "不过期就不该发网络请求")

    def test_never_raises_out_of_a_search_call(self):
        with mock.patch.object(u, "load", side_effect=RuntimeError("disk")), \
                mock.patch.object(u, "is_stale", side_effect=RuntimeError("clock")):
            u.ensure_ready()

    def test_stats_reports_the_dict_state_for_diagnostics(self):
        self._seed([("sh600519", "贵州茅台")])
        st = u.stats()
        self.assertEqual(st["count"], 1)
        self.assertFalse(st["disabled"])
        self.assertEqual(st["path"], self.path)
        self.assertIn("stale", st)
        u._last_error = "boom"
        self.assertEqual(u.stats()["last_error"], "boom")


if __name__ == "__main__":
    unittest.main()
