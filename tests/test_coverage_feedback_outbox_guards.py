"""本地队列的租约、回执与清理：把"另一个工人已经接手"写成看得见的拒绝。

这一层坏了的表现是**用户那侧的数据被写坏**：晚到的工人把新工人的结果覆盖掉、
清理到一半进程没了却再也没人重试、或一条已经拿到回执的记录又被重新上传。
所以每条判据都问同一件事：租约不匹配时，这一行有没有被改？
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sf_feedback as protocol
import sf_feedback_outbox as outbox_module
import sf_feedback_transport as transport
from sf_feedback_outbox import FeedbackOutbox, OutboxBusy
from sf_feedback_ui import FeedbackHistoryDialog
from tests._base import SFCase

NAIVE = datetime(2026, 10, 24, 10, 0)
ZONED = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)


def receipt_for(row):
    return {"case_id": "fixture-case", "archive_sha256": row["archive_sha256"],
            "size_bytes": row["archive_size"], "received_at": "2026-10-24T00:00:00+00:00",
            "storage_status": "stored", "validation_status": "validated",
            "retention_until": None, "retention_policy": "manual_deletion"}


class OutboxGuardTests(SFCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="outbox-guards-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "source.zip"
        protocol.build_archive(self.archive, {"state.json": "fixture"}, app_version="fixture",
                               bundle_id="com.skyrocket.app", run_id="fixture")
        self.store = FeedbackOutbox(self.root / "outbox.sqlite3", self.root / "objects")
        self.row = self.store.enqueue(self.archive, app_version="fixture",
                                      consent_policy_version="fixture")

    def claim(self, token="t" * 32):
        return self.store.claim(self.row["outbox_id"], kind="submit",
                                statuses=("locally_verified",), target_status="submitting",
                                token=token, lease_seconds=30, now=ZONED)

    def clear_lease(self):
        """把租约抹掉，模拟"另一个工人已经接手"。"""
        with self.store._db() as db:  # discipline-ok(R5): 跨进程租约状态没有公开写入口
            db.execute("UPDATE outbox SET lease_token=NULL, lease_kind=NULL, lease_until=NULL "
                       "WHERE outbox_id=?", (self.row["outbox_id"],))

    def test_a_database_that_refuses_its_schema_is_closed_and_reraised(self):
        """建表/迁移失败：连接必须回滚并关掉，错误照原样上抛，不许留半开的库。"""
        connection = mock.Mock()

        def execute(statement, *args):
            if statement.startswith("BEGIN"):
                raise sqlite3.OperationalError("table is locked")
            return mock.Mock()

        connection.execute.side_effect = execute
        with mock.patch.object(outbox_module, "sqlite3", mock.Mock(connect=lambda *a, **k: connection)):
            with self.assertRaises(sqlite3.OperationalError):
                FeedbackOutbox(self.root / "other.sqlite3", self.root / "other-objects")
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_a_lease_request_without_work_or_with_an_unknown_status_is_refused(self):
        broken = [
            {"kind": "", "statuses": ("locally_verified",)},
            {"kind": "submit", "statuses": ()},
            {"kind": "submit", "statuses": ("locally_verified",), "lease_seconds": 0},
            {"kind": "submit", "statuses": ("nonsense",)},
            {"kind": "submit", "statuses": ("locally_verified",), "target_status": "nonsense"},
        ]
        for index, kwargs in enumerate(broken):
            with self.subTest(request=index):
                with self.assertRaises(protocol.FeedbackProtocolError):
                    self.store.claim(self.row["outbox_id"], now=ZONED, **kwargs)
        self.assertEqual(self.store.get(self.row["outbox_id"])["status"], "locally_verified")

    def test_a_clock_without_a_timezone_is_refused(self):
        with self.assertRaises(protocol.FeedbackProtocolError):
            self.store.claim(self.row["outbox_id"], kind="submit", statuses=("locally_verified",),
                             now=NAIVE)
        with self.assertRaises(protocol.FeedbackProtocolError):
            self.store.recover_expired_leases(now=NAIVE)
        self.assertIsNone(self.store.get(self.row["outbox_id"])["lease_token"])

    def test_completing_a_lease_needs_the_status_token_and_fence(self):
        claimed = self.claim()
        broken = [
            {"token": claimed["lease_token"], "fence": claimed["lease_fence"], "status": "nonsense"},
            {"token": "", "fence": claimed["lease_fence"], "status": "response_unknown"},
            {"token": claimed["lease_token"], "fence": "1", "status": "response_unknown"},
            {"token": claimed["lease_token"], "fence": True, "status": "response_unknown"},
        ]
        for index, kwargs in enumerate(broken):
            with self.subTest(request=index):
                with self.assertRaises(protocol.FeedbackProtocolError):
                    self.store.finish_claim(self.row["outbox_id"], **kwargs)
        self.assertEqual(self.store.get(self.row["outbox_id"])["status"], "submitting")

    def test_a_completion_that_would_jump_states_is_refused(self):
        claimed = self.claim()
        with self.assertRaises(protocol.FeedbackProtocolError):
            self.store.finish_claim(self.row["outbox_id"], token=claimed["lease_token"],
                                    fence=claimed["lease_fence"], status="sealed", now=ZONED)
        self.assertEqual(self.store.get(self.row["outbox_id"])["lease_token"], "t" * 32)

    def test_a_lease_taken_over_underneath_is_never_written_over(self):
        """行还在、租约已不是我的：UPDATE 命中 0 行 → OutboxBusy，一条都不许改。"""
        claimed = self.claim()
        self.clear_lease()
        with mock.patch.object(self.store, "get", return_value=claimed):
            with self.assertRaises(OutboxBusy):
                self.store.finish_claim(self.row["outbox_id"], token=claimed["lease_token"],
                                        fence=claimed["lease_fence"], status="response_unknown",
                                        now=ZONED)
        current = self.store.get(self.row["outbox_id"])
        self.assertEqual(current["status"], "submitting", "租约丢了也不许把状态推进")
        self.assertIsNone(current["last_error"])

    def test_a_row_that_vanished_mid_flight_is_reported_as_missing_not_busy(self):
        claimed = self.claim()
        with self.store._db() as db:  # discipline-ok(R5): 制造"行被别人删掉"的现场没有公开入口
            db.execute("DELETE FROM outbox WHERE outbox_id=?", (self.row["outbox_id"],))
        with mock.patch.object(self.store, "get", return_value=claimed):
            with self.assertRaises(KeyError):
                self.store.finish_claim(self.row["outbox_id"], token=claimed["lease_token"],
                                        fence=claimed["lease_fence"], status="response_unknown",
                                        now=ZONED)

    def test_a_receipt_cannot_be_persisted_from_a_state_that_never_submitted(self):
        with self.assertRaises(protocol.FeedbackProtocolError):
            self.store.persist_claimed_receipt(self.row["outbox_id"], receipt_for(self.row),
                                               token="t" * 32, fence=1)
        self.assertNotIn("receipt", self.store.get(self.row["outbox_id"]))

    def test_a_late_receipt_loses_to_the_newer_worker(self):
        """已经交出去的行：晚到的回执只许被拒，不能把新工人的结果盖掉。"""
        claimed = self.claim()
        self.clear_lease()
        with mock.patch.object(self.store, "get", return_value=claimed):
            with self.assertRaises(OutboxBusy):
                self.store.persist_claimed_receipt(self.row["outbox_id"], receipt_for(claimed),
                                                   token=claimed["lease_token"],
                                                   fence=claimed["lease_fence"])
        current = self.store.get(self.row["outbox_id"])
        self.assertEqual(current["status"], "submitting", "晚到的回执不能替新工人做决定")
        self.assertNotIn("receipt", current)

    def test_history_limit_is_a_real_bound_not_a_default(self):
        for bad in (0, -1, 1001, "10", True):
            with self.subTest(limit=bad):
                with self.assertRaises(protocol.FeedbackProtocolError):
                    self.store.history(limit=bad)

    def received(self):
        """把一行推到"已收件，本机资料待清理"。"""
        claimed = self.claim()
        self.store.finish_claim(claimed["outbox_id"], token=claimed["lease_token"],
                                fence=claimed["lease_fence"], status="response_unknown",
                                now=ZONED)
        row = self.store.get(claimed["outbox_id"])
        return self.store.persist_receipt(row["outbox_id"], receipt_for(row))

    def point_archive_at_a_directory(self):
        directory = self.root / "objects" / "not-a-file"
        directory.mkdir()
        with self.store._db() as db:  # discipline-ok(R5): 现场是"归档位置被换成目录"，只能改这一行
            db.execute("UPDATE outbox SET archive_path=? WHERE outbox_id=?",
                       (str(directory), self.row["outbox_id"]))
        return directory

    def test_a_cleanup_that_cannot_delete_the_file_reports_the_failure(self):
        """清理失败必须写回 last_error 并松开租约，否则那条记录永远卡在"等待清理"。"""
        self.received()
        directory = self.point_archive_at_a_directory()
        with self.assertRaises(OSError):
            self.store.cleanup_local(self.row["outbox_id"])
        row = self.store.get(self.row["outbox_id"])
        self.assertIn(row["last_error"], ("IsADirectoryError", "PermissionError"),
                      "删除失败要把真实失败类型记下来，而不是留一个空错误")
        self.assertIsNone(row["lease_token"])
        self.assertEqual(row["status"], "cleanup_pending")
        self.assertTrue(directory.is_dir(), "清理失败不许把现场删掉")

    def test_a_failed_cleanup_is_returned_to_retry_by_the_next_startup(self):
        self.received()
        with self.store._db() as db:  # discipline-ok(R5): 同上，把归档位置指回真目录再让删除失败
            db.execute("UPDATE outbox SET archive_path=? WHERE outbox_id=?",
                       (str(self.root / "objects"), self.row["outbox_id"]))
        with self.assertRaises(OSError):
            self.store.cleanup_local(self.row["outbox_id"])
        self.assertEqual(self.store.recover_interrupted(now=ZONED), 1)
        row = self.store.get(self.row["outbox_id"])
        self.assertEqual(row["status"], "receipt_persisted")
        self.assertEqual(row["receipt"]["case_id"], "fixture-case")


class SubmitPendingFailureTests(SFCase):
    """上传失败时"松开租约"这一步自己也可能失败：原错误不能被它盖掉，但要留痕。"""

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="transport-finish-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "source.zip"
        protocol.build_archive(self.archive, {"state.json": "fixture"}, app_version="fixture",
                               bundle_id="com.skyrocket.app", run_id="fixture")
        self.store = FeedbackOutbox(self.root / "outbox.sqlite3", self.root / "objects")
        self.row = self.store.enqueue(self.archive, app_version="fixture",
                                      consent_policy_version="fixture")
        self.swallow = mock.Mock()
        self.track_patch(mock.patch("sf_logging.log_swallow", self.swallow))

    def test_a_lost_lease_during_the_failure_path_is_logged_and_the_original_error_wins(self):
        sender = mock.Mock(side_effect=RuntimeError("network down"))
        finish = mock.Mock(side_effect=OutboxBusy("newer worker owns the row"))
        with mock.patch.object(transport, "read_upload_archive", return_value={}), \
                mock.patch.object(self.store, "finish_claim", finish):
            with self.assertRaises(RuntimeError):
                transport.submit_pending(self.store, self.row["outbox_id"],
                                         "https://receiver.example.invalid/v1/feedback",
                                         sender=sender)
        finish.assert_called_once()
        self.swallow.assert_called_once()
        self.assertEqual(self.swallow.call_args.args[0],
                         "sf_feedback_transport.submit_pending.finish_claim")
        self.assertEqual(self.store.get(self.row["outbox_id"])["status"], "submitting")


class _FakeSignal:
    def __init__(self):
        self.callback = None

    def connect(self, callback):
        self.callback = callback


class _FakeLabel:
    def __init__(self, text, _parent=None):
        self._text = text

    def setWordWrap(self, _value):
        return None

    def setObjectName(self, _name):
        return None

    def setAlignment(self, _value):
        return None

    def setTextFormat(self, _value):
        return None

    def text(self):
        return self._text


class _FakeButton:
    def __init__(self, _parent=None):
        self.clicked = _FakeSignal()
        self.text = ""

    def setText(self, text):
        self.text = text

    def setToolTip(self, _text):
        return None


class _FakeBox:
    def __init__(self, _parent=None):
        self.label = None
        self.button = None

    def setObjectName(self, _name):
        return None

    def setAttribute(self, *_args):
        return None


class _FakeRowLayout:
    def __init__(self, owner=None):
        self.items = []
        self.owner = owner

    def setContentsMargins(self, *_margins):
        return None

    def setSpacing(self, _value):
        return None

    def count(self):
        return len(self.items)

    def takeAt(self, _index):
        class Item:
            def __init__(self, widget):
                self._widget = widget

            def widget(self):
                return self._widget

        return Item(self.items.pop(0))

    def addWidget(self, widget, *_args):
        self.items.append(widget)
        if isinstance(self.owner, _FakeBox):
            if isinstance(widget, _FakeLabel):
                self.owner.label = widget
            elif isinstance(widget, _FakeButton):
                self.owner.button = widget


class HistoryDialogDegradationTests(SFCase):
    """用纯替身执行记录页分支，不在覆盖率判据里重复创建原生 QDialog。"""

    def setUp(self):
        super().setUp()
        import sf_feedback_ui
        self.ui = sf_feedback_ui
        for patcher in (
                mock.patch.object(sf_feedback_ui, "QLabel", _FakeLabel),
                mock.patch.object(sf_feedback_ui, "QToolButton", _FakeButton),
                mock.patch.object(sf_feedback_ui, "QWidget", _FakeBox),
                mock.patch.object(sf_feedback_ui, "QHBoxLayout", lambda box: _FakeRowLayout(box))):
            self.track_patch(patcher)

    def dialog(self, outbox):
        dialog = FeedbackHistoryDialog.__new__(FeedbackHistoryDialog)  # discipline-ok(R5): 纯替身绕过原生 QDialog 构造，只覆盖 refresh/cleanup 分支
        dialog.outbox = outbox
        dialog.rows = _FakeRowLayout()
        return dialog

    def labels(self, dialog):
        return [item.text() for item in dialog.rows.items if hasattr(item, "text")]

    def test_an_unreadable_local_store_says_so_instead_of_showing_nothing(self):
        broken = mock.Mock()
        broken.history.side_effect = sqlite3.OperationalError("database is locked")
        dialog = self.dialog(broken)
        dialog.refresh()
        self.assertTrue(any(text.startswith("本机反馈记录暂时不可用") for text in self.labels(dialog)))
        broken.history.assert_called_once()

    def test_an_empty_store_is_reported_as_such(self):
        outbox = mock.Mock()
        outbox.history.return_value = []
        dialog = self.dialog(outbox)
        dialog.refresh()
        self.assertTrue(any(text.startswith("还没有反馈记录") for text in self.labels(dialog)))

    def test_a_failed_cleanup_leaves_the_row_visible_and_the_page_alive(self):
        row = {"outbox_id": "a" * 32, "case_id": "fixture-case", "received_at": None,
               "updated_at": "later", "app_version": "fixture", "status": "receipt_persisted",
               "last_error": None, "archive_size": 1, "archive_sha256_short": "a" * 12}
        outbox = mock.Mock()
        outbox.history.return_value = [row]
        outbox.cleanup_local.side_effect = sqlite3.OperationalError("disk full")
        dialog = self.dialog(outbox)
        dialog.refresh()
        self.assertIsNotNone(dialog.rows.items[0].button)
        dialog.refresh = mock.Mock()
        dialog._cleanup(row["outbox_id"])  # discipline-ok(R5): 清理槽函数本身没有公开调用入口
        outbox.cleanup_local.assert_called_once_with(row["outbox_id"])
        dialog.refresh.assert_called_once()

    def test_the_history_entry_stays_quiet_without_a_local_store(self):
        """没有本地队列（存储层没起来）时点入口不该建一个空壳窗口。"""
        host = SimpleNamespace(outbox=None, _history_dialog=None)
        self.ui.FeedbackDialog._open_history(host)  # discipline-ok(R5): 没有存储层时的短路只能直接调这个方法
        self.assertIsNone(host._history_dialog)  # discipline-ok(R5): 窗口句柄是本方法唯一的可观测产物
