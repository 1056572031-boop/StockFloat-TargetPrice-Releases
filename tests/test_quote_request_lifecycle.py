"""真实行情/分时请求生命周期：切股时迟到结果只能被真实消费链丢弃。"""
import queue
import threading
from contextlib import ExitStack
from unittest import mock

import sf_config
import sf_quote
import stock_float_window as window
from tests.test_minute_recovery_latency import _Host


class QuoteRequestLifecycle(_Host):
    """使用真 FloatWindow、真生产队列和 _poll；只替换网络出口。"""

    def setUp(self):
        # _Host 在 super().setUp 内构窗，故安全桩必须先于它安装。
        self.construction_guards = ExitStack()
        self.addCleanup(self.construction_guards.close)
        for target, name, options in (
                (window, "load_alerts", {"return_value": []}),
                (sf_config, "load_alerts", {"return_value": []}),
                (window.FloatWindow, "_init_targets", {}),
                (window.FloatWindow, "_request_notify_auth", {}),
                (window, "_notify_deliver", {})):
            self.construction_guards.enter_context(mock.patch.object(target, name, **options))
        super().setUp()

    def test_late_quote_from_old_stock_is_dropped_by_fetch_queue_and_poll(self):
        started = threading.Event()
        release = threading.Event()
        returned = {"code": "sh600519", "price": "12.00", "pct": "0",
                    "high": "12.00", "low": "12.00", "pre_close": "12.00"}

        def fetch(code):
            self.assertEqual(code, "sh600519")
            started.set()
            self.assertTrue(release.wait(2), "替身请求未等到切股后的放行信号")
            return returned

        self.win.code = "sh600519"
        self.win.running = True
        self.track_patch(mock.patch.object(window, "fetch_quote", side_effect=fetch))
        worker = threading.Thread(target=self.win._fetch_quote, daemon=True)  # discipline-ok(R5): 直接驱动真实行情生产入口，复现切股前发出、切股后返回的请求。
        worker.start()
        self.assertTrue(started.wait(2), "行情替身没有真正进入请求阶段")
        self.win.code = "sz000001"
        release.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "行情请求线程未结束")

        applied = []
        self.track_patch(mock.patch.object(self.win, "_apply_quote",
                                           side_effect=lambda quote: applied.append(quote)))
        self.win._poll()  # discipline-ok(R5): 必须由真实队列消费入口验证迟到行情被丢弃，而非只检查生产侧。
        self.assertEqual(applied, [], "切股后的 _poll 不得应用旧股迟到行情")
        with self.assertRaises(queue.Empty):
            self.win.q.get_nowait()

    def test_old_stock_minute_exception_does_not_mark_new_stock_stale(self):
        started = threading.Event()
        release = threading.Event()
        self.win.code = "sh600519"
        self.win.running = True
        self.win._minute_fail = 4  # discipline-ok(R5): 预置非零失败计数，以区分旧股异常是否错误重置或递增新股计数。

        def fetch(_code):
            started.set()
            self.assertTrue(release.wait(2), "分时替身未等到切股后的放行信号")
            raise OSError("old stock request failed")

        self.track_patch(mock.patch.object(window, "fetch_minute", side_effect=fetch))
        worker = threading.Thread(target=self.win._fetch_minute, daemon=True)  # discipline-ok(R5): 驱动真实分时请求入口，验证切股期间旧请求异常的状态隔离。
        worker.start()
        self.assertTrue(started.wait(2), "分时替身没有真正进入请求阶段")
        self.win.code = "sz000001"
        release.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "分时请求线程未结束")

        self.assertFalse(getattr(self.win, "_minute_stale", False),
                         "旧股异常不应给新股写入滞后状态")
        self.assertEqual(self.win._minute_fail, 4,  # discipline-ok(R5): 失败计数没有公开输出，须直接确认旧股异常未改变新股的既有计数。
                         "旧股异常不应重置或递增新股的失败计数")
        with self.assertRaises(queue.Empty):
            self.win.q.get_nowait()


if __name__ == "__main__":
    import unittest
    unittest.main()
