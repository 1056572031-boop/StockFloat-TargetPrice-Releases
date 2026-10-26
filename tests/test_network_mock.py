# -*- coding: utf-8 -*-
"""实时股价 v1.2.39 网络层 mock 单元测试。

覆盖建议项中"网络层无 mock 测试（重试/熔断/备源切换目前靠手动验证）"：
1. _http_get_pooled 重试语义：连接被服务端断开自动销毁重建 → 重试成功
2. 4xx/5xx 状态码：销毁连接并最终抛错
3. 全部尝试失败：抛最后一次异常（尝试次数 = retries+1）
4. ConnectionPool.close_all 幂等（退出漏调已修复，close 后可复用）
5. NetMonitor 监控统计：成功率/平均延迟/snapshot/环形缓冲容量截断
6. sanitize_url_for_log 敏感参数脱敏（token 不落日志）
7. 备源切换（fallback）保持不回归：主源失败回落备源

纯逻辑测试，不依赖 PyQt6。运行方式（在 StockFloat 目录）：
    python3 -m unittest tests/test_network_mock.py -v
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_network
from sf_network import (ConnectionPool, NetMonitor, _http_get_pooled,
                        sanitize_url_for_log)
from sf_constants import HTTP_RETRIES


class FakeConn:
    """最小 HTTPConnection 替代：可配置前 N 次请求抛 OSError（模拟连接被断），
    或返回指定 HTTP 状态码/响应体。"""

    def __init__(self, body=b"ok", status=200, fail_requests=0):
        self.body = body
        self.status = status
        self.fail_requests = fail_requests
        self.requested = 0
        self.closed = False

    def request(self, *args, **kwargs):
        self.requested += 1
        if self.requested <= self.fail_requests:
            raise OSError("conn reset by peer")

    def getresponse(self):
        body, status = self.body, self.status

        class Resp:
            def read(self, count=-1):
                return body
        Resp.status = status
        return Resp()

    def close(self):
        self.closed = True


class FakePool:
    """记录 discard / release 行为的假连接池。"""

    def __init__(self, conn):
        self.conn = conn
        self.discarded = []
        self.released = 0

    def get_connection(self, host, timeout):
        # 全量随机回归期间，别的窗口用例可能仍在收尾并发网络请求。
        # 这个假连接只服务当前用例的 example.com；其它请求不得借用它，
        # 否则会把后台收尾的 release 误算到本用例，造成假失败。
        if host != "example.com":
            raise OSError("unexpected test host: %s" % host)
        return self.conn

    def discard(self, host, conn):
        if host == "example.com":
            self.discarded.append(host)

    def release_connection(self, host, conn):
        if host == "example.com":
            self.released += 1


class TestHttpMonitorSingleRecord(unittest.TestCase):
    def test_controlled_walkthrough_hard_rejects_before_any_transport(self):
        """走查子进程不能因为自建 opener 忽略环境代理而悄悄触网。"""
        with patch.dict(os.environ, {"SF_REJECT_NETWORK": "1"}, clear=False), \
             patch("sf_network._http_get_pooled") as pooled, \
             patch("sf_network.safe_urlopen") as opened:
            with self.assertRaises(OSError) as caught:
                sf_network.http_get("https://example.com", retries=0)
        self.assertIn("network disabled", str(caught.exception))
        pooled.assert_not_called()
        opened.assert_not_called()

    def test_network_reject_flag_absent_keeps_existing_transport_path(self):
        """普通产品不设走查变量，网络入口不能被这条测试隔离逻辑改变。"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SF_REJECT_NETWORK", None)
            with patch("sf_network.feature_enabled", return_value=False), \
                 patch("sf_network.safe_urlopen", side_effect=OSError("transport reached")) as opened:
                with self.assertRaisesRegex(OSError, "transport reached"):
                    sf_network.http_get("https://example.com", retries=0)
        opened.assert_called_once()

    def test_pool_failure_and_urllib_failure_record_once(self):
        mon = NetMonitor(capacity=10, window=3600)
        with patch("sf_network.feature_enabled", return_value=True), \
             patch("sf_network._NET_MONITOR", mon), \
             patch("sf_network._http_get_pooled", side_effect=OSError("pool")), \
             patch("sf_network.safe_urlopen", side_effect=OSError("urllib")):
            with self.assertRaises(OSError):
                sf_network.http_get("https://example.com")
        self.assertEqual(mon.snapshot()["total"], 1)


class TestHttpGetPooledRetry(unittest.TestCase):
    """重试语义：连接被服务端断开 → 自动销毁并重试重建，最终成功。"""

    def test_retry_succeeds_after_conn_reset(self):
        conn = FakeConn(body=b"OK", fail_requests=1)   # 第 1 次断开，第 2 次成功
        pool = FakePool(conn)
        with patch("sf_network._NET_POOL", pool):
            body = _http_get_pooled("https://example.com/a?k=v")
        self.assertEqual(body, "OK")
        self.assertEqual(conn.requested, 2)          # 1 次失败 + 1 次成功
        self.assertEqual(pool.discarded, ["example.com"])   # 断开的连接被销毁
        self.assertEqual(pool.released, 1)           # 成功的连接放回池

    def test_retry_budget_exhausted_raises_last_error(self):
        # 每次都断开 → 尝试 retries+1 次后抛最后一次异常，连接全部销毁
        conn = FakeConn(fail_requests=10 ** 6)
        pool = FakePool(conn)
        with patch("sf_network._NET_POOL", pool):
            with self.assertRaises(OSError):
                _http_get_pooled("https://example.com/x")
        self.assertEqual(conn.requested, HTTP_RETRIES + 1)
        self.assertEqual(pool.released, 0)
        self.assertEqual(len(pool.discarded), HTTP_RETRIES + 1)


