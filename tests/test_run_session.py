"""Pre-implementation acceptance tests for crash-safe runtime sessions."""

from __future__ import annotations

import os
import json
import sqlite3
import tempfile
from unittest import mock
from pathlib import Path

from sf_run_session import (
    PHASE_CLEAN_EXIT,
    PHASE_FRAME_READY,
    PHASE_HEALTHY,
    PHASE_UPDATE_HANDOFF,
    PROMPT_DISMISSED,
    RuntimeSessionLedger,
    SessionLedgerError,
    session_path_for_log,
)
from tests._base import SFCase


class RuntimeSessionLedgerTests(SFCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="run-session-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = RuntimeSessionLedger(self.root / "run-session.sqlite3")

    def begin(self, identity="a" * 32):
        return self.ledger.begin(app_version="1.4.103", bundle_id="com.skyrocket.app",
                                 pid=1234, session_id=identity, started_at="2026-10-24T00:00:00Z")

    def test_log_path_derives_a_private_adjacent_session_path(self):
        path = session_path_for_log(self.root / "debug.log")
        self.assertEqual(path, (self.root / "run-session.sqlite3").resolve())

    def test_first_launch_has_no_prior_assessment_and_starts_launching(self):
        self.assertIsNone(self.begin())
        current = self.ledger.current()
        self.assertEqual(current["phase"], "launching")
        self.assertEqual(current["prompt_state"], "none")
        self.assertEqual(current["prompt_count"], 0)
        self.assertEqual(oct(os.stat(self.root / "run-session.sqlite3").st_mode & 0o777), "0o600")

    def test_startup_incomplete_never_prompts_on_the_next_launch(self):
        self.begin()
        assessment = self.begin("b" * 32)
        self.assertEqual((assessment.category, assessment.should_prompt), ("startup_incomplete", False))

    def test_frame_ready_without_exit_prompts_once(self):
        self.begin()
        self.ledger.mark_frame_ready("a" * 32)
        assessment = self.begin("b" * 32)
        self.assertEqual((assessment.category, assessment.should_prompt), ("post_frame_unfinished", True))

    def test_healthy_without_exit_prompts_once_and_a_dismissal_suppresses_repetition(self):
        self.begin()
        self.ledger.mark_frame_ready("a" * 32)
        self.ledger.mark_healthy("a" * 32)
        assessment = self.begin("b" * 32)
        self.assertTrue(assessment.should_prompt)
        self.ledger.mark_prompt(assessment.session_id, PROMPT_DISMISSED)
        self.assertFalse(self.begin("c" * 32).should_prompt)

    def test_deferred_prompt_can_be_resolved_once_without_incrementing_count(self):
        self.begin()
        self.ledger.mark_frame_ready("a" * 32)
        self.ledger.mark_healthy("a" * 32)
        assessment = self.begin("b" * 32)
        self.ledger.mark_prompt(assessment.session_id, "deferred")
        self.ledger.mark_prompt(assessment.session_id, PROMPT_DISMISSED)
        with self.assertRaises(SessionLedgerError):
            self.ledger.mark_prompt(assessment.session_id, "opened_feedback")
        self.assertFalse(self.begin("c" * 32).should_prompt)

    def test_clean_exit_never_prompts(self):
        self.begin()
        self.ledger.mark_frame_ready("a" * 32)
        self.ledger.mark_healthy("a" * 32)
        self.ledger.mark_clean_exit("a" * 32, ended_at="2026-10-24T00:01:00Z")
        assessment = self.begin("b" * 32)
        self.assertEqual((assessment.category, assessment.should_prompt), ("clean_exit", False))
        self.assertEqual(self.ledger.current()["phase"], "launching")

    def test_update_handoff_requires_identity_and_suppresses_crash_prompt(self):
        self.begin()
        self.ledger.mark_frame_ready("a" * 32)
        with self.assertRaises(SessionLedgerError):
            self.ledger.mark_update_handoff("a" * 32, handoff_id="", expected_version="1.4.104")
        self.ledger.mark_update_handoff("a" * 32, handoff_id="handoff-1", expected_version="1.4.104")
        assessment = self.begin("b" * 32)
        self.assertEqual((assessment.category, assessment.should_prompt), ("update_handoff", False))

    def test_phase_cannot_move_backward_or_after_terminal_exit(self):
        self.begin()
        self.ledger.mark_frame_ready("a" * 32)
        self.ledger.mark_healthy("a" * 32)
        with self.assertRaises(SessionLedgerError):
            self.ledger.mark_frame_ready("a" * 32)
        self.ledger.mark_clean_exit("a" * 32)
        self.assertEqual(self.ledger.current()["phase"], PHASE_CLEAN_EXIT)
        with self.assertRaises(SessionLedgerError):
            self.ledger.mark_healthy("a" * 32)

    def test_an_unreadable_ledger_never_blocks_startup(self):
        """账本坏掉照样起窗口，但「坏到什么程度」分两种现场，判据也跟着分。

        实测：不足一个页的残缺文件（`{`）被 SQLite 当空库直接接管，现场留不下；
        真的不像数据库的长文件则必须**改名留住**再开新账本 —— 里面可能还有上次崩溃的线索。
        """
        self.ledger.path.write_bytes(b"{")
        self.assertIsNone(self.begin())
        self.assertEqual(self.ledger.current()["phase"], "launching")

        self.ledger.path.unlink()
        self.ledger.path.write_bytes(b"x" * 40000)
        self.assertIsNone(self.begin("b" * 32))
        self.assertEqual(self.ledger.current()["phase"], "launching")
        kept = next(self.root.glob("run-session.sqlite3.corrupt-*"))
        self.assertEqual(kept.read_bytes()[:8], b"xxxxxxxx", "被挪走的那份必须还能查回现场")
        self.assertFalse(self.ledger.path.with_name("run-session.sqlite3.corrupt.tmp").exists())

    def test_unfinished_reraises_non_schema_operational_errors(self):
        connection = sqlite3.connect(self.ledger.path)
        connection.close()
        with mock.patch("sf_run_session._rows", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                self.ledger.unfinished()

    def test_schema_mismatch_is_quarantined_before_a_new_ledger_is_created(self):
        connection = sqlite3.connect(self.ledger.path)
        connection.execute("CREATE TABLE session_events (session_id TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        self.assertIsNone(self.begin())
        self.assertTrue(self.ledger.current())
        self.assertEqual(len(list(self.root.glob("run-session.sqlite3.corrupt-*"))), 1)

    def test_a_busy_database_is_not_quarantined(self):
        self.begin()
        with mock.patch.object(self.ledger, "_create", side_effect=sqlite3.OperationalError("database is locked")), \
                mock.patch("sf_run_session.os.replace") as replace:
            with self.assertRaisesRegex(SessionLedgerError, "busy"):
                self.begin("b" * 32)
        replace.assert_not_called()
        self.assertTrue(self.ledger.path.exists())
        self.assertEqual(self.ledger.current()["session_id"], "a" * 32)

    def test_non_busy_open_failure_is_quarantined_and_recreated(self):
        self.ledger.path.write_bytes(b"broken")
        real_create = self.ledger._create  # discipline-ok(R5): 仅此私有工厂能种一次非 busy 打开失败。
        calls = 0

        def fail_once(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("disk I/O error")
            return real_create(path)

        with mock.patch.object(self.ledger, "_create", side_effect=fail_once):
            self.assertIsNone(self.begin("b" * 32))
        self.assertEqual(self.ledger.current()["session_id"], "b" * 32)

    def test_recovery_refuses_to_replace_a_database_that_becomes_busy(self):
        self.ledger.path.write_bytes(b"broken")
        with mock.patch.object(self.ledger, "_create", side_effect=[None, sqlite3.OperationalError("database is busy")]):
            with self.assertRaisesRegex(SessionLedgerError, "busy"):
                self.begin("b" * 32)

    def test_recovery_non_busy_create_failure_is_reported(self):
        self.ledger.path.write_bytes(b"broken")
        with mock.patch.object(self.ledger, "_create", side_effect=[None, sqlite3.OperationalError("permission denied")]):
            with self.assertRaisesRegex(SessionLedgerError, "cannot be created"):
                self.begin("b" * 32)

    def test_create_reraises_busy_after_connection_setup(self):
        connection = mock.Mock()
        connection.execute.side_effect = sqlite3.OperationalError("database is locked")
        with mock.patch("sf_run_session.sqlite3.connect", return_value=connection):
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                self.ledger._create(self.ledger.path)  # discipline-ok(R5): 直接覆盖连接已建立后的 busy 分支。
        connection.close.assert_called_once()

    def test_recovery_rolls_back_and_closes_after_work_failure(self):
        self.begin()

        def fail(_connection):
            raise RuntimeError("recovery work failed")

        with self.assertRaisesRegex(RuntimeError, "recovery work failed"):
            self.ledger._recover_malformed_ledger(fail)  # discipline-ok(R5): 只能直接驱动恢复事务的回滚分支。

    def test_a_malformed_historical_row_is_quarantined_and_new_session_is_readable(self):
        self.begin()
        connection = sqlite3.connect(self.ledger.path)
        try:
            connection.execute("UPDATE session_events SET phase='nonsense'")
            connection.commit()
        finally:
            connection.close()
        self.assertIsNone(self.begin("b" * 32))
        self.assertEqual(self.ledger.current()["session_id"], "b" * 32)
        self.assertEqual(len(list(self.root.glob("run-session.sqlite3.corrupt-*"))), 1)

    def test_legacy_migration_requires_the_original_schema_version(self):
        legacy = self.root / "run-session.json"
        legacy.write_text(json.dumps({"schema_version": 999, "current": {
            "session_id": "l" * 32, "started_at": "2026-10-24T00:00:00Z",
            "app_version": "1.4.103", "bundle_id": "com.skyrocket.app", "pid": 1234,
            "phase": "healthy", "prompt_state": "none", "prompt_count": 0,
            "update_handoff_id": None, "expected_update_version": None, "ended_at": None,
        }, "pending": None}), encoding="utf-8")
        self.assertIsNone(self.begin())
        self.assertTrue(legacy.exists())
        self.assertFalse(list(self.root.glob("run-session.json.migrated-*")))

    def test_legacy_migration_tolerates_a_concurrent_archive(self):
        legacy = self.root / "run-session.json"
        record = {"session_id": "l" * 32, "started_at": "2026-10-24T00:00:00Z",
                  "app_version": "1.4.103", "bundle_id": "com.skyrocket.app", "pid": 1234,
                  "phase": "healthy", "prompt_state": "none", "prompt_count": 0,
                  "update_handoff_id": None, "expected_update_version": None, "ended_at": None}
        legacy.write_text(json.dumps({"schema_version": 1, "current": record, "pending": None}), encoding="utf-8")
        real_replace = os.replace

        def archived_elsewhere(source, target):
            if Path(source) == legacy:
                legacy.unlink()
                raise FileNotFoundError(source)
            return real_replace(source, target)

        with mock.patch("sf_run_session.os.replace", side_effect=archived_elsewhere):
            assessment = self.begin()
        self.assertEqual(assessment.session_id, "l" * 32)
        self.assertEqual(self.ledger.current()["session_id"], "a" * 32)


    def test_valid_legacy_json_without_records_is_left_for_manual_inspection(self):
        legacy = self.root / "run-session.json"
        legacy.write_text(json.dumps({"current": None, "pending": None}), encoding="utf-8")
        self.assertIsNone(self.begin())
        self.assertTrue(legacy.exists())
