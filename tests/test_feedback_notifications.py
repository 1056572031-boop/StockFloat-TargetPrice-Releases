"""Operator notifications, retries, privacy and restart-safe deduplication."""
import io
import fcntl
import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from tests._base import SFCase
from tools import feedback_agent
import sf_feedback_notifications as notices

CASE = "feedback-" + "a" * 32
RUN = "b" * 32


class NotificationTests(SFCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sent = []
        self.notifier = notices.FeedbackNotifications(self.root, sender=self.sent.append)

    def test_reports_are_batched_and_deduplicated_across_restart_and_reanalysis(self):
        versions = {CASE: RUN, "feedback-" + "c" * 32: "d" * 32}
        self.assertEqual(self.notifier.sync(versions)["submitted"], ["reports"])
        self.assertIn("2", self.sent[0])
        restarted = notices.FeedbackNotifications(self.root, sender=self.sent.append)
        self.assertEqual(restarted.sync(versions)["submitted"], [])
        versions[CASE] = "e" * 32
        self.assertEqual(restarted.sync(versions)["submitted"], ["reports"])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.notifier.path.stat().st_mode & 0o777, 0o600)

    def test_state_lock_and_database_links_are_rejected_without_external_writes(self):
        outside = self.root / "outside"
        outside.write_text("untouched")
        for name in ("notifications.json", "notifications.lock"):
            path = self.root / name
            path.symlink_to(outside)
            self.assertEqual(self.notifier.sync({CASE: RUN})["failed"], ["state_unavailable"])
            if name == "notifications.json":
                with self.assertRaises(OSError):
                    self.notifier.baseline({})
            path.unlink()
        for name in ("queue.sqlite3", "queue.sqlite3-wal", "queue.sqlite3-shm"):
            path = self.root / name
            path.symlink_to(outside)
            with self.assertRaises(OSError):
                notices.report_versions(self.root)
            path.unlink()
        self.assertEqual(outside.read_text(), "untouched")
        self.assertEqual(self.sent, [])

    def test_failures_remind_once_then_remind_on_change_and_recovery(self):
        for failure, expected in (("backend:authentication", ["failure"]),
                                  ("backend:authentication", []),
                                  ("backend:processing", ["failure"]),
                                  ("backend:notification_index", ["failure"]),
                                  ("", ["recovery"]), ("", [])):
            with self.subTest(failure=failure):
                self.assertEqual(self.notifier.sync({}, failure)["submitted"], expected)
        self.assertEqual(len(self.sent), 4)

    def test_undelivered_reports_retry_even_when_agent_no_longer_reports_them_as_new(self):
        sender = Mock(side_effect=[TimeoutError("secret-not-to-log"), None, None])
        notifier = notices.FeedbackNotifications(self.root, sender=sender)
        first = notifier.sync({CASE: RUN}, "cases:failure")
        self.assertEqual(first, {"submitted": ["failure"], "failed": ["reports"]})
        second = notices.FeedbackNotifications(self.root, sender=sender).sync({CASE: RUN}, "cases:failure")
        self.assertEqual(second, {"submitted": ["reports"], "failed": []})
        self.assertNotIn("secret", json.dumps(first))

    def test_notification_and_state_write_failures_never_consume_the_event(self):
        with patch.object(self.notifier, "_write", side_effect=OSError("disk full")):
            self.assertEqual(self.notifier.sync({CASE: RUN})["failed"], ["reports"])
        self.assertFalse(self.notifier.path.exists())
        self.assertEqual(self.notifier.sync({CASE: RUN})["submitted"], ["reports"])
        with patch.object(self.notifier, "sender", side_effect=PermissionError):
            self.assertEqual(self.notifier.sync({}, "backend:authentication")["failed"], ["failure"])
        self.assertEqual(self.notifier.sync({}, "backend:authentication")["submitted"], ["failure"])
        with patch.object(self.notifier, "sender", side_effect=TimeoutError):
            self.assertEqual(self.notifier.sync({})["failed"], ["recovery"])
        self.assertEqual(self.notifier.sync({})["submitted"], ["recovery"])

    def test_corrupt_or_oversized_state_does_not_reset_history_or_send(self):
        invalid = ["not json", "[]", '{"schema":2,"reports":{},"failure":""}',
                   json.dumps({"schema": 1, "reports": {}, "failure": 1}),
                   json.dumps({"schema": 1, "reports": {}, "failure": "x" * 129}),
                   json.dumps({"schema": 1, "reports": {"wrong": RUN}, "failure": ""}),
                   "x" * (256 * 1024 + 1)]
        for body in invalid:
            with self.subTest(size=len(body)):
                self.notifier.path.write_text(body)
                self.assertEqual(self.notifier.sync({CASE: RUN})["failed"], ["state_unavailable"])
                self.assertEqual(self.notifier.path.read_text(), body)
        self.assertEqual(self.sent, [])

    def test_baseline_preserves_reviewed_reports_and_refuses_overwrite(self):
        self.notifier.baseline({CASE: RUN})
        self.assertEqual(self.notifier.sync({CASE: RUN})["submitted"], [])
        with self.assertRaises(FileExistsError):
            self.notifier.baseline({})
        self.assertEqual(self.sent, [])

    def test_concurrent_notifiers_and_unwritable_state_never_send_duplicates(self):
        with self.notifier.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.notifier.sync({CASE: RUN})["failed"], ["notification_busy"])
        self.assertEqual(self.sent, [])
        with patch.object(Path, "mkdir", side_effect=PermissionError):
            self.assertEqual(self.notifier.sync({CASE: RUN})["failed"], ["state_unavailable"])
        self.assertEqual(self.notifier.sync({CASE: RUN})["submitted"], ["reports"])

    def test_invalid_identities_and_capacity_are_rejected_without_notifications(self):
        for versions in ([], {CASE: None}, {CASE: "wrong"}, {1: RUN}, {CASE: 1}):
            with self.subTest(versions=versions):
                self.assertEqual(self.notifier.sync(versions)["failed"], ["state_unavailable"])
        for failure in (None, "x" * 129):
            self.assertEqual(self.notifier.sync({}, failure)["failed"], ["state_unavailable"])
        full = {"feedback-%032x" % i: RUN for i in range(1000)}
        self.assertEqual(self.notifier.sync(full)["submitted"], ["reports"])
        new_full = {"feedback-%032x" % i: RUN for i in range(1000, 2000)}
        self.assertEqual(self.notifier.sync(new_full)["submitted"], ["reports"])
        self.assertEqual(len(self.notifier._read()["reports"]), 1000)  # discipline-ok(R5): inspect the persisted deduplication limit
        self.assertEqual(self.notifier.sync({**new_full, CASE: RUN})["failed"], ["state_unavailable"])

    def test_report_index_is_read_only_bounded_and_does_not_include_description(self):
        self.assertEqual(notices.report_versions(self.root), {})
        database = self.root / "queue.sqlite3"
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE queue(case_id TEXT, status TEXT, report TEXT)")
            db.execute("INSERT INTO queue VALUES (?, 'ready', ?)",
                       (CASE, json.dumps({"analysis_run_id": RUN, "user_description": "private text"})))
        self.assertEqual(notices.report_versions(self.root), {CASE: RUN})
        with sqlite3.connect(database) as db:
            db.execute("UPDATE queue SET report='not json'")
        with self.assertRaises(ValueError):
            notices.report_versions(self.root)

    def test_native_notification_uses_arguments_not_source_interpolation_and_times_out(self):
        message = 'synthetic " & do shell script "bad"'
        with patch.object(notices.subprocess, "run") as run:
            notices.display_notification(message)
        args = run.call_args.args[0]
        self.assertEqual(args[-1], message)
        self.assertNotIn(message, args[2])
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertTrue(run.call_args.kwargs["check"])

    def test_cli_notifies_authentication_failure_without_logging_credentials(self):
        notifier = Mock()
        notifier.sync.return_value = {"submitted": ["failure"], "failed": []}
        with patch("sys.argv", ["agent", "--data-dir", str(self.root), "--gh", "/absolute/gh"]), \
                patch.object(feedback_agent, "FeedbackNotifications", return_value=notifier), \
                patch.object(feedback_agent.subprocess, "run", side_effect=subprocess.CalledProcessError(
                    1, ["gh"], stderr="synthetic-secret")), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(feedback_agent.main(), 1)
        self.assertNotIn("synthetic-token", output.getvalue())
        notifier.sync.assert_called_once_with({}, "backend:authentication")
        self.assertNotIn("synthetic-secret", output.getvalue())

    def test_cli_notification_failure_keeps_processing_result_and_returns_nonzero(self):
        agent = Mock()
        agent.run_once.return_value = {"ready": [], "failed": [CASE], "discovered": 1}
        notifier = Mock()
        notifier.sync.return_value = {"submitted": [], "failed": ["failure"]}
        with patch("sys.argv", ["agent", "--data-dir", str(self.root), "--gh", "/absolute/gh"]), \
                patch.object(feedback_agent, "FeedbackNotifications", return_value=notifier), \
                patch.object(feedback_agent, "FeedbackAgent", return_value=agent), \
                patch.object(feedback_agent.subprocess, "run", return_value=Mock(stdout="synthetic-token")), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(feedback_agent.main(), 1)

        self.assertNotIn("synthetic-token", output.getvalue())

    def test_report_index_failure_and_backend_exception_notify_without_hiding_failure(self):
        agent = Mock()
        notifier = Mock()
        notifier.sync.return_value = {"submitted": ["failure"], "failed": []}
        with patch("sys.argv", ["agent", "--data-dir", str(self.root), "--gh", "/absolute/gh"]), \
                patch.object(feedback_agent, "FeedbackAgent", return_value=agent), \
                patch.object(feedback_agent, "FeedbackNotifications", return_value=notifier), \
                patch.object(feedback_agent.subprocess, "run", return_value=Mock(stdout="synthetic-token")), \
                patch("sys.stdout", new_callable=io.StringIO):
            agent.run_once.side_effect = OSError("private exception text")
            self.assertEqual(feedback_agent.main(), 1)
            notifier.sync.assert_called_with({}, "backend:processing")
            agent.run_once.side_effect = None
            agent.run_once.return_value = {"ready": [], "failed": [], "discovered": 0}
            with patch.object(feedback_agent, "report_versions", side_effect=OSError):
                self.assertEqual(feedback_agent.main(), 1)
            notifier.sync.assert_called_with({}, "backend:notification_index")
