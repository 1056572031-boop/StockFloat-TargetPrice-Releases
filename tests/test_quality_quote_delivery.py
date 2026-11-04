"""Received crossings survive frame coalescing, queue overflow and stale data."""
import datetime
import os
import queue
import tempfile
from types import SimpleNamespace
from unittest import mock

import sf_quote
from sf_quote_delivery import TargetTickBuffer
import stock_float_window as window
import targets
from tests._base import SFCase
from tests.test_window_poll_message_routing import _Host

NOW = datetime.datetime(2026, 10, 14, 10, 0,
                        tzinfo=datetime.timezone(datetime.timedelta(hours=8)))


class QuoteDeliveryTests(SFCase):
    """共享底座：把跨进程锁与租约登记文件都关进本用例的临时目录。

    `TargetTickBuffer.take()` 与发布租约都按 `sf_config.CONFIG_PATH` 取锁目录，
    默认值指向真实用户支持目录；不隔离就会把测试锁文件写进用户机器。
    """

    def setUp(self):
        import sf_config
        super().setUp()
        directory = tempfile.TemporaryDirectory(prefix="sf_quality_delivery_")
        self.addCleanup(directory.cleanup)
        self.tmp_path = directory.name
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH",
                                           os.path.join(self.tmp_path, "config.json")))

    def _revision(self, *conditions):
        import json
        return ("rev", 1, tuple(json.dumps(item, ensure_ascii=False) for item in conditions))

    def _alternating_extremes(self, count, start=100.0):
        """交替造出新最低/最高价，使按极值压缩时一条都不会被合掉。"""
        low = high = start
        prices = []
        for index in range(count):
            if index % 2:
                low -= 1.0
                prices.append(low)
            else:
                high += 1.0
                prices.append(high)
        return prices

    def _fill_ordered(self, ticks, code, revision, count=4096):
        """用恢复入口把缓冲一次性铺到指定条数（逐笔入口会按极值合批，条数不可控）。"""
        records = [self._record(index, price, revision)
                   for index, price in enumerate(self._alternating_extremes(count), start=1)]
        ticks.restore_ordered(code, records)

    def test_ordered_tick_is_refused_when_volatile_queue_is_full(self):
        from sf_target_tick_store import CapacityError
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "full.sqlite"))
        for index in range(4096):
            quote = sf_quote.QuoteData({"price": str(10 + index)}, "20261014100000")
            self.assertTrue(ticks.retain_unsaved("A", quote))
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            with self.assertRaises(CapacityError):
                ticks.add_ordered("A", sf_quote.QuoteData({"price": "9"}, "20261014100001"))
        self.assertEqual(ticks.unsaved_count(), 4096)

    def test_retained_plain_quote_is_replayed_after_durable_recovery(self):
        plain = {"price": "101"}
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "plain.sqlite"))
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            self.assertTrue(ticks.retain_unsaved("A", plain))
            self.assertEqual(ticks.unsaved_count(), 1)
            restored = ticks.take("A")
        self.assertEqual([quote["price"] for quote in restored], ["101"])
        self.assertIs(restored[0]._plain_original, plain)  # discipline-ok(R5): 原始字典身份只能由此内部标记承载
        self.assertEqual(ticks.unsaved_count(), 0)

    def test_release_failure_keeps_tick_durable(self):
        """发布登记的清理失败不能把这条行情一起带走：盘上还得留着，由清理方逐笔消费。"""
        import sf_config
        exits = []
        real_close = sf_config.TickPublisherToken.close

        def failing_close(token):
            exits.append(1)
            real_close(token)
            raise OSError("marker already gone")

        path = os.path.join(self.tmp_path, "lease.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            self.assertTrue(ticks.retain_unsaved("A", quote))
            self.assertEqual(ticks.unsaved_count(), 1)
            with mock.patch.object(sf_config.TickPublisherToken, "close", failing_close):
                taken = ticks.take("A")
        self.assertEqual([item["price"] for item in taken], ["101"])
        self.assertEqual(ticks.unsaved_count(), 0)
        self.assertEqual(exits, [1])
        # 判据必须换一个缓冲对象重新读盘：只看内存里那条还在，证明不了它落过盘。
        oldest = TargetTickBuffer(persistent_path=path).oldest_persistent()
        self.assertIsNotNone(oldest)
        self.assertEqual(oldest[0], "A")
        self.assertEqual(oldest[1]["price"], "101")

    def test_async_recovery_survives_a_failing_wakeup_callback(self):
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "wake.sqlite"),
                                 async_recovery=True,
                                 on_recovery=mock.Mock(side_effect=RuntimeError("wakeup failed")))
        quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            self.assertTrue(ticks.retain_unsaved("A", quote))
            with self.assertRaises(BlockingIOError):
                ticks.take("A")
            self.assertTrue(ticks.wait_recovery(4))
            restored = ticks.take("A")
        self.assertEqual([item["price"] for item in restored], ["101"])
        self.assertEqual(ticks.unsaved_count(), 0)

    def test_diagnostics_of_a_buffer_without_a_store_says_so(self):
        self.assertEqual(TargetTickBuffer().diagnostics(), {"persistent": False})

    def test_diagnostics_never_waits_for_a_busy_buffer(self):
        import threading
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "diag-busy.sqlite"))
        held, release = threading.Event(), threading.Event()

        def hold():
            with ticks._lock:  # discipline-ok(R5): 用真实的缓冲区锁制造"生产者正占着"。
                held.set()
                release.wait(5)

        worker = threading.Thread(target=hold, daemon=True)
        worker.start()
        self.assertTrue(held.wait(2))
        try:
            self.assertEqual(ticks.diagnostics(), {"persistent": True, "busy": True})
        finally:
            release.set()
            worker.join(2)

    def test_diagnostics_caps_the_stock_list_and_counts_the_rest(self):
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "diag-many.sqlite"))
        for index in range(23):
            code = "sh6%05d" % index
            ticks.retain_unsaved(code, sf_quote.QuoteData({"price": "10"}, "20261014100000"))
        body = ticks.diagnostics()
        self.assertEqual(body["unsaved"], 23)
        self.assertEqual(len(body["unsaved_codes"]), 20)
        self.assertEqual(len(body["held_markers"]), 20)
        self.assertEqual(body["truncated"], 3)
        self.assertFalse(body["recovery_error"])

    def test_oldest_persistent_without_a_store_has_no_tick(self):
        self.assertIsNone(TargetTickBuffer().oldest_persistent())

    def test_busy_buffer_refuses_reads_instead_of_waiting(self):
        import threading
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "busy.sqlite"))
        held, release = threading.Event(), threading.Event()

        def hold():
            if ticks._lock.acquire(blocking=False):  # discipline-ok(R5): 竞争方只能抢同一把缓冲锁
                try:
                    held.set()
                    release.wait(5)
                finally:
                    ticks._lock.release()  # discipline-ok(R5): 同上，收尾必须归还这把锁

        worker = threading.Thread(target=hold, daemon=True)
        worker.start()
        self.addCleanup(release.set)
        self.addCleanup(worker.join, 5)
        self.assertTrue(held.wait(2), "竞争线程应抢到锁")
        with self.assertRaises(BlockingIOError):
            ticks.oldest_persistent()
        self.assertIsNone(ticks.unsaved_count())
        release.set()
        worker.join(5)
        self.assertEqual(ticks.unsaved_count(), 0)

    def test_acknowledge_ignores_records_from_another_store(self):
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "foreign.sqlite"))
        untracked = sf_quote.QuoteData({"price": "101"}, "20261014100000")
        foreign = sf_quote.QuoteData({"price": "102"}, "20261014100000")
        foreign._target_store_record = (os.path.join(self.tmp_path, "other.sqlite"), "A", 1)  # discipline-ok(R5): 归属标记只有这一个内部字段
        ticks.acknowledge(untracked)
        ticks.acknowledge(foreign)
        self.assertTrue(hasattr(foreign, "_target_store_record"))

    def test_compaction_drops_ticks_that_hit_no_target(self):
        revision = self._revision({"direction": "above", "target": 105.0},
                                  {"direction": "below", "target": 95.0})
        ticks = TargetTickBuffer()
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            self._fill_ordered(ticks, "A", revision)
            last = self._quote_with(revision, 106.0)
            ticks.add_ordered("A", last)
            retained = ticks.take("A")
        self.assertTrue(retained, "命中目标的极值必须留下")
        self.assertLess(len(retained), 16, "两条目标都被认领后，中间值应全部合掉")
        self.assertIs(retained[-1], last, "最后一条必须留住，否则越线时刻会被抹平")

    def test_compaction_keeps_every_tick_when_structure_is_invalid(self):
        """目标结构读不懂时宁可不压缩：一条都不许丢，否则漏触发比多占内存更贵。"""
        revision = self._revision({"direction": "sideways", "target": 105.0})
        ticks = TargetTickBuffer()
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            self._fill_ordered(ticks, "A", revision)
            ticks.add_ordered("A", self._quote_with(revision, 106.0))
            self.assertEqual(len(ticks.take("A")), 4097)

    def _quote_with(self, revision, price):
        quote = sf_quote.QuoteData({"price": repr(price)}, "20261014100000")
        quote.target_revision = revision
        return quote

    def test_overflow_extremes_are_restored_then_parked(self):
        revision = self._revision({"direction": "sideways", "target": 105.0})
        ticks = TargetTickBuffer()
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            self._fill_ordered(ticks, "A", revision)
            ticks.pin("A", self._quote_with(revision, 50.0))
            overflow = self._quote_with(revision, 107.0)
            ticks.add_ordered("A", overflow)
            restored = ticks.restore_ordered("A", [self._record(5000, 108.0, revision)])
            parked = ticks.park("A")
        self.assertTrue(restored)
        self.assertTrue(any(item[2] is overflow for item in parked),
                        "第一条越界极值必须随停车一起带走")

    def _record(self, sequence, price, revision):
        return (sequence, price, self._quote_with(revision, price))

    def test_restore_ordered_keeps_data_when_target_structure_is_invalid(self):
        revision = self._revision({"direction": "sideways", "target": 105.0})
        ticks = TargetTickBuffer()
        records = [self._record(index, price, revision)
                   for index, price in enumerate(self._alternating_extremes(4097), start=1)]
        self.assertTrue(ticks.restore_ordered("A", records))
        first_page = ticks.take("A")
        self.assertEqual(len(first_page), 4096, "分页一次最多取 4096 条")
        self.assertEqual(len(ticks.take("A")), 1, "剩下的一条必须还在，不能当已投递")

    def test_global_order_reads_only_contiguous_stock_prefix(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                for code, price in (("A", "1"), ("B", "2"), ("A", "3")):
                    ticks.add_ordered(code, {"price": price})
            first = ticks.take("A", global_order=True)
            self.assertEqual([quote["price"] for quote in first], ["1"])
            ticks.acknowledge(first[0])
            self.assertEqual(ticks.take("A", global_order=True), [])
            old = ticks.take("B", global_order=True)
            ticks.acknowledge(old[0])
            self.assertEqual([quote["price"] for quote in ticks.take("A", global_order=True)], ["3"])

    def test_plain_retention_after_recovery_without_cached_object(self):
        import gc
        import os
        import tempfile
        from sf_target_tick_store import RetainedCapacityError
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"),
                                     persistent_max_entries=1)
            first = sf_quote.QuoteData({"price": "100"}, "20261014100000")
            second = {"price": "101"}
            with mock.patch("sf_quote_delivery.quote_is_current", new=lambda item: True):
                ticks.add_ordered("A", first)
                with self.assertRaises(RetainedCapacityError):
                    ticks.add_ordered("A", second)
                ticks.acknowledge(ticks.take("A")[0])
                ticks.take("A")
                gc.collect()
                restored = ticks.take("A")[0]
            ticks.acknowledge(restored)
            self.assertEqual(ticks.take("A"), [])

    def test_plain_capacity_retention_after_recovery_does_not_duplicate_tick(self):
        import os
        import tempfile
        from sf_target_tick_store import CapacityError
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"),
                                     persistent_max_entries=1)
            first = sf_quote.QuoteData({"price": "100"}, "20261014100000")
            second = {"price": "101"}
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("A", first)
                with self.assertRaises(CapacityError):
                    ticks.add_ordered("A", second)
                ticks.acknowledge(ticks.take("A")[0])
                restored = ticks.take("A")[0]
                ticks.retain_unsaved("A", second)
            ticks.acknowledge(restored)
            self.assertEqual(ticks.take("A"), [])

    def test_capacity_retention_after_recovery_does_not_duplicate_tick(self):
        import os
        import tempfile
        from sf_target_tick_store import CapacityError, TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ticks.sqlite")
            ticks = TargetTickBuffer(persistent_path=path, persistent_max_entries=1)
            first = sf_quote.QuoteData({"price": "100"}, "20261014100000")
            second = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("A", first)
                with self.assertRaises(CapacityError):
                    ticks.add_ordered("A", second)
                ticks.acknowledge(ticks.take("A")[0])
                self.assertEqual(ticks.take("A"), [second])
                ticks.retain_unsaved("A", second)
            ticks.acknowledge(second)
            self.assertEqual(ticks.take("A"), [])
            with TargetTickStore(path) as store:
                self.assertEqual(store.read("A"), [])

    def test_persistent_capacity_rejection_preserves_order_before_later_arrival(self):
        import os
        import tempfile
        from sf_target_tick_store import CapacityError
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"),
                                     persistent_max_entries=1)
            first = sf_quote.QuoteData({"price": "100"}, "20261014100000")
            second = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            third = sf_quote.QuoteData({"price": "102"}, "20261014100000")
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("A", first)
                with self.assertRaises(CapacityError):
                    ticks.add_ordered("A", second)
                ticks.acknowledge(ticks.take("A")[0])
                ticks.add_ordered("A", third)
                ticks.retain_unsaved("A", second)
            self.assertEqual([q["price"] for q in ticks.take("A")], ["101"])

    def test_persistent_capacity_fallback_can_store_plain_dict(self):
        import os
        import tempfile
        from sf_target_tick_store import CapacityError
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"),
                                     persistent_max_entries=1)
            first = sf_quote.QuoteData({"price": "100"}, "20261014100000")
            later = {"price": "101"}
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("A", first)
                with self.assertRaises(CapacityError):
                    ticks.add_ordered("A", later)
                ticks.retain_unsaved("A", later)
            ticks.acknowledge(ticks.take("A")[0])
            self.assertEqual([item["price"] for item in ticks.take("A")], ["101"])

    def test_persistent_pending_normalizes_equivalent_codes(self):
        import os
        import tempfile
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("600183", {"price": "101"})
            self.assertTrue(ticks.has_pending("sh600183"))
            with mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")), \
                    mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("600519", {"price": "102"})
            self.assertTrue(ticks.has_pending("sh600519"))

    def test_persistent_pending_checks_database_with_other_stock_unsaved(self):
        import os
        import tempfile
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            first = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            later = sf_quote.QuoteData({"price": "102"}, "20261014100000")
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("A", first)
                with mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")):
                    ticks.add_ordered("B", later)
            self.assertTrue(ticks.has_pending("A"))
            self.assertTrue(ticks.has_pending("B"))

    def test_persistent_queue_preserves_intermediate_crossing_order(self):
        import os
        import tempfile
        state = [{"direction": "above", "target": 100, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None},
                 {"direction": "below", "target": 90, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None}]
        fired = []
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                for price in (95, 101, 89, 110):
                    ticks.add_ordered("A", {"price": str(price)})
            for quote in ticks.take("A"):
                state, events = targets.evaluate_tick(state, float(quote["price"]), now=NOW)
                fired.extend((item["direction"], item["price"]) for item in events)
                ticks.acknowledge(quote)
            self.assertEqual(fired, [("above", 101.0), ("below", 89.0)])
            self.assertEqual(ticks.take("A"), [])

    def test_persistent_display_does_not_repeat_completed_plain_tick(self):
        import os
        import tempfile
        host = _Host()
        host.running = True
        host.q = queue.Queue(maxsize=2)
        received = []
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            for key, value in {"_minimized": False, "_target_ticks": ticks,
                               "_evaluate_received_quote": lambda q: received.append(q["price"]),
                               "_is_trading_time": lambda: True, "_icons_pending": lambda: False,
                               "_wake_ui": lambda: None}.items():
                setattr(host, key, value)
            quote = {"price": "101", "code": host.code}
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                window.FloatWindow._q_put(host, ("quote", (host.code, quote)))  # discipline-ok(R5): 实际接收。
            def apply(data):
                if id(data) not in host._target_display_skip:  # discipline-ok(R5): 模拟显示路径判断。
                    host._evaluate_received_quote(data)  # discipline-ok(R5): 统计重复判断。
            host._apply_quote = apply  # discipline-ok(R5): 注入显示回调。
            window.FloatWindow._poll(host)  # discipline-ok(R5): 消费持久队列及显示帧。
            self.assertEqual(received, ["101"])

    def test_persistent_display_does_not_overtake_unprocessed_tick(self):
        import os
        import tempfile
        host = _Host()
        host.q = queue.Queue(maxsize=2)
        host.running = True
        received = []
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            for key, value in {"_minimized": False, "_target_ticks": ticks,
                               "_evaluate_received_quote": lambda q: received.append(q["price"]),
                               "_is_trading_time": lambda: True, "_icons_pending": lambda: False,
                               "_wake_ui": lambda: None}.items():
                setattr(host, key, value)
            first = {"price": "90", "code": host.code}
            latest = {"price": "110", "code": host.code}
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                window.FloatWindow._q_put(host, ("quote", (host.code, first)))  # discipline-ok(R5): 实际入队。
                window.FloatWindow._q_put(host, ("quote", (host.code, latest)))  # discipline-ok(R5): 后续显示帧。
            def apply(quote):
                if id(quote) not in host._target_display_skip:  # discipline-ok(R5): 模拟显示路径的目标价入口。
                    host._evaluate_received_quote(quote)  # discipline-ok(R5): 检查是否越过队首。
            host._apply_quote = apply  # discipline-ok(R5): 注入显示回调以验证真实路由顺序。
            original_take = ticks.take
            with mock.patch.object(ticks, "take", side_effect=lambda code: original_take(code)[:1]):
                window.FloatWindow._poll(host)  # discipline-ok(R5): 模拟一页只消费第一笔。
            self.assertEqual(received, ["90"])

    def test_persistent_buffer_keeps_plain_dict_quote(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            quote = {"price": "101", "code": "sh600519"}
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("sh600519", quote)
            restored = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            batch = restored.take("sh600519")
            self.assertEqual([item["price"] for item in batch], ["101"])
            self.assertIsNone(batch[0].source_time)
            self.assertTrue(sf_quote.quote_is_current(batch[0]))
            restored.acknowledge(batch[0])
            self.assertEqual(restored.take("sh600519"), [])

    def test_persistent_buffer_saves_quote_before_memory_overflow(self):
        import os
        import tempfile
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ticks.sqlite")
            ticks = TargetTickBuffer(persistent_path=path)
            quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            quote.target_revision = ("epoch", "generation", ("structure",))
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("sh600519", quote)
            with TargetTickStore(path) as store:
                saved = store.read("sh600519")
            self.assertEqual(len(saved), 1)
            self.assertIsInstance(saved[0].quote, sf_quote.QuoteData)

    def test_persistent_buffer_loads_unacknowledged_quotes_after_reopen(self):
        import os
        import tempfile
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ticks.sqlite")
            quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            quote.target_revision = ("epoch", "generation", ("structure",))
            with TargetTickStore(path) as store:
                store.save("sh600519", quote)
            ticks = TargetTickBuffer(persistent_path=path)
            self.assertEqual(ticks.take("sh600519"), [quote])

    def test_persistent_buffer_acknowledges_only_after_consumer_ack(self):
        import os
        import tempfile
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ticks.sqlite")
            quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            ticks = TargetTickBuffer(persistent_path=path)
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("sh600519", quote)
            ticks.take("sh600519")
            with TargetTickStore(path) as store:
                self.assertEqual(len(store.read("sh600519")), 1)
            ticks.acknowledge(quote)
            with TargetTickStore(path) as store:
                self.assertEqual(store.read("sh600519"), [])

    def test_persistent_retry_does_not_insert_duplicate_tick(self):
        import os
        import tempfile
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ticks.sqlite")
            ticks = TargetTickBuffer(persistent_path=path)
            quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("sh600519", quote)
                retried = ticks.take("sh600519")[0]
                ticks.add_ordered("sh600519", retried)
            with TargetTickStore(path) as store:
                self.assertEqual(len(store.read("sh600519")), 1)

    def test_persistent_pin_retains_all_unacknowledged_quotes(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            quotes = [sf_quote.QuoteData({"price": str(price)}, "20261014100000")
                      for price in (101, 102)]
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                for quote in quotes:
                    ticks.add_ordered("sh600519", quote)
                batch = ticks.take("sh600519")
                retained = ticks.pin("sh600519", batch[0])
            self.assertEqual({id(item) for item in retained}, {id(item) for item in quotes})

    def test_persistent_park_and_restore_preserve_unacknowledged_quotes(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("sh600519", quote)
            parked = ticks.park("sh600519")
            ticks.restore_ordered("sh600519", parked)
            self.assertEqual(ticks.take("sh600519"), [quote])

    def test_persistent_buffer_capacity_rejects_without_losing_existing(self):
        import os
        import tempfile
        from sf_target_tick_store import CapacityError
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"),
                                     persistent_max_entries=1)
            first = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            later = sf_quote.QuoteData({"price": "102"}, "20261014100000")
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("sh600519", first)
                with self.assertRaises(CapacityError):
                    ticks.add_ordered("sh600519", later)
            self.assertEqual(ticks.take("sh600519"), [first])

    def test_sync_recovery_does_not_persist_when_target_lock_unavailable(self):
        from contextlib import contextmanager
        import sf_config
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ticks.sqlite")
            ticks = TargetTickBuffer(persistent_path=path)
            quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            ticks.retain_unsaved("sh600519", quote)
            @contextmanager
            def denied(**kwargs):
                raise PermissionError("target lock unavailable")
                yield
            with mock.patch.object(sf_config, "target_tick_transaction", denied):
                with self.assertRaises((PermissionError, BlockingIOError)):
                    ticks.take("sh600519")
            with TargetTickStore(path) as store:
                self.assertEqual(store.read("sh600519"), [])
            self.assertEqual(ticks.unsaved_count(), 1)

    def test_async_recovery_does_not_persist_when_target_lock_unavailable(self):
        from contextlib import contextmanager
        import sf_config
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ticks.sqlite")
            ticks = TargetTickBuffer(persistent_path=path, async_recovery=True)
            quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            quote.target_revision = None
            ticks.retain_unsaved("sh600519", quote)
            @contextmanager
            def denied(**kwargs):
                raise PermissionError("target lock unavailable")
                yield
            with mock.patch.object(sf_config, "target_tick_transaction", denied):
                with self.assertRaises(BlockingIOError):
                    ticks.take("sh600519")
                self.assertTrue(ticks.wait_recovery(4))
            with TargetTickStore(path) as store:
                self.assertEqual(store.read("sh600519"), [])
            self.assertEqual(ticks.unsaved_count(), 1)

    def test_sync_recovery_waits_for_real_cross_process_lock(self):
        import subprocess
        import sys
        import time
        import sf_config
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            config = os.path.join(directory, "config.json")
            path = os.path.join(directory, "ticks.sqlite")
            ticks = TargetTickBuffer(persistent_path=path)
            ticks.retain_unsaved("sh600519", sf_quote.QuoteData({"price": "101"}, "20261014100000"))
            child = ('import sys, sf_config; sf_config.CONFIG_PATH=sys.argv[1]; '
                     'lock=sf_config.target_tick_transaction(); lock.__enter__(); '
                     'print("locked", flush=True); sys.stdin.readline(); lock.__exit__(None,None,None)')
            with mock.patch.object(sf_config, "CONFIG_PATH", config):
                proc = subprocess.Popen([sys.executable, "-c", child, config],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(proc.stdout.readline().strip(), "locked")
                    started = time.monotonic()
                    with self.assertRaises(BlockingIOError):
                        ticks.take("sh600519")
                    self.assertLess(time.monotonic() - started, 1.0)
                    with TargetTickStore(path) as store:
                        self.assertEqual(store.read("sh600519"), [])
                    self.assertEqual(ticks.unsaved_count(), 1)
                finally:
                    proc.communicate(input="release\n", timeout=10)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(len(ticks.take("sh600519")), 1)
                self.assertEqual(ticks.unsaved_count(), 0)

    def test_async_recovery_waits_for_real_cross_process_lock(self):
        import subprocess
        import sys
        import sf_config
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            config = os.path.join(directory, "config.json")
            path = os.path.join(directory, "ticks.sqlite")
            ticks = TargetTickBuffer(persistent_path=path, async_recovery=True)
            quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            ticks.retain_unsaved("sh600519", quote)
            child = ('import sys, sf_config; sf_config.CONFIG_PATH=sys.argv[1]; '
                     'lock=sf_config.target_tick_transaction(); lock.__enter__(); '
                     'print("locked", flush=True); sys.stdin.readline(); lock.__exit__(None,None,None)')
            with mock.patch.object(sf_config, "CONFIG_PATH", config):
                proc = subprocess.Popen([sys.executable, "-c", child, config],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(proc.stdout.readline().strip(), "locked")
                    with self.assertRaises(BlockingIOError):
                        ticks.take("sh600519")
                    self.assertTrue(ticks.wait_recovery(4))
                    with TargetTickStore(path) as store:
                        self.assertEqual(store.read("sh600519"), [])
                    self.assertEqual(ticks.unsaved_count(), 1)
                finally:
                    proc.communicate(input="release\n", timeout=10)
                self.assertEqual(proc.returncode, 0)
                with self.assertRaises(BlockingIOError):
                    ticks.take("sh600519")
                with self.assertRaises(BlockingIOError):
                    ticks.take("sh600519")
                self.assertTrue(ticks.wait_recovery(4))
                with TargetTickStore(path) as store:
                    self.assertEqual(len(store.read("sh600519")), 1)
                self.assertEqual(ticks.unsaved_count(), 0)

    def test_async_recovery_reports_unexpected_serialization_error(self):
        import os
        import tempfile
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"),
                                     async_recovery=True)
            quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                    mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")):
                ticks.add_ordered("A", quote)
            with mock.patch.object(TargetTickStore, "save", side_effect=ValueError("invalid payload")):
                with self.assertRaises(BlockingIOError):
                    ticks.take("A")
                self.assertTrue(ticks.wait_recovery(4))
            with self.assertRaisesRegex(ValueError, "invalid payload"):
                ticks.take("A")
            self.assertEqual(ticks.unsaved_count(), 1)

    def test_async_recovery_thread_start_failure_preserves_ticks(self):
        import os
        import tempfile
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"),
                                     async_recovery=True)
            quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                    mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")):
                ticks.add_ordered("A", quote)
            with mock.patch("threading.Thread.start", side_effect=RuntimeError("cannot start")):
                with self.assertRaises(RuntimeError):
                    ticks.take("A")
            with self.assertRaises(BlockingIOError):
                ticks.take("A")
            self.assertTrue(ticks.wait_recovery(4))
            self.assertEqual(ticks.take("A"), [quote])

    def test_async_recovery_reports_capacity_blocked_by_other_stock(self):
        import os
        import tempfile
        from sf_target_tick_store import CapacityError
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"),
                                     persistent_max_entries=1, async_recovery=True)
            first = sf_quote.QuoteData({"price": "100"}, "20261014100000")
            later = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("A", first)
                with self.assertRaises(CapacityError):
                    ticks.add_ordered("B", later)
            with self.assertRaises(BlockingIOError):
                ticks.take("B")
            self.assertTrue(ticks.wait_recovery(4))
            with self.assertRaises(CapacityError):
                ticks.take("B")
            self.assertEqual(ticks.take("A"), [first])
            ticks.acknowledge(first)
            with self.assertRaises(BlockingIOError):
                ticks.take("B")
            self.assertTrue(ticks.wait_recovery(4))
            self.assertEqual(ticks.take("B"), [later])

    def test_async_recovery_preserves_order_across_batches(self):
        import os
        import tempfile
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"),
                                     async_recovery=True)
            quotes = [sf_quote.QuoteData({"price": str(price)}, "20261014100000")
                      for price in range(1, 131)]
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                    mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")):
                for quote in quotes:
                    ticks.add_ordered("A", quote)
            received = []
            for _ in range(8):
                try:
                    batch = ticks.take("A")
                except BlockingIOError:
                    self.assertTrue(ticks.wait_recovery(4))
                    continue
                received.extend(batch)
                for quote in batch:
                    ticks.acknowledge(quote)
                if len(received) == len(quotes):
                    break
            self.assertEqual(received, quotes)

    def test_async_recovery_drains_full_store_before_unsaved(self):
        import os
        import tempfile
        from sf_target_tick_store import CapacityError
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"),
                                     persistent_max_entries=1, async_recovery=True)
            first = sf_quote.QuoteData({"price": "100"}, "20261014100000")
            later = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("A", first)
                with self.assertRaises(CapacityError):
                    ticks.add_ordered("A", later)
            batch = ticks.take("A")
            self.assertEqual(batch, [first])
            ticks.acknowledge(first)
            try:
                ticks.take("A")
            except BlockingIOError:
                pass
            self.assertTrue(ticks.wait_recovery(4))
            self.assertEqual(ticks.take("A"), [later])

    def test_persistent_full_store_can_drain_before_unsaved_tick(self):
        import os
        import tempfile
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"),
                                     persistent_max_entries=1)
            first = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            later = sf_quote.QuoteData({"price": "102"}, "20261014100000")
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("sh600519", first)
                with mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")):
                    ticks.add_ordered("sh600519", later)
            self.assertEqual(ticks.take("sh600519"), [first])
            ticks.acknowledge(first)
            self.assertEqual(ticks.take("sh600519"), [later])

    def test_persistent_write_failure_allows_reading_saved_tick(self):
        import os
        import tempfile
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            first = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            later = sf_quote.QuoteData({"price": "102"}, "20261014100000")
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                ticks.add_ordered("sh600519", first)
                with mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")):
                    ticks.add_ordered("sh600519", later)
                    self.assertEqual(ticks.take("sh600519"), [first])
            ticks.acknowledge(first)
            self.assertEqual(ticks.take("sh600519"), [later])

    def test_persistent_buffer_bounds_object_identity_cache(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            with mock.patch("sf_quote_delivery.quote_is_current", new=lambda quote: True):
                for price in range(1, 4101):
                    ticks.add_ordered("sh600519", sf_quote.QuoteData({"price": str(price)}, "20261014100000"))
            self.assertLessEqual(len(ticks._persistent_objects), 4096)  # discipline-ok(R5): 防止持久队列把全部行情继续留在内存。

    def test_persistent_buffer_supports_producer_and_consumer_threads(self):
        import os
        import tempfile
        import threading
        errors = []
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            def produce():
                try:
                    ticks.add_ordered("sh600519", quote)
                except Exception as exc:
                    errors.append(exc)
            with mock.patch("sf_quote_delivery.quote_is_current", new=lambda item: True):
                worker = threading.Thread(target=produce)
                worker.start()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(ticks.take("sh600519"), [quote])
            ticks.acknowledge(quote)
            self.assertEqual(ticks.take("sh600519"), [])

    def test_persistent_consumer_does_not_do_blocking_recovery_write(self):
        import os
        import tempfile
        import threading
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ticks.sqlite")
            ticks = TargetTickBuffer(persistent_path=path, async_recovery=True)
            quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                    mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")):
                ticks.add_ordered("A", quote)
            started = threading.Event()
            release = threading.Event()
            finished = threading.Event()
            def blocked_save(store, code, value):
                started.set()
                release.wait(3)
                raise OSError("busy")
            def consume():
                try:
                    ticks.take("A")
                except (BlockingIOError, OSError):
                    pass
                finally:
                    finished.set()
            with mock.patch.object(TargetTickStore, "save", new=blocked_save):
                worker = threading.Thread(target=consume)
                worker.start()
                try:
                    self.assertTrue(finished.wait(0.5), "consumer performed blocking recovery write")
                    self.assertTrue(started.wait(2), "recovery worker was not started")
                finally:
                    release.set()
                    worker.join(4)
                self.assertTrue(ticks.wait_recovery(4))
            self.assertFalse(worker.is_alive())

    def test_persistent_consumer_does_not_wait_for_blocked_producer(self):
        import os
        import tempfile
        import threading
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            with TargetTickStore(ticks._persistent_path):  # discipline-ok(R5): 预建隔离数据库，排除建表争用。
                pass
            started = threading.Event()
            release = threading.Event()
            finished = threading.Event()
            errors = []
            def blocked_save(store, code, quote):
                started.set()
                if not release.wait(3):
                    raise TimeoutError("producer not released")
                raise OSError("simulated disk failure")
            def produce():
                try:
                    ticks.add_ordered("A", sf_quote.QuoteData({"price": "101"}, "20261014100000"))
                except Exception as exc:
                    errors.append(exc)
            def consume():
                try:
                    ticks.take("A")
                except BlockingIOError:
                    pass
                except Exception as exc:
                    errors.append(exc)
                finally:
                    finished.set()
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                    mock.patch.object(TargetTickStore, "save", new=blocked_save):
                producer = threading.Thread(target=produce)
                consumer = threading.Thread(target=consume)
                producer.start()
                try:
                    self.assertTrue(started.wait(2))
                    consumer.start()
                    self.assertTrue(finished.wait(0.5), "consumer waited for blocked producer")
                finally:
                    release.set()
                    producer.join(4)
                    if consumer.ident is not None:
                        consumer.join(4)
            self.assertFalse(producer.is_alive())
            self.assertFalse(consumer.is_alive())
            self.assertEqual(errors, [])

    def test_persistent_recovery_limits_writes_per_poll(self):
        import os
        import tempfile
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            quotes = [sf_quote.QuoteData({"price": str(price)}, "20261014100000")
                      for price in range(1, 201)]
            with mock.patch("sf_quote_delivery.quote_is_current", new=lambda item: True), \
                    mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")):
                for quote in quotes:
                    ticks.add_ordered("sh600519", quote)
            original = TargetTickStore.save
            calls = []
            def save(store, code, quote):
                calls.append(quote)
                return original(store, code, quote)
            with mock.patch.object(TargetTickStore, "save", new=save):
                first = ticks.take("sh600519")
            self.assertLessEqual(len(calls), 64)
            self.assertEqual(first, quotes[:len(first)])
            self.assertTrue(first)

    def test_persistent_write_failure_preserves_arrival_order(self):
        import os
        import tempfile
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ticks.sqlite")
            ticks = TargetTickBuffer(persistent_path=path)
            quotes = [sf_quote.QuoteData({"price": str(price)}, "20261014100000")
                      for price in (101, 90, 110)]
            with mock.patch("sf_quote_delivery.quote_is_current", new=lambda item: True), \
                    mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")):
                for quote in quotes:
                    ticks.add_ordered("sh600519", quote)
            self.assertEqual(ticks.take("sh600519"), quotes)
            with TargetTickStore(path) as store:
                self.assertEqual([item.quote["price"] for item in store.read("sh600519")],
                                 ["101", "90", "110"])

    def test_received_crossing_is_not_lost_when_only_final_price_is_drawn(self):
        host = _Host()
        host.running = True
        host.q = queue.Queue(maxsize=2)
        state = [{"direction": "above", "target": 100, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None}]
        fired = []

        def evaluate(quote):
            nonlocal state
            state, events = targets.evaluate_tick(state, float(quote["price"]), now=NOW)
            fired.extend(events)

        members = {"_minimized": False, "_target_ticks": TargetTickBuffer(),
                   "_evaluate_received_quote": evaluate, "_is_trading_time": lambda: True,
                   "_icons_pending": lambda: False, "_wake_ui": lambda: None}
        for key, value in members.items():
            setattr(host, key, value)
        for price in (99, 101, 99, 98, 99):
            window.FloatWindow._q_put(host, ("quote", (host.code, {"price": str(price)})))  # discipline-ok(R5): actual producer entry with deliberately overflowing display queue
        window.FloatWindow._poll(host)  # discipline-ok(R5): real queue consumer is the behavior under test
        self.assertEqual(len(fired), 1)
        self.assertEqual(host.args_of("_apply_quote"), [({"price": "99"},)])

    def test_crossings_follow_receipt_order_when_extremes_are_coalesced(self):
        host = _Host()
        host.running = True
        host.q = queue.Queue(maxsize=10)
        state = [{"direction": "above", "target": 100, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None},
                 {"direction": "below", "target": 95, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None}]
        fired = []

        def evaluate(quote):
            nonlocal state
            state, events = targets.evaluate_tick(state, float(quote["price"]), now=NOW)
            fired.extend(event["direction"] for event in events)

        for key, value in {"_minimized": False, "_target_ticks": TargetTickBuffer(),
                           "_evaluate_received_quote": evaluate, "_is_trading_time": lambda: True,
                           "_icons_pending": lambda: False, "_wake_ui": lambda: None}.items():
            setattr(host, key, value)
        for price in (101, 90, 110):
            window.FloatWindow._q_put(host, ("quote", (host.code, {"price": str(price)})))  # discipline-ok(R5): 模拟依序收到行情。
        window.FloatWindow._poll(host)  # discipline-ok(R5): 驱动完整轮询。
        self.assertEqual(fired, ["above", "below"])

    def test_overflow_preserves_first_crossing_before_opposite_extreme(self):
        host = _Host()
        host.running = True
        host.q = queue.Queue(maxsize=1)
        state = [{"direction": "above", "target": 100, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None},
                 {"direction": "below", "target": 95, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None}]
        fired = []

        def evaluate(quote):
            nonlocal state
            state, events = targets.evaluate_tick(state, float(quote["price"]), now=NOW)
            fired.extend(event["direction"] for event in events)

        for key, value in {"_minimized": False, "_target_ticks": TargetTickBuffer(),
                           "_evaluate_received_quote": evaluate, "_is_trading_time": lambda: True,
                           "_icons_pending": lambda: False, "_wake_ui": lambda: None}.items():
            setattr(host, key, value)
        for price in (101, 90, 110):
            window.FloatWindow._q_put(host, ("quote", (host.code, {"price": str(price)})))  # discipline-ok(R5): 真实入队并触发显示队列溢出。
        window.FloatWindow._poll(host)  # discipline-ok(R5): 驱动目标价和显示队列的实际消费。
        self.assertEqual(fired, ["above", "below"])

    def test_overflow_keeps_intermediate_first_crossing_order(self):
        host = _Host()
        host.running = True
        host.q = queue.Queue(maxsize=1)
        state = [{"direction": "above", "target": 102, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None},
                 {"direction": "below", "target": 95, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None}]
        fired = []

        def evaluate(quote):
            nonlocal state
            state, events = targets.evaluate_tick(state, float(quote["price"]), now=NOW)
            fired.extend(event["direction"] for event in events)

        for key, value in {"_minimized": False, "_target_ticks": TargetTickBuffer(),
                           "_evaluate_received_quote": evaluate, "_is_trading_time": lambda: True,
                           "_icons_pending": lambda: False, "_wake_ui": lambda: None}.items():
            setattr(host, key, value)
        for price in (101, 99, 103, 90, 110):
            window.FloatWindow._q_put(host, ("quote", (host.code, {"price": str(price)})))  # discipline-ok(R5): 模拟中间价越线后再出现极值。
        window.FloatWindow._poll(host)  # discipline-ok(R5): 驱动实际队列消费。
        self.assertEqual(fired, ["above", "below"])

    def test_full_target_buffer_does_not_lose_brief_crossing(self):
        host = _Host()
        host.running = True
        host.q = queue.Queue(maxsize=1)
        state = [{"direction": "above", "target": 100, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None}]
        fired = []

        def evaluate(quote):
            nonlocal state
            state, events = targets.evaluate_tick(state, float(quote["price"]), now=NOW)
            fired.extend(events)

        for key, value in {"_minimized": False, "_target_ticks": TargetTickBuffer(),
                           "_evaluate_received_quote": evaluate, "_is_trading_time": lambda: True,
                           "_icons_pending": lambda: False, "_wake_ui": lambda: None}.items():
            setattr(host, key, value)
        with mock.patch("sf_logging.log_line"):
            for price in [99] * 4096 + [101, 99]:
                window.FloatWindow._q_put(host, ("quote", (host.code, {"price": str(price)})))  # discipline-ok(R5): 实际入队并超过目标价与显示缓冲。
        window.FloatWindow._poll(host)  # discipline-ok(R5): 实际消费两个缓冲。
        self.assertEqual(len(fired), 1)

    def test_full_alternating_buffer_does_not_lose_brief_crossing(self):
        host = _Host()
        host.running = True
        host.q = queue.Queue(maxsize=1)
        state = [{"direction": "above", "target": 100, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None}]
        fired = []

        def evaluate(quote):
            nonlocal state
            state, events = targets.evaluate_tick(state, float(quote["price"]), now=NOW)
            fired.extend(events)

        for key, value in {"_minimized": False, "_target_ticks": TargetTickBuffer(),
                           "_evaluate_received_quote": evaluate, "_is_trading_time": lambda: True,
                           "_icons_pending": lambda: False, "_wake_ui": lambda: None}.items():
            setattr(host, key, value)
        with mock.patch("sf_logging.log_line"):
            for price in [98, 99] * 2048 + [101, 99]:
                window.FloatWindow._q_put(host, ("quote", (host.code, {"price": str(price)})))  # discipline-ok(R5): 模拟震荡行情填满缓冲后短暂越线。
        window.FloatWindow._poll(host)  # discipline-ok(R5): 实际消费行情。
        self.assertEqual(len(fired), 1)

    def test_full_buffer_preserves_revision_boundaries_during_compaction(self):
        ticks = TargetTickBuffer()
        earlier = sf_quote.QuoteData({"price": "99"}, "20261014100000")
        earlier.target_revision = ("old",)
        newer = sf_quote.QuoteData({"price": "99"}, "20261014100000")
        newer.target_revision = ("new",)
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            for _ in range(4095):
                ticks.add_ordered("sh600519", {"price": "98"})
            ticks.add_ordered("sh600519", earlier)
            ticks.add_ordered("sh600519", newer)
        retained = ticks.take("sh600519")
        self.assertTrue(any(item is earlier for item in retained))
        self.assertTrue(any(item is newer for item in retained))

    def test_full_buffer_compaction_matches_uncompressed_crossings(self):
        import random
        rng = random.Random(1729)
        prices = [rng.randrange(80, 121) for _ in range(4097)]
        initial = [{"direction": direction, "target": target, "session_date": "2026-10-14",
                    "state": "pending", "last_side": None}
                   for direction in ("above", "below") for target in (85, 95, 105, 115)]

        def events(sequence):
            state = initial
            result = []
            for price in sequence:
                state, fired = targets.evaluate_tick(state, price, now=NOW)
                result.extend((item["direction"], item["target"], item["price"]) for item in fired)
            return result

        ticks = TargetTickBuffer()
        for price in prices:
            ticks.add_ordered("sh600519", {"price": str(price)})
        self.assertEqual(events([float(q["price"]) for q in ticks.take("sh600519")]),
                         events(prices))

    def test_full_increasing_buffer_does_not_lose_brief_crossing(self):
        host = _Host()
        host.running = True
        host.q = queue.Queue(maxsize=1)
        state = [{"direction": "above", "target": 4097, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None}]
        fired = []

        def evaluate(quote):
            nonlocal state
            state, events = targets.evaluate_tick(state, float(quote["price"]), now=NOW)
            fired.extend(events)

        for key, value in {"_minimized": False, "_target_ticks": TargetTickBuffer(),
                           "_evaluate_received_quote": evaluate, "_is_trading_time": lambda: True,
                           "_icons_pending": lambda: False, "_wake_ui": lambda: None}.items():
            setattr(host, key, value)
        with mock.patch("sf_logging.log_line"):
            for price in list(range(1, 4098)) + [1]:
                window.FloatWindow._q_put(host, ("quote", (host.code, {"price": str(price)})))  # discipline-ok(R5): 完整接收和显示队列。
        window.FloatWindow._poll(host)  # discipline-ok(R5): 驱动实际消费。
        self.assertEqual(len(fired), 1)

    def test_full_buffer_preserves_first_crossing_using_revision_targets(self):
        import json
        ticks = TargetTickBuffer()
        structural = (json.dumps({"direction": "above", "target": 4097,
                                  "session_date": "2026-10-14"}, sort_keys=True),)
        revision = ("epoch", "generation", structural)
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch("sf_logging.log_line"):
            for price in range(1, 4098):
                quote = sf_quote.QuoteData({"price": str(price)}, "20261014100000")
                quote.target_revision = revision
                ticks.add_ordered("sh600519", quote)
        retained = ticks.take("sh600519")
        self.assertTrue(any(float(item["price"]) >= 4097 for item in retained))

    def test_full_increasing_buffer_keeps_overflow_crossing_order(self):
        host = _Host()
        host.running = True
        host.q = queue.Queue(maxsize=1)
        state = [{"direction": "above", "target": 4097, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None},
                 {"direction": "below", "target": 0.5, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None}]
        fired = []
        def evaluate(quote):
            nonlocal state
            state, events = targets.evaluate_tick(state, float(quote["price"]), now=NOW)
            fired.extend(item["direction"] for item in events)
        for key, value in {"_minimized": False, "_target_ticks": TargetTickBuffer(),
                           "_evaluate_received_quote": evaluate, "_is_trading_time": lambda: True,
                           "_icons_pending": lambda: False, "_wake_ui": lambda: None}.items():
            setattr(host, key, value)
        with mock.patch("sf_logging.log_line"):
            for price in list(range(1, 4098)) + [0.4, 5000, 1]:
                window.FloatWindow._q_put(host, ("quote", (host.code, {"price": str(price)})))  # discipline-ok(R5): 溢出后先涨破再跌破再刷新高。
        window.FloatWindow._poll(host)  # discipline-ok(R5): 验证真实消费顺序。
        self.assertEqual(fired, ["above", "below"])

    def test_overflow_intermediate_crossing_order(self):
        host = _Host()
        host.running = True
        host.q = queue.Queue(maxsize=1)
        state = [{"direction": "above", "target": 4097, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None},
                 {"direction": "below", "target": 0.5, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None}]
        fired = []
        def evaluate(quote):
            nonlocal state
            state, events = targets.evaluate_tick(state, float(quote["price"]), now=NOW)
            fired.extend(item["direction"] for item in events)
        for key, value in {"_minimized": False, "_target_ticks": TargetTickBuffer(),
                           "_evaluate_received_quote": evaluate, "_is_trading_time": lambda: True,
                           "_icons_pending": lambda: False, "_wake_ui": lambda: None}.items():
            setattr(host, key, value)
        with mock.patch("sf_logging.log_line"):
            for price in list(range(1, 4097)) + [4096.5, 4097, 0.4, 5000, 1]:
                window.FloatWindow._q_put(host, ("quote", (host.code, {"price": str(price)})))  # discipline-ok(R5): 溢出后的中间行情首次越线。
        window.FloatWindow._poll(host)  # discipline-ok(R5): 完整队列消费。
        self.assertEqual(fired, ["above", "below"])

    def test_polling_other_stock_preserves_overflow_first_tick(self):
        ticks = TargetTickBuffer()
        with mock.patch("sf_logging.log_line"):
            for price in range(1, 4097):
                ticks.add_ordered("B", {"price": str(price)})
            first = {"price": "4097"}
            ticks.add_ordered("B", first)
            ticks.add_ordered("B", {"price": "0.4"})
            ticks.add_ordered("B", {"price": "5000"})
        ticks.take("A")
        self.assertTrue(any(quote is first for quote in ticks.take("B")))

    def test_versioned_compaction_preserves_multiple_first_hit_prices(self):
        import json
        values = [("above", 100), ("above", 110), ("below", 90)]
        alerts = [{"direction": direction, "target": target, "session_date": "2026-10-14",
                   "state": "pending", "last_side": None} for direction, target in values]
        revision = ("epoch", "revision", tuple(json.dumps({"direction": direction, "target": target,
                                                           "session_date": "2026-10-14"}, sort_keys=True)
                                                for direction, target in values))
        prices = [95] * 4090 + [101, 89, 111, 95, 112, 95, 96]
        ticks = TargetTickBuffer()
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            for price in prices:
                quote = sf_quote.QuoteData({"price": str(price)}, "20261014100000")
                quote.target_revision = revision
                ticks.add_ordered("sh600519", quote)
        expected = []
        state = alerts
        for price in prices:
            state, fired = targets.evaluate_tick(state, price, now=NOW)
            expected.extend((item["direction"], item["target"], item["price"]) for item in fired)
        actual = []
        state = alerts
        for quote in ticks.take("sh600519"):
            state, fired = targets.evaluate_tick(state, float(quote["price"]), now=NOW)
            actual.extend((item["direction"], item["target"], item["price"]) for item in fired)
        self.assertEqual(actual, expected)

    def test_both_directions_and_original_extreme_order_are_preserved(self):
        ticks = TargetTickBuffer()
        for price in (100, 110, 90, 100):
            ticks.add("sh600519", {"price": str(price)})
        self.assertEqual([q["price"] for q in ticks.take("sh600519")], ["110", "90"])
        self.assertEqual(ticks.take("sh600519"), [])

    def test_retried_older_extreme_stays_before_newer_extreme(self):
        ticks = TargetTickBuffer()
        earlier = {"price": "90"}
        later = {"price": "110"}
        ticks.add("sh600519", earlier)
        original = ticks.take("sh600519")[0]
        ticks.add("sh600519", later)
        ticks.add("sh600519", original)
        self.assertEqual([q["price"] for q in ticks.take("sh600519")], ["90", "110"])

    def test_taken_tick_can_be_requeued_after_its_freshness_window(self):
        ticks = TargetTickBuffer()
        quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.add("sh600519", quote)
            original = ticks.take("sh600519")[0]
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=False):
            ticks.add("sh600519", original)
        self.assertEqual(ticks.take("sh600519"), [quote])

    def test_ordered_retry_preserves_quote_after_freshness_expires(self):
        ticks = TargetTickBuffer()
        quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.add_ordered("sh600519", quote)
            original = ticks.take("sh600519")[0]
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=False):
            ticks.add_ordered("sh600519", original)
        self.assertEqual(ticks.take("sh600519"), [quote])

    def test_restore_keeps_quote_arriving_during_switch(self):
        ticks = TargetTickBuffer()
        old = {"price": "90"}
        new = {"price": "110"}
        ticks.add("sh600519", old)
        parked = ticks.park("sh600519")
        ticks.pin("sh600519", new)
        ticks.restore("sh600519", parked)
        self.assertEqual([q["price"] for q in ticks.take("sh600519")], ["90", "110"])

    def test_restore_applies_pinned_queue_limit(self):
        ticks = TargetTickBuffer()
        parked = tuple((number, float(number), {"price": str(number)})
                       for number in range(128))
        ticks.restore("sh600519", parked)
        ticks.pin("sh600519", {"price": "999"})
        self.assertEqual(len(ticks.take("sh600519")), 128)

    def test_pin_rejects_invalid_price(self):
        ticks = TargetTickBuffer()
        self.assertEqual(ticks.pin("sh600519", {"price": "bad"}), ())
        self.assertEqual(ticks.take("sh600519"), [])

    def test_pin_full_preserves_earlier_ticks_and_warns(self):
        ticks = TargetTickBuffer()
        quotes = [{"price": str(number + 1)} for number in range(128)]
        for quote in quotes:
            ticks.pin("sh600519", quote)
        with mock.patch("sf_logging.log_line") as log:
            ticks.pin("sh600519", {"price": "999"})
        self.assertEqual(ticks.take("sh600519"), quotes)
        log.assert_called_once()

    def test_restore_merges_existing_failed_ticks_with_capacity_limit(self):
        ticks = TargetTickBuffer()
        old = {"price": "90"}
        ticks.pin("sh600519", old)
        parked = ticks.park("sh600519")
        newer = [{"price": str(number + 1)} for number in range(128)]
        for quote in newer:
            ticks.pin("sh600519", quote)
        with mock.patch("sf_logging.log_line") as log:
            ticks.restore("sh600519", parked)
        self.assertEqual(ticks.take("sh600519"), newer)
        log.assert_called_once()

    def test_pinned_restore_advances_sequence_for_new_arrivals(self):
        ticks = TargetTickBuffer()
        earlier = {"price": "100"}
        later = {"price": "101"}
        ticks.restore("sh600519", [(1, 100.0, earlier)])
        ticks.pin("sh600519", later)
        self.assertEqual(ticks.take("sh600519"), [earlier, later])

    def test_restore_deduplicates_same_record(self):
        ticks = TargetTickBuffer()
        quote = {"price": "100"}
        ticks.pin("sh600519", quote)
        parked = ticks.park("sh600519")
        ticks.restore("sh600519", parked)
        ticks.restore("sh600519", parked)
        self.assertEqual(ticks.take("sh600519"), [quote])

    def test_switch_restores_all_ordered_ticks_beyond_retry_limit(self):
        ticks = TargetTickBuffer()
        quotes = [{"price": str(number + 1)} for number in range(160)]
        for quote in quotes:
            ticks.add_ordered("sh600519", quote)
        parked = ticks.park("sh600519")
        ticks.restore_ordered("sh600519", parked)
        self.assertEqual(ticks.take("sh600519"), quotes)

    def test_restore_ordered_merges_new_arrivals_without_reordering(self):
        ticks = TargetTickBuffer()
        earlier = {"price": "100"}
        later = {"price": "110"}
        ticks.add_ordered("sh600519", earlier)
        parked = ticks.park("sh600519")
        ticks.add_ordered("sh600519", later)
        ticks.restore_ordered("sh600519", parked)
        self.assertEqual(ticks.take("sh600519"), [earlier, later])

    def test_ordered_restore_is_idempotent(self):
        ticks = TargetTickBuffer()
        first = {"price": "100"}
        later = {"price": "101"}
        ticks.add_ordered("sh600519", first)
        parked = ticks.park("sh600519")
        ticks.add_ordered("sh600519", later)
        ticks.restore_ordered("sh600519", parked)
        ticks.restore_ordered("sh600519", parked)
        result = ticks.take("sh600519")
        self.assertEqual(result, [first, later])

    def test_restored_records_advance_sequence_for_new_arrivals(self):
        ticks = TargetTickBuffer()
        earlier = {"price": "100"}
        later = {"price": "101"}
        ticks.restore_ordered("sh600519", [(1, 100.0, earlier)])
        ticks.add_ordered("sh600519", later)
        self.assertEqual(ticks.take("sh600519"), [earlier, later])

    def test_restore_overflow_records_gap_when_middle_ticks_cannot_be_retained(self):
        ticks = TargetTickBuffer()
        records = [(i, float(i), {"price": str(i)}) for i in range(1, 8196)]
        with mock.patch("sf_logging.log_line") as log:
            ticks.restore_ordered("sh600519", records)
        self.assertTrue(any("target tick gap" in str(call.args[0]) for call in log.call_args_list))

    def test_ordered_restore_pages_all_records_without_loss(self):
        ticks = TargetTickBuffer()
        quotes = [{"price": str(number + 1)} for number in range(4096)]
        for quote in quotes:
            ticks.add_ordered("sh600519", quote)
        parked = ticks.park("sh600519")
        later = [{"price": str(number + 1)} for number in range(100)]
        for quote in later:
            ticks.add_ordered("sh600519", quote)
        with mock.patch("sf_logging.log_line"):
            ticks.restore_ordered("sh600519", parked)
        first = ticks.take("sh600519")
        second = ticks.take("sh600519")
        self.assertLessEqual(len(first), 4096)
        self.assertEqual(first + second, quotes + later)

    def test_ordered_restore_is_bounded_when_new_quotes_arrive(self):
        ticks = TargetTickBuffer()
        for number in range(4096):
            ticks.add_ordered("sh600519", {"price": str(number + 1)})
        parked = ticks.park("sh600519")
        for number in range(100):
            ticks.add_ordered("sh600519", {"price": str(number + 1)})
        with mock.patch("sf_logging.log_line") as log:
            ticks.restore_ordered("sh600519", parked)
        self.assertLessEqual(len(ticks.take("sh600519")), 4096)
        log.assert_called_once()

    def test_ordered_rejects_invalid_and_stale_quotes(self):
        ticks = TargetTickBuffer()
        for quote in ({"price": "nan"}, {"price": "0"}, {"price": "bad"}, {}):
            ticks.add_ordered("sh600519", quote)
        stale = sf_quote.QuoteData({"price": "101"}, "20261014100000")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=False):
            ticks.add_ordered("sh600519", stale)
        self.assertEqual(ticks.take("sh600519"), [])

    def test_persistent_plain_dict_rechecks_currentness_after_wrapping(self):
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            with mock.patch("sf_quote_delivery.quote_is_current", side_effect=[True, False]):
                ticks.add_ordered("sh600519", {"price": "101"})
            self.assertEqual(ticks.take("sh600519"), [])

        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            stale = {"price": "101"}
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=False):
                ticks.add_ordered("sh600519", stale)
            self.assertEqual(ticks.take("sh600519"), [])

        ticks = TargetTickBuffer()
        for number in range(5):
            ticks.add_ordered(str(number), {"price": "100"})
        self.assertEqual(ticks.take("0"), [])
        self.assertEqual(ticks.take("4"), [{"price": "100"}])

    def test_ordered_eviction_clears_old_stock_overflow_state(self):
        ticks = TargetTickBuffer()
        with mock.patch("sf_logging.log_line"), mock.patch("sf_logging.log_swallow"):
            for code in range(6):
                for price in range(1, 4098):
                    ticks.add_ordered(str(code), {"price": str(price)})
        self.assertLessEqual(len(ticks._overflow_first), 4)  # discipline-ok(R5): 验证被淘汰股票的溢出资源不会无限累积。
        self.assertLessEqual(len(ticks._pending), 4)  # discipline-ok(R5): 验证辅助极值与股票队列同界。

    def test_restore_empty_records_leaves_existing_quotes(self):
        ticks = TargetTickBuffer()
        quote = {"price": "100"}
        ticks.add("sh600519", quote)
        ticks.restore("sh600519", ())
        self.assertEqual(ticks.take("sh600519"), [quote])

    def test_extreme_buffer_limits_late_stock_codes(self):
        ticks = TargetTickBuffer()
        for number in range(5):
            ticks.add(str(number), {"price": "100"})
        self.assertEqual(ticks.take("4"), [{"price": "100"}])
        self.assertEqual(ticks.take("0"), [])

    def test_memory_restore_display_does_not_overtake_second_page(self):
        host = _Host()
        host.running = True
        host.q = queue.Queue(maxsize=1)
        ticks = TargetTickBuffer()
        received = []
        for key, value in {"_minimized": False, "_target_ticks": ticks,
                           "_evaluate_received_quote": lambda q: received.append(q["price"]),
                           "_is_trading_time": lambda: True,
                           "_icons_pending": lambda: False, "_wake_ui": lambda: None}.items():
            setattr(host, key, value)
        def apply(quote):
            if id(quote) not in host._target_display_skip:  # discipline-ok(R5): 模拟真实显示入口。
                host._evaluate_received_quote(quote)  # discipline-ok(R5): 验证目标判断顺序。
        host._apply_quote = apply  # discipline-ok(R5): 使用可观察显示回调。
        with mock.patch("sf_logging.log_line"):
            for number in range(4096):
                ticks.add_ordered(host.code, {"price": str(number + 1)})
            parked = ticks.park(host.code)
            earlier = {"price": "4097"}
            latest = {"price": "5000"}
            ticks.add_ordered(host.code, earlier)
            ticks.add_ordered(host.code, latest)
            ticks.restore_ordered(host.code, parked)
        host.q.put_nowait(("quote", (host.code, latest)))
        window.FloatWindow._poll(host)  # discipline-ok(R5): 消费第一页及最新显示帧。
        self.assertNotIn("5000", received)
        window.FloatWindow._poll(host)  # discipline-ok(R5): 消费第二页。
        self.assertEqual(received[-2:], ["4097", "5000"])

    def test_restored_second_page_crossing_is_consumed_in_order(self):
        host = _Host()
        host.running = True
        host.q = queue.Queue(maxsize=1)
        ticks = TargetTickBuffer()
        state = [{"direction": "above", "target": 5000, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None}]
        fired = []
        def evaluate(quote):
            nonlocal state
            state, events = targets.evaluate_tick(state, float(quote["price"]), now=NOW)
            fired.extend(events)
        for key, value in {"_minimized": False, "_target_ticks": ticks,
                           "_evaluate_received_quote": evaluate, "_is_trading_time": lambda: True,
                           "_icons_pending": lambda: False, "_wake_ui": lambda: None}.items():
            setattr(host, key, value)
        with mock.patch("sf_logging.log_line"):
            for price in range(1, 4097):
                ticks.add_ordered(host.code, {"price": str(price)})
            parked = ticks.park(host.code)
            ticks.add_ordered(host.code, {"price": "5000"})
            ticks.restore_ordered(host.code, parked)
        window.FloatWindow._poll(host)  # discipline-ok(R5): 第一页保持接收顺序。
        self.assertEqual(fired, [])
        window.FloatWindow._poll(host)  # discipline-ok(R5): 后续定时轮询消费第二页。
        self.assertEqual(len(fired), 1)

    def test_restore_preserves_existing_overflow_crossing(self):
        ticks = TargetTickBuffer()
        with mock.patch("sf_logging.log_line"):
            for price in range(1, 4097):
                ticks.add_ordered("sh600519", {"price": str(price)})
            parked = ticks.park("sh600519")
            for price in range(1, 4097):
                ticks.add_ordered("sh600519", {"price": str(price)})
            crossing = {"price": "9999"}
            ticks.add_ordered("sh600519", crossing)
            ticks.restore_ordered("sh600519", parked)
        self.assertTrue(any(quote is crossing for quote in ticks.take("sh600519")))

    def test_empty_ordered_restore_preserves_overflow(self):
        ticks = TargetTickBuffer()
        with mock.patch("sf_logging.log_line"), mock.patch("sf_logging.log_swallow"):
            for price in range(1, 4097):
                ticks.add_ordered("sh600519", {"price": str(price)})
            crossing = {"price": "9999"}
            ticks.add_ordered("sh600519", crossing)
            ticks.restore_ordered("sh600519", ())
        self.assertEqual(sum(quote is crossing for quote in ticks.take("sh600519")), 1)

    def test_restore_overflow_preserves_versioned_later_crossing(self):
        import json
        ticks = TargetTickBuffer()
        structural = (json.dumps({"direction": "above", "target": 5000,
                                  "session_date": "2026-10-14"}, sort_keys=True),)
        revision = ("epoch", "generation", structural)
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch("sf_logging.log_line"):
            for price in range(1, 4097):
                quote = sf_quote.QuoteData({"price": str(price)}, "20261014100000")
                quote.target_revision = revision
                ticks.add_ordered("sh600519", quote)
            parked = ticks.park("sh600519")
            crossing = sf_quote.QuoteData({"price": "5000"}, "20261014100000")
            crossing.target_revision = revision
            ticks.add_ordered("sh600519", crossing)
            ticks.restore_ordered("sh600519", parked)
        self.assertTrue(any(quote is crossing for quote in ticks.take("sh600519")))

    def test_full_buffer_rejects_malformed_revision_structure_without_crashing(self):
        ticks = TargetTickBuffer()
        revision = ("epoch", "revision", ("[]",))
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch("sf_logging.log_line"):
            for price in range(1, 4098):
                quote = sf_quote.QuoteData({"price": str(price)}, "20261014100000")
                quote.target_revision = revision
                ticks.add_ordered("sh600519", quote)
        self.assertTrue(ticks.take("sh600519"))

    def test_stale_quote_is_rejected_before_buffering(self):
        ticks = TargetTickBuffer()
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=False):
            ticks.add("sh600519", {"price": "101"})
        self.assertEqual(ticks.take("sh600519"), [])

    def test_clear_resets_ordered_restore_pagination(self):
        ticks = TargetTickBuffer()
        with mock.patch("sf_logging.log_line"):
            ticks.restore_ordered("sh600519", [(i, float(i), {"price": str(i)})
                                               for i in range(1, 4098)])
        ticks.clear()
        self.assertFalse(ticks._paged_codes)  # discipline-ok(R5): 验证分页内部状态已清除。

    def test_clear_discards_pending_and_taken_quotes(self):
        ticks = TargetTickBuffer()
        quote = {"price": "101"}
        ticks.add("sh600519", quote)
        ticks.take("sh600519")
        ticks.add("sh600519", quote)
        ticks.clear()
        self.assertEqual(ticks.take("sh600519"), [])

    def test_restore_preserves_pending_earliest_equal_price(self):
        ticks = TargetTickBuffer()
        old = {"price": "100", "tag": "old"}
        new = {"price": "100", "tag": "new"}
        ticks.add("sh600519", old)
        parked = ticks.park("sh600519")
        ticks.add("sh600519", new)
        ticks.restore("sh600519", parked)
        self.assertEqual(ticks.take("sh600519"), [old, new])

    def test_pin_reports_existing_ordered_quotes_as_retained(self):
        ticks = TargetTickBuffer()
        waiting = {"price": "99"}
        failed = {"price": "101"}
        ticks.add_ordered("sh600519", waiting)
        retained = ticks.pin("sh600519", failed)
        self.assertIn(waiting, retained)
        self.assertIn(failed, retained)

    def test_full_pinned_queue_still_reports_ordered_quotes(self):
        ticks = TargetTickBuffer()
        waiting = {"price": "99"}
        ticks.add_ordered("sh600519", waiting)
        for number in range(128):
            ticks.pin("sh600519", {"price": str(number + 1)})
        with mock.patch("sf_logging.log_line"):
            retained = ticks.pin("sh600519", {"price": "200"})
        self.assertTrue(any(quote is waiting for quote in retained))

    def test_pin_retains_overflow_first_identity(self):
        ticks = TargetTickBuffer()
        with mock.patch("sf_logging.log_line"):
            for price in range(1, 4097):
                ticks.add_ordered("sh600519", {"price": str(price)})
            first = {"price": "4096.5"}
            ticks.add_ordered("sh600519", first)
            ticks.add_ordered("sh600519", {"price": "0.4"})
            ticks.add_ordered("sh600519", {"price": "5000"})
            retained = ticks.pin("sh600519", {"price": "100"})
        self.assertTrue(any(quote is first for quote in retained))

    def test_multiple_failed_ticks_are_kept_in_order(self):
        ticks = TargetTickBuffer()
        first = {"price": "90"}
        second = {"price": "110"}
        ticks.pin("sh600519", first)
        ticks.pin("sh600519", second)
        self.assertEqual(ticks.take("sh600519"), [first, second])

    def test_old_stock_and_bad_values_do_not_pollute_new_stock(self):
        ticks = TargetTickBuffer()
        for price in ("nan", "inf", "bad", 0, -1):
            ticks.add("A", {"price": price})
        ticks.add("A", {"price": "5"})
        self.assertEqual(ticks.take("B"), [])
        self.assertEqual(ticks.take("A"), [])

    def test_target_tick_buffer_edge_paths_are_retained_and_reported(self):
        from sf_target_tick_store import CapacityError
        ticks = TargetTickBuffer()
        self.assertIsNone(ticks.oldest_persistent())
        with ticks._lock:  # discipline-ok(R5): 持锁验证非阻塞查询不会伪造空闲状态。
            self.assertIsNone(ticks.unsaved_count())
        ticks.acknowledge({"price": "1"})

        with tempfile.TemporaryDirectory() as directory:
            persistent = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            with persistent._lock:  # discipline-ok(R5): 注入生产者占锁现场，验证 oldest_persistent 的忙路径。
                with self.assertRaises(BlockingIOError):
                    persistent.oldest_persistent()
                self.assertIsNone(persistent.unsaved_count())
            for _ in range(4096):
                persistent._unsaved_ticks.append(("A", sf_quote.QuoteData({"price": "1"}, "20261014100000")))  # discipline-ok(R5): 填满隔离 volatile 队列。
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                    self.assertRaises(CapacityError):
                persistent.add_ordered("A", sf_quote.QuoteData({"price": "2"}, "20261014100000"))

    def test_acknowledge_ignores_quote_from_another_store(self):
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            quote._target_store_record = (os.path.join(directory, "other.sqlite"), "A", 1)  # discipline-ok(R5): 构造另一持久队列的记录标记。
            ticks.acknowledge(quote)
            self.assertEqual(quote._target_store_record[0], os.path.join(directory, "other.sqlite"))  # discipline-ok(R5): 验证其他队列的记录标记未被移除。

    def test_park_preserves_overflow_first_record(self):
        ticks = TargetTickBuffer()
        with mock.patch("sf_logging.log_line"):
            for price in range(8193):
                ticks.add_ordered("A", {"price": str(price + 1)})
        parked = ticks.park("A")
        self.assertTrue(parked)
        self.assertEqual(parked[0][1], 1.0)

    def test_restore_ordered_merges_existing_overflow_first_record(self):
        ticks = TargetTickBuffer()
        with mock.patch("sf_logging.log_line"):
            for price in range(8193):
                ticks.add_ordered("A", {"price": str(price + 1)})
        extra = (9000, 9000.0, {"price": "9000"})
        ticks.restore_ordered("A", (extra,))
        self.assertTrue(any(item["price"] == "1" for item in ticks.take("A")))

    def test_retain_unsaved_wraps_plain_quotes_and_rejects_full_queue(self):
        from sf_target_tick_store import CapacityError
        with tempfile.TemporaryDirectory() as directory:
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"))
            plain = {"price": "101"}
            ticks.retain_unsaved("A", plain)
            self.assertEqual(ticks.unsaved_count(), 1)
            self.assertIsNot(ticks._unsaved_ticks[0][1], plain)  # discipline-ok(R5): 核验普通字典已转换为可恢复对象。
            for _ in range(4095):
                ticks._unsaved_ticks.append(("A", sf_quote.QuoteData({"price": "1"}, "20261014100000")))  # discipline-ok(R5): 填满隔离 volatile 队列。
            with self.assertRaises(CapacityError):
                ticks.retain_unsaved("A", {"price": "102"})

    def test_async_recovery_logs_callback_failure_after_saving(self):
        from sf_target_tick_store import TargetTickStore
        with tempfile.TemporaryDirectory() as directory:
            def broken_wakeup():
                raise RuntimeError("wake failed")
            ticks = TargetTickBuffer(persistent_path=os.path.join(directory, "ticks.sqlite"),
                                     on_recovery=broken_wakeup)
            quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
            ticks._unsaved_ticks.append(("A", quote))  # discipline-ok(R5): 注入隔离恢复队列以驱动回调异常分支。
            with mock.patch.object(TargetTickStore, "save", return_value=7), \
                    mock.patch("sf_logging.log_line") as log:
                ticks._recover_async_batch()  # discipline-ok(R5): 直接驱动后台恢复函数，避免启动真实线程。
            self.assertEqual(ticks.unsaved_count(), 0)
            self.assertTrue(any("recovery wake failed" in str(call.args[0])
                                for call in log.call_args_list))

    def test_ordered_compaction_handles_valid_crossing_and_invalid_target(self):
        import json
        revision = ("epoch", "generation", (json.dumps({"direction": "above", "target": 4000}),))
        ticks = TargetTickBuffer()
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            for price in range(1, 4097):
                quote = sf_quote.QuoteData({"price": str(price)}, "20261014100000")
                quote.target_revision = revision
                ticks.add_ordered("A", quote)
            crossing = sf_quote.QuoteData({"price": "5000"}, "20261014100000")
            crossing.target_revision = revision
            ticks.add_ordered("A", crossing)
        self.assertTrue(ticks.take("A"))

        invalid = ("epoch", "generation", (json.dumps({"direction": "above", "target": 0}),))
        malformed = TargetTickBuffer()
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch("sf_logging.log_swallow") as swallow:
            for price in range(1, 4098):
                quote = sf_quote.QuoteData({"price": str(price)}, "20261014100000")
                quote.target_revision = invalid
                malformed.add_ordered("A", quote)
        self.assertTrue(malformed.take("A"))
        swallow.assert_called()

    def test_restore_ordered_handles_overflow_first_and_malformed_revision(self):
        import json
        ticks = TargetTickBuffer()
        with mock.patch("sf_logging.log_line"), mock.patch("sf_logging.log_swallow"):
            for price in range(1, 4098):
                ticks.add_ordered("A", {"price": str(price)})
            parked = ticks.park("A")
            ticks.restore_ordered("A", parked)
        self.assertTrue(ticks.take("A"))

        bad_revision = ("epoch", "generation", (json.dumps({"direction": "above", "target": 0}),))
        malformed = TargetTickBuffer()
        records = []
        for number in range(1, 4098):
            quote = sf_quote.QuoteData({"price": str(number)}, "20261014100000")
            quote.target_revision = bad_revision
            records.append((number, float(number), quote))
        with mock.patch("sf_logging.log_swallow") as swallow:
            self.assertTrue(malformed.restore_ordered("A", records))
        swallow.assert_called()

    def test_timestamp_does_not_change_dictionary_contract(self):
        quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
        self.assertEqual(set(quote), {"price"})
        self.assertTrue(sf_quote.quote_is_current(quote, NOW))
        self.assertFalse(sf_quote.quote_is_current(quote, NOW + datetime.timedelta(days=1)))
        self.assertFalse(sf_quote.quote_is_current(sf_quote.QuoteData({"price": "101"}), NOW))

    def test_stale_and_future_quotes_are_not_trusted(self):
        for stamp in ("20251014100000", "20261014100101", "20261014095000", "bad"):
            self.assertFalse(sf_quote.quote_is_current(sf_quote.QuoteData({}, stamp), NOW), stamp)

    def test_weekend_and_preopen_use_last_trading_day(self):
        quote = sf_quote.QuoteData({}, "20261016150000")
        weekend = NOW.replace(day=18, hour=12)
        self.assertTrue(sf_quote.quote_is_current(quote, weekend))
        self.assertTrue(sf_quote.quote_is_current(quote, NOW.replace(day=19, hour=8)))

    def test_current_backup_can_recover_stale_primary(self):
        stale = sf_quote.QuoteData({"price": "1"}, "20251014100000")
        fresh = sf_quote.QuoteData({"price": "2"}, "20261014100000")
        with mock.patch.object(sf_quote, "_beijing_now", return_value=NOW), \
                mock.patch.object(sf_quote, "fetch_quote_fallback", return_value=fresh):
            self.assertIs(sf_quote.ensure_current_quote("sh600519", stale), fresh)
        with mock.patch.object(sf_quote, "_beijing_now", return_value=NOW), \
                mock.patch.object(sf_quote, "fetch_quote_fallback", return_value=stale):
            with self.assertRaises(sf_quote.QuoteDataError):
                sf_quote.ensure_current_quote("sh600519", stale)

    def test_ui_business_entry_rejects_wrong_stock_and_nontrading(self):
        seen = []
        host = SimpleNamespace(code="sh600519", _targets_enabled=lambda: True,
                               _safe_alerts=lambda: [{"target": 100}],
                               _is_trading_time=lambda: True, _evaluate_targets=seen.append)
        entry = window.FloatWindow._evaluate_received_quote  # discipline-ok(R5): validate the business entry without constructing a native window
        entry(host, {"code": "000001", "price": "101"})
        entry(host, {"code": "600519", "price": "101"})
        self.assertEqual(seen, [101])
        host._is_trading_time = lambda: False  # discipline-ok(R5): inject the nontrading decision at the same business boundary
        entry(host, {"code": "600519", "price": "102"})
        self.assertEqual(seen, [101])


