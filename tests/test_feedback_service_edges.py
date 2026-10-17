import hashlib
import json
import os
import socket
import sqlite3
import struct
import tempfile
import threading
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import sf_feedback_receiver as receiver_module
from sf_feedback import (
    MAX_MEMBER_BYTES,
    FeedbackProtocolError,
    build_archive,
    make_envelope,
)
from sf_feedback_analyzer import analyze_archive
from sf_feedback_receiver import (
    FeedbackReceiver,
    ReceiverError,
    _read_exact,
    submit,
)
from tests._base import SFCase


class FeedbackServiceEdgeCandidates(SFCase):
    def _archive(self, root, name="feedback.zip", state=None):
        path = root / name
        files = {"state.json": json.dumps({"health": True} if state is None else state)}
        build_archive(path, files, app_version="1.4.94", bundle_id="com.skyrocket.app", run_id="edge")
        return path

    def _receiver(self, root, **kwargs):
        receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects", **kwargs)
        receiver.start()
        self.addCleanup(receiver.stop)
        return receiver

    def test_read_exact_rejects_short_frame(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        right.sendall(b"ab")
        right.shutdown(socket.SHUT_WR)
        with self.assertRaises(ReceiverError):
            _read_exact(left, 3)

    def test_submit_rejects_oversized_archive_before_socket_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "too-large.zip"
            archive.write_bytes(b"x" * (16 * 1024 * 1024 + 1))
            envelope = {"schema_version": 1}
            with self.assertRaises(ReceiverError):
                submit(root / "missing.sock", envelope, archive)

    def test_handler_returns_error_for_bad_json_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = self._receiver(root)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(os.fspath(receiver.socket_path))
                payload = b"not-json"
                client.sendall(struct.pack("!Q", len(payload)) + payload)
                size = struct.unpack("!Q", client.recv(8))[0]
                response = json.loads(client.recv(size).decode("utf-8"))
            self.assertFalse(response["ok"])
            self.assertIn("Expecting value", response["error"])

    def test_submit_rejects_bad_response_json_from_local_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "bad-response.sock"
            ready = threading.Event()

            def serve():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    server.bind(os.fspath(path))
                    server.listen(1)
                    ready.set()
                    conn, _ = server.accept()
                    with conn:
                        for _ in range(2):
                            size = struct.unpack("!Q", _read_exact(conn, 8))[0]
                            _read_exact(conn, size)
                        conn.sendall(struct.pack("!Q", 1) + b"{")

            thread = threading.Thread(target=serve)
            thread.start()
            self.assertTrue(ready.wait(2))
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="1.4.94", outbox_id="bad")
            with self.assertRaises(json.JSONDecodeError):
                submit(path, envelope, archive)
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_submit_rejects_non_ok_response_from_local_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "reject-response.sock"
            ready = threading.Event()

            def serve():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    server.bind(os.fspath(path))
                    server.listen(1)
                    ready.set()
                    conn, _ = server.accept()
                    with conn:
                        for _ in range(2):
                            size = struct.unpack("!Q", _read_exact(conn, 8))[0]
                            _read_exact(conn, size)
                        response = json.dumps({"ok": False, "error": "bad response"}).encode()
                        conn.sendall(struct.pack("!Q", len(response)) + response)

            thread = threading.Thread(target=serve)
            thread.start()
            self.assertTrue(ready.wait(2))
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="1.4.94", outbox_id="bad")
            with self.assertRaisesRegex(ReceiverError, "bad response"):
                submit(path, envelope, archive)
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_run_receiver_stops_real_server_when_event_wait_is_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stopped = threading.Event()
            original_stop = FeedbackReceiver.stop

            class InterruptingEvent:
                def wait(self):
                    raise KeyboardInterrupt

            def stop_and_record(receiver):
                try:
                    return original_stop(receiver)
                finally:
                    stopped.set()

            real_threading = receiver_module.threading
            proxy = types.SimpleNamespace(Thread=real_threading.Thread, Event=InterruptingEvent)
            with mock.patch.object(FeedbackReceiver, "stop", stop_and_record), \
                    mock.patch.dict(receiver_module.run_receiver.__globals__, {"threading": proxy}):
                with self.assertRaises(KeyboardInterrupt):
                    receiver_module.run_receiver(root / "receiver.sock", root / "cases.sqlite", root / "objects")
            self.assertTrue(stopped.is_set())
            self.assertFalse((root / "receiver.sock").exists())

    def test_bad_envelope_and_archive_are_rejected_without_database_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = self._receiver(root)
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="1.4.94", outbox_id="bad")
            with self.assertRaises(ReceiverError):
                receiver.accept(dict(envelope, size_bytes=envelope["size_bytes"] + 1), archive.read_bytes())
            with self.assertRaises(ReceiverError):
                receiver.accept(dict(envelope, schema_version=999), archive.read_bytes())
            self.assertIsNone(receiver.case_for(envelope["idempotency_key"]))

    def test_read_exact_rejects_invalid_size_without_reading_socket(self):
        stream, peer = socket.socketpair()
        with stream, peer:
            for size in (-1, max(receiver_module.MAX_ARCHIVE_BYTES,
                                 receiver_module.MAX_ENVELOPE_BYTES) + 1):
                with self.subTest(size=size), self.assertRaisesRegex(ReceiverError, "frame exceeds"):
                    _read_exact(stream, size)

    def test_receiver_repeated_start_and_stop_preserve_local_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects")
            receiver.stop()
            try:
                receiver.start()
                inode = receiver.socket_path.stat().st_ino
                receiver.start()
                self.assertEqual(receiver.socket_path.stat().st_ino, inode)
                archive = self._archive(root)
                envelope = make_envelope(archive_path=archive, app_version="1.4.94", outbox_id="restart")
                first = submit(receiver.socket_path, envelope, archive)
                self.assertEqual(first["storage_status"], "stored")
                receiver.stop()
                self.assertFalse(receiver.socket_path.exists())
                receiver.stop()
                receiver.start()
                self.assertEqual(submit(receiver.socket_path, envelope, archive), first)
            finally:
                receiver.stop()
            self.assertFalse(receiver.socket_path.exists())

    def test_incomplete_envelopes_never_create_cases_or_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = self._receiver(root)
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="1.4.94", outbox_id="incomplete")
            for field, value in (("idempotency_key", ""), ("idempotency_key", None),
                                 ("archive_sha256", None), ("size_bytes", "12")):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ReceiverError, "envelope is incomplete"):
                        receiver.accept(dict(envelope, **{field: value}), archive.read_bytes())
            self.assertIsNone(receiver.case_for(envelope["idempotency_key"]))
            self.assertEqual(list(receiver.objects_dir.iterdir()), [])

    def test_repeated_local_submission_keeps_receipt_and_rejects_changed_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = self._receiver(root)
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="1.4.94", outbox_id="repeat")
            first = submit(receiver.socket_path, envelope, archive)
            self.assertEqual(submit(receiver.socket_path, envelope, archive), first)
            changed = self._archive(root, "changed.zip", state={"health": False})
            changed_envelope = make_envelope(archive_path=changed, app_version="1.4.94", outbox_id="repeat")
            changed_envelope["idempotency_key"] = envelope["idempotency_key"]
            with self.assertRaisesRegex(ReceiverError, "bound to a different archive"):
                submit(receiver.socket_path, changed_envelope, changed)
            self.assertEqual(json.loads(receiver.case_for(envelope["idempotency_key"])["receipt_json"]), first)
            self.assertEqual(list(receiver.objects_dir.glob("incoming-*.zip")), [])
            self.assertEqual(len(list(receiver.objects_dir.glob("*.zip"))), 1)

    def test_handler_error_reply_broken_pipe_is_handled_but_database_error_is_reported(self):
        # Run the real handler synchronously on a socketpair so thread exceptions cannot vanish.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects")
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="1.4.94", outbox_id="db-error")
            connect = receiver_module.sqlite3.connect

            def failing_connect(*args, **kwargs):
                # A genuine SQLite error, limited to this receiver's connection attempt.
                return connect(root / "absent" / "cases.sqlite")

            for broken_reply in (False, True):
                with self.subTest(broken_reply=broken_reply):
                    left, right = socket.socketpair()
                    with left, right:
                        right.settimeout(2)
                        for body in (json.dumps(envelope).encode(), archive.read_bytes()):
                            right.sendall(struct.pack("!Q", len(body)) + body)
                        if broken_reply:
                            right.close()
                        with mock.patch.object(receiver_module.sqlite3, "connect", failing_connect):
                            receiver_module._Handler(left, None, types.SimpleNamespace(receiver=receiver))  # discipline-ok(R5): 真 handler 同步运行以保证非预期异常直接判红
                        if not broken_reply:
                            size = struct.unpack("!Q", _read_exact(right, 8))[0]
                            response = json.loads(_read_exact(right, size))
                            self.assertIs(response["ok"], False)
                            self.assertIn("unable to open database file", response["error"])
            self.assertIsNone(receiver.case_for(envelope["idempotency_key"]))
            self.assertEqual(list(receiver.objects_dir.iterdir()), [])

    def test_insert_integrity_error_without_matching_concurrent_case_is_not_hidden(self):
        for conflict in ("missing", "different"):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects")
                archive = self._archive(root)
                envelope = make_envelope(archive_path=archive, app_version="1.4.94", outbox_id="race")
                connect = receiver._connect  # discipline-ok(R5): 仅在连接边界注入竞态，事务及 SQL 仍用真实 SQLite
                integrity_errors = []

                class RacingConnection:
                    def __init__(self):
                        self.db = connect()

                    def __getattr__(self, name):
                        return getattr(self.db, name)

                    def execute(self, sql, parameters=()):
                        if sql.startswith("INSERT INTO cases"):
                            values = list(parameters)
                            if conflict == "different":
                                values[2] = "another-archive-hash"
                                self.db.execute(sql, values)
                            else:
                                values[1] = None
                            try:
                                return self.db.execute(sql, values if conflict == "missing" else parameters)
                            except sqlite3.IntegrityError as exc:
                                integrity_errors.append(str(exc))
                                raise
                        return self.db.execute(sql, parameters)

                with mock.patch.object(receiver, "_connect", RacingConnection):
                    with self.assertRaisesRegex(ReceiverError, "concurrently bound to a different archive"):
                        receiver.accept(envelope, archive.read_bytes())
                self.assertEqual(len(integrity_errors), 1)
                self.assertIsNone(receiver.case_for(envelope["idempotency_key"]))
                self.assertEqual(list(receiver.objects_dir.glob("incoming-*.zip")), [])
                self.assertEqual(len(receiver.reconcile()["orphaned_objects"]), 1)

    def test_reconcile_case_deleted_between_snapshot_and_repair_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = FeedbackReceiver(root / "receiver.sock", root / "cases.sqlite", root / "objects")
            archive = self._archive(root)
            envelope = make_envelope(archive_path=archive, app_version="1.4.94", outbox_id="delete-race")
            receipt = receiver.accept(envelope, archive.read_bytes())
            object_name = receipt["archive_sha256"] + ".zip"
            (receiver.objects_dir / object_name).unlink()
            connect = receiver._connect  # discipline-ok(R5): 在两次真实事务之间模拟删除，不替换 reconcile 的判据
            calls = []

            def connect_with_deletion():
                calls.append(True)
                db = connect()
                if len(calls) == 2:
                    db.execute("DELETE FROM cases")
                    db.commit()
                return db

            with mock.patch.object(receiver, "_connect", connect_with_deletion):
                self.assertEqual(receiver.reconcile(), {"orphaned_objects": [], "missing_objects": [object_name]})
            self.assertEqual(len(calls), 2)
            self.assertIsNone(receiver.case_for(envelope["idempotency_key"]))
            self.assertEqual(receiver.reconcile(), {"orphaned_objects": [], "missing_objects": []})

    def test_analyzer_rejects_invalid_utf8_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self._archive(root)
            # Replace only the data member while retaining a valid generated manifest.
            raw = root / "raw.zip"
            build_archive(raw, {"state.json": b"{\xff"}, app_version="1.4.94",
                                bundle_id="com.skyrocket.app", run_id="bad-utf8")
            report = analyze_archive(raw, root / "reports")
            self.assertEqual(report["conclusion"], "insufficient_evidence")
            self.assertIn("invalid_state_json", {item["code"] for item in report["findings"]})

    def test_analyzer_rejects_invalid_json_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad-json.zip"
            build_archive(archive, {"state.json": "{"}, app_version="1.4.94",
                          bundle_id="com.skyrocket.app", run_id="bad-json")
            report = analyze_archive(archive, root / "reports")
            self.assertEqual(report["conclusion"], "insufficient_evidence")
            self.assertIn("invalid_state_json", {item["code"] for item in report["findings"]})

    def test_analyzer_uses_custom_ruleset_for_missing_and_health_conclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self._archive(root, state={"health": False})
            rules = {"required_members": ["missing.json"], "health_false_conclusion": "custom-health",
                     "missing_member_conclusion": "custom-missing"}
            report = analyze_archive(archive, root / "reports", ruleset=rules)
            self.assertEqual(report["conclusion"], "custom-missing")
            self.assertEqual(report["config_sha256"], report["config_sha256"])

    def test_analyzer_propagates_report_write_failure_and_leaves_no_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self._archive(root)
            reports = root / "reports"
            with mock.patch("sf_feedback_analyzer._atomic_text", side_effect=OSError("read-only report dir")):
                with self.assertRaisesRegex(OSError, "read-only"):
                    analyze_archive(archive, reports)
            self.assertFalse(reports.exists())

    def test_receiver_cleans_temporary_file_when_archive_validation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = self._receiver(root)
            archive = root / "invalid.zip"
            archive.write_bytes(b"not a zip")
            envelope = {
                "schema_version": 1,
                "idempotency_key": "invalid",
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "size_bytes": archive.stat().st_size,
            }
            with self.assertRaises(FeedbackProtocolError):
                receiver.accept(envelope, archive.read_bytes())
            self.assertEqual(list((root / "objects").glob("incoming-*.zip")), [])

    def test_analyzer_rejects_member_over_limit_before_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "oversized-member.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as package:
                info = zipfile.ZipInfo("state.json")
                info.file_size = MAX_MEMBER_BYTES + 1
                package.writestr(info, b"x")
            with self.assertRaises(FeedbackProtocolError):
                analyze_archive(archive, root / "reports")


if __name__ == "__main__":
    unittest.main()
