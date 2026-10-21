"""Real menu and Qt interaction with an isolated persistent feedback outbox."""

import tempfile
import json
import threading
import zipfile
import sqlite3
import time
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6 import sip
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QTabWidget, QWidget

import stock_float_window as window
from sf_feedback import FeedbackProtocolError, build_archive
from sf_feedback_capture import CONSENT_VERSION, package_files
from sf_feedback_github import GitHubInbox
from sf_feedback_ui import FeedbackDialog
from sf_feedback_ui import feedback_diagnostics
from sf_feedback_ui import _failure
from sf_diag import Needle
import sf_diag
from tests._base import SFCase
from tests.test_feedback_pipeline import MemoryGitHub, snapshot


class _FeedbackCase(SFCase):
    def setUp(self):
        super().setUp()
        self._feedback_alert_patch = patch("sf_system_alert.show_alert", return_value=None)
        self._feedback_alert_patch.start()
        self.addCleanup(self._feedback_alert_patch.stop)

    def tearDown(self):
        widgets = list(self._sf_widgets)  # discipline-ok(R5): drain deletion for this fixture's registered Qt widgets
        super().tearDown()
        # Deliver native focus events while the closed windows are still alive.
        QApplication.processEvents()
        for widget in reversed(widgets):
            try:
                widget.deleteLater()
            except RuntimeError:
                pass
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        for widget in widgets:
            self.assertTrue(sip.isdeleted(widget), "feedback fixture left a Qt window alive")