def _tencent_payload(price="1500.00", pre_close="1490.00", stamp="20261014100000"):
    fields = [""] * 50
    fields[0], fields[1], fields[2], fields[3], fields[4] = "1", "贵州茅台", "600519", price, pre_close
    fields[30], fields[31], fields[32], fields[33], fields[34] = stamp, "10", "0.67", "1510", "1480"
    fields[36], fields[47], fields[48] = "100", "1639", "1341"
    return 'v_sh600519="%s";\n' % "~".join(fields)


def _sina_payload(low="1480.00"):
    fields = [""] * 33
    fields[0], fields[1], fields[2], fields[3] = "贵州茅台", "1495", "1490.00", "1500.00"
    fields[4], fields[5], fields[30], fields[31] = "1510", low, "2026-10-14", "10:00:10"
    return 'var hq_str_sh600519="%s";\n' % ",".join(fields)


class TruncatedPayloadIsNotAQuote(SFCase):
    """2026-11-02 走查 F-A-05：行情文本缺结束符（被截断）时，不得当作有效行情解析。

    腾讯/新浪的正常回包都以 `";` 收尾。旧解析 rstrip 掉结尾的引号分号换行之后再切字段，
    截断在某个数值字段中间（如昨收 `149`、最低 `148`）时字段数与数值校验都照样通过，
    于是带着错价格的"行情"进了界面和目标价判定。
    """

    def setUp(self):
        super().setUp()
        self.track_patch(mock.patch.object(sf_quote, "log_line"))

    def _http(self, text):
        return mock.patch.object(sf_quote, "http_get", return_value=text)

    def test_complete_primary_payload_still_parses(self):
        with self._http(_tencent_payload()):
            quote = sf_quote._fetch_quote_primary("sh600519")  # discipline-ok(R5): 单独验证主源解析，不让备源掩盖结果
        self.assertEqual(quote["pre_close"], "1490.00")
        self.assertEqual(quote["name"], "贵州茅台")

    def test_primary_cut_inside_a_numeric_field_is_rejected(self):
        full = _tencent_payload()
        cut = full[:full.index("~1490.00") + 4]                       # ...~149
        with self._http(cut), self.assertRaises(sf_quote.QuoteDataError):
            sf_quote._fetch_quote_primary("sh600519")  # discipline-ok(R5): 单独验证截断的主源会抛错

    def test_primary_without_trailing_newline_is_still_complete(self):
        with self._http(_tencent_payload().rstrip("\n")):
            self.assertEqual(sf_quote._fetch_quote_primary("sh600519")["price"], "1500.00")  # discipline-ok(R5): 单独验证主源允许省略换行

    def test_complete_backup_payload_still_parses(self):
        with self._http(_sina_payload()):
            quote = sf_quote.fetch_quote_fallback("sh600519")
        self.assertEqual(quote["low"], "1480.00")

    def test_backup_cut_inside_a_numeric_field_is_rejected(self):
        full = _sina_payload()
        cut = full[:full.index(",1480.00") + 4]                       # ...,148
        with self._http(cut), self.assertRaises(sf_quote.QuoteDataError):
            sf_quote.fetch_quote_fallback("sh600519")

    def test_truncated_primary_falls_back_to_the_backup_source(self):
        full = _tencent_payload()
        cut = full[:full.index("~1490.00") + 4]

        def fake_get(url, **kwargs):
            return cut if "gtimg" in url else _sina_payload()

        with mock.patch.object(sf_quote, "http_get", side_effect=fake_get):
            quote = sf_quote.fetch_quote("sh600519")
        self.assertEqual(sf_quote.get_last_quote_source(), "sina")
        self.assertEqual(quote["pre_close"], "1490.00")
