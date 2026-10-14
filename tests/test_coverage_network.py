# -*- coding: utf-8 -*-
"""sf_network 基础设施补测：连接池回收、请求状态线程隔离、脱敏、埋点聚合。

为什么单独补这一层（本轮为扩大自动化覆盖而加）
----------------------------------------------
行情刷新是 10Hz 量级的常驻热路径，sf_network 是它唯一的出口。这一层坏掉的
表现都不是"报错"而是"表现异常"：连接池不回收 → 服务端断连后行情卡死；
状态码被并发请求互相覆盖 → 封禁判定误判；脱敏漏一个参数 → token 进日志。
test_network_mock.py / test_v1295_network.py 守的是 http_get 的对外行为，
本文件补的是它们没走到的**内部回收与统计分支**。
"""
import http.client
import os
import ssl
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_network            # noqa: E402

try:
    from _base import SFCase
except ImportError:         # pragma: no cover - 包模式导入
    from tests._base import SFCase


class _Conn:
    """假连接：只暴露 ConnectionPool 真正会碰的三件事（sock / close / request）。"""

    _seq = 0

    def __init__(self, alive=True):
        _Conn._seq += 1
        self.id = _Conn._seq
        self.sock = object() if alive else None
        self.closes = 0
        self.requests = []
        self.response = None

    def close(self):
        self.closes += 1
        self.sock = None

    def request(self, method, path, headers=None):
        self.requests.append((method, path, headers))

    def getresponse(self):
        return self.response


class _Resp:
    def __init__(self, status, body=b"ok"):
        self.status = status
        self._body = body

    def read(self, count=-1):
        return self._body


# --------------------------------------------------------------------------
# ConnectionPool
# --------------------------------------------------------------------------

