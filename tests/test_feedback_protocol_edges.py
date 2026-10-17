"""Edge candidates only; run with project venv and PYTHONDONTWRITEBYTECODE=1.

Set SF_FEEDBACK_REPO to test another checkout. No application imports, Qt app,
network, user paths, or repository writes are needed. Existing protocol tests'
small archive/receipt fixture shape is reused, not their happy-path assertions.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import sf_feedback as protocol
from sf_feedback_outbox import FeedbackOutbox
from tests._base import SFCase


class FeedbackEdgeCandidates(SFCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="feedback-edge-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.track_patch(mock.patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.archive = self.root / "source.zip"
        # Same minimal fixture as test_feedback_protocol; version is fixture data,
        # never an assertion about the product release.
        self.metadata = protocol.build_archive(
            self.archive, {"state.json": "one"}, app_version="fixture",
            bundle_id="com.skyrocket.app", run_id="edge-fixture")

    def test_valid_projection_returns_independent_allowlisted_snapshot(self):
        snapshot = {"health": True, "counts": {"frames": 3}}
        projected = protocol.project_snapshot(snapshot)
        self.assertEqual(projected, snapshot)
        self.assertIsNot(projected, snapshot)

    def store(self):
        return FeedbackOutbox(self.root / "db" / "outbox.sqlite", self.root / "objects")

    def enqueue(self, store):
        return store.enqueue(self.archive, app_version="fixture", consent_policy_version="v1")

    def receipt(self, row):
        envelope = row["envelope"]
        return {"case_id": "case-edge", "archive_sha256": envelope["archive_sha256"],
                "size_bytes": envelope["size_bytes"], "received_at": "2026-10-17T00:00:00Z",
                "storage_status": "stored", "validation_status": "pending",
                "retention_until": "2026-11-17T00:00:00Z"}

    def raw_zip(self, entries):
        target = self.root / "mutated.zip"
        with zipfile.ZipFile(target, "w") as archive:
            for name, value in entries:
                archive.writestr(name, value)
        return target

    def changed_manifest(self, change):
        manifest = copy.deepcopy(self.metadata["manifest"])
        change(manifest)
        return self.raw_zip([("state.json", b"one"), ("manifest.json", json.dumps(manifest))])

    def test_projection_shape_limits_reject_instead_of_truncating(self):
        nested = None
        for _ in range(10):
            nested = [nested]
        for payload in ([], {"health": {str(i): i for i in range(65)}},
                        {"health": {1: True}}, {"health": {"": True}},
                        {"health": {"x" * 97: True}}, {"counts": [0] * 129},
                        {"summary": "x" * 4097}, {"health": {1, 2}}, {"health": nested}):
            with self.subTest(payload_type=type(payload).__name__):
                with self.assertRaises(protocol.PrivacyRejected):
                    protocol.project_snapshot(payload)

    def test_builder_rejects_reserved_empty_and_case_colliding_members(self):
        for files in ({}, {"manifest.json": "{}"}, {"State.json": "a", "state.json": "b"}):
            with self.subTest(files=files):
                with self.assertRaises(protocol.FeedbackProtocolError):
                    protocol.build_archive(self.root / "rejected.zip", files,
                                           app_version="fixture", bundle_id="fixture", run_id="fixture")
                self.assertFalse((self.root / "rejected.zip").exists())

    def test_builder_member_and_manifest_limits_are_enforced(self):
        for limit, value, files in (("MAX_MEMBER_COUNT", 2, {"a": "a", "b": "b"}),
                                    ("MAX_MEMBER_BYTES", 2, {"a": "abc"}),
                                    ("MAX_MANIFEST_BYTES", 2, {"a": "a"})):
            with self.subTest(limit=limit), mock.patch.object(protocol, limit, value):
                with self.assertRaises(protocol.FeedbackProtocolError):
                    protocol.build_archive(self.root / "rejected.zip", files,
                                           app_version="fixture", bundle_id="fixture", run_id="fixture")
                self.assertFalse((self.root / "rejected.zip").exists())

    def test_nonfinite_or_unserializable_manifest_metadata_rejected(self):
        for value in (float("nan"), object()):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(protocol.FeedbackProtocolError, "serializable"):
                    protocol.build_archive(self.root / "rejected.zip", {"state.json": "one"},
                                           app_version="fixture", bundle_id="fixture", run_id="fixture",
                                           omitted=[value])
                self.assertFalse((self.root / "rejected.zip").exists())

    def test_unsafe_names_rejected_through_public_validator(self):
        for name in ("a" * 241, "a\\b", "a//b", "/root", "~root", "a/../b", "é.json", "e\u0301.json"):
            with self.subTest(name=name):
                with self.assertRaises(protocol.FeedbackProtocolError):
                    protocol.validate_archive(self.raw_zip([(name, b"bad")]))

    def test_validator_rejects_case_collision_and_missing_manifest(self):
        for entries, message in (([("State.json", b"a"), ("state.json", b"b")], "collision"),
                                 ([("state.json", b"a")], "manifest is missing")):
            with self.subTest(message=message):
                with self.assertRaisesRegex(protocol.FeedbackProtocolError, message):
                    protocol.validate_archive(self.raw_zip(entries))

    def test_validator_rejects_empty_and_nonzip_bytes(self):
        for data in (b"", b"not a zip"):
            self.archive.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(protocol.FeedbackProtocolError):
                protocol.validate_archive(self.archive)

    def test_validator_enforces_archive_member_and_manifest_budgets(self):
        for limit, value in (("MAX_ARCHIVE_BYTES", 1), ("MAX_MEMBER_COUNT", 1),
                             ("MAX_MEMBER_BYTES", 1), ("MAX_MANIFEST_BYTES", 1)):
            with self.subTest(limit=limit), mock.patch.object(protocol, limit, value):
                with self.assertRaises(protocol.FeedbackProtocolError):
                    protocol.validate_archive(self.archive)

    def test_manifest_decode_and_version_errors(self):
        for data in (b"\xff", b"{", b"[]", b'{"schema_version":999}'):
            with self.subTest(data=data), self.assertRaises(protocol.FeedbackProtocolError):
                protocol.validate_archive(self.raw_zip([("manifest.json", data)]))

    def test_manifest_entry_errors_are_rejected(self):
        changes = [lambda m: m.update(files=None), lambda m: m.update(files=[]),
                   lambda m: m.update(files=[None]),
                   lambda m: m["files"][0].update(name=4),
                   lambda m: m["files"][0].update(name="manifest.json"),
                   lambda m: m["files"][0].update(size_bytes="3"),
                   lambda m: m["files"][0].update(sha256="not-a-hash"),
                   lambda m: m["files"][0].update(name="other.json"),
                   lambda m: m["files"][0].update(size_bytes=99),
                   lambda m: m["files"][0].update(sha256="0" * 64)]
        for index, change in enumerate(changes):
            with self.subTest(case=index), self.assertRaises(protocol.FeedbackProtocolError):
                protocol.validate_archive(self.changed_manifest(change))

    def test_envelope_binds_real_bytes_and_preserves_explicit_identity(self):
        envelope = protocol.make_envelope(archive_path=self.archive, app_version="fixture",
                                          outbox_id="local-edge", idempotency_key="retry-edge",
                                          consent_policy_version="consent-edge")
        self.assertEqual(envelope["archive_sha256"], hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertEqual(envelope["size_bytes"], self.archive.stat().st_size)
        self.assertEqual((envelope["outbox_id"], envelope["idempotency_key"], envelope["consent_policy_version"]),
                         ("local-edge", "retry-edge", "consent-edge"))
        self.archive.write_bytes(b"invalid")
        with self.assertRaises(protocol.FeedbackProtocolError):
            protocol.make_envelope(archive_path=self.archive, app_version="fixture", outbox_id="bad")

    def test_receipt_mismatch_and_case_identity_rejected(self):
        row = self.enqueue(self.store())
        original = self.receipt(row)
        for key, value in (("archive_sha256", "0" * 64), ("size_bytes", original["size_bytes"] + 1),
                           ("case_id", 7), ("case_id", "x" * 129)):
            receipt = dict(original, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(protocol.FeedbackProtocolError):
                protocol.validate_receipt(receipt, row["envelope"])
        with self.assertRaises(protocol.FeedbackProtocolError):
            protocol.validate_receipt([], row["envelope"])

    def test_archive_atomic_replace_failure_preserves_old_bytes_and_removes_temporary(self):
        before = self.archive.read_bytes()
        with mock.patch("sf_feedback.os.replace", side_effect=OSError("replace denied")):
            with self.assertRaisesRegex(OSError, "replace denied"):
                protocol.build_archive(self.archive, {"state.json": "changed"},
                                       app_version="fixture", bundle_id="fixture", run_id="fixture")
        self.assertEqual(self.archive.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_archive_file_fsync_failure_preserves_existing_archive(self):
        before = self.archive.read_bytes()
        with mock.patch("sf_feedback.os.fsync", side_effect=OSError("fsync denied")):
            with self.assertRaisesRegex(OSError, "fsync denied"):
                protocol.build_archive(self.archive, {"state.json": "changed"},
                                       app_version="fixture", bundle_id="fixture", run_id="fixture")
        self.assertEqual(self.archive.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_directory_fsync_failure_does_not_invalidate_replaced_archive(self):
        fsync = os.fsync
        calls = []

        def fail_directory(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError("directory fsync unsupported")
            return fsync(fd)

        with mock.patch("sf_feedback.os.fsync", side_effect=fail_directory):
            with self.assertRaisesRegex(protocol.FeedbackProtocolError, "directory durability"):
                protocol.build_archive(self.archive, {"state.json": "changed"},
                                       app_version="fixture", bundle_id="fixture", run_id="fixture")
        self.assertEqual(len(calls), 2)
        self.assertEqual(protocol.validate_archive(self.archive)["run_id"], "fixture")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_missing_outbox_id_is_reported_without_creating_rows(self):
        store = self.store()
        for action in (lambda: store.get("absent"), lambda: store.transition("absent", "submitting"),
                       lambda: store.persist_receipt("absent", {})):
            with self.assertRaises(KeyError) as error:
                action()
            self.assertEqual(error.exception.args, ("absent",))
        self.assertEqual(store.pending(), [])

    def test_unknown_status_and_early_receipt_leave_row_unchanged(self):
        store = self.store()
        row = self.enqueue(store)
        for action in (lambda: store.transition(row["outbox_id"], "invented"),
                       lambda: store.persist_receipt(row["outbox_id"], self.receipt(row))):
            with self.assertRaises(protocol.FeedbackProtocolError):
                action()
            self.assertEqual(store.get(row["outbox_id"]), {k: v for k, v in row.items() if k != "manifest"})

    def test_response_unknown_retry_and_receipt_survive_reopening(self):
        store = self.store()
        row = self.enqueue(store)
        identity = row["outbox_id"]
        store.transition(identity, "submitting")
        unknown = store.transition(identity, "response_unknown", error="response lost")
        self.assertEqual(unknown["last_error"], "response lost")
        retried = store.transition(identity, "submitting")
        self.assertIsNone(retried["last_error"])
        self.assertEqual(retried["envelope"], row["envelope"])
        store.transition(identity, "response_unknown", error="response lost again")
        receipt = self.receipt(row)
        store.persist_receipt(identity, receipt)
        reopened = self.store()
        self.assertEqual(reopened.get(identity)["receipt"], receipt)
        reopened.transition(identity, "cleanup_pending")
        reopened.transition(identity, "cleaned")
        self.assertEqual(reopened.pending(), [])
        self.assertTrue(Path(row["archive_path"]).exists(), "transition alone is not file deletion")
        with self.assertRaises(protocol.FeedbackProtocolError):
            reopened.transition(identity, "submitting")

    def test_mismatched_receipt_does_not_advance_outbox(self):
        store = self.store()
        row = self.enqueue(store)
        identity = row["outbox_id"]
        before = store.transition(identity, "submitting")
        receipt = self.receipt(row)
        receipt["size_bytes"] += 1
        with self.assertRaises(protocol.FeedbackProtocolError):
            store.persist_receipt(identity, receipt)
        self.assertEqual(self.store().get(identity), before)

    def test_copy_replace_failure_rolls_back_and_removes_tempfile(self):
        store = self.store()
        with mock.patch("sf_feedback_outbox.os.replace", side_effect=OSError("copy replace denied")):
            with self.assertRaisesRegex(OSError, "copy replace denied"):
                self.enqueue(store)
        self.assertEqual(self.store().pending(), [])
        self.assertEqual(list((self.root / "objects").iterdir()), [])
        self.assertEqual(protocol.validate_archive(self.archive), self.metadata["manifest"])

    def test_copy_failure_rolls_back_and_removes_tempfile(self):
        store = self.store()
        with mock.patch("sf_feedback_outbox.shutil.copyfileobj", side_effect=OSError("copy interrupted")):
            with self.assertRaisesRegex(OSError, "copy interrupted"):
                self.enqueue(store)
        self.assertEqual(self.store().pending(), [])
        self.assertEqual(list((self.root / "objects").iterdir()), [])

    def test_sql_insert_failure_removes_copied_object_and_rolls_back(self):
        store = self.store()
        # Real SQLite failure, not a fake _db: trigger is confined to this temp database.
        with sqlite3.connect(self.root / "db" / "outbox.sqlite") as db:
            db.execute("""CREATE TRIGGER reject_candidate BEFORE INSERT ON outbox
                          BEGIN SELECT RAISE(ABORT, 'candidate insert rejected'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "candidate insert rejected"):
            self.enqueue(store)
        self.assertEqual(self.store().pending(), [])
        self.assertEqual(list((self.root / "objects").iterdir()), [])

    def test_orphan_reconciliation_reports_only_unreferenced_zips_without_deleting(self):
        store = self.store()
        row = self.enqueue(store)
        orphan = self.root / "objects" / "orphan.zip"
        orphan.write_bytes(b"orphan")
        other = self.root / "objects" / "inflight.tmp"
        other.write_bytes(b"inflight")
        self.assertEqual(store.reconcile_orphans(), [str(orphan)])
        self.assertEqual(orphan.read_bytes(), b"orphan")
        self.assertEqual(other.read_bytes(), b"inflight")
        self.assertTrue(Path(row["archive_path"]).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
