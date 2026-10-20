import hashlib
import json
import math
import os
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from sf_feedback import (
    FeedbackProtocolError,
    PrivacyRejected,
    _check_json_shape,
    _json_bytes,
    _safe_member_name,
    build_archive,
    make_envelope,
    project_snapshot,
    sha256_file,
    validate_archive,
    validate_receipt,
)
from sf_feedback_outbox import FeedbackOutbox, OutboxConflict
from tests._base import SFCase


class FeedbackProtocolTests(SFCase):
    def test_directory_sync_failure_reports_uncertain_durability_and_preserves_archive(self):
        real_sync = os.fsync

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("injected directory sync failure")
            real_sync(fd)

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "feedback.zip"
            with patch("sf_feedback.os.fsync", side_effect=fail_directory):
                with self.assertRaisesRegex(FeedbackProtocolError, "directory durability"):
                    build_archive(path, {"state.json": "{}"}, app_version="test",
                                  bundle_id="com.skyrocket.app", run_id="sync-fault")
            self.assertEqual(validate_archive(path)["run_id"], "sync-fault")
            self.assertEqual(list(Path(root).glob("*.tmp")), [])

    def test_file_sync_failure_leaves_no_target_or_temporary_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "feedback.zip"
            with patch("sf_feedback.os.fsync", side_effect=OSError("file sync failure")):
                with self.assertRaisesRegex(OSError, "file sync failure"):
                    build_archive(path, {"state.json": "{}"}, app_version="test",
                                  bundle_id="com.skyrocket.app", run_id="sync-fault")
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_outbox_insert_failure_removes_copied_object_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.zip"
            build_archive(source, {"state.json": "{}"}, app_version="test",
                          bundle_id="com.skyrocket.app", run_id="insert-fault")
            database = root / "outbox.sqlite"
            objects = root / "objects"
            outbox = FeedbackOutbox(database, objects)
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("CREATE TRIGGER reject_insert BEFORE INSERT ON outbox "
                           "BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected insert failure"):
                outbox.enqueue(source, app_version="test", consent_policy_version="test")
            self.assertEqual(outbox.pending(), [])
            self.assertEqual(list(objects.iterdir()), [])

    def test_manifest_is_not_self_referential_and_round_trips_hashes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "feedback.zip"
            result = build_archive(path, {"state.json": '{"health":true}'},
                                   app_version="1.4.94", bundle_id="com.skyrocket.app", run_id="run-1")
            manifest = result["manifest"]
            self.assertEqual([item["name"] for item in manifest["files"]], ["state.json"])
            self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(b'{"health":true}').hexdigest())
            self.assertEqual(validate_archive(path)["run_id"], "run-1")

    def test_unknown_snapshot_field_is_rejected_not_silently_dropped(self):
        with self.assertRaises(PrivacyRejected):
            project_snapshot({"health": {}, "target_price": 12.3})

    def test_archive_rejects_path_traversal_and_symlink_entries(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "bad.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("../state.json", b"bad")
            with self.assertRaises(FeedbackProtocolError):
                validate_archive(path)

            with zipfile.ZipFile(path, "w") as archive:
                info = zipfile.ZipInfo("state.json")
                info.external_attr = (0o120777 << 16) | 0xA000
                archive.writestr(info, b"bad")
            with self.assertRaises(FeedbackProtocolError):
                validate_archive(path)

    def test_outbox_same_key_recovers_and_conflicting_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "source.zip"
            build_archive(source, {"state.json": "one"}, app_version="1.4.94",
                          bundle_id="com.skyrocket.app", run_id="a")
            outbox = FeedbackOutbox(root / "db" / "outbox.sqlite", root / "objects")
            first = outbox.enqueue(source, app_version="1.4.94", consent_policy_version="v1",
                                   idempotency_key="same-key")
            second = FeedbackOutbox(root / "db" / "outbox.sqlite", root / "objects").enqueue(
                source, app_version="1.4.94", consent_policy_version="v1", idempotency_key="same-key")
            self.assertEqual(first["outbox_id"], second["outbox_id"])
            self.assertEqual(second["status"], "locally_verified")
            self.assertEqual(len(outbox.pending()), 1)

            other = root / "other.zip"
            build_archive(other, {"state.json": "two"}, app_version="1.4.94",
                          bundle_id="com.skyrocket.app", run_id="b")
            with self.assertRaises(OutboxConflict):
                outbox.enqueue(other, app_version="1.4.94", consent_policy_version="v1",
                               idempotency_key="same-key")

    def test_outbox_rejects_state_regression_and_validates_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "source.zip"
            build_archive(source, {"state.json": "one"}, app_version="1.4.94",
                          bundle_id="com.skyrocket.app", run_id="a")
            outbox = FeedbackOutbox(root / "db.sqlite", root / "objects")
            row = outbox.enqueue(source, app_version="1.4.94", consent_policy_version="v1")
            with self.assertRaises(FeedbackProtocolError):
                outbox.transition(row["outbox_id"], "cleaned")
            outbox.transition(row["outbox_id"], "submitting")
            with self.assertRaises(FeedbackProtocolError):
                outbox.persist_receipt(row["outbox_id"], {"case_id": "wrong"})
            envelope = outbox.get(row["outbox_id"])["envelope"]
            receipt = {"case_id": "case-1", "archive_sha256": envelope["archive_sha256"],
                       "size_bytes": envelope["size_bytes"], "received_at": "2026-10-17T00:00:00Z",
                       "storage_status": "stored", "validation_status": "pending",
                       "retention_until": "2026-11-17T00:00:00Z"}
            self.assertEqual(outbox.persist_receipt(row["outbox_id"], receipt)["status"], "receipt_persisted")

    def test_projection_rejects_invalid_shapes_and_json_values(self):
        for snapshot in (None, {"health": object()}):
            with self.subTest(snapshot=snapshot), self.assertRaises(PrivacyRejected):
                project_snapshot(snapshot)
        self.assertTrue(math.isnan(project_snapshot({"health": float("nan")})["health"]))
        with self.assertRaises(PrivacyRejected):
            project_snapshot({"health": {"nested": {"x": {"y": {"z": {"a": {"b": {"c": {"d": {"e": 1}}}}}}}}}})
        with self.assertRaises(PrivacyRejected):
            project_snapshot({"health": {"x": "x" * 4097}})
        with self.assertRaises(PrivacyRejected):
            project_snapshot({"health": {str(i): i for i in range(65)}})
        with self.assertRaises(PrivacyRejected):
            project_snapshot({"health": [0] * 129})

    def test_archive_builder_rejects_member_limits_collisions_and_manifest_input(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "archive.zip"
            cases = [
                ({"manifest.json": b"x"}, "manifest"),
                ({}, "contain"),
                ({"a": b"x", "A": b"y"}, "collision"),
                ({"a": b"x" * (2 * 1024 * 1024 + 1)}, "size"),
                ({"../a": b"x"}, "unsafe"),
                ({"a\\b": b"x"}, "unsafe"),
            ]
            for files, text in cases:
                with self.subTest(text=text), self.assertRaises(FeedbackProtocolError):
                    build_archive(path, files, app_version="test", bundle_id="id", run_id="run")

    def test_validate_archive_rejects_manifest_variants_and_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            path = root / "bad.zip"
            variants = [
                {"state.json": b"{}", "manifest.json": b"{"},
                {"state.json": b"{}", "manifest.json": b"[]"},
                {"state.json": b"{}", "manifest.json": b'{"schema_version":99}'},
                {"state.json": b"{}", "manifest.json": b'{"schema_version":1,"diagnostic_payload_version":1,"files":[]}'},
            ]
            for members in variants:
                with zipfile.ZipFile(path, "w") as archive:
                    for name, data in members.items():
                        archive.writestr(name, data)
                with self.subTest(members=members), self.assertRaises(FeedbackProtocolError):
                    validate_archive(path)
            build_archive(path, {"state.json": "{}"}, app_version="test", bundle_id="id", run_id="run")
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("state.json", b"changed")
            with self.assertRaises(FeedbackProtocolError):
                validate_archive(path)

    def test_protocol_helpers_reject_bad_values_and_limits(self):
        with self.assertRaises(FeedbackProtocolError):
            _json_bytes({"bad": float("nan")})
        with self.assertRaises(PrivacyRejected):
            _check_json_shape({"x": {"y": {"z": {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}}}})
        with self.assertRaises(FeedbackProtocolError):
            _safe_member_name("~bad")
        with self.assertRaises(FeedbackProtocolError):
            _safe_member_name("bad//name")
        with self.assertRaises(FeedbackProtocolError):
            _safe_member_name("é")
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(FileNotFoundError):
                sha256_file(Path(root) / "missing")

    def test_validate_archive_rejects_oversized_and_missing_files(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "bad.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("manifest.json", b'{}')
            with self.assertRaises(FeedbackProtocolError):
                validate_archive(path)


        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "ok.zip"
            build_archive(path, {"state.json": "{}"}, app_version="test", bundle_id="id", run_id="run")
            envelope = make_envelope(archive_path=path, app_version="test", outbox_id="outbox")
            with self.assertRaises(FeedbackProtocolError):
                validate_receipt({}, envelope)
            bad = {key: "x" for key in ("case_id", "archive_sha256", "size_bytes", "received_at",
                                        "storage_status", "validation_status", "retention_until")}
            with self.assertRaises(FeedbackProtocolError):
                validate_receipt(bad, envelope)
            bad = {"case_id": "x" * 129, "archive_sha256": envelope["archive_sha256"],
                   "size_bytes": envelope["size_bytes"], "received_at": "now", "storage_status": "stored",
                   "validation_status": "validated", "retention_until": "later"}
            with self.assertRaises(FeedbackProtocolError):
                validate_receipt(bad, envelope)

    def test_validate_archive_rejects_extreme_manifest_and_zip_members(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cases = []
            cases.append((root / "empty.zip", lambda z: None))
            cases = []
            cases.append((root / "many.zip", lambda z: [z.writestr(f"f{i}", b"") for i in range(65)]))
            for path, writer in cases:
                with zipfile.ZipFile(path, "w") as archive:
                    writer(archive)
                with self.assertRaises(FeedbackProtocolError):
                    validate_archive(path)

            path = root / "manifest-cases.zip"
            variants = [
                {"state.json": b"{}"},
                {"state.json": b"{}", "manifest.json": b"x" * (64 * 1024 + 1)},
                {"state.json": b"{}", "manifest.json": b'{"schema_version":1,"diagnostic_payload_version":1,"files":[1]}'},
                {"state.json": b"{}", "manifest.json": b'{"schema_version":1,"diagnostic_payload_version":1,"files":[{"name":null,"size_bytes":0,"sha256":"' + b"0" * 64 + b'"}]}'},
                {"state.json": b"{}", "manifest.json": b'{"schema_version":1,"diagnostic_payload_version":1,"files":[{"name":"state.json","size_bytes":0,"sha256":"bad"}]}'},
                {"state.json": b"{}", "manifest.json": b'{"schema_version":1,"diagnostic_payload_version":1,"files":[]}'},
            ]
            for members in variants:
                with zipfile.ZipFile(path, "w") as archive:
                    for name, data in members.items():
                        archive.writestr(name, data)
                with self.subTest(members=members), self.assertRaises(FeedbackProtocolError):
                    validate_archive(path)

            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("state.json", b"{}")
                archive.writestr("manifest.json", b'{"schema_version":1,"diagnostic_payload_version":1,"files":[{"name":"state.json","size_bytes":1,"sha256":"' + b"0" * 64 + b'"}]}')
            with self.assertRaises(FeedbackProtocolError):
                validate_archive(path)
            path.write_bytes(b"not a zip")
            with self.assertRaises(FeedbackProtocolError):
                validate_archive(path)

    def test_projection_accepts_nested_json_and_builder_rejects_huge_manifest(self):
        self.assertEqual(project_snapshot({"health": {"nested": [True, None, {"x": 1}]}})["health"]["nested"][0], True)
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(FeedbackProtocolError):
                build_archive(Path(root) / "too-large-manifest.zip", {"state.json": "{}"},
                              app_version="test", bundle_id="id", run_id="run",
                              omitted=["x" * 4096] * 64)

    def test_projection_rejects_invalid_nested_field_names(self):
        for key in (1, "", "x" * 97):
            with self.subTest(key=key), self.assertRaisesRegex(PrivacyRejected, "field name"):
                project_snapshot({"health": {key: True}})

    def test_builder_rejects_empty_and_overlong_member_names(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "invalid.zip"
            for name in ("", "x" * 241):
                with self.subTest(name=name), self.assertRaisesRegex(FeedbackProtocolError, "member name"):
                    build_archive(target, {name: b"x"}, app_version="test", bundle_id="id", run_id="run")
                self.assertFalse(target.exists())

    def test_validator_rejects_archive_and_member_size_limits(self):
        from sf_feedback import MAX_FULL_ARCHIVE_BYTES, MAX_MEMBER_BYTES
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.zip"
            path.touch()
            with self.assertRaisesRegex(FeedbackProtocolError, "archive exceeds"):
                validate_archive(path)
            with path.open("wb") as stream:
                stream.truncate(MAX_FULL_ARCHIVE_BYTES + 1)
            with self.assertRaisesRegex(FeedbackProtocolError, "archive exceeds"):
                validate_archive(path)
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("state.json", b"x" * (MAX_MEMBER_BYTES + 1))
            with self.assertRaisesRegex(FeedbackProtocolError, "member exceeds"):
                validate_archive(path)

    def test_validator_rejects_manifest_collisions_and_undeclared_members(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.zip"
            entry = {"name": "a.json", "size_bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}
            for entries, message in (
                ([dict(entry, name="manifest.json"), entry], "collision"),
                ([entry, entry], "collision"),
                ([entry, dict(entry, name="A.json")], "collision"),
                ([entry, dict(entry, name="absent.json")], "incomplete"),
            ):
                manifest = {"schema_version": 1, "diagnostic_payload_version": 1, "files": entries}
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("a.json", b"")
                    archive.writestr("b.json", b"")
                    archive.writestr("manifest.json", json.dumps(manifest))
                with self.subTest(entries=entries), self.assertRaisesRegex(FeedbackProtocolError, message):
                    validate_archive(path)

    def test_outbox_missing_row_invalid_status_and_premature_receipt_do_not_mutate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.zip"
            build_archive(source, {"state.json": "{}"}, app_version="test", bundle_id="id", run_id="run")
            outbox = FeedbackOutbox(root / "outbox.sqlite", root / "objects")
            with self.assertRaises(KeyError):
                outbox.get("missing")
            row = outbox.enqueue(source, app_version="test", consent_policy_version="v1")
            with self.assertRaisesRegex(FeedbackProtocolError, "unknown outbox status"):
                outbox.transition(row["outbox_id"], "unknown")
            with self.assertRaisesRegex(FeedbackProtocolError, "cannot be persisted"):
                outbox.persist_receipt(row["outbox_id"], {})
            self.assertEqual(outbox.get(row["outbox_id"]), {k: v for k, v in row.items() if k != "manifest"})

    def test_outbox_orphan_reconciliation_preserves_referenced_and_unreferenced_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.zip"
            build_archive(source, {"state.json": "{}"}, app_version="test", bundle_id="id", run_id="run")
            outbox = FeedbackOutbox(root / "outbox.sqlite", root / "objects")
            row = outbox.enqueue(source, app_version="test", consent_policy_version="v1")
            orphan = outbox.objects_dir / "orphan.zip"
            orphan.write_bytes(b"orphan")
            unrelated = outbox.objects_dir / "unfinished.tmp"
            unrelated.write_bytes(b"unfinished")
            self.assertEqual(outbox.reconcile_orphans(), [str(orphan)])
            self.assertEqual(Path(row["archive_path"]).read_bytes(), source.read_bytes())
            self.assertEqual(orphan.read_bytes(), b"orphan")
            self.assertEqual(unrelated.read_bytes(), b"unfinished")


if __name__ == "__main__":
    unittest.main()