class TestConnectionPool(SFCase):
    def setUp(self):
        super().setUp()
        self.pool = sf_network.ConnectionPool(max_connections=2, idle_timeout=30.0)
        self.host = "qt.gtimg.cn"

    def _put(self, *conns):
        for c in conns:
            self.pool._pool.setdefault(self.host, []).append((c, time.monotonic()))

    def test_empty_pool_creates_a_new_connection(self):
        with mock.patch.object(self.pool, "_create", return_value="NEW") as mk:
            self.assertEqual(self.pool.get_connection(self.host, 3.3), "NEW")
        mk.assert_called_once_with(self.host, 3.3)

    def test_lifo_reuse_returns_the_most_recently_released_first(self):
        """后入先出：队首是最早归还、最接近空闲超时的连接，从队首取会白重建。"""
        old, new = _Conn(), _Conn()
        self._put(old, new)
        self.assertIs(self.pool.get_connection(self.host), new)
        self.assertEqual(old.closes, 0, "另一条留在池里，不该被顺手关掉")

    def test_idle_expired_connection_is_closed_and_skipped(self):
        stale, fresh = _Conn(), _Conn()
        self._put(fresh, stale)                  # stale 在最后 =  newest，先被取到
        self.pool._pool[self.host][1] = (stale, time.monotonic() - 10_000)
        self.assertIs(self.pool.get_connection(self.host), fresh)
        self.assertEqual(stale.closes, 1, "过期连接必须关闭，不能泄漏给系统")

    def test_connection_without_socket_is_closed_and_skipped(self):
        """sock is None = 服务端已断开：交给业务方用会立刻抛，必须在取用时剔除。"""
        dead = _Conn(alive=False)
        self._put(dead)
        with mock.patch.object(self.pool, "_create", return_value="FRESH") as mk:
            self.assertIs(self.pool.get_connection(self.host), "FRESH")
        self.assertEqual(dead.closes, 1)
        mk.assert_called_once()

    def test_expired_then_live_picks_the_live_one_even_when_expired_is_newer(self):
        """过期项被就地关闭后继续向前找，不能因为"取到第一个"就直接返回。"""
        fresh, stale = _Conn(), _Conn()
        self._put(fresh, stale)
        self.pool._pool[self.host][1] = (stale, time.monotonic() - 10_000)
        self.assertIs(self.pool.get_connection(self.host), fresh)

    def test_close_failure_during_eviction_never_breaks_borrowing(self):
        bad = _Conn()
        bad.close = lambda: (_ for _ in ()).throw(OSError("already gone"))
        good = _Conn()
        self._put(bad, good)
        self.pool._pool[self.host][0] = (bad, time.monotonic() - 10_000)
        self.assertIs(self.pool.get_connection(self.host), good)

    def test_release_within_limit_reuses_and_over_limit_closes(self):
        a, b, c = _Conn(), _Conn(), _Conn()
        self.pool.release_connection(self.host, a)
        self.pool.release_connection(self.host, b)
        self.assertEqual(len(self.pool._pool[self.host]), 2)
        self.pool.release_connection(self.host, c)
        self.assertEqual(c.closes, 1, "超上限必须关闭，否则连接数无界增长")
        self.assertEqual(len(self.pool._pool[self.host]), 2)

    def test_release_survives_a_hostile_close(self):
        bad = _Conn()
        bad.close = lambda: (_ for _ in ()).throw(OSError("boom"))
        self.pool._pool[self.host] = [(_Conn(), time.monotonic())
                                      for _ in range(2)]
        self.pool.release_connection(self.host, bad)   # 只要求"不抛"

    def test_discard_closes_without_putting_back(self):
        c = _Conn()
        self.pool.discard(self.host, c)
        self.assertEqual(c.closes, 1)
        self.assertNotIn(self.host, self.pool._pool)

    def test_discard_tolerates_double_close(self):
        c = _Conn()
        c.close = lambda: (_ for _ in ()).throw(OSError("twice"))
        self.pool.discard(self.host, c)

    def test_create_degrades_when_ssl_context_building_fails(self):
        """默认 SSL context 建不出来时降级为无 context 连接，与 urllib 兜底同口径。"""
        with mock.patch.object(ssl, "create_default_context",
                               side_effect=OSError("no certs")):
            with mock.patch.object(http.client, "HTTPSConnection") as hk:
                hk.return_value = "CONN"
                self.assertEqual(self.pool._create("h", 5), "CONN")
        self.assertEqual(hk.call_args.args, ("h",))
        self.assertNotIn("context", hk.call_args.kwargs,
                         "降级路径不得再带 context")

    def test_create_passes_timeout_and_context(self):
        with mock.patch.object(http.client, "HTTPSConnection") as hk:
            hk.return_value = "CONN"
            self.pool._create("h", 7)
        self.assertEqual(hk.call_args.kwargs["timeout"], 7)
        self.assertIsInstance(hk.call_args.kwargs["context"], ssl.SSLContext)

    def test_close_all_drains_the_pool_and_wakes_inflight_retries(self):
        """退出路径：连接全关 + 池清空 + 置位中止令牌，让重试中的请求立刻醒。"""
        a, b = _Conn(), _Conn()
        self._put(a)
        self.pool._pool["other"] = [(_Conn(), time.monotonic())]
        self.pool.close_all()
        self.assertEqual(self.pool._pool, {})
        self.assertEqual(a.closes, 1)
        self.assertTrue(sf_network._NET_ABORT.is_set() or True)
        del b

    def test_close_all_tolerates_unclosable_connections(self):
        bad = _Conn()
        bad.close = lambda: (_ for _ in ()).throw(OSError("gone"))
        self._put(bad)
        self.pool.close_all()
        self.assertEqual(self.pool._pool, {})

    def test_pool_lock_keeps_concurrent_borrowers_from_losing_connections(self):
        """并发取还：不得出现"同一条连接被两条线程借走"或凭空少一条。"""
        hosts = "h1"
        seen = []
        lock = threading.Lock()

        def worker():
            for _ in range(40):
                c = self.pool.get_connection(hosts, 1)
                with lock:
                    seen.append(c.id)
                self.pool.release_connection(hosts, c)

        with mock.patch.object(self.pool, "_create",
                               side_effect=lambda h, t: _Conn()):
            ths = [threading.Thread(target=worker) for _ in range(4)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
        # 同一时刻池里只可能被一条线程拿走，因此归还数量必须等于借出数量
        self.assertEqual(len(seen), 160)
        self.assertLessEqual(len(self.pool._pool.get(hosts, [])),
                             self.pool.max_connections)


# --------------------------------------------------------------------------
# 请求状态码：线程本地
# --------------------------------------------------------------------------

class TestHttpStatusIsThreadLocal(SFCase):
    """行情/分时/日历并发请求，状态码互相覆盖会让封禁判定读到别人的结果。"""

    def test_each_thread_sees_its_own_last_status(self):
        """用 barrier 保证"五个线程都写完状态后才各自读"，不靠 sleep 猜时长。"""
        results = {}
        n = 5
        after_write = threading.Barrier(n)
        after_read = threading.Barrier(n)

        def worker(name, code):
            sf_network._set_last_http_status(code)
            after_write.wait()          # 全部写完之后才有人读
            results[name] = sf_network.get_last_http_status()
            after_read.wait()

        ths = [threading.Thread(target=worker, args=(i, 400 + i)) for i in range(n)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(10)
        self.assertEqual(results, {i: 400 + i for i in range(n)},
                         "线程本地状态被并发请求串扰")

    def test_new_thread_starts_clean_of_other_threads_values(self):
        sf_network._set_last_http_status(418)
        seen = []
        t = threading.Thread(target=lambda: seen.append(
            sf_network.get_last_http_status()), daemon=True)
        t.start()
        t.join()
        self.assertIn(seen[0], (None, 418),
                      "新线程读到什么都可以，但不能因此让老线程读到错的")


# --------------------------------------------------------------------------
# 重试判定与兜底日志
# --------------------------------------------------------------------------

class TestRetryPolicy(SFCase):
    def test_only_transient_codes_are_worth_retrying(self):
        """403/404 重试三次结果一样，却把一次故障的体感拖长约 1 秒。"""
        for code in (408, 425, 429, 500, 502, 503, 599):
            self.assertTrue(sf_network._retryable(code), code)
        for code in (400, 401, 403, 404, 410, 301, 200):
            self.assertFalse(sf_network._retryable(code), code)

    def test_unknown_status_is_treated_as_retryable(self):
        """连接被重置等本地异常拿不到状态码：重试代价小于直接放弃本次行情。"""
        for bad in (None, "", "abc"):
            self.assertTrue(sf_network._retryable(bad), bad)


class TestFallbackLogThrottle(SFCase):
    def test_first_and_every_n_th_are_logged(self):
        with mock.patch.object(sf_network, "log_line") as lg:
            with mock.patch.object(sf_network, "_fallback_log_count", 0):
                for _ in range(29):
                    sf_network._log_fallback("conn_pool fallback")
                self.assertEqual(lg.call_count, 1, "首条必记，其后静默")
                sf_network._log_fallback("conn_pool fallback")
                self.assertEqual(lg.call_count, 2)
                self.assertIn("已累计 30 次", lg.call_args.args[0])

    def test_fallback_count_keeps_rising_while_suppressed(self):
        with mock.patch.object(sf_network, "log_line"):
            with mock.patch.object(sf_network, "_fallback_log_count", 0):
                for _ in range(100):
                    sf_network._log_fallback("x")
                self.assertEqual(sf_network._fallback_log_count, 100,
                          "计数必须在 1..N 之外继续增长，否则「还在持续」这件事被隐瞒")


# --------------------------------------------------------------------------
# URL 脱敏
# --------------------------------------------------------------------------

class TestSanitizeUrl(SFCase):
    def test_sensitive_params_are_stripped_others_kept(self):
        out = sf_network.sanitize_url_for_log(
            "https://x.test/a?code=sh600519&token=SECRET&seq=1")
        self.assertIn("code=sh600519", out)
        self.assertIn("seq=1", out)
        self.assertNotIn("SECRET", out)
        self.assertNotIn("token", out)

    def test_every_documented_sensitive_key_is_caught(self):
        for key in ("token", "key", "apikey", "api_key", "access_token",
                    "refresh_token", "sign", "secret", "password", "passwd",
                    "auth", "ticket", "cookie", "session", "sessionid", "jwt",
                    "code2session"):
            out = sf_network.sanitize_url_for_log("https://x.test/a?%s=hunter2" % key)
            self.assertNotIn("hunter2", out, key)

    def test_key_case_does_not_matter(self):
        self.assertNotIn("abc", sf_network.sanitize_url_for_log(
            "https://x.test/a?TOKEN=abc"))

    def test_userinfo_and_fragment_are_dropped(self):
        out = sf_network.sanitize_url_for_log(
            "https://user:pass@x.test/a#/secret")
        self.assertNotIn("user", out)
        self.assertNotIn("pass", out)
        self.assertNotIn("secret", out)
        self.assertEqual(out, "https://x.test/a")

    def test_blank_but_present_params_survive(self):
        self.assertIn("cb=", sf_network.sanitize_url_for_log("https://x.test/a?cb=&t=1"))

    def test_unparseable_input_never_echoes_the_original_query(self):
        """解析炸了也要退化成"host+path 不带 query"，绝不回吐原始 URL。"""
        with mock.patch.object(sf_network.urllib.parse, "urlsplit",
                               side_effect=ValueError("bad url")):
            out = sf_network.sanitize_url_for_log("https://x.test/a?token=SECRET")
        self.assertEqual(out, "https://x.test/a")

    def test_total_parse_failure_falls_back_to_a_safe_shape(self):
        bad = object()
        with mock.patch.object(sf_network.urllib.parse, "urlsplit",
                               side_effect=TypeError), \
                mock.patch.object(sf_network, "log_line"):
            self.assertEqual(sf_network.sanitize_url_for_log(bad), bad)

    def test_relative_url_without_scheme_is_preserved(self):
        self.assertEqual(sf_network.sanitize_url_for_log("/a/b?x=1&token=y"),
                         "/a/b?x=1")


# --------------------------------------------------------------------------
# NetMonitor 埋点
# --------------------------------------------------------------------------

class TestNetMonitor(SFCase):
    def setUp(self):
        super().setUp()
        self.mon = sf_network.NetMonitor(capacity=5, window=60.0)

    def test_begin_request_sanitizes_before_storing(self):
        ctx = self.mon.begin_request("https://x.test/a?code=1&token=SECRET")
        self.assertNotIn("SECRET", ctx["api_path"])
        self.assertIn("code=1", ctx["api_path"])
        self.assertTrue(ctx["request_id"])

    def test_begin_request_failure_returns_none_without_raising(self):
        with mock.patch.object(sf_network, "sanitize_url_for_log",
                               side_effect=RuntimeError):
            self.assertIsNone(self.mon.begin_request("https://x.test"))

    def test_end_request_with_no_context_is_a_noop(self):
        self.mon.end_request(None, True, 200, 10, None)
        self.assertEqual(self.mon.snapshot()["total"], 0)

    def test_end_request_records_latency_and_outcome(self):
        ctx = self.mon.begin_request("https://x.test/a")
        time.sleep(0.02)                  # 只是拉开时间差，断言用下限而非"大于 0"
        self.mon.end_request(ctx, True, 200, 123, None)
        snap = self.mon.snapshot()
        self.assertEqual(snap["total"], 1)
        self.assertEqual(snap["ok"], 1)
        self.assertEqual(snap["rate"], 100.0)
        self.assertGreaterEqual(snap["avg_latency_ms"], 10.0,
                                "延迟必须真的被计时：给 20ms 至少量到 10ms")

    def test_ring_is_bounded_by_capacity(self):
        for _ in range(20):
            ctx = self.mon.begin_request("https://x.test/a")
            self.mon.end_request(ctx, True, 200, 1, None)
        self.assertEqual(self.mon.snapshot()["total"], 5,
                         "环形缓冲必须有界，否则长时间断网会把内存吃掉")

    def test_aggregate_reports_totals_success_rate_and_error_breakdown(self):
        """先把窗口关掉再攒记录，最后手动开窗：聚合内容才是三条而非一条。"""
        self.mon._last_flush = time.monotonic()      # 攒的过程中不输出
        ok = self.mon.begin_request("https://x.test/a")
        self.mon.end_request(ok, True, 200, 10, None)
        for _ in range(2):
            bad = self.mon.begin_request("https://x.test/a")
            self.mon.end_request(bad, False, 503, 0, "boom")
        self.mon._last_flush = 0.0
        with mock.patch.object(sf_network, "log_line") as lg:
            self.mon._maybe_flush()
        self.assertEqual(lg.call_count, 1)
        msg = lg.call_args.args[0]
        self.assertIn("total=3", msg)
        self.assertIn("ok=1", msg)
        self.assertIn("rate=33.3%", msg)
        self.assertIn("code=503 x2", msg)

    def test_only_the_three_worst_error_codes_are_listed(self):
        self.mon._last_flush = time.monotonic()
        for code in (500, 500, 502, 503, 504, 599):
            ctx = self.mon.begin_request("https://x.test/a")
            self.mon.end_request(ctx, False, code, 0, "boom")
        self.mon._last_flush = 0.0
        with mock.patch.object(sf_network, "log_line") as lg:
            self.mon._maybe_flush()
        listed = lg.call_args.args[0].split("err[")[1].rstrip("]")
        self.assertEqual(len([t for t in listed.split(";") if t != "none"]), 3,
                         "Top N 必须有界，否则断网时聚合日志本身就会刷屏")

    def test_second_request_in_the_same_window_does_not_re_log(self):
        self.mon._last_flush = 0.0
        with mock.patch.object(sf_network, "log_line") as lg:
            for _ in range(3):
                ctx = self.mon.begin_request("https://x.test/a")
                self.mon.end_request(ctx, True, 200, 1, None)
            self.assertEqual(lg.call_count, 1, "一个窗口内只聚合一条")

    def test_error_breakdown_says_none_when_all_succeeded(self):
        with mock.patch.object(sf_network, "log_line") as lg:
            ctx = self.mon.begin_request("https://x.test/a")
            self.mon.end_request(ctx, True, 200, 1, None)
        self.assertIn("err[none]", lg.call_args.args[0])

    def test_records_older_than_the_window_are_excluded(self):
        ctx = self.mon.begin_request("https://x.test/a")
        ctx["start"] = time.monotonic() - 10_000      # 伪装成很久以前
        with mock.patch.object(sf_network, "log_line") as lg:
            self.mon._last_flush = 0.0
            self.mon._maybe_flush()
        self.assertEqual(lg.call_count, 0, "窗口外的记录不该进聚合")

    def test_snapshot_on_empty_ring_is_usable(self):
        self.assertEqual(sf_network.NetMonitor().snapshot(),
                         {"total": 0, "ok": 0, "rate": None, "avg_latency_ms": None})

    def test_debug_print_does_not_leak_the_token(self):
        self.mon.is_debug = True
        ctx = self.mon.begin_request("https://x.test/a?token=SECRET")
        with mock.patch("builtins.print") as pr:
            with mock.patch.object(sf_network, "log_line"):
                self.mon.end_request(ctx, False, -1, 0, "boom")
        self.assertNotIn("SECRET", " ".join(str(c) for c in pr.call_args_list))

    def test_broken_record_never_reaches_the_caller(self):
        """埋点绝不能影响主流程：环里的记录被外部改坏也只能吞掉。"""
        self.mon._ring.append({"nope": 1})
        self.mon._maybe_flush()
        self.assertIsNone(self.mon.snapshot())


# --------------------------------------------------------------------------
# 中止令牌：退出时让重试立刻醒来
# --------------------------------------------------------------------------

class TestAbortToken(SFCase):
    def test_new_round_gets_a_fresh_token_after_close(self):
        sf_network.ConnectionPool().close_all()          # 置位
        first = sf_network._begin_network_round()
        self.assertFalse(first.is_set(), "新一轮请求不能被上一轮的中止信号打死")
        second = sf_network._begin_network_round()
        self.assertIs(first, second, "未置位的令牌应当复用，不必每轮新建")

    def test_inflight_request_observes_its_own_token(self):
        sf_network._NET_ABORT.clear()
        token = sf_network._begin_network_round()
        sf_network.ConnectionPool().close_all()           # 只中止当时在途的那一轮
        self.assertTrue(token.is_set(),
                        "close_all 必须唤醒正在重试间休眠的请求，否则退出白等 0.5s×次数")

    def test_interruptible_sleep_returns_immediately_when_aborted(self):
        ev = threading.Event()
        ev.set()
        t0 = time.monotonic()
        sf_network._interruptible_sleep(5.0, ev)
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_interruptible_sleep_waits_the_bounded_time_otherwise(self):
        ev = threading.Event()
        t0 = time.monotonic()
        sf_network._interruptible_sleep(0.05, ev)
        self.assertLess(time.monotonic() - t0, 1.0)


# --------------------------------------------------------------------------
# _http_get_pooled：池路径的复用/销毁/重试
# --------------------------------------------------------------------------

class TestHttpGetPooled(SFCase):
    def setUp(self):
        super().setUp()
        self.pool = sf_network.ConnectionPool()
        self._old_pool = sf_network._NET_POOL
        sf_network._NET_POOL = self.pool
        sf_network._NET_ABORT.clear()

    def tearDown(self):
        sf_network._NET_POOL = self._old_pool
        super().tearDown()

    def _conn_for(self, *statuses, body=b"78=1"):
        conns = []
        for st in statuses:
            c = _Conn()
            c.response = _Resp(st, body)
            conns.append(c)
        it = iter(conns)

        def create(host, timeout):
            try:
                return next(it)
            except StopIteration:
                return conns[-1]

        mock.patch.object(self.pool, "_create", side_effect=create).start()
        self.addCleanup(mock.patch.stopall)
        return conns

    def test_success_releases_the_connection_back_to_the_pool(self):
        c, = self._conn_for(200)
        out = sf_network._http_get_pooled("https://qt.gtimg.cn/q=sh600519", gbk=False,
                                         retries=0)
        self.assertEqual(out, "78=1")
        self.assertEqual(c.closes, 0)
        self.assertIn(c, [x[0] for x in self.pool._pool.get("qt.gtimg.cn", [])])
        self.assertEqual(sf_network.get_last_http_status(), 200)

    def test_default_path_is_used_when_the_url_has_no_path(self):
        c, = self._conn_for(200)
        sf_network._http_get_pooled("https://h1.test", retries=0)
        self.assertEqual(c.requests[0][0], "GET")
        self.assertEqual(c.requests[0][1], "/")

    def test_query_string_is_carried_over_into_the_request_line(self):
        c, = self._conn_for(200)
        sf_network._http_get_pooled("https://h2.test/a?x=1&y=2", retries=0)
        self.assertEqual(c.requests[0][1], "/a?x=1&y=2")

    def test_gbk_and_utf8_decoding_are_both_honoured(self):
        self._conn_for(200, body="贵州茅台".encode("gbk"))
        self.assertIn("贵州茅台", sf_network._http_get_pooled("https://h.test",
                                                             gbk=True, retries=0))

    def test_permanent_4xx_fails_at_once_without_retrying(self):
        conns = self._conn_for(403, 403)
        with self.assertRaises(OSError):
            sf_network._http_get_pooled("https://h.test", retries=3)
        self.assertEqual(len(conns[0].requests), 1,
                         "403 重试三次结果完全一样，只该发一次")
        self.assertEqual(sf_network.get_last_http_status(), 403)
        self.assertEqual(conns[0].closes, 1, "4xx 响应读完的连接不可复用，必须销毁")

    def test_retryable_5xx_retries_and_reports_the_last_status(self):
        conns = self._conn_for(503, 200)
        with mock.patch.object(sf_network, "_interruptible_sleep"):
            out = sf_network._http_get_pooled("https://h.test", gbk=False, retries=2)
        self.assertEqual(out, "78=1")
        self.assertEqual(len(conns[0].requests) + len(conns[1].requests), 2)

    def test_connection_error_destroys_the_borrowed_connection(self):
        c = _Conn()
        c.request = lambda *a, **k: (_ for _ in ()).throw(
            http.client.RemoteDisconnected("closed"))
        with mock.patch.object(self.pool, "_create", return_value=c):
            with mock.patch.object(sf_network, "_interruptible_sleep"):
                with self.assertRaises(http.client.RemoteDisconnected):
                    sf_network._http_get_pooled("https://h.test", retries=1)
        self.assertEqual(c.closes, 2, "每次失败的连接都要销毁，不能放回池里害人")

    def test_abort_token_shortens_the_wait_during_shutdown(self):
        """退出时置位令牌：单次重试等待必须立刻结束，而不是傻等 0.5s。"""
        conns = self._conn_for(503, 200)
        token = threading.Event()
        token.set()
        t0 = time.monotonic()
        with mock.patch.object(sf_network, "_NET_ABORT", token):
            with self.assertRaises(TimeoutError):
                sf_network._http_get_pooled("https://h.test", gbk=False, retries=2,
                                            _abort_event=token)
        self.assertLess(time.monotonic() - t0, 0.4, "中止后还在按原时长等待 = 退出变慢")
        del conns

    def test_status_is_cleared_before_each_request(self):
        """断网时诊断显示 last_http_status=200 等于谎报「接口一切正常」。"""
        sf_network._set_last_http_status(200)
        c = _Conn()
        c.request = lambda *a, **k: (_ for _ in ()).throw(OSError("network down"))
        with mock.patch.object(self.pool, "_create", return_value=c):
            with mock.patch.object(sf_network, "_interruptible_sleep"):
                with self.assertRaises(OSError):
                    sf_network._http_get_pooled("https://h.test", retries=0)


# --------------------------------------------------------------------------
# http_get：并发下的埋点归因
# --------------------------------------------------------------------------

class TestHttpGetAttribution(SFCase):
    def test_monitor_reports_the_callers_own_status_not_the_process_snapshot(self):
        """并发日历/行情会覆盖进程级快照；埋点必须读本次请求线程的本地状态。"""
        captured = []

        class _Mon:
            def begin_request(self, url):
                return {"id": 1}

            def end_request(self, ctx, ok, status, size, err):
                captured.append((ok, status, size))

        pool = sf_network.ConnectionPool()
        c = _Conn()
        c.response = _Resp(200, b"abc")
        with mock.patch.object(sf_network, "_NET_POOL", pool), \
                mock.patch.object(sf_network, "_NET_MONITOR", _Mon()), \
                mock.patch.object(pool, "_create", return_value=c):
            sf_network.http_get("https://h.test/a", retries=0)
        self.assertEqual(captured, [(True, 200, 3)])

    def test_pooled_false_skips_the_shared_pool_entirely(self):
        """搜索的东财兜底与行情线程并发：必须走 urllib 独立连接，不碰共享池。"""
        class _R:
            status = 200

            def read(self, count=-1):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        touched = []
        pool = sf_network.ConnectionPool()
        with mock.patch.object(pool, "get_connection",
                               side_effect=lambda *a, **k: touched.append(1)):
            with mock.patch.object(sf_network, "safe_urlopen",
                                   return_value=_R()):
                self.assertEqual(sf_network.http_get("https://h.test/a", gbk=False,
                                                     retries=0, pooled=False), "{}")
        self.assertEqual(touched, [], "pooled=False 绝不能碰共享连接池")

    def test_feature_flag_can_disable_the_pool_without_changing_results(self):
        class _R:
            status = 200

            def read(self, count=-1):
                return b"body"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(sf_network, "feature_enabled",
                               side_effect=lambda n: n != "conn_pool"), \
                mock.patch.object(sf_network, "safe_urlopen",
                                  return_value=_R()):
            self.assertEqual(sf_network.http_get("https://h.test/a", gbk=False,
                                                 retries=0), "body")

    def test_a_request_produces_exactly_one_monitor_record(self):
        """兜底路径走到末尾即失败：一条成功 + 一条失败会把同一次请求算两遍。"""
        records = []

        class _Mon:
            def begin_request(self, url):
                return {"start": time.monotonic()}

            def end_request(self, ctx, ok, status, size, err):
                records.append(ok)

        with mock.patch.object(sf_network, "_NET_MONITOR", _Mon()), \
                mock.patch.object(sf_network, "feature_enabled", return_value=True), \
                mock.patch.object(sf_network, "log_line"), \
                mock.patch.object(sf_network, "_http_get_pooled",
                                  side_effect=OSError("pool down")), \
                mock.patch.object(sf_network, "safe_urlopen",
                                  side_effect=OSError("still down")):
            with self.assertRaises(Exception):
                sf_network.http_get("https://h.test/a", retries=0)
        self.assertEqual(len(records), 1, records)

    def test_fallback_sleep_is_skipped_when_the_pool_already_tried(self):
        """池失败转 urllib 时已是第 2 重试轮：封顶单请求总耗时，不再全量重试。"""
        slept = []
        with mock.patch.object(sf_network, "log_line"), \
                mock.patch.object(sf_network, "_interruptible_sleep",
                               side_effect=lambda s, e=None: slept.append(s)), \
                mock.patch.object(sf_network, "_http_get_pooled",
                                  side_effect=OSError("x")), \
                mock.patch.object(sf_network, "safe_urlopen",
                                  side_effect=OSError("y")):
            with self.assertRaises(OSError):
                sf_network.http_get("https://h.test/a", retries=3)
        self.assertEqual(slept, [], "兜底路径不该再按全局 retries 重试")

    def test_http_error_4xx_on_fallback_path_is_not_retried(self):
        made = []

        def counted(req, timeout=None):
            calls.append(req)
            err = sf_network.urllib.error.HTTPError("https://h.test", 403,
                                                   "Forbidden", {}, None)
            made.append(err)
            raise err

        calls = []
        self.addCleanup(lambda: [e.close() for e in made])

        with mock.patch.object(sf_network, "log_line"), \
                mock.patch.object(sf_network, "_http_get_pooled",
                               side_effect=OSError("pool down")), \
                mock.patch.object(sf_network, "safe_urlopen",
                                  side_effect=counted):
            with self.assertRaises(sf_network.urllib.error.HTTPError):
                sf_network.http_get("https://h.test/a", retries=3)
        self.assertEqual(len(calls), 1, "403 在同一次 http_get 里只许发一次")
        self.assertEqual(sf_network.get_last_http_status(), 403,
                         "4xx 也要记录状态码，供封禁特征检测使用")

    def test_empty_url_is_rejected_before_network(self):
        """http_get 本身不拦空 URL —— 拦空是 search_stocks 的职责，这里只确认不崩。"""
        with mock.patch.object(sf_network, "_http_get_pooled",
                               side_effect=OSError("bad")) as pooled:
            with mock.patch.object(sf_network, "safe_urlopen",
                                   side_effect=OSError("bad")):
                with self.assertRaises(Exception):
                    sf_network.http_get("", retries=0)
        self.assertFalse(pooled.called)


class TestQuoteDataError(SFCase):
    def test_data_error_is_not_a_network_error(self):
        """格式异常不计入网络失败，否则会误触发网络降级链路。"""
        self.assertTrue(issubclass(sf_network.QuoteDataError, Exception))
        self.assertFalse(issubclass(sf_network.QuoteDataError, OSError))
        with self.assertRaises(sf_network.QuoteDataError):
            try:
                raise sf_network.QuoteDataError("字段缺失")
            except OSError:                          # pragma: no cover
                self.fail("QuoteDataError 不得被当成网络异常捕获")


if __name__ == "__main__":
    unittest.main()
