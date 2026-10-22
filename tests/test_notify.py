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
    from _base import SFCase, fixed_clock, wait_for, wait_until_false
except ImportError:  # pragma: no cover - 包模式导入时走这里
    from tests._base import SFCase, fixed_clock, wait_for, wait_until_false


class TestSemanticKey(unittest.TestCase):
    """R35-2：通知键必须带日期，否则同一价位次日再触发会被系统静默替换。"""

    def _bj(self, y, m, d):
        return datetime.datetime(y, m, d, 10, 0)

    def test_key_contains_date(self):
        with fixed_clock(self._bj(2026, 9, 7), module_names=("sf_notify",)):
            key = sf_notify.semantic_key("above", 1600)
        self.assertEqual(key, "target-unknown-above-1600.00-20260907")

    def test_different_stocks_same_target_have_different_keys(self):
        with fixed_clock(self._bj(2026, 9, 7), module_names=("sf_notify",)):
            self.assertNotEqual(
                sf_notify.semantic_key("above", 1600, "sh600519"),
                sf_notify.semantic_key("above", 1600, "sz000001"))

    def test_same_price_different_day_is_a_different_key(self):
        with fixed_clock(self._bj(2026, 9, 7), module_names=("sf_notify",)):
            k1 = sf_notify.semantic_key("above", 1600)
        with fixed_clock(self._bj(2026, 9, 8), module_names=("sf_notify",)):
            k2 = sf_notify.semantic_key("above", 1600)
        self.assertNotEqual(k1, k2, "同价位次日再触发必须是一条全新通知")

    def test_stable_within_same_day(self):
        """同一天同一价位保持稳定，才能真正起到去重作用。"""
        with fixed_clock(self._bj(2026, 9, 7), module_names=("sf_notify",)):
            self.assertEqual(sf_notify.semantic_key("above", 1600),
                             sf_notify.semantic_key("above", 1600.0))

    def test_direction_and_price_both_matter(self):
        with fixed_clock(self._bj(2026, 9, 7), module_names=("sf_notify",)):
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
        self.assertEqual(sf_notify._notification_identifier(Response()), "target-a")  # discipline-ok(R5): test the private adapter because no public identifier reader exists

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


class _SettingsStub:
    def __init__(self, status):
        self._status = status

    def authorizationStatus(self):
        return self._status


class _AsyncSettingsCenter:
    def __init__(self):
        self.callbacks = []
        self.auth_callback = None

    def getNotificationSettingsWithCompletionHandler_(self, callback):
        self.callbacks.append(callback)

    def requestAuthorizationWithOptions_completionHandler_(self, _options, callback):
        self.auth_callback = callback


