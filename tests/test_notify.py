# -*- coding: utf-8 -*-
"""「目标价」sf_notify.py 单测（不触碰真实 UN / 不发起子进程）。"""
import datetime
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_notify

# v1.3.68：统一轮询器（见 TESTS_AUDIT_2026-09-14.md 之 P1-4）—— 本文件原先四处
# "for + break + sleep" 手写轮询 + 一处 sleep 后做负向断言，已全部改为
# wait_for / wait_until_false：超时带现场，负向断言从"那一会儿没发生"升级为
# "持续观察也没发生"。
try:
    from _base import wait_for, wait_until_false
except ImportError:  # pragma: no cover - 包模式导入时走这里
    from tests._base import wait_for, wait_until_false


class TestSemanticKey(unittest.TestCase):
    """R35-2：通知键必须带日期，否则同一价位次日再触发会被系统静默替换。"""

    def _bj(self, y, m, d):
        return datetime.datetime(y, m, d, 10, 0)

    def test_key_contains_date(self):
        with mock.patch.object(sf_notify, "_beijing_now",
                               return_value=self._bj(2026, 9, 7)):
            key = sf_notify.semantic_key("above", 1600)
        self.assertEqual(key, "target-unknown-above-1600.00-20260907")

    def test_different_stocks_same_target_have_different_keys(self):
        with mock.patch.object(sf_notify, "_beijing_now",
                               return_value=self._bj(2026, 9, 7)):
            self.assertNotEqual(
                sf_notify.semantic_key("above", 1600, "sh600519"),
                sf_notify.semantic_key("above", 1600, "sz000001"))

    def test_same_price_different_day_is_a_different_key(self):
        with mock.patch.object(sf_notify, "_beijing_now",
                               return_value=self._bj(2026, 9, 7)):
            k1 = sf_notify.semantic_key("above", 1600)
        with mock.patch.object(sf_notify, "_beijing_now",
                               return_value=self._bj(2026, 9, 8)):
            k2 = sf_notify.semantic_key("above", 1600)
        self.assertNotEqual(k1, k2, "同价位次日再触发必须是一条全新通知")

    def test_stable_within_same_day(self):
        """同一天同一价位保持稳定，才能真正起到去重作用。"""
        with mock.patch.object(sf_notify, "_beijing_now",
                               return_value=self._bj(2026, 9, 7)):
            self.assertEqual(sf_notify.semantic_key("above", 1600),
                             sf_notify.semantic_key("above", 1600.0))

    def test_direction_and_price_both_matter(self):
        with mock.patch.object(sf_notify, "_beijing_now",
                               return_value=self._bj(2026, 9, 7)):
            self.assertNotEqual(sf_notify.semantic_key("above", 1600),
                                sf_notify.semantic_key("below", 1600))
            self.assertNotEqual(sf_notify.semantic_key("above", 1600),
                                sf_notify.semantic_key("above", 1601))

    def test_bad_target_does_not_raise(self):
        """R35-1：脏目标值不得打断整条通知链路。"""
        for bad in ("abc", None, float("nan"), object()):
            self.assertTrue(sf_notify.semantic_key("above", bad).startswith("target-unknown-above-"))

    def test_beijing_now_failure_is_tolerated(self):
        with mock.patch.object(sf_notify, "_beijing_now", side_effect=RuntimeError):
            self.assertIn("unknown", sf_notify.semantic_key("above", 1600))


class TestTargetNotificationTitle(unittest.TestCase):
    """目标价通知标题应优先展示股票名称，代码仅作缺失时回退。"""

    def test_uses_stock_name_when_available(self):
        self.assertEqual(sf_notify.target_notification_title("英雄克", "002837"),
                         "英雄克 目标价")

    def test_falls_back_to_code_when_name_is_missing(self):
        self.assertEqual(sf_notify.target_notification_title("", "002837"),
                         "002837 目标价")
        self.assertEqual(sf_notify.target_notification_title(None, "002837"),
                         "002837 目标价")


