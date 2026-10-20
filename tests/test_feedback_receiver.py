import multiprocessing
import socket
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from sf_feedback import build_archive, make_envelope
from sf_feedback_receiver import FeedbackReceiver, ReceiverError, submit
from tests._base import SFCase


def _receiver_process(socket_path, database_path, objects_dir, ready):
    receiver = FeedbackReceiver(socket_path, database_path, objects_dir)
    receiver.start()
    ready.set()
    try:
        time.sleep(10)
    finally:
        receiver.stop()


class FeedbackReceiverTests(SFCase):
    def _archive(self, root, value="one"):
        path = root / (value + ".zip")
        build_archive(path, {"state.json": value}, app_version="1.4.94",
                      bundle_id="com.skyrocket.app", run_id=value)
        return path

    def test_cross_process_submit_is_idempotent_and_conflict_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path, database, objects = root / "receiver.sock", root / "cases.sqlite", root / "objects"
            ready = multiprocessing.Event()
            process = multiprocessing.Process(target=_receiver_process,
                                               args=(socket_path, database, objects, ready))
            process.start()
            self.addCleanup(process.terminate)
            self.assertTrue(ready.wait(3))
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="1.4.94",
                                     outbox_id="outbox-1", idempotency_key="same-key")
            first = submit(socket_path, envelope, archive)
            second = submit(socket_path, envelope, archive)
            self.assertEqual(first, second)
            other = self._archive(root, "two")
            other_envelope = make_envelope(archive_path=other, app_version="1.4.94",
                                           outbox_id="outbox-2", idempotency_key="same-key")
            with self.assertRaises(ReceiverError):
                submit(socket_path, other_envelope, other)
            process.terminate()
            process.join(3)

    def test_response_loss_can_retry_and_corrupt_archive_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects")
            receiver.start()
            self.addCleanup(receiver.stop)
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="1.4.94",
                                     outbox_id="outbox-1", idempotency_key="lost-response")
            self.assertIsNone(submit(receiver.socket_path, envelope, archive, read_response=False))
            receipt = submit(receiver.socket_path, envelope, archive)
            self.assertEqual(receipt["storage_status"], "stored")
            broken = root / "broken.zip"
            broken.write_bytes(b"not zip")
            broken_envelope = dict(envelope, archive_sha256="0" * 64, size_bytes=7,
                                   idempotency_key="broken")
            with self.assertRaises(ReceiverError):
                submit(receiver.socket_path, broken_envelope, broken)

    def test_orphan_after_object_write_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects",
                                        fault_after_object=True)
            receiver.start()
            self.addCleanup(receiver.stop)
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="1.4.94",
                                     outbox_id="outbox-1", idempotency_key="orphan")
            with self.assertRaises(ReceiverError):
                submit(receiver.socket_path, envelope, archive)
            self.assertEqual(len(receiver.reconcile()["orphaned_objects"]), 1)

    def test_missing_object_is_reported_without_claiming_received(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects")
            receiver.start()
            self.addCleanup(receiver.stop)
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="1.4.94",
                                     outbox_id="outbox-1", idempotency_key="missing")
            submit(receiver.socket_path, envelope, archive)
            next(root.joinpath("objects").glob("*.zip")).unlink()
            self.assertEqual(len(receiver.reconcile()["missing_objects"]), 1)
            case = receiver.case_for("missing")
            self.assertEqual(case["storage_status"], "storage_missing")
            self.assertEqual(case["validation_status"], "storage_fault")

    def test_accept_rejects_envelope_hash_size_and_schema_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects")
            archive = self._archive(root)
            digest = __import__("hashlib").sha256(archive.read_bytes()).hexdigest()
            valid = {"schema_version": 1, "idempotency_key": "k", "archive_sha256": digest,
                     "size_bytes": archive.stat().st_size}
            for bad in (
                dict(valid, schema_version=2),
                dict(valid, idempotency_key=""),
                dict(valid, archive_sha256="0" * 64),
                dict(valid, size_bytes=0),
            ):
                with self.subTest(bad=bad), self.assertRaises(ReceiverError):
                    receiver.accept(bad, archive.read_bytes())
            with self.assertRaises(ReceiverError):
                receiver.accept(valid, b"bad")

    def test_lifecycle_is_idempotent_and_socket_permissions_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects")
            receiver.stop()
            receiver.start()
            receiver.start()
            self.assertEqual(receiver.socket_path.stat().st_mode & 0o777, 0o600)
            receiver.stop()
            receiver.stop()
            self.assertFalse(receiver.socket_path.exists())

    def test_reconcile_reports_orphan_without_mutating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects")
            orphan = root / "objects" / "orphan.zip"
            orphan.write_bytes(b"orphan")
            result = receiver.reconcile()
            self.assertEqual(result["orphaned_objects"], ["orphan.zip"])
            self.assertTrue(orphan.exists())

    def test_frame_reader_rejects_eof_and_oversized_frame(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        from sf_feedback_receiver import _read_exact, _read_frame
        right.close()
        with self.assertRaises(ReceiverError):
            _read_exact(left, 1)
        left2, right2 = socket.socketpair()
        self.addCleanup(left2.close)
        self.addCleanup(right2.close)
        left2.settimeout(1)
        right2.sendall((16 * 1024 * 1024 + 1).to_bytes(8, "big"))
        with self.assertRaises(ReceiverError):
            _read_frame(left2)

    def test_handler_calls_receiver_and_handles_broken_pipe(self):
        from sf_feedback_receiver import _Handler
        receiver = Mock()
        receiver.accept.return_value = {"case_id": "case"}
        server = Mock(receiver=receiver)
        request, peer = socket.socketpair()
        self.addCleanup(request.close)
        self.addCleanup(peer.close)
        request.settimeout(2)
        peer.sendall((2).to_bytes(8, "big") + b"{}" + (1).to_bytes(8, "big") + b"x")
        _Handler(request, None, server)
        self.assertEqual(receiver.accept.call_count, 1)

    def test_handler_swallows_broken_pipe_on_success_response(self):
        from sf_feedback_receiver import _Handler
        receiver = Mock()
        receiver.accept.return_value = {"case_id": "case"}
        server = Mock(receiver=receiver)
        request = Mock()
        request.recv.side_effect = [(2).to_bytes(8, "big"), b"{}", (1).to_bytes(8, "big"), b"x"]
        request.sendall.side_effect = BrokenPipeError
        _Handler(request, None, server)
        receiver.accept.assert_called_once()

    def test_run_receiver_closes_receiver_after_keyboard_interrupt(self):
        import sf_feedback_receiver as module
        receiver = Mock()
        with patch.object(module, "FeedbackReceiver", return_value=receiver), \
             patch.object(module.threading.Event, "wait", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                module.run_receiver("sock", "db", "objects")
        receiver.start.assert_called_once()
        receiver.stop.assert_called_once()

    def test_integrity_error_competition_uses_existing_matching_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects")
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="1.4.94", outbox_id="one", idempotency_key="race")
            original = receiver._connect  # discipline-ok(R5): 保留真实临时数据库连接，仅在INSERT处注入竞争，不替代事务与回滚行为。
            connections = []
            class Racing:
                def __init__(self, db): self.db = db; self.inserted = False
                def execute(self, sql, params=()):
                    if sql.lstrip().startswith("INSERT INTO cases") and not self.inserted:
                        self.inserted = True
                        self.db.execute("INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        ("case-race", params[1], params[2], params[3], params[4], params[5], params[6], params[7],
                                         '{"case_id":"case-race","archive_sha256":"' + params[2] + '","size_bytes":' + str(params[3]) + ',"received_at":"now","storage_status":"stored","validation_status":"validated","retention_until":"later"}'))
                        raise sqlite3.IntegrityError("race")
                    return self.db.execute(sql, params)
                def __getattr__(self, name): return getattr(self.db, name)
            def connect():
                db = original()
                connections.append(db)
                return Racing(db)
            try:
                with patch.object(receiver, "_connect", side_effect=connect):
                    result = receiver.accept(envelope, archive.read_bytes())
                self.assertEqual(result["case_id"], "case-race")
                self.assertEqual(receiver.case_for("race")["case_id"], "case-race")
            finally:
                for db in connections:
                    db.close()

    def test_submit_size_rejections_happen_before_socket_creation(self):
        from sf_feedback_receiver import MAX_ARCHIVE_BYTES, MAX_ENVELOPE_BYTES
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "oversized.zip"
            archive.write_bytes(b"x")
            with patch("sf_feedback_receiver.socket.socket") as connect:
                with self.assertRaisesRegex(ReceiverError, "exceeds"):
                    submit("unused.sock", {"padding": "x" * MAX_ENVELOPE_BYTES}, archive)
                with archive.open("wb") as stream:
                    stream.truncate(MAX_ARCHIVE_BYTES + 1)
                with self.assertRaisesRegex(ReceiverError, "exceeds"):
                    submit("unused.sock", {}, archive)
                connect.assert_not_called()

    def test_handler_error_response_broken_pipe_is_contained(self):
        from sf_feedback_receiver import _Handler
        receiver = Mock()
        receiver.accept.side_effect = ReceiverError("rejected archive")
        request, peer = socket.socketpair()
        with request, peer:
            request.settimeout(2)
            peer.settimeout(2)
            peer.sendall((2).to_bytes(8, "big") + b"{}" + (1).to_bytes(8, "big") + b"x")
            peer.close()
            _Handler(request, None, Mock(receiver=receiver))
        receiver.accept.assert_called_once_with({}, b"x")

    def test_accept_retries_return_persisted_receipt_and_reject_conflicting_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects")
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="test", outbox_id="one",
                                     idempotency_key="direct-retry")
            first = receiver.accept(envelope, archive.read_bytes())
            self.assertEqual(receiver.accept(envelope, archive.read_bytes()), first)
            other = self._archive(root, "two")
            conflicting = make_envelope(archive_path=other, app_version="test", outbox_id="two",
                                        idempotency_key="direct-retry")
            with self.assertRaisesRegex(ReceiverError, "bound to a different archive"):
                receiver.accept(conflicting, other.read_bytes())
            self.assertEqual(receiver.case_for("direct-retry")["archive_sha256"], envelope["archive_sha256"])
            self.assertEqual(receiver.reconcile(), {"orphaned_objects": [], "missing_objects": []})
            self.assertEqual(list(receiver.objects_dir.glob("incoming-*")), [])

    def test_integrity_failure_without_matching_concurrent_row_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects")
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="test", outbox_id="one",
                                     idempotency_key="insert-rejected")
            with closing(sqlite3.connect(receiver.database_path)) as db, db:
                db.execute("CREATE TRIGGER reject_case BEFORE INSERT ON cases "
                           "BEGIN SELECT RAISE(ABORT, 'insert rejected'); END")
            with self.assertRaisesRegex(ReceiverError, "concurrently bound"):
                receiver.accept(envelope, archive.read_bytes())
            self.assertIsNone(receiver.case_for("insert-rejected"))
            self.assertEqual(receiver.reconcile()["orphaned_objects"], [envelope["archive_sha256"] + ".zip"])
            self.assertEqual(list(receiver.objects_dir.glob("incoming-*")), [])

    def test_reconcile_tolerates_case_deleted_after_missing_object_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects")
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="test", outbox_id="one",
                                     idempotency_key="deleted-case")
            receiver.accept(envelope, archive.read_bytes())
            object_path = receiver.objects_dir / (envelope["archive_sha256"] + ".zip")
            object_path.unlink()
            real_exists = Path.exists
            deleted = []

            def exists(path):
                if path == object_path and not deleted:
                    with closing(sqlite3.connect(receiver.database_path)) as db, db:
                        db.execute("DELETE FROM cases WHERE idempotency_key = ?", ("deleted-case",))
                    deleted.append(True)
                return real_exists(path)

            with patch.object(Path, "exists", exists):
                report = receiver.reconcile()
            self.assertEqual(deleted, [True])
            self.assertEqual(report, {"orphaned_objects": [], "missing_objects": [object_path.name]})
            self.assertIsNone(receiver.case_for("deleted-case"))


if __name__ == "__main__":
    unittest.main()