class TestAsyncAuthorizationStatus(SFCase):
    def setUp(self):
        super().setUp()
        sf_notify.reset_delivery_probe()
        self.center = _AsyncSettingsCenter()
        self.clock_value = 1000.0
        self.patchers = [
            mock.patch.object(sf_notify, "_ensure_un"),
            mock.patch.object(sf_notify.time, "monotonic", side_effect=lambda: self.clock_value),
            mock.patch.object(sf_notify, "_UN_UNAVAILABLE", False),
            mock.patch.object(sf_notify, "_un_center", return_value=self.center),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(self.patchers)])

    def tearDown(self):
        sf_notify.reset_delivery_probe()
        super().tearDown()

    def _pending_fixture(self):
        # Only main-queue work may create UN content/delegate; manually pump it
        # after the real delayed settings callback, never invoke ObjC in a worker.
        self.main_jobs = []
        self.sent = []
        for patcher in (
                mock.patch.object(sf_notify, "dispatch_get_main_queue", return_value="main"),
                mock.patch.object(sf_notify, "dispatch_async",
                                  side_effect=lambda q, fn: self.main_jobs.append(fn)),
                mock.patch.object(sf_notify, "_install_delegate"),
                mock.patch.object(sf_notify, "UNMutableNotificationContent"),
                mock.patch.object(sf_notify, "UNNotificationRequest"),
                mock.patch.object(sf_notify, "NSString"),
                mock.patch.object(sf_notify, "has_bundle_identity", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.center.addNotificationRequest_withCompletionHandler_ = \
            lambda req, done: self.sent.append((req, done, threading.get_ident()))

    def test_delayed_authorized_flushes_after_hit_without_a_second_delivery(self):
        self._pending_fixture()
        click = mock.Mock()
        with mock.patch.object(sf_notify, "_deliver_osascript") as fallback:
            self.assertEqual(sf_notify.deliver("T", "S", "B", "hit", click), "pending")
            self.assertEqual(self.sent, [])
            callback = self.center.callbacks[0]
            worker = threading.Thread(target=lambda: callback(_SettingsStub(2)))
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(self.sent, [], "settings worker must not build/send UN")
            self.assertEqual(len(self.main_jobs), 1)
            callback(_SettingsStub(2))
            self.main_jobs.pop()()
            self.assertEqual(len(self.sent), 1)
            self.assertEqual(self.sent[0][2], threading.get_ident())
            fallback.assert_not_called()
            stats = sf_notify.delivery_stats()
            self.assertEqual(stats["pending"]["size"], 0)
            self.assertEqual(stats["pending"]["counts"]["flushed"], 1)

    def test_first_denied_never_escapes_through_osascript(self):
        self._pending_fixture()
        with mock.patch.object(sf_notify, "_deliver_osascript") as fallback:
            self.assertEqual(sf_notify.deliver("T", "", "B", "hit"), "pending")
            self.center.callbacks[0](_SettingsStub(1))
            self.assertEqual(sf_notify.deliver("T", "", "B", "hit"), "denied")
            self.assertEqual(self.sent, [])
            self.assertEqual(self.main_jobs, [])
            stats = sf_notify.delivery_stats()["pending"]
            self.assertEqual(stats["size"], 0)
            self.assertEqual(stats["counts"]["denied"], 1)
            fallback.assert_not_called()

    def test_not_determined_reply_delivers_the_queued_reminder_once_by_fallback(self):
        """系统答「尚未询问」：排队的提醒不能丢（该目标价已落盘记为已触发），补发恰好一次。"""
        self._pending_fixture()
        with mock.patch.object(sf_notify, "_deliver_osascript",
                               return_value="osascript") as fallback:
            for _ in range(3):
                self.assertEqual(sf_notify.deliver("T", "", "B", "hit", mock.Mock()),
                                 "pending")
            self.assertEqual(sf_notify.delivery_stats()["pending"]["size"], 1)
            self.center.callbacks[0](_SettingsStub(0))
            stats = sf_notify.delivery_stats()["pending"]
            self.assertEqual(stats["size"], 0)
            self.assertEqual(stats["counts"]["deduplicated"], 2)
            self.assertEqual(stats["counts"]["unanswered"], 1)
            self.assertEqual(stats["counts"]["fallback"], 1)
            fallback.assert_called_once_with("T", "", "B")
            self.assertEqual(self.sent, [], "未授权时不得走 UN 路线")
            self.assertFalse(sf_notify._CLICK_HANDLERS)  # discipline-ok(R5): no public click-registration reader

    def test_not_determined_known_before_delivery_falls_back_immediately(self):
        self._pending_fixture()
        sf_notify.authorization_status()
        self.center.callbacks[-1](_SettingsStub(0))
        with mock.patch.object(sf_notify, "_deliver_osascript",
                               return_value="osascript") as fallback:
            self.assertEqual(sf_notify.deliver("T", "S", "B", "hit"), "osascript")
            fallback.assert_called_once_with("T", "S", "B")
        self.assertEqual(sf_notify.delivery_stats()["pending"]["size"], 0)

    def test_denial_wins_over_not_determined_fallback_when_it_races_in(self):
        """快照读到「尚未询问」的同一刻，用户点了拒绝：不得补发。"""
        def status_then_refuse():
            with sf_notify._DELIVERY_LOCK:  # discipline-ok(R5): model the refusal racing with the status snapshot
                sf_notify._AUTH_STATUS_CACHE = "denied"  # discipline-ok(R5): seed the cache at the lock boundary under test
            return "notDetermined"

        with mock.patch.object(sf_notify, "authorization_status", side_effect=status_then_refuse), \
             mock.patch.object(sf_notify, "_deliver_osascript") as fallback:
            self.assertEqual(sf_notify._deliver_impl("T", "", "B", "race"), "denied")  # discipline-ok(R5): exercise the lock-protected refusal race
            fallback.assert_not_called()

    def test_denial_between_the_two_rechecks_still_blocks_the_not_determined_fallback(self):
        """拒绝落在「读状态后的第一次复核」与「补发分支自己的复核」之间：仍不得补发。"""
        real_lock = sf_notify._DELIVERY_LOCK  # discipline-ok(R5): wrap the real lock to inject the refusal at an exact lock boundary, without timing guesses
        state = {"armed": False, "entries": 0}

        class RefuseAtSecondRecheck:
            def __enter__(self):
                if state["armed"]:
                    state["entries"] += 1
                    if state["entries"] == 2:
                        state["armed"] = False
                        with real_lock:
                            sf_notify._AUTH_STATUS_CACHE = "denied"  # discipline-ok(R5): the refusal lands inside the window under test
                return real_lock.__enter__()

            def __exit__(self, *args):
                return real_lock.__exit__(*args)

        def snapshot_not_determined():
            state["armed"] = True
            return "notDetermined"

        with mock.patch.object(sf_notify, "_DELIVERY_LOCK", RefuseAtSecondRecheck()), \
             mock.patch.object(sf_notify, "authorization_status", side_effect=snapshot_not_determined), \
             mock.patch.object(sf_notify, "_deliver_osascript") as fallback:
            self.assertEqual(sf_notify._deliver_impl("T", "", "B", "race2"), "denied")  # discipline-ok(R5): exercise the lock-protected refusal window
        self.assertEqual(state["entries"], 2)
        fallback.assert_not_called()

    def test_reminder_without_any_reply_is_delivered_after_the_queue_timeout(self):
        """授权答复一直不来：排队满 60 秒后补发，而不是丢掉。"""
        self._pending_fixture()
        with mock.patch.object(sf_notify, "_deliver_osascript",
                               return_value="osascript") as fallback:
            self.assertEqual(sf_notify.deliver("old", "", "body-old", "old"), "pending")
            fallback.assert_not_called()
            self.clock_value += sf_notify._PENDING_TTL_SEC  # discipline-ok(R5): private TTL is the contract boundary under the injected monotonic clock
            self.assertEqual(sf_notify.deliver("new", "", "body-new", "new"), "pending")
            fallback.assert_called_once_with("old", "", "body-old")
            counts = sf_notify.delivery_stats()["pending"]["counts"]
            self.assertEqual(counts["expired"], 1)
            self.assertEqual(counts["fallback"], 1)

    def test_unanswered_backlog_is_bounded_and_the_loss_is_counted(self):
        """补发名单也有上限：超出的最老条目被裁掉，并计入 overflow，不会无限涨。"""
        extra = 44
        with sf_notify._DELIVERY_LOCK:  # discipline-ok(R5): seed the backlog beyond its bound because no public API can grow it past the pending limit
            sf_notify._UNANSWERED.extend(  # discipline-ok(R5): seed the backlog under test
                ("k%d" % i, "T", "S", "B") for i in range(sf_notify._PENDING_MAX + extra))  # discipline-ok(R5): the bound is the contract under test
            sf_notify._expire_pending_locked()  # discipline-ok(R5): call the locked helper directly to reach the trimming branch
            kept = list(sf_notify._UNANSWERED)  # discipline-ok(R5): read back the trimmed backlog
        self.assertEqual(len(kept), sf_notify._PENDING_MAX)  # discipline-ok(R5): the bound is the contract under test
        self.assertEqual(kept[0][0], "k%d" % extra, "裁掉的是最老的，留下最新的")
        self.assertEqual(sf_notify.delivery_stats()["pending"]["counts"]["overflow"], extra)

    def test_fallback_failure_is_recorded_and_never_escapes(self):
        """补发本身抛异常：留痕、计入 error，不外抛、不把同批其余提醒带走。"""
        with sf_notify._DELIVERY_LOCK:  # discipline-ok(R5): seed the timed-out backlog because no public API exposes it
            sf_notify._UNANSWERED.extend([("a", "T1", "", "B1"), ("b", "T2", "", "B2")])  # discipline-ok(R5): seed the backlog under test
        calls = []

        def flaky(title, subtitle, body):
            calls.append(title)
            if title == "T1":
                raise RuntimeError("osascript missing")
            return "osascript"

        errors_before = sf_notify.delivery_stats()["counts"]["error"]
        with mock.patch.object(sf_notify, "_deliver_osascript", side_effect=flaky), \
             mock.patch.object(sf_notify, "log_line") as log:
            sf_notify._deliver_unanswered()  # discipline-ok(R5): drain directly to isolate the failure handling
        self.assertEqual(calls, ["T1", "T2"], "第一条失败不得影响第二条")
        stats = sf_notify.delivery_stats()
        self.assertEqual(stats["counts"]["error"], errors_before + 1)
        self.assertEqual(stats["pending"]["counts"]["fallback"], 1)
        self.assertTrue(any("unanswered fallback error" in str(c.args[0]) for c in log.call_args_list))

    def test_timed_out_reminder_is_dropped_not_sent_when_the_user_has_refused(self):
        with sf_notify._DELIVERY_LOCK:  # discipline-ok(R5): seed the timed-out backlog because no public API exposes it
            sf_notify._UNANSWERED.append(("k", "T", "S", "B"))  # discipline-ok(R5): seed the backlog under test
            sf_notify._AUTH_STATUS_CACHE = "denied"  # discipline-ok(R5): the refusal arrived while the reminder waited
        with mock.patch.object(sf_notify, "_deliver_osascript") as fallback:
            sf_notify._deliver_unanswered()  # discipline-ok(R5): drain directly to isolate the refusal-first rule
            fallback.assert_not_called()
        self.assertEqual(sf_notify.delivery_stats()["pending"]["counts"]["denied"], 1)

    def test_pending_bound_expiry_and_lost_query_are_visible_in_diagnostics(self):
        self._pending_fixture()
        with mock.patch.object(sf_notify, "_deliver_osascript",
                               return_value="osascript") as fallback:
            for _ in range(258):
                self.assertEqual(sf_notify.deliver("T", "", "B"), "pending")
            stats = sf_notify.delivery_stats()["pending"]
            self.assertEqual(stats["size"], 256)
            self.assertEqual(stats["counts"]["overflow"], 2)
            self.clock_value += 5.0
            sf_notify.deliver("T", "", "B", "query-retry-trigger")
            stats = sf_notify.delivery_stats()["pending"]
            self.assertEqual(stats["counts"]["query_lost"], 1)
            self.assertEqual(len(self.center.callbacks), 2)
            fallback.assert_not_called()
            self.clock_value += 60.0
            sf_notify.deliver("T", "", "B", "expiry-trigger")
            stats = sf_notify.delivery_stats()["pending"]
            self.assertEqual(stats["size"], 1)
            self.assertEqual(stats["counts"]["expired"], 256)
            # 过期的 256 条不是丢弃，而是补发（见 _deliver_unanswered）
            self.assertEqual(fallback.call_count, 256)
            self.assertEqual(stats["counts"]["fallback"], 256)

    def test_delivery_stats_marks_expired_authorization_without_refreshing(self):
        sf_notify.authorization_status()
        self.center.callbacks[-1](_SettingsStub(2))
        self.clock_value += 10.0
        with mock.patch.object(sf_notify, "has_bundle_identity", return_value=True):
            stats = sf_notify.delivery_stats()
        self.assertEqual(stats["authorization"], "unknown")
        self.assertEqual(stats["authorization_cache"]["value"], "authorized")
        self.assertFalse(stats["authorization_cache"]["fresh"])
        self.assertEqual(stats["authorization_cache"]["age_sec"], 10.0)
        self.assertEqual(len(self.center.callbacks), 1)

    def test_delivery_stats_is_read_only_with_authorized_pending(self):
        self._pending_fixture()
        sf_notify.deliver("T", "", "B", "pending")
        self.center.callbacks[-1](_SettingsStub(2))
        queued_jobs = list(self.main_jobs)
        with mock.patch.object(sf_notify, "authorization_status",
                               side_effect=AssertionError("diagnostics must not query")), \
             mock.patch.object(sf_notify, "_schedule_pending_flush",
                               side_effect=AssertionError("diagnostics must not flush")), \
             mock.patch.object(sf_notify, "_expire_pending_locked",
                               side_effect=AssertionError("diagnostics must not expire")), \
             mock.patch.object(sf_notify, "_deliver_osascript",
                               side_effect=AssertionError("diagnostics must not fallback")):
            self.clock_value += 3.0
            stats = sf_notify.delivery_stats()
        self.assertEqual(stats["authorization"], "authorized")
        self.assertEqual(stats["pending"]["size"], 1)
        self.assertEqual(stats["pending"]["oldest_age_sec"], 3.0)
        self.assertEqual(self.main_jobs, queued_jobs)

    def test_denied_after_flush_scheduled_wins_and_reset_cancels_old_jobs(self):
        self._pending_fixture()
        with mock.patch.object(sf_notify, "_deliver_osascript") as fallback:
            sf_notify.deliver("T", "", "B", "hit")
            self.center.callbacks[-1](_SettingsStub(2))
            sf_notify._refresh_authorization_status()  # discipline-ok(R5): re-enter the private refresh to model a concurrent generation
  # discipline-ok(R5): force a fresh system answer while a main-queue job is pending
            self.center.callbacks[-1](_SettingsStub(1))
            self.main_jobs.pop()()
            self.assertEqual(self.sent, [])
            sf_notify.reset_delivery_probe()
            sf_notify.deliver("T", "", "B", "new")
            self.center.callbacks[-1](_SettingsStub(2))
            sf_notify.reset_delivery_probe()
            self.main_jobs.pop()()
            self.assertEqual(self.sent, [])
            fallback.assert_not_called()

    def test_concurrent_settings_and_diagnostics_schedule_only_one_flush(self):
        self._pending_fixture()
        sf_notify.deliver("T", "", "B", "same")
        callback = self.center.callbacks[-1]
        workers = [threading.Thread(target=lambda: callback(_SettingsStub(2)))
                   for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        sf_notify.delivery_stats()
        self.assertEqual(sf_notify.deliver("T", "", "B", "same"), "pending")
        self.assertEqual(len(self.main_jobs), 1)
        self.main_jobs.pop()()
        self.assertEqual(len(self.sent), 1)

    def test_unknown_then_async_settings_status(self):
        self.assertEqual(sf_notify.authorization_status(), "unknown")
        self.assertEqual(len(self.center.callbacks), 1)
        self.assertEqual(sf_notify.authorization_status(), "unknown")
        self.assertEqual(len(self.center.callbacks), 1, "在途查询必须去重")
        self.center.callbacks[0](_SettingsStub(2))
        self.assertEqual(sf_notify.authorization_status(), "authorized")
        self.assertEqual(len(self.center.callbacks), 1, "缓存有效期内不应重复查询")

    def test_settings_denied_wins_over_fallback(self):
        self.center.callbacks.append(None)
        callback = sf_notify.authorization_status
        self.assertEqual(callback(), "unknown")
        self.center.callbacks[-1](_SettingsStub(1))
        with mock.patch.object(sf_notify, "_deliver_osascript") as fallback:
            self.assertEqual(sf_notify._deliver_impl("T", "", "B"), "denied")  # discipline-ok(R5): verify denial uses cached authorization without invoking fallback
            fallback.assert_not_called()

    def test_query_failure_releases_in_flight_and_late_callback_is_ignored_after_reset(self):
        self.assertEqual(sf_notify.authorization_status(), "unknown")
        old_callback = self.center.callbacks[0]
        sf_notify.reset_delivery_probe()
        self.assertEqual(sf_notify.authorization_status(), "unknown")
        self.assertEqual(len(self.center.callbacks), 2)
        old_callback(_SettingsStub(1))
        self.assertEqual(sf_notify.authorization_status(), "unknown", "旧测试回调不得污染新缓存")
        self.center.callbacks[1](_SettingsStub(3))
        self.assertEqual(sf_notify.authorization_status(), "provisional")

    def test_request_authorization_completion_refreshes_cache(self):
        with mock.patch.object(sf_notify, "_install_delegate"), \
             mock.patch.object(sf_notify, "_refresh_authorization_status") as refresh:
            sf_notify.request_authorization()
            self.assertIsNotNone(self.center.auth_callback)
            self.center.auth_callback(False, RuntimeError("denied"))
        refresh.assert_called_once_with()

    def test_lost_callback_expires_and_old_callback_cannot_publish(self):
        self.assertEqual(sf_notify.authorization_status(), "unknown")
        old_callback = self.center.callbacks[0]
        self.clock_value += sf_notify._AUTH_QUERY_TTL_SEC  # discipline-ok(R5): private TTL is the contract boundary under the injected monotonic clock  # query policy boundary, no real sleep
        self.assertEqual(sf_notify.authorization_status(), "unknown")
        self.assertEqual(len(self.center.callbacks), 2)
        old_callback(_SettingsStub(2))
        self.assertEqual(sf_notify.authorization_status(), "unknown")
        self.assertEqual(len(self.center.callbacks), 2, "旧回调不能释放新查询")
        self.center.callbacks[1](_SettingsStub(1))
        old_callback(_SettingsStub(2))
        self.assertEqual(sf_notify.authorization_status(), "denied")

    def test_callback_after_lease_without_new_query_is_ignored(self):
        sf_notify.authorization_status()
        self.clock_value += sf_notify._AUTH_QUERY_TTL_SEC  # discipline-ok(R5): private TTL is the contract boundary under the injected monotonic clock
        self.center.callbacks[0](_SettingsStub(2))
        self.assertEqual(sf_notify.authorization_status(), "unknown")
        self.assertEqual(len(self.center.callbacks), 2)

    def test_expired_authorized_is_unknown_but_denied_stays_fail_closed(self):
        for raw, expected in ((2, "unknown"), (1, "denied")):
            with self.subTest(raw=raw):
                sf_notify.reset_delivery_probe()
                sf_notify.authorization_status()
                self.center.callbacks[-1](_SettingsStub(raw))
                self.clock_value += sf_notify._AUTH_CACHE_TTL_SEC  # discipline-ok(R5): private TTL is the contract boundary under the injected monotonic clock
                self.assertEqual(sf_notify.authorization_status(), expected)
                with mock.patch.object(sf_notify, "_deliver_osascript",
                                       return_value="osascript") as fallback:
                    result = sf_notify.deliver("T", "", "B")
                    self.assertEqual(result, "pending" if raw == 2 else "denied")
                    fallback.assert_not_called()

    def test_fresh_negative_cache_refreshes_after_authorization_completion(self):
        for raw in (0, 1):
            with self.subTest(raw=raw):
                sf_notify.reset_delivery_probe()
                sf_notify.authorization_status()
                old_callback = self.center.callbacks[-1]
                old_callback(_SettingsStub(raw))
                before = len(self.center.callbacks)
                with mock.patch.object(sf_notify, "_install_delegate"):
                    sf_notify.request_authorization()
                    self.center.auth_callback(True, None)
                self.assertEqual(len(self.center.callbacks), before + 1)
                old_callback(_SettingsStub(raw))
                self.center.callbacks[-1](_SettingsStub(2))
                self.assertEqual(sf_notify.authorization_status(), "authorized")

    def test_old_synchronous_failure_cannot_release_new_query(self):
        def start_new_then_fail(callback):
            self.center.callbacks.append(callback)
            # Reentrant refresh deterministically models a concurrent new generation.
            self.center.getNotificationSettingsWithCompletionHandler_ = self.center.callbacks.append
            sf_notify._refresh_authorization_status()  # discipline-ok(R5): re-enter the private refresh to model a concurrent generation

            raise RuntimeError("old query failed")

        self.center.getNotificationSettingsWithCompletionHandler_ = start_new_then_fail
        self.assertEqual(sf_notify.authorization_status(), "unknown")
        self.assertEqual(sf_notify.authorization_status(), "unknown")
        self.assertEqual(len(self.center.callbacks), 2)
        self.center.callbacks[0](_SettingsStub(1))
        self.center.callbacks[1](_SettingsStub(2))
        self.assertEqual(sf_notify.authorization_status(), "authorized")

    def test_center_exception_is_logged_and_does_not_escape(self):
        with mock.patch.object(sf_notify, "_un_center", side_effect=RuntimeError("center")), \
             mock.patch.object(sf_notify, "log_line") as log:
            self.assertEqual(sf_notify.authorization_status(), "unknown")
        log.assert_called_once()
        self.assertEqual(sf_notify.authorization_status(), "unknown")
        self.assertEqual(len(self.center.callbacks), 1)

    def test_known_denied_survives_center_or_settings_failure(self):
        sf_notify.authorization_status()
        self.center.callbacks[-1](_SettingsStub(1))
        self.clock_value += sf_notify._AUTH_CACHE_TTL_SEC  # discipline-ok(R5): private TTL is the contract boundary under the injected monotonic clock
        with mock.patch.object(sf_notify, "_un_center", side_effect=RuntimeError("center")):
            self.assertEqual(sf_notify.authorization_status(), "denied")
        self.assertEqual(sf_notify.authorization_status(), "denied")
        self.center.callbacks[-1](None)
        with mock.patch.object(sf_notify, "_deliver_osascript") as fallback:
            self.assertEqual(sf_notify.deliver("T", "", "B"), "denied")
            fallback.assert_not_called()

    def test_synchronous_query_failure_allows_retry(self):
        self.center.getNotificationSettingsWithCompletionHandler_ = mock.Mock(
            side_effect=RuntimeError("settings unavailable"))
        self.assertEqual(sf_notify.authorization_status(), "unknown")
        self.assertEqual(sf_notify.authorization_status(), "unknown")
        self.assertEqual(self.center.getNotificationSettingsWithCompletionHandler_.call_count, 2)

    def test_missing_center_is_fail_closed_without_starting_query(self):
        with mock.patch.object(sf_notify, "_un_center", return_value=None):
            self.assertEqual(sf_notify.authorization_status(), "denied")
        self.assertEqual(self.center.callbacks, [])

    def test_authorization_race_denial_wins_before_pending_enqueue(self):
        def status_then_refuse():
            with sf_notify._DELIVERY_LOCK:  # discipline-ok(R5): model the authorization answer racing with the delivery snapshot
                sf_notify._AUTH_STATUS_CACHE = "denied"  # discipline-ok(R5): seed the cache at the lock boundary under test
            return "unknown"

        with mock.patch.object(sf_notify, "authorization_status",
                               side_effect=status_then_refuse):
            self.assertEqual(sf_notify._deliver_impl("T", "", "B", "race"), "denied")  # discipline-ok(R5): exercise the lock-protected authorization race
        self.assertNotIn("race", sf_notify._PENDING)  # discipline-ok(R5): no public pending-key reader exposes this race outcome

    def test_pending_sending_duplicate_is_coalesced(self):
        with sf_notify._DELIVERY_LOCK:  # discipline-ok(R5): inspect the in-flight claim set because no public duplicate state is exposed
            sf_notify._PENDING_SENDING.add("in-flight")  # discipline-ok(R5): seed the claimed key at the concurrency boundary under test
        self.addCleanup(lambda: sf_notify._PENDING_SENDING.discard("in-flight"))  # discipline-ok(R5): clean the claimed-key fixture because no public reset hook exists
        self.assertEqual(sf_notify._deliver_impl("T", "", "B", "in-flight"), "pending")  # discipline-ok(R5): inspect the in-flight set because no public duplicate state is exposed
        self.assertEqual(sf_notify.delivery_stats()["pending"]["counts"]["deduplicated"], 1)

    def test_flush_pauses_when_authorization_expires_and_resumes_after_query(self):
        self._pending_fixture()
        with mock.patch.object(sf_notify, "_deliver_osascript") as fallback:
            self.assertEqual(sf_notify.deliver("T", "S", "B", "first"), "pending")
            self.assertEqual(sf_notify.deliver("T2", "", "B2", "second"), "pending")
            self.center.callbacks[-1](_SettingsStub(2))
            self.clock_value += sf_notify._AUTH_CACHE_TTL_SEC  # discipline-ok(R5): advance the real cache policy boundary to force reauthorization during flush
            self.main_jobs.pop()()
            stats = sf_notify.delivery_stats()["pending"]
            self.assertEqual(stats["size"], 2)
            self.assertEqual(stats["counts"].get("flushed", 0), 0)
            self.assertEqual(self.sent, [])
            self.assertEqual(self.main_jobs, [], "pending result must stop rather than spin")
            self.center.callbacks[-1](_SettingsStub(2))
            self.assertEqual(len(self.main_jobs), 1, "paused flush must release its lease")
            self.main_jobs.pop()()
            self.assertEqual(len(self.sent), 2)
            self.assertEqual(sf_notify.delivery_stats()["pending"]["size"], 0)
            fallback.assert_not_called()

    def test_denied_callback_between_snapshot_and_enqueue_prevents_pending(self):
        self._pending_fixture()
        self.assertEqual(sf_notify.authorization_status(), "unknown")
        callback = self.center.callbacks[-1]
        real_lock = sf_notify._DELIVERY_LOCK  # discipline-ok(R5): wrap the real lock to inject a settings callback at the enqueue boundary, without timing guesses
        state = {"armed": False, "entries": 0}

        class CallbackAtEnqueueLock:
            def __enter__(self):
                if state["armed"]:
                    state["entries"] += 1
                    if state["entries"] == 2:
                        state["armed"] = False
                        callback(_SettingsStub(1))
                return real_lock.__enter__()

            def __exit__(self, *args):
                return real_lock.__exit__(*args)

        def snapshot_unknown():
            state["armed"] = True
            return "unknown"

        with mock.patch.object(sf_notify, "_DELIVERY_LOCK", CallbackAtEnqueueLock()), \
             mock.patch.object(sf_notify, "authorization_status", side_effect=snapshot_unknown), \
             mock.patch.object(sf_notify, "_deliver_osascript") as fallback:
            self.assertEqual(sf_notify.deliver("T", "", "B", "race"), "denied")
        self.assertEqual(state["entries"], 2)
        self.assertEqual(sf_notify.delivery_stats()["pending"]["size"], 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.main_jobs, [])
        fallback.assert_not_called()

    def test_completion_error_from_old_epoch_cannot_degrade_new_delivery(self):
        self._pending_fixture()
        with mock.patch.object(sf_notify, "_deliver_osascript") as fallback:
            self.assertEqual(sf_notify.deliver("T", "", "B", "old"), "pending")
            self.center.callbacks[-1](_SettingsStub(2))
            self.main_jobs.pop()()
            old_completion = self.sent[-1][1]
            sf_notify.reset_delivery_probe()
            old_completion(RuntimeError("stale delivery failure"))
            self.assertFalse(sf_notify.delivery_stats()["degraded_osascript"])
            self.assertEqual(sf_notify.deliver("new", "", "body", "new"), "pending")
            self.center.callbacks[-1](_SettingsStub(2))
            self.main_jobs.pop()()
            self.assertEqual(len(self.sent), 2)
            fallback.assert_not_called()

    def test_denied_completion_error_never_falls_back_or_degrades(self):
        self._pending_fixture()
        with mock.patch.object(sf_notify, "_deliver_osascript") as fallback:
            sf_notify.deliver("T", "", "B", "hit")
            self.center.callbacks[-1](_SettingsStub(2))
            self.main_jobs.pop()()
            completion = self.sent[-1][1]
            self.clock_value += sf_notify._AUTH_CACHE_TTL_SEC  # discipline-ok(R5): expire authorization so a real settings callback can publish denial
            self.assertEqual(sf_notify.authorization_status(), "unknown")
            self.center.callbacks[-1](_SettingsStub(1))
            completion(RuntimeError("delivery failed after denial"))
            self.assertFalse(sf_notify.delivery_stats()["degraded_osascript"])
            self.assertEqual(sf_notify.deliver("T", "", "B", "later"), "denied")
            fallback.assert_not_called()

    def test_un_creation_or_submission_exception_falls_back_unless_denied(self):
        self._pending_fixture()
        for stage in ("creation", "submission"):
            for denied in (False, True):
                with self.subTest(stage=stage, denied=denied):
                    sf_notify.reset_delivery_probe()
                    sf_notify.authorization_status()
                    self.center.callbacks[-1](_SettingsStub(2))
                    click = mock.Mock()

                    def fail(*args):
                        if denied:
                            self.clock_value += sf_notify._AUTH_CACHE_TTL_SEC  # discipline-ok(R5): model a real denial callback arriving inside the failing ObjC call
                            sf_notify.authorization_status()
                            self.center.callbacks[-1](_SettingsStub(1))
                        raise RuntimeError(stage + " failed")

                    target, method = ((sf_notify.UNMutableNotificationContent, "alloc")
                                      if stage == "creation" else
                                      (self.center, "addNotificationRequest_withCompletionHandler_"))
                    # A delegate sentinel lets submission register the real click
                    # handler; failure must remove it rather than leave a dead route.
                    with mock.patch.object(sf_notify, "_DELEGATE", object()), \
                         mock.patch.object(target, method, side_effect=fail), \
                         mock.patch.object(sf_notify, "_deliver_osascript",
                                           return_value="osascript") as fallback, \
                         mock.patch.object(sf_notify, "log_line") as log:
                        result = sf_notify.deliver("T", "S", "B", "failed", click)
                    self.assertEqual(result, "denied" if denied else "osascript")
                    self.assertEqual(sf_notify.delivery_stats()["degraded_osascript"],
                                     not denied)
                    self.assertNotIn("failed", sf_notify._CLICK_HANDLERS)  # discipline-ok(R5): no public click-registration reader exists; failure must remove the registered callback
                    click.assert_not_called()
                    log.assert_called_once_with(
                        "notify deliver error: RuntimeError('%s failed'); fallback osascript" % stage,
                        "WARNING")
                    if denied:
                        fallback.assert_not_called()
                    else:
                        fallback.assert_called_once_with("T", "S", "B")

    def test_lost_flush_records_error_and_releases_schedule(self):
        self._pending_fixture()
        errors_before = sf_notify.delivery_stats()["counts"]["error"]
        self.assertEqual(sf_notify.deliver("T", "", "B", "flush-error"), "pending")
        self.center.callbacks[-1](_SettingsStub(2))
        self.assertEqual(len(self.main_jobs), 1)
        with mock.patch.object(sf_notify, "_deliver_impl",
                               side_effect=RuntimeError("flush failed")), \
             mock.patch.object(sf_notify, "log_line") as log:
            self.main_jobs.pop()()
        self.assertEqual(sf_notify.delivery_stats()["counts"]["error"], errors_before + 1)
        self.assertFalse(sf_notify._PENDING_FLUSH_SCHEDULED)  # discipline-ok(R5): no public flush-state reader exposes release of the scheduled lease
        log.assert_called_once_with(
            "notify pending flush error: RuntimeError('flush failed')", "WARNING")
        self.assertEqual(sf_notify.delivery_stats()["pending"]["size"], 0)
        self.assertEqual(sf_notify.deliver("retry", "", "B", "flush-error"), "un",
                         "failed flush must release the sending claim for the same key")
        self.assertEqual(len(self.sent), 1)


class TestAuthorizationGuard(SFCase):
    def setUp(self):
        super().setUp()

    def test_real_center_is_not_touched_without_bundle_identity(self):
        real_center = mock.Mock()
        with mock.patch.object(sf_notify, "UNUserNotificationCenter", real_center), \
             mock.patch.object(sf_notify, "_UN_REAL_CLASS", real_center), \
             mock.patch.object(sf_notify, "has_bundle_identity", return_value=False):
            self.assertIsNone(sf_notify._un_center())
            real_center.currentNotificationCenter.assert_not_called()


class TestUnavailableDegradation(SFCase):
    """UN 不可用时：视作 denied，走 osascript，不得抛异常。"""

    def setUp(self):
        super().setUp()


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
