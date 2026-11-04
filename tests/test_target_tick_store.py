"""Behavioral tests for the isolated durable target tick queue."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sf_quote import QuoteData
from sf_target_tick_store import CapacityError, TargetTickStore
from tests._base import SFCase


def make_quote(price="10", revision=("sh600000", 2, ("a", ("b", 3)))):
    quote = QuoteData({"price": price, "name": "股票", "nested": [1, {"x": None}]})
    quote.source_time = datetime(2026, 11, 2, 9, 30, tzinfo=timezone(timedelta(hours=8)))
    quote.received_at = datetime(2026, 11, 2, 9, 30, 1, 123456, tzinfo=UTC)
    quote.target_revision = revision
    return quote


class TargetTickStoreTests(SFCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ticks.sqlite"

    def test_reopen_pagination_stock_isolation_and_acknowledgement(self):
        a, b, c = make_quote("10"), make_quote("20"), make_quote("11")
        with TargetTickStore(self.path) as store:
            ids = [store.save("sh600000", a), store.save("sz000001", b),
                   store.save("sh600000", c)]
        with TargetTickStore(self.path) as store:
            first = store.read("sh600000", limit=1)
            second = store.read("sh600000", after_id=first[0].id, limit=1)
            self.assertEqual([first[0].id, second[0].id], [ids[0], ids[2]])
            restored = first[0].quote
            self.assertIsInstance(restored, QuoteData)
            self.assertEqual(dict(restored), dict(a))
            self.assertEqual(restored.source_time, a.source_time)
            self.assertEqual(restored.received_at, a.received_at)
            self.assertEqual(restored.target_revision, a.target_revision)
            self.assertIsInstance(restored.target_revision[2][1], tuple)
            self.assertEqual(first[0].code, "sh600000")
            self.assertEqual(store.acknowledge("sh600000", [ids[0], ids[1]]), 1)
            self.assertEqual([r.id for r in store.read("sz000001")], [ids[1]])
            self.assertEqual([r.id for r in store.read("sh600000")], [ids[2]])
        with TargetTickStore(self.path) as store:
            self.assertEqual([r.id for r in store.read("sh600000")], [ids[2]])

    def test_tick_survives_writer_process_exit(self):
        import subprocess
        import sys
        code = (
            "import sys; from sf_quote import QuoteData; "
            "from sf_target_tick_store import TargetTickStore; "
            "q=QuoteData({'price':'101'}); q.target_revision=('epoch','rev',()); "
            "s=TargetTickStore(sys.argv[1]); s.save('sh600183',q); s.close()"
        )
        subprocess.run([sys.executable, "-c", code, str(self.path)], check=True, timeout=10)
        with TargetTickStore(self.path) as store:
            records = store.read("sh600183")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].quote["price"], "101")
            self.assertEqual(records[0].quote.target_revision, ("epoch", "rev", ()))
            store.acknowledge("sh600183", [records[0].id])
        with TargetTickStore(self.path) as store:
            self.assertEqual(store.read("sh600183"), [])

    def test_global_oldest_retains_stock_and_order(self):
        with TargetTickStore(self.path) as store:
            ids = [store.save("old", make_quote("10")),
                   store.save("new", make_quote("20")),
                   store.save("old", make_quote("11"))]
            page = store.read_global(limit=2)
            self.assertEqual([(item.id, item.code, item.quote["price"]) for item in page],
                             [(ids[0], "old", "10"), (ids[1], "new", "20")])
            page = store.read_global(after_id=ids[1], limit=2)
            self.assertEqual([(item.id, item.code) for item in page], [(ids[2], "old")])

    def test_actionable_prefix_skips_unknown_stock_across_reopen(self):
        with TargetTickStore(self.path) as store:
            ids = [store.save("600000", make_quote("10", None)),
                   store.save("sh600000", make_quote("11")),
                   store.save("sz000001", make_quote("20")),
                   store.save("sz000001", make_quote("21")),
                   store.save("sh600519", make_quote("30"))]
        with TargetTickStore(self.path) as store:
            items, blocked = store.read_actionable_prefix(limit=10)
            self.assertEqual([item.id for item in items], ids[2:4])
            self.assertEqual(blocked, {"sh600000"})
            self.assertEqual(len(store.read_global()), 5)

    def test_unknown_revision_blocks_later_ticks_for_same_stock(self):
        with TargetTickStore(self.path) as store:
            first = store.save("sh600000", make_quote("10"))
            unknown = store.save("sh600000", make_quote("11", None))
            later = store.save("sh600000", make_quote("12"))
            other = store.save("sz000001", make_quote("20"))
            items, blocked = store.read_actionable_prefix(limit=10)
            self.assertEqual([item.id for item in items], [first])
            self.assertEqual(blocked, {"sh600000"})
            self.assertEqual([item.id for item in store.read("sh600000")],
                             [first, unknown, later])

    def test_blocked_stock_scan_does_not_visit_every_tick(self):
        with TargetTickStore(self.path) as store:
            store.save("sh600000", make_quote("10", None))
            store._db.execute("INSERT INTO target_ticks(code, payload, size) "  # discipline-ok(R5): 注入持久队列边界记录。
                              "SELECT code, payload, size FROM target_ticks "  # discipline-ok(R5): 直接构造持久队列压测数据。
                              "CROSS JOIN (SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3)")
            for _ in range(14):
                store._db.execute("INSERT INTO target_ticks(code, payload, size) "  # discipline-ok(R5): 注入持久队列边界记录。
                                  "SELECT code, payload, size FROM target_ticks")
            calls = []
            store._db.set_progress_handler(lambda: calls.append(1) or 0, 100)  # discipline-ok(R5): 测量 SQL 扫描工作量。
            try:
                records, blocked = store.read_actionable_prefix(limit=10)
            finally:
                store._db.set_progress_handler(None, 0)  # discipline-ok(R5): 清理测试注入的 SQLite 探针。
            self.assertEqual(records, [])
            self.assertEqual(blocked, {"sh600000"})
            self.assertLess(len(calls), 100)

    def test_actionable_limit_one_ignores_later_invalid_code(self):
        with TargetTickStore(self.path) as store:
            first = store.save("sh600000", make_quote())
            store._db.execute("INSERT INTO target_ticks(code, payload, size) VALUES (?, ?, ?)",  # discipline-ok(R5): 注入非法代码记录。
                              (sqlite3.Binary(b"bad"), "{}", 3))
            items, blocked = store.read_actionable_prefix(limit=1)
            self.assertEqual([item.id for item in items], [first])
            self.assertEqual(blocked, set())

    def test_blocked_prefix_does_not_expose_later_invalid_code(self):
        with TargetTickStore(self.path) as store:
            store.save("sh600000", make_quote("10", None))
            expected = store.save("sh600519", make_quote("20"))
            store._db.execute("INSERT INTO target_ticks(code, payload, size) VALUES (?, ?, ?)",  # discipline-ok(R5): 注入非法代码记录。
                              (sqlite3.Binary(b"bad"), "{}", 3))
            items, blocked = store.read_actionable_prefix(limit=1)
            self.assertEqual([item.id for item in items], [expected])
            self.assertEqual(blocked, {"sh600000"})

    def test_actionable_limit_one_does_not_scan_all_codes(self):
        with TargetTickStore(self.path) as store:
            for i in range(200):
                store.save("stock%04d" % i, make_quote())
            selects = []
            store._db.set_trace_callback(lambda sql: selects.append(sql) if sql.startswith("SELECT") else None)  # discipline-ok(R5): 测量查询数量。
            try:
                items, _ = store.read_actionable_prefix(limit=1)
            finally:
                store._db.set_trace_callback(None)  # discipline-ok(R5): 清理测试注入的 SQLite 探针。
            self.assertEqual(len(items), 1)
            self.assertLess(len(selects), 10)

    def test_entry_limit_is_global_and_ack_releases_capacity(self):
        with TargetTickStore(self.path, max_entries=1) as store:
            first = store.save("a", make_quote())
            with self.assertRaises(CapacityError):
                store.save("b", make_quote())
            self.assertEqual([r.id for r in store.read("a")], [first])
            self.assertEqual(store.read("b"), [])
            store.acknowledge("a", [first])
            self.assertGreater(store.save("b", make_quote()), first)

    def test_byte_limit_rejects_without_removing_previous_record(self):
        with TargetTickStore(self.path) as store:
            first = store.save("a", make_quote())
        with TargetTickStore(self.path, max_bytes=1) as store:
            with self.assertRaises(CapacityError):
                store.save("a", make_quote("11"))
            self.assertEqual([r.id for r in store.read("a")], [first])

    def test_unsupported_data_is_rejected_without_losing_prior_data(self):
        with TargetTickStore(self.path) as store:
            first = store.save("a", make_quote())
            quote = make_quote()
            quote["unsupported"] = object()
            with self.assertRaises(TypeError):
                store.save("a", quote)
            self.assertEqual([r.id for r in store.read("a")], [first])

    def test_sqlite_failure_during_ack_rolls_back(self):
        with TargetTickStore(self.path) as store:
            first = store.save("a", make_quote())
            second = store.save("a", make_quote("11"))
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("CREATE TRIGGER deny_delete BEFORE DELETE ON target_ticks "
                           f"WHEN OLD.id = {second} BEGIN SELECT RAISE(ABORT, 'denied'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                store.acknowledge("a", [first, second])
            self.assertEqual([r.id for r in store.read("a")], [first, second])

    def test_missing_revision_and_source_time_round_trip(self):
        quote = QuoteData({"price": "10"})
        quote.source_time = None
        with TargetTickStore(self.path) as store:
            store.save("a", quote)
            restored = store.read("a")[0].quote
            self.assertIsNone(restored.source_time)
            self.assertEqual(restored.received_at, quote.received_at)
            self.assertFalse(hasattr(restored, "target_revision"))

    def test_exact_byte_boundary_and_multiple_connections(self):
        with TargetTickStore(self.path) as store:
            store.save("a", make_quote())
        with closing(sqlite3.connect(self.path)) as db:
            size = db.execute("SELECT size FROM target_ticks").fetchone()[0]
        with TargetTickStore(self.path, max_bytes=size * 2) as first, \
                TargetTickStore(self.path, max_bytes=size * 2) as second:
            first.save("a", make_quote())
            with self.assertRaises(CapacityError):
                second.save("a", make_quote())
            self.assertEqual(len(second.read("a")), 2)

    def test_failed_insert_does_not_remove_existing_tick(self):
        with TargetTickStore(self.path) as store:
            first = store.save("a", make_quote())
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("CREATE TRIGGER deny_insert BEFORE INSERT ON target_ticks "
                           "BEGIN SELECT RAISE(ABORT, 'denied'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                store.save("a", make_quote())
            self.assertEqual([r.id for r in store.read("a")], [first])

    def test_busy_database_fails_promptly_without_losing_existing_tick(self):
        import threading
        import time
        with TargetTickStore(self.path) as store:
            first = store.save("a", make_quote())
        acquired = threading.Event()
        release = threading.Event()
        def hold_lock():
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("BEGIN IMMEDIATE")
                acquired.set()
                release.wait(5)
                db.rollback()
        worker = threading.Thread(target=hold_lock)
        worker.start()
        try:
            self.assertTrue(acquired.wait(2))
            started = time.monotonic()
            with TargetTickStore(self.path) as store, self.assertRaises(sqlite3.OperationalError):
                store.save("a", make_quote("11"))
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        with TargetTickStore(self.path) as store:
            self.assertEqual([item.id for item in store.read("a")], [first])

    def test_new_database_is_private_under_permissive_umask(self):
        import os
        import stat
        original = os.umask(0)
        try:
            with TargetTickStore(self.path) as store:
                store.save("a", make_quote())
        finally:
            os.umask(original)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_symlink_database_is_rejected_without_touching_target(self):
        target = Path(self.directory.name) / "other.sqlite"
        with closing(sqlite3.connect(target)) as db:
            db.execute("CREATE TABLE unrelated (value TEXT)")
        before = target.read_bytes()
        self.path.symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            TargetTickStore(self.path)
        self.assertEqual(target.read_bytes(), before)

    def test_encode_decode_and_timestamp_reject_unsupported_values(self):
        from sf_target_tick_store import _decode, _encode, _timestamp
        with self.assertRaises(ValueError):
            _encode(float("nan"))
        self.assertEqual(_encode(1.25), 1.25)
        with self.assertRaises(ValueError):
            TargetTickStore(":memory:")
        with self.assertRaises(TypeError):
            _encode({1: "not a string key"})
        with self.assertRaises(TypeError):
            _encode(object())
        with self.assertRaises(ValueError):
            _decode({"type": "unknown", "items": []})
        with self.assertRaises(TypeError):
            _timestamp("not a datetime")

    def test_constructor_and_save_validate_limits_and_types(self):
        with self.assertRaises(ValueError):
            TargetTickStore(self.path, max_bytes=-1)
        with TargetTickStore(self.path) as store:
            with self.assertRaises(ValueError):
                store.save("", make_quote())
            with self.assertRaises(TypeError):
                store.save("a", {"price": "10"})
            with self.assertRaises(ValueError):
                store.read("a", after_id=-1)
            with self.assertRaises(ValueError):
                store.read_global(limit=0)
            with self.assertRaises(ValueError):
                store.acknowledge("a", [0])

    def test_schema_initialization_failure_closes_connection(self):
        connection = mock.Mock()
        connection.execute.side_effect = RuntimeError("schema failed")
        with mock.patch("sf_target_tick_store.sqlite3.connect", return_value=connection):
            with self.assertRaises(RuntimeError):
                TargetTickStore(self.path)
        connection.close.assert_called_once_with()

        with self.assertRaises((TypeError, ValueError)):
            TargetTickStore(None)
        with self.assertRaises(ValueError):
            TargetTickStore(self.path, max_entries=-1)
        with TargetTickStore(self.path) as store, self.assertRaises(ValueError):
            store.read("a", limit=0)
        with self.assertRaises(sqlite3.ProgrammingError):
            store.read("a")


if __name__ == "__main__":
    unittest.main()