class TestHttpGetPooledHttpStatus(unittest.TestCase):
    """熔断相关状态处理：4xx/5xx 属服务端错误，连接不可复用，销毁后按重试。

    v1.2.28 起失败状态也写入 _LAST_HTTP_STATUS，供封禁特征检测命中。"""

    def test_5xx_discard_and_raise(self):
        conn = FakeConn(body=b"err", status=503)
        pool = FakePool(conn)
        with patch("sf_network._NET_POOL", pool):
            with self.assertRaises(OSError) as cm:
                _http_get_pooled("https://example.com/y")
        self.assertIn("HTTP 503", str(cm.exception))
        self.assertEqual(len(pool.discarded), HTTP_RETRIES + 1)  # 每次 5xx 都销毁
        self.assertEqual(pool.released, 0)

    def test_200_release_back(self):
        conn = FakeConn(body=b"fine", status=200)
        pool = FakePool(conn)
        with patch("sf_network._NET_POOL", pool):
            body = _http_get_pooled("https://example.com/z")
        self.assertEqual(body, "fine")
        self.assertEqual(pool.discarded, [])
        self.assertEqual(pool.released, 1)


class TestConnectionPool(unittest.TestCase):
    """连接池：close_all 幂等、close 后仍可复用。"""

    def _mk(self, n=2):
        return [FakeConn() for _ in range(n)]

    def test_close_all_idempotent(self):
        pool = ConnectionPool(max_connections=5)
        conns = self._mk()
        pool.release_connection("h1", conns[0])
        pool.release_connection("h1", conns[1])
        pool.close_all()          # 第一次关闭
        pool.close_all()          # 第二次调用不抛（幂等）
        self.assertTrue(all(c.closed for c in conns))

    def test_close_all_sets_abort_for_inflight_waiters(self):
        import sf_network
        sf_network._NET_ABORT.clear()
        pool = ConnectionPool(max_connections=5)
        pool.close_all()
        self.assertTrue(sf_network._NET_ABORT.is_set())

    def test_new_network_round_replaces_set_abort_without_clearing_old_token(self):
        import sf_network
        sf_network._NET_ABORT.set()
        old = sf_network._NET_ABORT
        current = sf_network._begin_network_round()
        self.assertTrue(old.is_set(), "在途请求持有的旧令牌必须继续保持中止")
        self.assertIsNot(current, old)
        self.assertFalse(current.is_set(), "新一轮请求必须恢复正常等待能力")

    def test_pool_reusable_after_close_all(self):
        pool = ConnectionPool(max_connections=5)
        c = FakeConn()
        pool.release_connection("h2", c)
        pool.close_all()
        self.assertTrue(c.closed)
        # close_all 只清空缓存不停用：后续 get_connection 仍能新建并复用
        c2 = FakeConn()
        pool.release_connection("h2", c2)
        self.assertFalse(c2.closed)


class TestNetMonitor(unittest.TestCase):
    """网络监控：成功率/平均延迟统计、snapshot、环形缓冲容量截断。"""

    def test_success_rate(self):
        mon = NetMonitor(capacity=100, window=3600)   # window 放大避免触发 flush
        for _ in range(3):
            ctx = mon.begin_request("https://example.com/a")
            mon.end_request(ctx, True, 200, 100, None)
        ctx = mon.begin_request("https://example.com/b")
        mon.end_request(ctx, False, 500, 0, "boom")
        snap = mon.snapshot()
        self.assertEqual(snap["total"], 4)
        self.assertEqual(snap["ok"], 3)
        self.assertEqual(snap["rate"], 75.0)
        self.assertIsNotNone(snap["avg_latency_ms"])   # 同一时钟内耗时可能为 0.0，仅断言非 None

    def test_ring_capacity_truncate(self):
        mon = NetMonitor(capacity=3, window=3600)
        for i in range(5):
            ctx = mon.begin_request("https://example.com/c")
            mon.end_request(ctx, True, 200, i, None)
        snap = mon.snapshot()
        self.assertEqual(snap["total"], 3)   # FIFO 截断：只保留最近 3 条

    def test_begin_request_sanitizes_path(self):
        mon = NetMonitor(capacity=10, window=3600)
        ctx = mon.begin_request("https://x.com/api?token=SECRET123&v=2")
        self.assertEqual(ctx["api_path"], "https://x.com/api?v=2")  # token 已剥离


class TestSanitizeUrlForLog(unittest.TestCase):
    """日志脱敏：token/凭据/密码等敏感 query 一律不落完整值。"""

    def test_strip_sensitive_query(self):
        url = "https://api.example.com/get?token=SECRET&input=600519&type=14"
        s = sanitize_url_for_log(url)
        self.assertNotIn("SECRET", s)
        self.assertIn("input=600519", s)     # 非敏感参数保留

    def test_strip_userinfo(self):
        url = "https://user:pass@example.com/data"
        s = sanitize_url_for_log(url)
        self.assertNotIn("user:pass", s)
        self.assertIn("example.com", s)

    def test_malformed_falls_back(self):
        s = sanitize_url_for_log("not a url at all")
        self.assertIsInstance(s, str)


if __name__ == "__main__":
    unittest.main()
