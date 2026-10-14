# -*- coding: utf-8 -*-
"""v1.2.95 第2轮走查：网络层重试策略与状态码语义。

守护三条修复：
  1. 明确拒绝类 4xx（403/404/410 等）不再走满重试 —— 否则每次刷新多等约 1 秒。
  2. 请求没拿到任何响应时，_LAST_HTTP_STATUS 必须复位为 None —— 否则断网时
     导出的诊断信息会显示上一次的 200，谎报"接口正常"。
  3. 连接池回退 urllib 的兜底路径（fb_retries=0）不得白睡 0.5s。
"""
import unittest
import urllib.request

import sf_network


class _FakeResp(object):
    def __init__(self, code):
        self.status = code

    def read(self, count=-1):
        return b"" if self.status >= 400 else b"ok"


class _FakeConn(object):
    def __init__(self, code):
        self.code = code
        self.requests = 0

    def request(self, *a, **k):
        pass

    def getresponse(self):
        self.requests += 1
        return _FakeResp(self.code)


class _NetCase(unittest.TestCase):
    """隔离网络层：连接池与 urllib 全部打桩，绝不真的发请求。"""

    def setUp(self):
        self._pool_get = sf_network._NET_POOL.get_connection
        self._pool_discard = sf_network._NET_POOL.discard
        self._pool_release = sf_network._NET_POOL.release_connection
        self._urlopen = sf_network.safe_urlopen
        self._sleep = sf_network.time.sleep
        self._status = sf_network._LAST_HTTP_STATUS
        self.conns = []
        self.sleeps = []
        sf_network._NET_POOL.discard = lambda *a, **k: None
        sf_network._NET_POOL.release_connection = lambda *a, **k: None
        sf_network.time.sleep = lambda s: self.sleeps.append(s)

    def tearDown(self):
        sf_network._NET_POOL.get_connection = self._pool_get
        sf_network._NET_POOL.discard = self._pool_discard
        sf_network._NET_POOL.release_connection = self._pool_release
        sf_network.safe_urlopen = self._urlopen
        sf_network.time.sleep = self._sleep
        sf_network._LAST_HTTP_STATUS = self._status

    def _serve(self, code):
        def _get(host, timeout=None):
            # 主窗残留线程也可能在同一进程里取连接；只为本用例的 h 主机建桩并计数，
            # 不让后台请求混进“应重试几次”的断言，造成随机多一次。
            if host != "h":
                raise OSError("test pool only serves h")
            c = _FakeConn(code)
            self.conns.append(c)
            return c
        sf_network._NET_POOL.get_connection = _get


class TestRetryPolicy(_NetCase):

    def _attempts_for(self, code):
        self.conns = []
        self._serve(code)
        try:
            sf_network._http_get_pooled("https://h/x", retries=2)
        except Exception:
            pass
        return len(self.conns)

    def test_explicit_rejection_is_not_retried(self):
        """403/404/410 这类明确拒绝，重试三次结果完全一样，只该尝试一次。"""
        for code in (400, 401, 403, 404, 410):
            self.assertEqual(self._attempts_for(code), 1,
                             "HTTP %d 不该重试，否则每次刷新都白等约 1 秒" % code)

    def test_retryable_status_still_retried(self):
        """408/429/5xx 重试有意义，必须保留重试。"""
        for code in (408, 429, 500, 502, 503):
            self.assertEqual(self._attempts_for(code), 3,
                             "HTTP %d 应当重试（retries=2 → 共 3 次）" % code)

    def test_retryable_helper_boundary(self):
        """_retryable 的边界：4xx 除 408/425/429 外一律不重试，5xx 一律重试。"""
        R = sf_network._retryable
        self.assertFalse(R(403))
        self.assertFalse(R(404))
        self.assertTrue(R(408))
        self.assertTrue(R(425))
        self.assertTrue(R(429))
        self.assertTrue(R(500))
        self.assertTrue(R(503))
        self.assertFalse(R(200))


class TestStatusSemantics(_NetCase):

    def test_status_reset_when_no_response_at_all(self):
        """网络全断时不得残留上一次的状态码，否则诊断信息会谎报接口正常。"""
        sf_network._NET_POOL.get_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        sf_network.safe_urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        sf_network._LAST_HTTP_STATUS = 200          # 模拟"之前成功过"
        with self.assertRaises(Exception):
            sf_network.http_get("https://example.invalid/x", retries=0)
        self.assertIsNone(sf_network.get_last_http_status(),
                          "没拿到任何响应时状态码必须复位，不能残留 200")

    def test_status_recorded_on_success(self):
        self._serve(200)
        body = sf_network._http_get_pooled("https://h/x", retries=0)
        self.assertEqual(body, "ok")
        self.assertEqual(sf_network.get_last_http_status(), 200)

    def test_status_recorded_on_4xx(self):
        """失败状态码仍要记录 —— 封禁特征检测依赖它（v1.2.28 起的行为）。"""
        self._serve(403)
        with self.assertRaises(Exception):
            sf_network._http_get_pooled("https://h/x", retries=0)
        self.assertEqual(sf_network.get_last_http_status(), 403)

    def test_status_is_isolated_between_concurrent_threads(self):
        """并行日历/行情请求不能把本线程的 4xx 判定覆盖成别的请求的 200。"""
        import threading
        sf_network._set_last_http_status(403)
        seen = []

        def other_request():
            sf_network._set_last_http_status(200)
            seen.append(sf_network.get_last_http_status())

        t = threading.Thread(target=other_request)
        t.start()
        t.join()
        self.assertEqual(seen, [200])
        self.assertEqual(sf_network.get_last_http_status(), 403)


class TestFallbackPath(_NetCase):

    def test_no_wasted_sleep_when_fallback_has_zero_retries(self):
        """fb_retries=0 的兜底路径本应一次就返回，不该白睡 0.5s。"""
        self.sleeps = []
        sf_network._NET_POOL.get_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        sf_network.safe_urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        with self.assertRaises(Exception):
            sf_network.http_get("https://example.invalid/x", retries=0)
        self.assertEqual(self.sleeps, [], "兜底路径不应产生任何退避等待")

    def test_fallback_log_is_throttled(self):
        """断网时该告警可达 10 条/秒，必须节流，否则挤掉真日志。"""
        logged = []
        orig_log = sf_network.log_line
        sf_network.log_line = lambda m, *a, **k: logged.append(m)
        self.addCleanup(setattr, sf_network, "log_line", orig_log)
        sf_network._NET_POOL.get_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        sf_network.safe_urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        sf_network._fallback_log_count = 0
        for _ in range(100):
            sf_network._log_fallback("conn_pool fallback to urllib: x")
        self.assertLessEqual(len(logged), 5,
                             "100 次同类告警只应记首条加每 30 条一次，实记 %d 条" % len(logged))
        self.assertGreaterEqual(len(logged), 1, "首条必须记录，不能全吞掉")


if __name__ == "__main__":
    unittest.main()