class TestOsascriptFallback(unittest.TestCase):
    """R35-4：osascript 降级必须在后台线程，不得在 UI 线程同步跑子进程。"""

    def test_does_not_run_on_calling_thread(self):
        main_ident = threading.get_ident()
        seen = []

        def _spy(*_a, **_k):
            seen.append(threading.get_ident())
            return None

        with mock.patch.object(sf_notify.subprocess, "run", side_effect=_spy):
            self.assertEqual(sf_notify._deliver_osascript("T", "S", "B"),
                             "osascript")
            # v1.3.68：手写轮询换成统一轮询器，超时带现场信息（见 _base.wait_for）
            wait_for(lambda: bool(seen), timeout=2.0,
                     msg="osascript 投递线程未执行")
        self.assertTrue(seen, "应当真的投递了")
        self.assertNotIn(main_ident, seen)

    def test_returns_immediately(self):
        """投递即返回，不阻塞（原先同步跑 osascript，最坏 5s 卡界面）。"""
        with mock.patch.object(sf_notify.subprocess, "run") as m:
            t0 = time.time()
            self.assertEqual(sf_notify._deliver_osascript("T", "", "B"), "osascript")
            # 阈值守的是"不同步跑 osascript"（同步最坏 5s），所以放到 1.0s 仍有
            # 数倍余量。v1.3.68：原为 0.5，在首帧 Qt 预热/机器繁忙时会误判。
            self.assertLess(time.time() - t0, 1.0,
                            "投递必须立即返回，不得同步等待 osascript（同步最坏 5s）")

    def test_quotes_are_sanitized(self):
        """脚本里的双引号必须转义，否则整条 osascript 会语法错误、静默不弹。"""
        calls = []
        with mock.patch.object(sf_notify.subprocess, "run",
                               side_effect=lambda *a, **k: calls.append(a)):
            sf_notify._deliver_osascript('say "hi"', "", 'body "x"')
            wait_for(lambda: bool(calls), timeout=2.0,
                     msg="osascript 未被调用")
        self.assertTrue(calls)
        script = calls[0][0][2]
        self.assertNotIn('say "hi"', script)
        self.assertIn("say 'hi'", script)

    def test_subprocess_failure_is_logged_not_raised(self):
        with mock.patch.object(sf_notify.subprocess, "run",
                               side_effect=OSError("boom")):
            self.assertEqual(sf_notify._deliver_osascript("T", "", "B"), "osascript")


class _FakeCenter:
    def __init__(self):
        self.handler = None

    def setDelegate_(self, _delegate):
        pass

    def addNotificationRequest_withCompletionHandler_(self, _req, handler):
        self.handler = handler


class TestCompletionFallback(unittest.TestCase):
    def setUp(self):
        sf_notify.reset_delivery_probe()

    def _deliver(self):
        center = _FakeCenter()
        content = mock.MagicMock()
        with mock.patch.object(sf_notify, "_UN_UNAVAILABLE", False), \
             mock.patch.object(sf_notify, "authorization_status", return_value="authorized"), \
             mock.patch.object(sf_notify, "UNUserNotificationCenter") as unc, \
             mock.patch.object(sf_notify, "UNMutableNotificationContent") as mut, \
             mock.patch.object(sf_notify, "UNNotificationRequest") as req, \
             mock.patch.object(sf_notify, "NSString") as ns:
            unc.currentNotificationCenter.return_value = center
            mut.alloc.return_value.init.return_value = content
            req.requestWithIdentifier_content_trigger_.return_value = object()
            ns.stringWithString_.side_effect = lambda s: s
            result = sf_notify.deliver("标题", "副标题", "正文", semantic_key="same-key")
        return result, center

    def test_completion_error_falls_back_for_same_notification(self):
        result, center = self._deliver()
        self.assertEqual(result, "un")
        with mock.patch.object(sf_notify, "_deliver_osascript", return_value="osascript") as fallback:
            center.handler(RuntimeError("delivery failed"))
        fallback.assert_called_once_with("标题", "副标题", "正文")
        self.assertTrue(sf_notify._USE_OSASCRIPT)

    def test_fallback_dedup_cache_is_bounded(self):
        with sf_notify._DELIVERY_LOCK:
            sf_notify._FALLBACK_KEYS.clear()
            for i in range(sf_notify._FALLBACK_KEYS_MAX + 10):
                sf_notify._FALLBACK_KEYS["k%d" % i] = None
                while len(sf_notify._FALLBACK_KEYS) > sf_notify._FALLBACK_KEYS_MAX:
                    sf_notify._FALLBACK_KEYS.popitem(last=False)
        self.assertEqual(len(sf_notify._FALLBACK_KEYS), sf_notify._FALLBACK_KEYS_MAX)

    def test_duplicate_completion_callback_only_falls_back_once(self):
        _result, center = self._deliver()
        with mock.patch.object(sf_notify, "_deliver_osascript", return_value="osascript") as fallback:
            center.handler(RuntimeError("first"))
            center.handler(RuntimeError("duplicate"))
        fallback.assert_called_once()


