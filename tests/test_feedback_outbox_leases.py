"""Pre-implementation acceptance tests for outbox leases and fencing."""

from __future__ import annotations

import fcntl
import shutil
import sqlite3
import tempfile
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sf_feedback as protocol
from sf_feedback_outbox import FeedbackOutbox, OutboxBusy, OutboxConflict
from sf_feedback_transport import submit_pending
from tests._base import SFCase


class OutboxLeaseTests(SFCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="outbox-lease-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "source.zip"
        protocol.build_archive(self.archive, {"state.json": "fixture"}, app_version="fixture",
                               bundle_id="com.skyrocket.app", run_id="fixture")
        self.store = FeedbackOutbox(self.root / "outbox.sqlite3", self.root / "objects")
        self.row = self.store.enqueue(self.archive, app_version="fixture", consent_policy_version="fixture")
        self.clock = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)

    def claim(self, token="a" * 32, seconds=30):
        return self.store.claim(self.row["outbox_id"], kind="submit", statuses=("locally_verified",),
                                target_status="submitting", token=token, lease_seconds=seconds, now=self.clock)

    def test_cancel_recovery_rejects_when_flock_support_is_missing(self):
        with mock.patch("sf_feedback_outbox.fcntl", None):
            with self.assertRaises(protocol.FeedbackProtocolError):
                self.store.cancel_local(self.row["outbox_id"])

    def test_cancel_recovery_skips_a_row_resolved_by_another_worker(self):
        claimed = self.store.claim(self.row["outbox_id"], kind="cancel",
                                   statuses=("locally_verified",), target_status="cancel_pending",
                                   token="c" * 32, lease_seconds=1, now=self.clock)
        lock = Path(claimed["archive_path"]).with_suffix(".cancel.lock")
        with lock.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        self.store.finish_claim(self.row["outbox_id"], token=claimed["lease_token"],
                                fence=claimed["lease_fence"], status="cancelled", now=self.clock)
        self.assertEqual(self.store.recover_interrupted(now=self.clock), 0)

    def test_cancel_pending_recovery_keeps_expired_lease_while_cancel_lock_is_held(self):
        claimed = self.store.claim(self.row["outbox_id"], kind="cancel",
                                   statuses=("locally_verified",), target_status="cancel_pending",
                                   token="c" * 32, lease_seconds=1, now=self.clock)
        lock_path = Path(claimed["archive_path"]).with_suffix(".cancel.lock")
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            expired = self.clock + timedelta(seconds=2)
            self.assertEqual(self.store.recover_interrupted(now=expired), 0)
        still_owned = self.store.get(self.row["outbox_id"])
        self.assertEqual(still_owned["status"], "cancel_pending")
        self.assertEqual(still_owned["lease_token"], claimed["lease_token"])
        self.assertTrue(Path(still_owned["archive_path"]).exists())


    def test_cancel_pending_recovery_acquires_lock_fences_expired_worker_and_cancels(self):
        claimed = self.store.claim(self.row["outbox_id"], kind="cancel",
                                   statuses=("locally_verified",), target_status="cancel_pending",
                                   token="c" * 32, lease_seconds=1, now=self.clock)
        archive = Path(claimed["archive_path"])
        expired = self.clock + timedelta(seconds=2)
        self.assertEqual(self.store.recover_interrupted(now=expired), 1)
        recovered = self.store.get(self.row["outbox_id"])
        self.assertEqual(recovered["status"], "cancelled")
        self.assertFalse(archive.exists())
        self.assertGreater(recovered["lease_fence"], claimed["lease_fence"])
        self.assertIsNone(recovered["lease_token"])

    def test_cancel_pending_recovery_uses_unlocked_cancel_lock_over_live_database_lease(self):
        claimed = self.store.claim(self.row["outbox_id"], kind="cancel",
                                   statuses=("locally_verified",), target_status="cancel_pending",
                                   token="c" * 32, lease_seconds=60, now=self.clock)
        archive = Path(claimed["archive_path"])
        self.assertEqual(self.store.recover_interrupted(now=self.clock), 1)
        recovered = self.store.get(self.row["outbox_id"])
        self.assertEqual(recovered["status"], "cancelled")
        self.assertFalse(archive.exists())
        self.assertGreater(recovered["lease_fence"], claimed["lease_fence"])

    def test_cancel_pending_recovery_delete_failure_stays_visible_and_non_uploadable(self):
        claimed = self.store.claim(self.row["outbox_id"], kind="cancel",
                                   statuses=("locally_verified",), target_status="cancel_pending",
                                   token="c" * 32, lease_seconds=1, now=self.clock)
        archive = Path(claimed["archive_path"])
        expired = self.clock + timedelta(seconds=2)
        with mock.patch("sf_feedback_outbox.Path.unlink", side_effect=OSError("synthetic retry failure")):
            self.assertEqual(self.store.recover_interrupted(now=expired), 1)
        pending = self.store.get(self.row["outbox_id"])
        self.assertEqual(pending["status"], "cancel_pending")
        self.assertEqual(pending["last_error"], "cancel_recovery_OSError")
        self.assertTrue(archive.exists())
        self.assertEqual([item["status"] for item in self.store.history(include_cleaned=False)],
                         ["cancel_pending"])
        self.assertEqual(self.store.pending(), [])
        with self.assertRaises(protocol.FeedbackProtocolError):
            submit_pending(self.store, self.row["outbox_id"], "https://receiver.example/v1/feedback",
                           sender=lambda *_: self.fail("cancel_pending must never submit"))

    def test_cancel_pending_recovery_records_delete_failure_without_restoring_upload(self):
        with mock.patch("sf_feedback_outbox.Path.unlink", side_effect=OSError("synthetic retry failure")):
            with self.assertRaises(OSError):
                self.store.cancel_local(self.row["outbox_id"])
        pending = self.store.get(self.row["outbox_id"])
        expired = self.clock - timedelta(seconds=1)
        with self.store._db() as db:  # discipline-ok(R5): seed an expired interrupted-cancel lease.
            db.execute("UPDATE outbox SET lease_until=? WHERE outbox_id=?",
                       (expired.isoformat(), pending["outbox_id"]))
        with mock.patch("sf_feedback_outbox.Path.unlink", side_effect=OSError("synthetic recovery failure")):
            self.assertEqual(self.store.recover_interrupted(now=self.clock, abandoned=True), 1)
        still_pending = self.store.get(self.row["outbox_id"])
        self.assertEqual(still_pending["status"], "cancel_pending")
        self.assertEqual(still_pending["last_error"], "cancel_recovery_OSError")
        self.assertIsNone(still_pending["lease_token"])
        self.assertTrue(Path(still_pending["archive_path"]).exists())
        self.assertEqual([item["status"] for item in self.store.history(include_cleaned=False)],
                         ["cancel_pending"])
        self.assertEqual(self.store.pending(), [])
        with self.assertRaises(protocol.FeedbackProtocolError):
            submit_pending(self.store, self.row["outbox_id"], "https://receiver.example/v1/feedback",
                           sender=lambda *_: self.fail("cancel_pending must never submit"))

    def test_cancel_local_deletes_response_unknown_archive_and_marks_cancelled(self):
        self.store.transition(self.row["outbox_id"], "submitting")
        unknown = self.store.transition(self.row["outbox_id"], "response_unknown")
        archive_path = Path(unknown["archive_path"])
        cancelled = self.store.cancel_local(unknown["outbox_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(archive_path.exists())

    def test_cancel_local_rejects_received_feedback(self):
        row = self.store.transition(self.row["outbox_id"], "submitting")
        receipt = {"case_id": "feedback-test", "archive_sha256": row["archive_sha256"],
                   "size_bytes": row["archive_size"], "received_at": "2026-10-24T00:00:00Z",
                   "storage_status": "stored", "validation_status": "validated",
                   "retention_until": "2026-11-23T00:00:00Z", "retention_policy": "auto_delete"}
        self.store.persist_receipt(row["outbox_id"], receipt)
        with self.assertRaises(protocol.FeedbackProtocolError):
            self.store.cancel_local(row["outbox_id"])
        self.assertEqual(self.store.get(row["outbox_id"])["status"], "receipt_persisted")

    def test_cancel_local_rejects_live_submission_lease(self):
        self.clock = datetime(2030, 10, 24, 10, 0, tzinfo=timezone.utc)
        claimed = self.claim()
        with self.assertRaises(OutboxBusy):
            self.store.cancel_local(self.row["outbox_id"])
        self.assertEqual(self.store.get(self.row["outbox_id"])["status"], "submitting")
        self.assertEqual(self.store.get(self.row["outbox_id"])["lease_token"], claimed["lease_token"])

    def test_cancel_pending_blocks_expired_lease_recovery_and_submit_attempts(self):
        with mock.patch("sf_feedback_outbox.os.fsync", side_effect=OSError("synthetic fsync failure")):
            with self.assertRaises(OSError):
                self.store.cancel_local(self.row["outbox_id"])
        cancelling = self.store.get(self.row["outbox_id"])
        self.assertEqual(cancelling["status"], "cancel_pending")
        expired = self.clock - timedelta(seconds=1)
        with self.store._db() as db:  # discipline-ok(R5): seed an expired interrupted-cancel lease.
            db.execute("UPDATE outbox SET lease_until=? WHERE outbox_id=?",
                       (expired.isoformat(), cancelling["outbox_id"]))
        self.assertEqual(self.store.recover_expired_leases(now=self.clock), 0)  # discipline-ok(R6): lease recovery count is the public result under test.
        with self.assertRaises(protocol.FeedbackProtocolError):
            submit_pending(self.store, cancelling["outbox_id"], "https://receiver.example/v1/feedback",
                           sender=lambda *_: self.fail("cancel_pending must never submit"))
        after = self.store.get(cancelling["outbox_id"])
        self.assertEqual(after["status"], "cancel_pending")
        self.assertEqual(after["lease_token"], cancelling["lease_token"])

    def test_interrupted_cancel_after_unlink_recovers_cancelled_when_fsync_fails(self):
        archive = Path(self.row["archive_path"])
        calls = 0

        def fail_directory_fsync(fd):
            nonlocal calls
            calls += 1
            raise OSError("synthetic fsync failure")

        with mock.patch("sf_feedback_outbox.os.fsync", side_effect=fail_directory_fsync):
            with self.assertRaises(OSError):
                self.store.cancel_local(self.row["outbox_id"])
        interrupted = self.store.get(self.row["outbox_id"])
        self.assertFalse(archive.exists())
        self.assertEqual(interrupted["status"], "cancel_pending")
        self.assertIn("cancel", interrupted["last_error"])
        expired = self.clock - timedelta(seconds=1)
        with self.store._db() as db:  # discipline-ok(R5): prove recovery waits for the cancel lease to expire.
            db.execute("UPDATE outbox SET lease_until=? WHERE outbox_id=?",
                       (expired.isoformat(), interrupted["outbox_id"]))
        self.assertEqual(self.store.recover_interrupted(now=self.clock, abandoned=True), 1)
        recovered = self.store.get(self.row["outbox_id"])
        self.assertEqual(recovered["status"], "cancelled")
        self.assertIn("cancel", recovered["last_error"])

    def test_interrupted_cancel_after_unlink_recovers_cancelled_when_final_commit_fails(self):
        archive = Path(self.row["archive_path"])
        with mock.patch.object(self.store, "finish_claim", side_effect=sqlite3.OperationalError("synthetic commit failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.cancel_local(self.row["outbox_id"])
        interrupted = self.store.get(self.row["outbox_id"])
        self.assertFalse(archive.exists())
        self.assertEqual(interrupted["status"], "cancel_pending")
        self.assertIn("cancel", interrupted["last_error"])
        expired = self.clock - timedelta(seconds=1)
        with self.store._db() as db:  # discipline-ok(R5): seed an expired interrupted-cancel lease.
            db.execute("UPDATE outbox SET lease_until=? WHERE outbox_id=?",
                       (expired.isoformat(), interrupted["outbox_id"]))
        self.assertEqual(self.store.recover_interrupted(now=self.clock, abandoned=True), 1)
        self.assertEqual(self.store.get(self.row["outbox_id"])["status"], "cancelled")

    def test_interrupted_cancel_before_unlink_keeps_pending_until_retry_deletes_archive(self):
        archive = Path(self.row["archive_path"])
        with mock.patch("sf_feedback_outbox.Path.unlink", side_effect=OSError("synthetic before unlink")):
            with self.assertRaises(OSError):
                self.store.cancel_local(self.row["outbox_id"])
        pending = self.store.get(self.row["outbox_id"])
        self.assertEqual(pending["status"], "cancel_pending")
        self.assertTrue(archive.exists())
        expired = self.clock - timedelta(seconds=1)
        with self.store._db() as db:  # discipline-ok(R5): seed an expired interrupted-cancel lease.
            db.execute("UPDATE outbox SET lease_until=? WHERE outbox_id=?",
                       (expired.isoformat(), pending["outbox_id"]))
        self.assertEqual(self.store.recover_interrupted(now=self.clock, abandoned=True), 1)
        recovered = self.store.get(self.row["outbox_id"])
        self.assertEqual(recovered["status"], "cancelled")
        self.assertFalse(archive.exists())
        self.assertIn("cancel", recovered["last_error"])

    def test_interrupted_cancel_retry_failure_remains_visible_and_not_uploadable(self):
        archive = Path(self.row["archive_path"])
        with mock.patch("sf_feedback_outbox.Path.unlink", side_effect=OSError("synthetic before unlink")):
            with self.assertRaises(OSError):
                self.store.cancel_local(self.row["outbox_id"])
        pending = self.store.get(self.row["outbox_id"])
        expired = self.clock - timedelta(seconds=1)
        with self.store._db() as db:  # discipline-ok(R5): seed an expired interrupted-cancel lease.
            db.execute("UPDATE outbox SET lease_until=? WHERE outbox_id=?",
                       (expired.isoformat(), pending["outbox_id"]))
        with mock.patch("sf_feedback_outbox.Path.unlink", side_effect=OSError("synthetic retry failure")):
            self.assertEqual(self.store.recover_interrupted(now=self.clock, abandoned=True), 1)
        still_pending = self.store.get(self.row["outbox_id"])
        self.assertEqual(still_pending["status"], "cancel_pending")
        self.assertTrue(archive.exists())
        self.assertEqual([item["status"] for item in self.store.history(include_cleaned=False)],
                         ["cancel_pending"])
        self.assertEqual(self.store.pending(), [])
        with self.assertRaises(protocol.FeedbackProtocolError):
            submit_pending(self.store, self.row["outbox_id"], "https://receiver.example/v1/feedback",
                           sender=lambda *_: self.fail("cancel_pending must never submit"))

    def test_cancel_pending_recovery_never_restores_uploadable_state_when_archive_remains(self):
        with mock.patch("sf_feedback_outbox.Path.unlink", side_effect=OSError("synthetic before unlink")):
            with self.assertRaises(OSError):
                self.store.cancel_local(self.row["outbox_id"])
        pending = self.store.get(self.row["outbox_id"])
        expired = self.clock - timedelta(seconds=1)
        with self.store._db() as db:  # discipline-ok(R5): model A's expired lease while A may still be alive.
            db.execute("UPDATE outbox SET lease_until=? WHERE outbox_id=?",
                       (expired.isoformat(), pending["outbox_id"]))
        with mock.patch("sf_feedback_outbox.Path.unlink", side_effect=OSError("A still blocks unlink")):
            self.assertEqual(self.store.recover_interrupted(now=self.clock), 1)
        after = self.store.get(self.row["outbox_id"])
        self.assertEqual(after["status"], "cancel_pending")
        self.assertTrue(Path(after["archive_path"]).exists())
        self.assertIsNone(after["lease_token"])
        with self.assertRaises(protocol.FeedbackProtocolError):
            submit_pending(self.store, self.row["outbox_id"], "https://receiver.example/v1/feedback",
                           sender=lambda *_: self.fail("expired cancel must remain non-uploadable"))

    def test_interrupted_cancel_with_archive_already_deleted_converges_to_cancelled(self):
        archive = Path(self.row["archive_path"])
        with mock.patch("sf_feedback_outbox.os.fsync", side_effect=OSError("synthetic fsync failure")):
            with self.assertRaises(OSError):
                self.store.cancel_local(self.row["outbox_id"])
        self.assertFalse(archive.exists())
        pending = self.store.get(self.row["outbox_id"])
        expired = self.clock - timedelta(seconds=1)
        with self.store._db() as db:  # discipline-ok(R5): seed an expired interrupted-cancel lease.
            db.execute("UPDATE outbox SET lease_until=? WHERE outbox_id=?",
                       (expired.isoformat(), pending["outbox_id"]))
        self.assertEqual(self.store.recover_interrupted(now=self.clock), 1)
        self.assertEqual(self.store.get(self.row["outbox_id"])["status"], "cancelled")

    def test_only_one_live_worker_can_claim_the_same_row(self):
        first = self.claim()
        self.assertEqual(first["lease_token"], "a" * 32)
        self.assertEqual(first["lease_kind"], "submit")
        self.assertEqual(first["lease_fence"], 1)
        with self.assertRaises(OutboxBusy):
            self.store.claim(self.row["outbox_id"], kind="submit", statuses=("locally_verified",),
                             token="b" * 32, now=self.clock)

    def test_expired_lease_can_be_recovered_and_fences_out_late_worker(self):
        first = self.claim(seconds=1)
        after_expiry = self.clock + timedelta(seconds=2)
        self.assertEqual(self.store.recover_expired_leases(now=after_expiry), 1)  # discipline-ok(R6): lease expiry count, not geometry.
        second = self.store.claim(self.row["outbox_id"], kind="submit", statuses=("submitting",),
                                  token="b" * 32, now=after_expiry)
        self.assertEqual(second["lease_fence"], first["lease_fence"] + 1)
        with self.assertRaises(OutboxBusy):
            self.store.finish_claim(self.row["outbox_id"], token=first["lease_token"],
                                    fence=first["lease_fence"], status="response_unknown",
                                    now=after_expiry)
        completed = self.store.finish_claim(self.row["outbox_id"], token=second["lease_token"],
                                            fence=second["lease_fence"], status="response_unknown",
                                            now=after_expiry)
        self.assertEqual(completed["status"], "response_unknown")
        self.assertIsNone(completed["lease_token"])

    def test_cleanup_claim_cannot_race_with_a_live_submission_claim(self):
        submission = self.claim()
        with self.assertRaises(OutboxBusy):
            self.store.claim(self.row["outbox_id"], kind="cleanup", statuses=("locally_verified",),
                             token="c" * 32, now=self.clock)
        self.store.finish_claim(self.row["outbox_id"], token=submission["lease_token"],
                                fence=submission["lease_fence"], status="response_unknown",
                                now=self.clock)
        cleanup = self.store.claim(self.row["outbox_id"], kind="cleanup", statuses=("response_unknown",),
                                   token="c" * 32, now=self.clock)
        self.assertEqual(cleanup["lease_kind"], "cleanup")

    def test_incompatible_status_and_unknown_id_are_not_claimable(self):
        with self.assertRaises(OutboxBusy):
            self.store.claim(self.row["outbox_id"], kind="cleanup", statuses=("cleaned",), now=self.clock)
        with self.assertRaises(protocol.FeedbackProtocolError):
            self.store.claim(self.row["outbox_id"], kind="submit", statuses=("locally_verified",),
                             target_status="cleaned", now=self.clock)
        with self.assertRaises(KeyError):
            self.store.claim("missing", kind="submit", statuses=("locally_verified",), now=self.clock)

    def test_history_never_reads_archive_and_cleanup_keeps_safe_receipt_metadata(self):
        row = self.store.transition(self.row["outbox_id"], "submitting")
        receipt = {"case_id": "feedback-test", "archive_sha256": row["archive_sha256"],
                   "size_bytes": row["archive_size"], "received_at": "2026-10-24T00:00:00Z",
                   "storage_status": "stored", "validation_status": "validated",
                   "retention_until": None, "retention_policy": "manual_deletion"}
        self.store.persist_receipt(row["outbox_id"], receipt)
        history = self.store.history()
        self.assertEqual(history[0]["case_id"], "feedback-test")
        self.assertNotIn("archive_path", history[0])
        cleaned = self.store.cleanup_local(row["outbox_id"])
        self.assertEqual(cleaned["status"], "cleaned")
        self.assertFalse(Path(row["archive_path"]).exists())
        record = self.store.history()[0]
        self.assertEqual(record["case_id"], "feedback-test")
        self.assertEqual(record["status"], "cleaned")
        # 界面用 include_cleaned=False：已清理的记录不再返回，但库里的回执元数据仍在（对账、去重要用）。
        self.assertEqual(self.store.history(include_cleaned=False), [])
        self.assertEqual(self.store.get(row["outbox_id"])["receipt"]["case_id"], "feedback-test")

    def test_hidden_cleaned_records_do_not_use_up_the_history_limit(self):
        keep = self.store.transition(self.row["outbox_id"], "submitting")
        # 再造一条会被清理的记录：清理会刷新 updated_at，排到最前面；若过滤放在界面里，limit=1 就只剩它。
        second_src = Path(keep["archive_path"]).parent / "second-source.zip"
        shutil.copy(keep["archive_path"], second_src)
        second = self.store.enqueue(second_src, app_version="fixture", consent_policy_version="fixture")
        for item in (keep, second):
            current = item if item["status"] == "submitting" else self.store.transition(item["outbox_id"], "submitting")
            receipt = {"case_id": "case-" + current["outbox_id"][:4], "archive_sha256": current["archive_sha256"],
                       "size_bytes": current["archive_size"], "received_at": "2026-10-24T00:00:00Z",
                       "storage_status": "stored", "validation_status": "validated",
                       "retention_until": None, "retention_policy": "manual_deletion"}
            self.store.persist_receipt(current["outbox_id"], receipt)
        self.store.cleanup_local(second["outbox_id"])
        shown = self.store.history(limit=1, include_cleaned=False)
        self.assertEqual([r["outbox_id"] for r in shown], [keep["outbox_id"]],
                         "已清理的记录占了名额，较早的未清理记录被挤出")

    def test_recovery_does_not_release_a_live_lease(self):
        self.claim(seconds=30)
        self.assertEqual(self.store.recover_expired_leases(now=self.clock + timedelta(seconds=29)), 0)  # discipline-ok(R6): live-lease recovery count, not geometry.
        self.assertEqual(self.store.get(self.row["outbox_id"])["lease_token"], "a" * 32)

    def test_startup_recovery_keeps_a_live_submission_lease_but_recovers_an_expired_one(self):
        live = self.claim(seconds=30)
        future = self.clock + timedelta(seconds=3600)
        with self.store._db() as db:  # discipline-ok(R5): seed cross-process lease deadlines for recovery.
            db.execute("UPDATE outbox SET lease_until=? WHERE outbox_id=?", (future.isoformat(), self.row["outbox_id"]))
        self.assertEqual(self.store.recover_interrupted(now=self.clock), 0)
        self.assertEqual(self.store.get(self.row["outbox_id"])["lease_token"], live["lease_token"])
        expired = self.clock - timedelta(seconds=1)
        with self.store._db() as db:  # discipline-ok(R5): seed an expired lease for deterministic startup recovery.
            db.execute("UPDATE outbox SET lease_until=? WHERE outbox_id=?", (expired.isoformat(), self.row["outbox_id"]))
        self.assertEqual(self.store.recover_interrupted(now=self.clock), 1)
        self.assertEqual(self.store.get(self.row["outbox_id"])["status"], "response_unknown")

    def test_interrupted_cleanup_preserves_receipt_and_recovers_from_archive_fact(self):
        claimed = self.store.claim(self.row["outbox_id"], kind="submit", statuses=("locally_verified",),
                                   target_status="submitting", token="a" * 32, now=self.clock)
        receipt = {"case_id": "feedback-test", "archive_sha256": claimed["archive_sha256"],
                   "size_bytes": claimed["archive_size"], "received_at": "2026-10-24T00:00:00Z",
                   "storage_status": "stored", "validation_status": "validated",
                   "retention_until": None, "retention_policy": "manual_deletion"}
        self.store.persist_claimed_receipt(claimed["outbox_id"], receipt,
                                           token=claimed["lease_token"], fence=claimed["lease_fence"],
                                           now=self.clock)
        cleanup = self.store.claim(claimed["outbox_id"], kind="cleanup", statuses=("receipt_persisted",),
                                   target_status="cleanup_pending", token="c" * 32, now=self.clock)
        expired = self.clock - timedelta(seconds=1)
        with self.store._db() as db:  # discipline-ok(R5): seed a crashed cleanup lease without invoking deletion.
            db.execute("UPDATE outbox SET lease_until=? WHERE outbox_id=?", (expired.isoformat(), cleanup["outbox_id"]))
        self.assertEqual(self.store.recover_interrupted(now=self.clock), 1)
        recovered = self.store.get(cleanup["outbox_id"])
        self.assertEqual(recovered["status"], "receipt_persisted")
        self.assertEqual(recovered["receipt"]["case_id"], "feedback-test")
        Path(recovered["archive_path"]).unlink()
        self.assertEqual(self.store.recover_interrupted(now=self.clock), 0)
        with self.store._db() as db:  # discipline-ok(R5): simulate process death after deletion before status commit.
            db.execute("UPDATE outbox SET status='cleanup_pending', lease_until=? WHERE outbox_id=?",
                       (expired.isoformat(), cleanup["outbox_id"]))
        self.assertEqual(self.store.recover_interrupted(now=self.clock), 1)
        self.assertEqual(self.store.get(cleanup["outbox_id"])["status"], "cleaned")


class LegacyEntryPointsRespectTheLease(OutboxLeaseTests):
    """旧入口不得绕过租约：领到租约的工人还在跑时，第三者不能改这一行。

    `claim` / `finish_claim` 之外还留着 `transition` 与 `persist_receipt`（历史调用形状）。
    它们先前只按状态机判断，不看这行此刻在谁手里 —— 晚到的工人可以把新工人的结果覆盖掉，
    这正是租约本来要治的那件事。现在租约条件写进同一条 UPDATE，不留先读后写的窗口。
    """

    def receipt(self):
        row = self.store.get(self.row["outbox_id"])
        return {"case_id": "feedback-test", "archive_sha256": row["archive_sha256"],
                "size_bytes": row["archive_size"], "received_at": "2026-10-24T00:00:00Z",
                "storage_status": "stored", "validation_status": "validated",
                "retention_until": None, "retention_policy": "manual_deletion"}

    def release_lease_without_changing_status(self):
        with self.store._db() as db:  # discipline-ok(R5): 造「租约已过期」只能直接写这行。
            db.execute("UPDATE outbox SET lease_until=? WHERE outbox_id=?",
                       ((self.clock - timedelta(seconds=1)).isoformat(), self.row["outbox_id"]))

    def test_invalid_timestamp_inside_a_live_lease_is_treated_as_busy(self):
        self.claim()
        with self.store._db() as db:  # discipline-ok(R5): 注入损坏 lease 时间只能直接操作测试数据库
            db.execute("UPDATE outbox SET lease_until=? WHERE outbox_id=?",
                       ("not-a-timestamp", self.row["outbox_id"]))
        with self.assertRaises(OutboxBusy):
            self.store.transition(self.row["outbox_id"], "response_unknown", now=self.clock)

    def test_conflicting_enqueue_reuses_matching_existing_record(self):
        original = self.store.enqueue
        copied = self.root / "copy.zip"
        copied.write_bytes(self.archive.read_bytes())
        key = "b" * 32
        existing = original(copied, app_version="fixture", consent_policy_version="fixture",
                            idempotency_key=key)
        # discipline-ok(R5): 仅私有封存入口能种唯一键竞争后的临时文件清理。
        with mock.patch.object(self.store, "find_by_idempotency_key", return_value=existing), \
                mock.patch.object(self.store, "_copy_archive", return_value=self.root / "race.zip"), \
                mock.patch.object(self.store, "_db") as database:
            database.return_value.__enter__.return_value.execute.side_effect = sqlite3.IntegrityError("unique")
            row = self.store.enqueue(self.archive, app_version="fixture", consent_policy_version="fixture",
                                     idempotency_key=key)
        self.assertEqual(row["outbox_id"], existing["outbox_id"])

    def test_conflicting_enqueue_rejects_different_existing_archive(self):
        other = dict(self.row, archive_sha256="0" * 64)
        # discipline-ok(R5): 同上，必须让唯一键冲突发生在封存副本创建之后。
        with mock.patch.object(self.store, "find_by_idempotency_key", return_value=other), \
                mock.patch.object(self.store, "_copy_archive", return_value=self.root / "race.zip"), \
                mock.patch.object(self.store, "_db") as database:
            database.return_value.__enter__.return_value.execute.side_effect = sqlite3.IntegrityError("unique")
            with self.assertRaises(OutboxConflict):
                self.store.enqueue(self.archive, app_version="fixture", consent_policy_version="fixture",
                                   idempotency_key="c" * 32)

    def test_refusal_without_lease_reports_the_state_transition(self):
        self.store.transition(self.row["outbox_id"], "submitting", now=self.clock)
        stale = self.store.get(self.row["outbox_id"])
        with self.store._db() as db:  # discipline-ok(R5): 仅直接写库能让首次读取与实际状态之间形成无 lease 竞争窗口。
            db.execute("UPDATE outbox SET status='response_unknown' WHERE outbox_id=?", (self.row["outbox_id"],))
        real_get = self.store.get  # discipline-ok(R5): 只替首次读取造竞争窗口，第二次必须读真实数据库状态。
        with mock.patch.object(self.store, "get", side_effect=[stale, real_get(self.row["outbox_id"])]):
            with self.assertRaises(protocol.FeedbackProtocolError) as raised:
                self.store.transition(self.row["outbox_id"], "receipt_persisted", now=self.clock)
        self.assertIn("response_unknown -> receipt_persisted", str(raised.exception))

    def test_refusal_without_lease_rejects_the_actual_state(self):
        with self.store._db() as db:  # discipline-ok(R5): 直接调用私有拒绝分支以覆盖无 lease 的竞争结果。
            with self.assertRaises(protocol.FeedbackProtocolError) as raised:
                self.store._refusal(  # discipline-ok(R5): 必须直接驱动无 lease 的内部拒绝分支，公共入口会先做状态更新。
                    db, self.row["outbox_id"], "2026-10-24T10:00:00+00:00", "submitting")
        self.assertIn("locally_verified -> submitting", str(raised.exception))

    def test_utc_stamp_parser_normalizes_z_suffix(self):
        self.assertEqual(FeedbackOutbox.__module__, "sf_feedback_outbox")
        from sf_feedback_outbox import _utc_clock_from_stamp
        self.assertEqual(_utc_clock_from_stamp("2026-10-24T10:00:00Z"), self.clock)

    def test_transition_is_refused_while_a_live_lease_is_held(self):
        self.claim()
        with self.assertRaises(OutboxBusy):
            self.store.transition(self.row["outbox_id"], "response_unknown", now=self.clock)
        after = self.store.get(self.row["outbox_id"])
        self.assertEqual(after["status"], "submitting")
        self.assertEqual(after["lease_token"], "a" * 32)

    def test_persist_receipt_is_refused_while_a_live_lease_is_held(self):
        self.claim()
        with self.assertRaises(OutboxBusy):
            self.store.persist_receipt(self.row["outbox_id"], self.receipt(), now=self.clock)
        after = self.store.get(self.row["outbox_id"])
        self.assertEqual(after["status"], "submitting")
        self.assertIsNone(after.get("receipt"))

    def test_both_entries_require_the_lease_to_be_explicitly_recovered(self):
        self.claim()
        self.release_lease_without_changing_status()
        with self.assertRaises(OutboxBusy):
            self.store.transition(self.row["outbox_id"], "response_unknown", now=self.clock)
        with self.assertRaises(OutboxBusy):
            self.store.persist_receipt(self.row["outbox_id"], self.receipt(), now=self.clock)
        self.assertEqual(self.store.recover_expired_leases(now=self.clock), 1,  # discipline-ok(R6): 验过期恢复行数，不是布局裸数字。
                         "过期 lease 必须先被恢复，旧入口才允许推进状态")
        self.assertEqual(self.store.transition(self.row["outbox_id"], "response_unknown",
                                               now=self.clock)["status"], "response_unknown")

    def test_persist_receipt_race_reports_the_refusal_from_the_database(self):
        """旧入口的最后一次 UPDATE 失去竞态时，必须走统一拒绝解释器。"""
        self.store.transition(self.row["outbox_id"], "submitting", now=self.clock)
        receipt = self.receipt()
        current = self.store.get(self.row["outbox_id"])
        db = mock.MagicMock()
        db.__enter__.return_value.execute.return_value.rowcount = 0
        refusal = protocol.FeedbackProtocolError("race lost")
        with mock.patch.object(self.store, "_db", return_value=db), \
             mock.patch.object(self.store, "get", side_effect=[current, current]), \
             mock.patch.object(self.store, "_refusal", side_effect=refusal) as explain, \
             self.assertRaisesRegex(protocol.FeedbackProtocolError, "race lost"):
            self.store.persist_receipt(self.row["outbox_id"], receipt, now=self.clock)
        explain.assert_called_once_with(db.__enter__.return_value, self.row["outbox_id"],
                                        self.clock.isoformat(), "receipt_persisted",
                                        expected_status="submitting")


    def test_persist_receipt_refuses_a_naive_clock_before_writing(self):
        """无时区的时间一律拒绝：租约过没过期全靠这一串字符串比较，naive 会算错。

        必须先进入 submitting 才走得到这一刻 —— 状态不对时它会先被状态机拦下，
        那条路径测不到这个守卫（上一版用例就是这么假绿的）。
        """
        self.claim()
        with self.assertRaises(protocol.FeedbackProtocolError) as raised:
            self.store.persist_receipt(self.row["outbox_id"], self.receipt(),
                                       now=datetime(2026, 10, 24, 10, 0))
        self.assertIn("requires timezone", str(raised.exception))
        after = self.store.get(self.row["outbox_id"])
        self.assertEqual(after["status"], "submitting")
        self.assertIsNone(after.get("receipt"), "被拒的这次回执不许留下一半状态")

    def test_the_refusal_names_the_real_reason_when_the_status_moved_under_us(self):
        """状态被别人改走了要说「状态不是我们以为的那个」，不能推给租约。"""
        self.claim()
        self.release_lease_without_changing_status()
        stale = dict(self.store.get(self.row["outbox_id"]), status="response_unknown")
        with mock.patch.object(self.store, "get", return_value=stale):
            with self.assertRaises(protocol.FeedbackProtocolError) as raised:
                self.store.transition(self.row["outbox_id"], "receipt_persisted", now=self.clock)
        self.assertIn("submitting -> receipt_persisted", str(raised.exception))

    def test_a_row_that_disappeared_is_named_instead_of_silently_ignored(self):
        ghost = dict(self.store.get(self.row["outbox_id"]), outbox_id="missing-id")
        with mock.patch.object(self.store, "get", return_value=ghost):
            with self.assertRaises(KeyError):
                self.store.transition("missing-id", "submitting", now=self.clock)