class DialogTests(_FeedbackCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.api = MemoryGitHub()
        self.inbox = GitHubInbox(self.api)
        self.dialog = self.make_dialog()

    def sender(self, url, envelope, path):
        return self.inbox.accept(envelope, Path(path).read_bytes())

    def make_dialog(self, endpoint="https://receiver.example/v1/feedback"):
        dialog = self.track_widget(FeedbackDialog(
            snapshot=snapshot(), storage_dir=self.temporary.name,
            endpoint=endpoint, sender=self.sender))
        dialog.show()
        self.assertTrue(QTest.qWaitForWindowExposed(dialog, 1000), "feedback test window was never exposed")
        return dialog

    def finish(self, dialog=None):
        dialog = dialog or self.dialog
        deadline = time.monotonic() + 5
        while dialog.worker is not None and time.monotonic() < deadline:
            QTest.qWait(20)
            dialog._poll()  # discipline-ok(R5): deliver this dialog's queued worker results deterministically
        self.assertIsNone(dialog.worker, "worker did not finish")

    def test_upload_does_not_require_manual_consent_and_receives_bound_receipt(self):
        self.assertTrue(self.dialog.upload_button.isEnabled())
        self.dialog.description.setPlainText("刷新失败 password=secret-value")
        self.assertNotIn("secret-value", self.dialog.preview.toPlainText())
        self.assertTrue(self.dialog.upload_button.isEnabled())
        QTest.mouseClick(self.dialog.upload_button, Qt.MouseButton.LeftButton)
        self.finish()
        self.assertEqual(self.dialog.stage, "received")
        self.assertIn("GitHub 已收件", self.dialog.status.text())
        self.assertIn("不代表已分析或已修复", self.dialog.status.text())
        self.assertEqual(len(self.api.rows), 2)
        self.assertEqual(self.dialog.outbox.get(self.dialog.current_id)["status"], "receipt_persisted")

    def test_feedback_controls_are_hidden_and_description_uses_product_language(self):
        self.assertFalse(hasattr(self.dialog, "tabs"))
        self.assertEqual(self.dialog.findChildren(QTabWidget), [])
        self.assertFalse(self.dialog.preview.isVisible())
        self.assertFalse(self.dialog.counter.isVisible())
        self.assertFalse(self.dialog.consent.isVisible())
        self.assertFalse(self.dialog.history.isVisible())
        self.assertFalse(self.dialog.status.isVisible())
        self.assertEqual(
            [label.text() for label in self.dialog.findChildren(QLabel) if label.isVisible()],
            ["反馈问题"],
        )
        self.assertEqual(self.dialog.title.alignment(), Qt.AlignmentFlag.AlignCenter)
        self.assertEqual(self.dialog.description.document().defaultTextOption().alignment(),
                         Qt.AlignmentFlag.AlignHCenter)
        self.assertEqual(
            self.dialog.description.placeholderText(),
            "告诉我们你遇到的问题或建议，这些信息将用于改进我们的产品。",
        )

    def test_upload_status_popups_replace_each_other_and_close_with_feedback(self):
        progress, received = Mock(), Mock()
        with patch("sf_system_alert.show_alert", side_effect=(progress, received)) as alerts:
            self.dialog.upload_button.click()
            self.assertFalse(self.dialog.isVisible(), "点击上传后应立即隐藏反馈窗口")
            self.assertEqual(alerts.call_args_list[0].kwargs["title"], "反馈上传中")
            self.assertFalse(self.dialog.status.isVisible())
            self.finish()
            self.assertEqual(alerts.call_args_list[1].kwargs["title"], "反馈已上传")
            self.assertNotIn("下载和分析将在维护者电脑进行", alerts.call_args_list[1].kwargs["info"])
            self.assertTrue(alerts.call_args_list[1].kwargs["info"].startswith("反馈编号："))
            progress.close.assert_called_once_with()
            self.assertFalse(self.dialog.status.isVisible())
            self.dialog.close()
            received.close.assert_called_once_with()

    def test_failed_upload_uses_popup_and_keeps_retry_available(self):
        self.dialog.sender = Mock(side_effect=URLError("synthetic offline"))
        with patch("sf_system_alert.show_alert") as alerts:
            self.dialog.upload_button.click()
            self.finish()
        self.assertEqual(alerts.call_args.kwargs["title"], "反馈上传未完成")
        self.assertFalse(self.dialog.status.isVisible())
        self.assertTrue(self.dialog.upload_button.isEnabled())
        self.assertEqual(self.dialog.upload_button.text(), "重试上传")
        self.assertIsNotNone(self.dialog.outbox.get(self.dialog.current_id))

    def test_uncertain_save_popup_does_not_claim_the_draft_was_not_saved(self):
        with patch("sf_feedback_ui.FeedbackOutbox.enqueue", side_effect=OSError), patch(
                "sf_system_alert.show_alert") as alerts:
            self.dialog.upload_button.click()
            self.finish()
        self.assertEqual(self.dialog.stage, "uncertain")
        self.assertIn("保存结果暂无法确认", alerts.call_args.kwargs["info"])
        self.assertNotIn("尚未保存", alerts.call_args.kwargs["info"])
        self.assertEqual(self.dialog.upload_button.text(), "核对并重试")

    def test_unavailable_popup_does_not_change_a_verified_upload_result(self):
        with patch("sf_system_alert.show_alert", side_effect=RuntimeError("synthetic popup failure")), \
                self.assertLogs("sf_feedback_ui", level="WARNING") as records:
            self.dialog.upload_button.click()
            self.finish()
        self.assertEqual(self.dialog.stage, "received")
        self.assertEqual(self.dialog.outbox.get(self.dialog.current_id)["status"], "receipt_persisted")
        self.assertEqual(sum("popup unavailable" in line for line in records.output), 2)
        self.assertFalse(self.dialog.status.isVisible())

    def test_broken_popup_handles_do_not_block_replacement_or_close(self):
        progress, received = Mock(), Mock()
        progress.close.side_effect = RuntimeError("synthetic close failure")
        received.close.side_effect = RuntimeError("synthetic close failure")
        with patch("sf_system_alert.show_alert", side_effect=(progress, received)), \
                self.assertLogs("sf_feedback_ui", level="DEBUG") as records:
            self.dialog.upload_button.click()
            self.finish()
            self.dialog.close()
        progress.close.assert_called_once_with()
        received.close.assert_called_once_with()
        self.assertEqual(self.dialog.stage, "received")
        self.assertFalse(self.dialog.isVisible())
        self.assertEqual(sum("popup close failed" in line for line in records.output), 2)

    def test_preview_is_the_exact_sealed_description_even_if_identity_changes(self):
        needle = Needle("synthetic_operator", "<user>", False, "synthetic_operator")
        with patch("sf_feedback_capture.sf_diag.identity_needles", return_value=(needle,)):
            self.dialog.description.setPlainText("synthetic_operator")
        self.assertIn("<user>", self.dialog.preview.toPlainText())
        self.dialog.consent.setChecked(True)
        with patch("sf_feedback_capture.sf_diag.identity_needles", return_value=()):
            self.dialog.upload_button.click()
            self.finish()
        row = self.dialog.outbox.get(self.dialog.current_id)
        with zipfile.ZipFile(row["archive_path"]) as archive:
            self.assertEqual(archive.read("problem.txt").decode(), "<user>")

    def test_preview_cache_does_not_become_automatic_diagnostic_content(self):
        marker = "synthetic_private_description_marker"
        self.dialog.description.setPlainText(marker)
        automatic = sf_diag.collect(SimpleNamespace(_feedback_dialog=self.dialog))
        self.assertNotIn(marker, json.dumps(automatic, ensure_ascii=False))

    def test_pending_extra_attachment_cannot_be_sent_and_can_start_a_new_draft(self):
        path = Path(self.temporary.name) / "unexpected.zip"
        build_archive(path, {**package_files(snapshot(), "previewed"), "unexpected.txt": "not previewed"},
                      app_version="1.4.96", bundle_id="com.skyrocket.app", run_id="abcdef")
        row = self.dialog.outbox.enqueue(path, app_version="1.4.96", consent_policy_version=CONSENT_VERSION)
        replacement = self.make_dialog()
        self.assertEqual(replacement.current_id, row["outbox_id"])
        self.assertEqual(replacement.upload_button.text(), "新建反馈")
        self.assertEqual(replacement.last_error, "local_package_invalid")
        self.assertEqual(replacement.description.toPlainText(), "")
        replacement.history.setCurrentIndex(0)
        replacement.description.setPlainText("unrelated previous draft")
        replacement.history.setCurrentIndex(replacement.history.findData(row["outbox_id"]))
        replacement.consent.setChecked(True)
        self.assertEqual(replacement.upload_button.text(), "新建反馈")
        self.assertEqual(replacement.last_error, "local_package_invalid")
        self.assertEqual(replacement.description.toPlainText(), "")
        replacement.upload_button.click()
        self.assertIsNone(replacement.current_id)
        self.assertEqual(replacement.stage, "draft")
        self.assertEqual(self.api.rows, {})
        self.assertTrue(Path(row["archive_path"]).is_file())

    def test_missing_preview_cache_never_transmits_or_reenables_submission(self):
        self.dialog.consent.setChecked(True)
        self.dialog.preview_files = None
        self.dialog.upload_button.click()
        self.assertEqual(self.api.rows, {})
        self.assertEqual(self.dialog.last_failure_phase, "prepare")
        self.assertFalse(self.dialog.upload_button.isEnabled())

    def test_saved_receipt_is_revalidated_when_reopened_in_history(self):
        self.dialog.consent.setChecked(True)
        self.dialog.upload_button.click()
        self.finish()
        previous = self.dialog.current_id
        index = self.dialog.history.findData(previous)
        self.dialog.history.setCurrentIndex(0)
        self.dialog.history.setCurrentIndex(index)
        self.assertEqual(self.dialog.stage, "received")
        row = self.dialog.outbox.get(previous)
        receipt = {**row["receipt"], "repository": "unapproved/repository"}
        with sqlite3.connect(self.dialog.outbox.database_path) as db:
            db.execute("UPDATE outbox SET receipt_json=? WHERE outbox_id=?", (json.dumps(receipt), previous))
        self.dialog.history.setCurrentIndex(0)
        self.dialog.history.setCurrentIndex(index)
        self.assertEqual(self.dialog.last_error, "local_package_invalid")
        self.assertNotIn("已收件", self.dialog.status.text())
        self.assertEqual(self.dialog.upload_button.text(), "新建反馈")
        previous_remote_count = len(self.api.rows)
        self.dialog.upload_button.click()
        self.assertEqual(len(self.api.rows), previous_remote_count)
        self.assertEqual(self.dialog.stage, "draft")
        self.assertIsNotNone(self.dialog.outbox.get(previous))

    def test_editing_keeps_consent_hidden_and_overlong_description_blocks_upload(self):
        self.dialog.consent.setChecked(True)
        self.dialog.description.setPlainText("changed")
        self.assertFalse(self.dialog.consent.isChecked())
        self.dialog.description.setPlainText("x" * 4001)
        self.dialog.consent.setChecked(True)
        self.assertFalse(self.dialog.upload_button.isEnabled())
        self.assertIn("4000", self.dialog.status.text())
        self.assertIn("4000", self.dialog.upload_button.toolTip())
        self.assertIn("4000", self.dialog.description.toolTip())
        self.dialog.description.setPlainText("valid again")
        self.assertEqual(self.dialog.status.text(), "")
        self.assertEqual(self.dialog.description.toolTip(), "")

    def test_keyboard_submission_never_closes_and_upload_button_stays_explicit(self):
        self.dialog.description.setFocus()
        QTest.keyClick(self.dialog.description, Qt.Key.Key_Return)
        self.assertTrue(self.dialog.isVisible())
        self.assertEqual(self.dialog.outbox.pending(), [])
        QTest.mouseClick(self.dialog.upload_button, Qt.MouseButton.LeftButton)
        self.finish()
        self.assertEqual(self.dialog.stage, "received")
        self.assertEqual(self.dialog.upload_button.text(), "已上传")
        self.assertFalse(self.dialog.upload_button.isEnabled())

    def test_multiline_return_and_tab_focus_do_not_accidentally_submit(self):
        self.dialog.description.setPlainText("first line")
        self.dialog.consent.setChecked(True)
        self.dialog.description.setFocus()
        QTest.keyClick(self.dialog.description, Qt.Key.Key_End)
        QTest.keyClick(self.dialog.description, Qt.Key.Key_Return)
        self.assertIn("\n", self.dialog.description.toPlainText())
        self.assertFalse(self.dialog.consent.isChecked())
        self.assertEqual(self.dialog.outbox.pending(), [])
        QTest.keyClick(self.dialog.description, Qt.Key.Key_Tab)
        self.assertFalse(self.dialog.description.hasFocus())

    def test_escape_preserves_draft_and_close_during_upload_does_not_cancel(self):
        self.dialog.description.setPlainText("retained synthetic draft")
        QTest.keyClick(self.dialog, Qt.Key.Key_Escape)
        self.assertFalse(self.dialog.isVisible())
        self.dialog.show()
        self.assertEqual(self.dialog.description.toPlainText(), "retained synthetic draft")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(url, envelope, path):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("synthetic blocker")
            return self.sender(url, envelope, path)
        self.dialog.sender = blocked
        self.dialog.consent.setChecked(True)
        self.dialog.upload_button.click()
        try:
            self.assertTrue(entered.wait(2))
            self.dialog.close_button.click()
            self.assertFalse(self.dialog.isVisible())
        finally:
            release.set()
        self.finish()
        self.assertEqual(self.dialog.stage, "received")
        self.dialog.show()
        self.assertEqual(self.dialog.upload_button.text(), "已上传")
        self.assertEqual(len(self.api.rows), 2)

    def test_worker_start_failure_preserves_editable_draft_and_retry(self):
        self.dialog.description.setPlainText("preserved draft")
        self.dialog.consent.setChecked(True)
        with patch("sf_feedback_ui.threading.Thread.start", side_effect=RuntimeError("synthetic thread exhaustion")):
            self.dialog.upload_button.click()
        self.assertIsNone(self.dialog.worker)
        self.assertEqual(self.dialog.stage, "draft")
        self.assertFalse(self.dialog.description.isReadOnly())
        self.assertEqual(self.dialog.description.toPlainText(), "preserved draft")
        self.assertEqual(self.dialog.outbox.pending(), [])
        self.assertTrue(self.dialog.upload_button.isEnabled())
        self.assertIn("尚未保存", self.dialog.status.text())
        self.assertNotIn("内容已保留", self.dialog.status.text())
        self.assertEqual(self.dialog.last_error, "RuntimeError")
        diagnostics = feedback_diagnostics(SimpleNamespace(_feedback_dialog=self.dialog))
        self.assertEqual(diagnostics["last_error"], "RuntimeError")
        self.assertEqual(diagnostics["last_failure_phase"], "worker_start")

    def test_archive_and_enqueue_failures_never_claim_durable_storage(self):
        self.dialog.close()
        for target, phase in (("sf_feedback_ui.json.loads", "prepare"),
                              ("sf_feedback_ui.threading.Thread", "worker_start"),
                              ("sf_feedback_ui.tempfile.TemporaryDirectory", "archive"),
                              ("sf_feedback_ui.build_archive", "archive"),
                              ("sf_feedback_ui.FeedbackOutbox.enqueue", "enqueue")):
            with self.subTest(phase=phase):
                dialog = self.make_dialog()
                dialog.description.setPlainText("synthetic unsaved draft")
                dialog.consent.setChecked(True)
                with patch(target, side_effect=OSError("password=must-not-be-logged")):
                    dialog.upload_button.click()
                    self.finish(dialog)
                self.assertIsNone(dialog.current_id)
                self.assertEqual(dialog.outbox.pending(), [])
                self.assertIn("保存结果未知" if phase == "enqueue" else "尚未保存", dialog.status.text())
                self.assertEqual(dialog.last_error, "OSError")
                self.assertEqual(dialog.last_failure_phase, phase)
                self.assertNotIn("must-not-be-logged", dialog.status.text())
                self.assertTrue(dialog.upload_button.isEnabled())
                dialog.close()

    def test_enqueue_commit_then_readback_loss_recovers_same_record_and_original_bytes(self):
        enqueue = self.dialog.outbox.enqueue
        def lost(*args, **kwargs):
            enqueue(*args, **kwargs)
            raise OSError("synthetic return lost after commit")
        self.dialog.consent.setChecked(True)
        with patch.object(self.dialog.outbox, "enqueue", side_effect=lost):
            self.dialog.upload_button.click()
            self.finish()
        self.assertEqual(self.dialog.stage, "uncertain")
        self.assertIn("保存结果未知", self.dialog.status.text())
        self.assertTrue(self.dialog.description.isReadOnly())
        original = self.dialog.outbox.pending()[0]
        with patch("sf_feedback_ui.build_archive", side_effect=AssertionError("must reuse committed ZIP")):
            self.dialog.upload_button.click()
            self.finish()
        self.assertEqual(self.dialog.stage, "received")
        self.assertEqual(self.dialog.current_id, original["outbox_id"])
        self.assertEqual(len(self.dialog.outbox.pending()), 1)
        self.assertEqual(self.dialog.outbox.get(self.dialog.current_id)["archive_sha256"], original["archive_sha256"])

    def test_failed_identity_recovery_never_creates_another_record(self):
        self.dialog.consent.setChecked(True)
        with patch.object(self.dialog.outbox, "find_by_idempotency_key", side_effect=sqlite3.OperationalError):
            self.dialog.upload_button.click()
            self.finish()
            key = self.dialog.draft_key
            self.dialog.upload_button.click()
            self.finish()
        self.assertEqual(self.dialog.stage, "uncertain")
        self.assertEqual(self.dialog.draft_key, key)
        self.assertEqual(self.dialog.outbox.pending(), [])
        self.assertEqual(self.dialog.last_error, "SQLiteError")
        self.assertEqual(self.dialog.last_failure_phase, "recovery")
        with patch("sf_feedback_ui.threading.Thread.start", side_effect=RuntimeError):
            self.dialog.upload_button.click()
        self.assertEqual(self.dialog.stage, "uncertain")
        self.assertEqual(self.dialog.draft_key, key)
        self.assertTrue(self.dialog.description.isReadOnly())

    def test_failure_categories_are_allowlisted_and_never_log_exception_text(self):
        for error, expected in ((HTTPError("https://synthetic.invalid", 401, "secret", {}, None), "HTTPError"),
                                (URLError("secret"), "URLError"), (TimeoutError("secret"), "TimeoutError"),
                                (PermissionError("secret"), "PermissionError"),
                                (FileNotFoundError("secret"), "FileNotFoundError"),
                                (OSError("secret"), "OSError"),
                                (sqlite3.OperationalError("secret"), "SQLiteError"),
                                (FeedbackProtocolError("secret"), "FeedbackProtocolError"),
                                (ValueError("secret"), "ValueError"),
                                (RuntimeError("secret"), "RuntimeError"),
                                (Exception("secret"), "unexpected_error")):
            with self.subTest(expected=expected):
                self.assertEqual(_failure("enqueue", error), {"phase": "enqueue", "category": expected})
        self.dialog.consent.setChecked(True)
        with patch("sf_feedback_ui.threading.Thread.start", side_effect=RuntimeError("password=secret")), \
                self.assertLogs("sf_feedback_ui", level="WARNING") as records:
            self.dialog.upload_button.click()
        self.assertNotIn("secret", "".join(records.output))

    def test_network_failure_preserves_sealed_identity_and_failure_phase(self):
        def fail(*args):
            raise TimeoutError("password=never-export")
        self.dialog.sender = fail
        self.dialog.consent.setChecked(True)
        self.dialog.upload_button.click()
        self.finish()
        self.assertIsNotNone(self.dialog.current_id)
        self.assertIn("待提交队列", self.dialog.status.text())
        self.assertNotIn("尚未保存", self.dialog.status.text())
        self.assertEqual(self.dialog.last_error, "TimeoutError")
        self.assertEqual(self.dialog.last_failure_phase, "submit")
        self.assertTrue(self.dialog.upload_button.isEnabled())

    def test_missing_service_configuration_cannot_transmit(self):
        dialog = self.make_dialog(endpoint="")
        dialog.consent.setChecked(True)
        self.assertFalse(dialog.upload_button.isEnabled())
        self.assertIn("尚未就绪", dialog.status.text())
        self.assertEqual(self.api.rows, {})
        dialog.description.setPlainText("x" * 4001)
        dialog.description.setPlainText("valid again")
        self.assertIn("尚未就绪", dialog.status.text())
        self.assertFalse(dialog.upload_button.isEnabled())

    def test_close_without_click_does_not_enqueue_or_transmit_and_preserves_draft(self):
        self.dialog.description.setPlainText("draft")
        self.dialog.close()
        self.dialog.show()
        self.assertEqual(self.dialog.description.toPlainText(), "draft")
        self.assertEqual(self.dialog.outbox.pending(), [])
        self.assertEqual(self.api.rows, {})

    def test_failure_retries_same_archive_and_does_not_create_duplicate_cases(self):
        sent = []

        def lost(url, envelope, path):
            sent.append(envelope["idempotency_key"])
            self.sender(url, envelope, path)
            raise TimeoutError()

        self.dialog.sender = lost
        self.dialog.consent.setChecked(True)
        self.dialog.upload_button.click()
        self.finish()
        self.assertEqual(self.dialog.stage, "pending")
        original = self.dialog.current_id
        self.dialog.sender = self.sender
        self.dialog.upload_button.click()
        self.finish()
        self.assertEqual(self.dialog.current_id, original)
        self.assertEqual(self.dialog.stage, "received")
        self.assertEqual(len(self.api.rows), 2)
        self.assertEqual(len(self.dialog.outbox.pending()), 1)

    def test_restart_restores_pending_and_only_explicit_click_retries(self):
        self.dialog.sender = lambda *args: (_ for _ in ()).throw(TimeoutError())
        self.dialog.description.setPlainText("原现场")
        self.dialog.consent.setChecked(True)
        self.dialog.upload_button.click()
        self.finish()
        old_id = self.dialog.current_id
        self.dialog.close()
        replacement = self.make_dialog()
        self.assertEqual(self.api.rows, {})
        self.assertEqual(replacement.current_id, old_id)
        self.assertFalse(replacement.history.isVisible())
        self.assertEqual(replacement.description.toPlainText(), "原现场")
        self.assertTrue(replacement.description.isReadOnly())
        replacement.consent.setChecked(True)
        replacement.upload_button.click()
        self.finish(replacement)
        self.assertEqual(replacement.current_id, old_id)
        self.assertEqual(replacement.stage, "received")

    def test_corrupt_pending_is_visible_and_cannot_be_uploaded(self):
        path = Path(self.temporary.name) / "initial.zip"
        build_archive(path, package_files(snapshot(), "draft"), app_version="1.4.96",
                      bundle_id="com.skyrocket.app", run_id="abcdef")
        row = self.dialog.outbox.enqueue(path, app_version="1.4.96", consent_policy_version=CONSENT_VERSION)
        Path(row["archive_path"]).write_bytes(b"corrupt")
        replacement = self.make_dialog()
        replacement.history.setCurrentIndex(replacement.history.findData(row["outbox_id"]))
        replacement.consent.setChecked(True)
        self.assertEqual(replacement.upload_button.text(), "新建反馈")
        self.assertIn("损坏", replacement.status.text())
        replacement.upload_button.click()
        self.assertIsNone(replacement.current_id)
        self.assertEqual(self.api.rows, {})
        self.assertEqual(Path(row["archive_path"]).read_bytes(), b"corrupt")

    def test_new_feedback_clears_previous_success_without_losing_receipt(self):
        self.dialog.consent.setChecked(True)
        self.dialog.upload_button.click()
        self.finish()
        previous_id = self.dialog.current_id
        self.dialog.history.setCurrentIndex(0)
        self.assertEqual(self.dialog.stage, "draft")
        self.assertIsNone(self.dialog.current_id)
        self.assertFalse(self.dialog.description.isReadOnly())
        self.assertEqual(self.dialog.outbox.get(previous_id)["status"], "receipt_persisted")

    def test_final_result_is_drained_when_worker_finishes_at_queue_boundary(self):
        dialog = self.dialog
        class FinishingWorker:
            def is_alive(self):
                dialog.results.put(("received", {"case_id": "synthetic-case"}))
                return False
        self.dialog.worker = FinishingWorker()
        self.dialog._poll()  # discipline-ok(R5): directly exercise the queue/thread-exit race boundary under test
        self.assertEqual(self.dialog.stage, "received")
        self.assertIsNone(self.dialog.worker)

    def test_new_case_clears_previous_success_without_requiring_consent(self):
        self.dialog.consent.setChecked(True)
        self.dialog.upload_button.click()
        self.finish()
        self.dialog.history.setCurrentIndex(0)
        self.assertEqual(self.dialog.status.text(), "")
        self.assertFalse(self.dialog.consent.isEnabled())
        self.assertFalse(self.dialog.consent.isChecked())


class _MenuHost(QWidget):
    contextMenuEvent: Any = window.FloatWindow.contextMenuEvent
    _on_ctx_menu_closed: Any = window.FloatWindow._on_ctx_menu_closed
    _queue_feedback_open: Any = window.FloatWindow._queue_feedback_open
    _open_feedback: Any = window.FloatWindow._open_feedback

    def _force_fetch(self):
        pass

    def _stop_face_guard_menu_timer(self):
        pass


class MenuTests(_FeedbackCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_real_menu_click_opens_dialog_and_reuses_draft(self):
        host = self.track_widget(_MenuHost())
        with tempfile.TemporaryDirectory() as directory:
            with patch("sf_constants.APP_SUPPORT_DIR", directory), patch.object(window, "feature_enabled", return_value=False):
                host.contextMenuEvent(QContextMenuEvent(
                    QContextMenuEvent.Reason.Mouse, QPoint(20, 20), QPoint(100, 100)))
                menu = host._ctx_menu  # discipline-ok(R5): inspect the real menu held by the handler under test
                action = next(a for a in menu.actions() if a.text() == "反馈问题")
                QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
                QApplication.processEvents()
                dialog = self.track_widget(host._feedback_dialog)  # discipline-ok(R5): inspect and clean up the dialog created by the real menu handler
                self.assertTrue(dialog.isVisible())
                dialog.description.setPlainText("draft")
                dialog.close()
                host._open_feedback()  # discipline-ok(R5): exercise the actual reopen handler under test
                self.assertIs(host._feedback_dialog, dialog)  # discipline-ok(R5): assert dialog reuse across real handler calls
                self.assertEqual(dialog.description.toPlainText(), "draft")

    def test_stale_hide_signal_cannot_destroy_replacement_menu(self):
        host = self.track_widget(_MenuHost())
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(20, 20), QPoint(100, 100))
        with patch.object(window, "feature_enabled", return_value=False), patch("PyQt6.QtWidgets.QMenu.popup"):
            host.contextMenuEvent(event)
            old = vars(host)["_ctx_menu"]
            host.contextMenuEvent(event)
            replacement = vars(host)["_ctx_menu"]
            old.aboutToHide.emit()
        self.assertIs(vars(host)["_ctx_menu"], replacement)

    def test_repeated_requests_queue_only_one_open_after_menu_event(self):
        host = self.track_widget(_MenuHost())
        with tempfile.TemporaryDirectory() as directory:
            with patch("sf_constants.APP_SUPPORT_DIR", directory), patch.object(
                    host, "_open_feedback", wraps=host._open_feedback) as opened:  # discipline-ok(R5): observe the real deferred opener without replacing its behavior
                host._queue_feedback_open()  # discipline-ok(R5): exercise deferred menu action
                host._queue_feedback_open()  # discipline-ok(R5): repeated action before event delivery
                opened.assert_not_called()
                QApplication.processEvents()
                opened.assert_called_once_with()
                dialog = self.track_widget(vars(host)["_feedback_dialog"])
                self.assertTrue(dialog.isVisible())
                self.assertFalse(vars(host)["_feedback_open_pending"])

    def test_reopening_closed_success_starts_a_new_feedback_without_history_selector(self):
        host = self.track_widget(_MenuHost())
        with tempfile.TemporaryDirectory() as directory, patch("sf_constants.APP_SUPPORT_DIR", directory):
            host._open_feedback()  # discipline-ok(R5): exercise feedback window lifecycle
            previous = self.track_widget(vars(host)["_feedback_dialog"])
            previous.stage = "received"
            host._open_feedback()  # discipline-ok(R5): visible completed window is only raised
            self.assertIs(vars(host)["_feedback_dialog"], previous)
            previous.close()
            host._open_feedback()  # discipline-ok(R5): reopening completed feedback creates a new draft
            current = self.track_widget(vars(host)["_feedback_dialog"])
            self.assertIsNot(current, previous)
            self.assertEqual(current.stage, "draft")
            self.assertFalse(current.description.isReadOnly())
            self.assertTrue(current.isVisible())
            self.assertFalse(current.history.isVisible())