class TestNotificationClickRouting(unittest.TestCase):
    """多条通知同时存在时，点击必须回到各自的窗口回调。"""

    def setUp(self):
        sf_notify.reset_delivery_probe()

    def test_identifier_reader_supports_objc_style_response(self):
        class Request:
            def identifier(self):
                return "target-a"
        class Notification:
            def request(self):
                return Request()
        class Response:
            def notification(self):
                return Notification()
        self.assertEqual(sf_notify._notification_identifier(Response()), "target-a")

    def test_click_handlers_are_keyed_by_notification_identifier(self):
        calls = []
        with sf_notify._DELIVERY_LOCK:
            sf_notify._CLICK_HANDLERS["target-first"] = lambda: calls.append("first")
            sf_notify._CLICK_HANDLERS["target-last"] = lambda: calls.append("last")
            callback = sf_notify._CLICK_HANDLERS.pop("target-first")
        callback()
        self.assertEqual(calls, ["first"])
        self.assertIn("target-last", sf_notify._CLICK_HANDLERS)


class TestAuthorizationApi(unittest.TestCase):
    def test_source_uses_options_authorization_api(self):
        import inspect
        src = inspect.getsource(sf_notify.request_authorization)
        self.assertIn("requestAuthorizationWithOptions_completionHandler_", src)
        self.assertNotIn("requestAuthorizationWithCompletionHandler_", src)


class TestUnavailableDegradation(unittest.TestCase):
    """UN 不可用时：视作 denied，走 osascript，不得抛异常。"""

    def test_denied_status_when_un_unavailable(self):
        with mock.patch.object(sf_notify, "_UN_UNAVAILABLE", True):
            self.assertEqual(sf_notify.authorization_status(), "denied")

    def test_deliver_falls_back_to_osascript(self):
        with mock.patch.object(sf_notify, "_UN_UNAVAILABLE", True), \
             mock.patch.object(sf_notify.subprocess, "run") as m:
            self.assertEqual(sf_notify.deliver("T", "S", "B"), "osascript")
            # 后台线程跑，主线程立即返回；轮询等它执行完再断言（超时带现场）
            wait_for(lambda: bool(m.call_count), timeout=2.0,
                     msg="降级 osascript 未被调用")
            self.assertEqual(m.call_count, 1)

    def test_deliver_denied_does_not_pop(self):
        """授权 denied：只走彩虹，不弹窗、不降级 osascript。"""
        with mock.patch.object(sf_notify, "_UN_UNAVAILABLE", False), \
             mock.patch.object(sf_notify, "authorization_status",
                               return_value="denied"), \
             mock.patch.object(sf_notify.subprocess, "run") as m:
            self.assertEqual(sf_notify.deliver("T", "S", "B"), "denied")
            # v1.3.68：原来是 sleep(0.05) 后断言 0 —— 那只能证明"睡的那一小会儿没发生"。
            # 改成持续观察 0.5s 都没发生，才是真的守住了"denied 不弹窗"这条契约。
            self.assertTrue(wait_until_false(lambda: m.call_count, timeout=0.5,
                                             msg="denied 状态下仍然投递了通知"),
                            "denied 时不得有任何投递")
            self.assertEqual(m.call_count, 0)


if __name__ == "__main__":
    unittest.main()
