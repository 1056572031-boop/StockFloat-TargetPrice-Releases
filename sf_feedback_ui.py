"""User-triggered capture, preview, consent, persistent upload and retry."""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QTabWidget, QVBoxLayout,
)

from sf_feedback import FeedbackProtocolError, build_archive
from sf_feedback_capture import (
    CONSENT_VERSION, MAX_DESCRIPTION, package_files, read_upload_archive, validate_upload_receipt,
)
from sf_feedback_outbox import FeedbackOutbox
from sf_feedback_transport import endpoint_url, submit_pending
from sf_glass import GlassMixin, paint_popup_surface, popup_stylesheet, prepare_popup_surface
from sf_constants import FG_DISABLED_HEX, FG_TEXT_HEX

_BLOCKING_ERRORS = {"configuration_or_storage_unavailable", "configuration_unavailable",
                    "capture_failed", "local_package_invalid"}


def _failure(phase, exc):
    for kind, category in ((HTTPError, "HTTPError"), (URLError, "URLError"),
                           (TimeoutError, "TimeoutError"), (PermissionError, "PermissionError"),
                           (FileNotFoundError, "FileNotFoundError"), (OSError, "OSError"),
                           (sqlite3.Error, "SQLiteError"), (FeedbackProtocolError, "FeedbackProtocolError"),
                           (ValueError, "ValueError"), (RuntimeError, "RuntimeError")):
        if isinstance(exc, kind):
            return {"phase": phase, "category": category}
    return {"phase": phase, "category": "unexpected_error"}


class FeedbackDialog(GlassMixin, QDialog):
    _prefer_liquid_glass = True

    def __init__(self, parent=None, *, snapshot, storage_dir, endpoint, sender=None, capture_again=None):
        super().__init__(parent)
        self._init_glass_state()
        self._drag_origin = None
        if isinstance(parent, GlassMixin):
            self.set_surface_owner(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setSizeGripEnabled(True)
        self.setStyleSheet(popup_stylesheet(
            "QLabel{font-size:12px;} QLabel#feedbackTitle{font-size:14px;font-weight:bold;}"
            "QPlainTextEdit{background:rgba(255,255,255,0.06);border:1px solid rgba(154,163,181,0.30);"
            "border-radius:6px;padding:8px;font-size:12px;selection-background-color:rgba(79,124,255,0.65);}"
            "QPlainTextEdit:focus{border:1px solid rgba(174,190,212,0.70);}"
            "QTabWidget::pane{border:0;background:transparent;}"
            "QTabBar::tab{padding:6px 12px;background:rgba(255,255,255,0.06);border:0;}"
            "QTabBar::tab:selected{background:rgba(255,255,255,0.18);}"
            "QCheckBox{font-size:12px;spacing:6px;} QComboBox{font-size:12px;padding:5px 8px;"
            "border:1px solid rgba(154,163,181,0.30);border-radius:6px;}"
            "QComboBox QAbstractItemView{background:rgb(36,36,36);color:%s;}"
            "QCheckBox:disabled{color:%s;}" % (FG_TEXT_HEX, FG_DISABLED_HEX)))
        self.snapshot = snapshot
        self.draft_snapshot = json.loads(json.dumps(snapshot))
        self.capture_again = capture_again
        self.endpoint = endpoint
        self.sender = sender
        self.storage_dir = Path(storage_dir)
        self.outbox = None
        self.worker = None
        self.results: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.current_id = None
        self.draft_key = None
        self.storage_uncertain = False
        self.failure_count = 0
        self.stage = "draft"
        self.last_error = ""
        self.last_failure_phase = ""
        self.preview_valid = False
        self.preview_files: dict[str, str] | None = None
        self.validation_error = ""
        self.setWindowTitle("反馈问题")
        self.setMinimumSize(440, 470)
        self.resize(540, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        self.title = QLabel("反馈问题", self)
        self.title.setObjectName("feedbackTitle")
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.title)
        self.tabs = QTabWidget(self)
        self.description = QPlainTextEdit(self)
        self.description.setTabChangesFocus(True)
        self.description.setAccessibleName("问题描述（可选）")
        self.description.setPlaceholderText("可补充操作步骤、预期与实际结果。请勿填写密码、账户或交易明细。")
        self.tabs.addTab(self.description, "问题描述")
        self.preview = QPlainTextEdit(self)
        self.preview.setTabChangesFocus(True)
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName("实际上传内容预览")
        self.tabs.addTab(self.preview, "上传内容")
        layout.addWidget(self.tabs, 1)
        self.counter = QLabel(self)
        layout.addWidget(self.counter)
        policy = QLabel(
            "接收方：Skyrocket 维护者；经 Cloudflare 转发，存储于 GitHub 私有仓库 Skyrocket-Private-Feedback。"
            "发送脱敏诊断计数、运行标识、版本和下方确认的描述；不含原始日志、股票配置、截图、摄像头内容。"
            "保留至维护者按反馈编号人工删除；无自动到期删除，不承诺平台备份即时删除。"
            "生成并保存后可在本机重试；未保存草稿退出后会丢失。", self)
        policy.setWordWrap(True)
        layout.addWidget(policy)
        self.consent = QCheckBox("我已核对上传内容，同意发送诊断信息和填写的描述", self)
        layout.addWidget(self.consent)
        self.history = QComboBox(self)
        self.history.setAccessibleName("本机待处理反馈")
        layout.addWidget(self.history)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.close_button = QPushButton("关闭", self)
        self.close_button.setObjectName("cancel")
        self.close_button.setAutoDefault(False)
        self.close_button.setToolTip("关闭窗口不会取消正在进行的上传。")
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.close_button)
        buttons.addStretch()
        self.upload_button = QPushButton("上传反馈", self)
        self.upload_button.setObjectName("ok")
        self.upload_button.setDefault(True)
        self.upload_button.clicked.connect(self._upload)
        buttons.addWidget(self.upload_button)
        layout.addLayout(buttons)
        self.description.textChanged.connect(self._draft_changed)
        self.consent.toggled.connect(self._update_enabled)
        self.history.currentIndexChanged.connect(self._select_pending)
        self.poll = QTimer(self)
        self.poll.setInterval(100)
        self.poll.timeout.connect(self._poll)
        self._draft_changed()
        try:
            self.storage_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.storage_dir.chmod(0o700)
            self.outbox = FeedbackOutbox(self.storage_dir / "outbox.sqlite3", self.storage_dir / "objects")
            self.outbox.recover_interrupted()
            self.history.addItem("新建反馈", None)
            for row in self.outbox.pending():
                if row["status"] in ("locally_verified", "response_unknown", "receipt_persisted"):
                    created = datetime.fromisoformat(row["created_at"]).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
                    self.history.addItem(created + " · " + row["outbox_id"][:8], row["outbox_id"])
            endpoint_url(endpoint)
        except Exception:
            self.last_error = "configuration_or_storage_unavailable"
            self.status.setText("反馈服务或本地存储尚未就绪，暂不能上传。原有“导出诊断包”仍可使用。")
        self._update_enabled()
        self.description.setFocus()
        prepare_popup_surface(self)

    def showEvent(self, event):
        super().showEvent(event)
        if self._glass_pending:
            self._glass_pending = False
            QTimer.singleShot(0, self._inject_glass)

    def hideEvent(self, event):
        self._drag_origin = None
        self._close_glass()
        super().hideEvent(event)

    def moveEvent(self, event):
        super().moveEvent(event)
        self._sync_glass_frame(force_mask=True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_glass_frame(force_mask=True)

    def paintEvent(self, event):
        self._glass_mask_flush()
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            paint_popup_surface(self, painter)
        finally:
            painter.end()

    def mousePressEvent(self, event):
        self._drag_origin = None
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= self.title.geometry().bottom() + 8:
            handle = self.windowHandle()
            if handle is None or not handle.startSystemMove():
                self._drag_origin = event.globalPosition().toPoint() - self.pos()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_origin = None
        super().mouseReleaseEvent(event)

    def _draft_changed(self):
        if self.current_id is None and self.stage == "draft":
            self.draft_key = None
        text = self.description.toPlainText()
        self.counter.setText("%d / %d" % (len(text), MAX_DESCRIPTION))
        self.consent.setChecked(False)
        if self.current_id is not None:
            self._update_enabled()
            return
        previous_error = self.validation_error
        try:
            self.preview_files = package_files(self.snapshot, text)
            self.preview_valid = True
            self.validation_error = ""
            self._render_preview()
        except Exception:
            self.preview_files = None
            self.preview_valid = False
            self.validation_error = "无法完成脱敏或描述超过 4000 字，暂不能上传。"
            self.preview.setPlainText("无法完成脱敏或描述超过 4000 字，禁止上传。")
        if self.stage == "draft" and self.last_error not in _BLOCKING_ERRORS:
            if self.validation_error:
                self.status.setText(self.validation_error)
            elif self.status.text() == previous_error:
                self.status.clear()
        self._update_enabled()

    def _render_preview(self):
        files = self.preview_files
        if files is not None:
            self.preview.setPlainText(json.dumps(json.loads(files["state.json"]), ensure_ascii=False, indent=2)
                                      + "\n\n--- problem.txt ---\n" + files["problem.txt"])

    def _update_enabled(self):
        busy = self.worker is not None and self.worker.is_alive()
        valid = self.preview_valid and self.preview_files is not None and len(self.description.toPlainText()) <= MAX_DESCRIPTION
        self.upload_button.setEnabled(bool(self.outbox is not None and self.last_error not in _BLOCKING_ERRORS
                                           and valid and self.consent.isChecked() and not busy
                                           and self.stage != "received"))

    def _select_pending(self):
        if self.outbox is None:
            return
        self.storage_uncertain = False
        self.current_id = self.history.currentData()
        self.last_error = ""
        try:
            endpoint_url(self.endpoint)
        except Exception:
            self.last_error = "configuration_unavailable"
        if self.current_id:
            self.preview_valid = False
            self.preview_files = None
            self.stage = "pending"
            try:
                row = self.outbox.get(self.current_id)
                packet = read_upload_archive(row["archive_path"], row["envelope"])
                self.snapshot = packet["state"]
                self.description.setPlainText(packet["files"]["problem.txt"])
                self.preview_files = packet["files"]
                self.preview_valid = True
                self.validation_error = ""
                self._render_preview()
                if row["status"] == "receipt_persisted":
                    validate_upload_receipt(row["receipt"], row["envelope"])
                self.stage = "received" if row["status"] == "receipt_persisted" else "pending"
                self.status.setText(
                    "GitHub 已收件：" + row["receipt"]["case_id"] if self.stage == "received"
                    else "上次提交尚未确认收件；重试将使用同一反馈包，不会重新采集。")
            except Exception:
                self.last_error = "local_package_invalid"
                self.preview_valid = False
                self.preview_files = None
                self.description.clear()
                self.preview.setPlainText("待处理包或回执不符合当前上传协议，禁止提交。")
                self.status.setText("本地待处理反馈损坏或格式不符合要求，禁止提交。请保留诊断包联系维护者。")
        else:
            if self.capture_again is not None and self.stage != "draft":
                try:
                    self.draft_snapshot = self.capture_again()
                except Exception:
                    self.last_error = "capture_failed"
                    self.status.setText("重新采集失败，未上传。请保留本地诊断包。")
            self.stage = "draft"
            self.snapshot = json.loads(json.dumps(self.draft_snapshot))
            self.description.clear()
            self._draft_changed()
            if not self.last_error and not self.validation_error:
                self.status.clear()
        self.description.setReadOnly(bool(self.current_id))
        self.consent.setEnabled(self.stage != "received")
        self.consent.setChecked(False)
        self.upload_button.setText("已上传" if self.stage == "received" else
                                   "重试上传" if self.stage == "pending" else "上传反馈")
        self._update_enabled()

    def _upload(self):
        outbox = self.outbox
        if outbox is None or not self.upload_button.isEnabled():
            return
        try:
            if self.preview_files is None:
                raise FeedbackProtocolError("没有已确认的上传预览")
            files = dict(self.preview_files)
            snapshot = json.loads(files["state.json"])
        except Exception as exc:
            self.results.put(("failed", _failure("prepare", exc)))
            self._poll()
            return
        self.description.setReadOnly(True)
        self.history.setEnabled(False)
        self.consent.setEnabled(False)
        self.upload_button.setEnabled(False)
        self.status.setText("正在生成并校验反馈包…")
        self.stage = "submitting"
        pending_id = self.current_id
        self.draft_key = self.draft_key or uuid.uuid4().hex
        draft_key = self.draft_key

        def work():
            phase = "recovery"
            try:
                if pending_id is None:
                    # A previous enqueue can commit before its readback fails.
                    # Recover its identity before generating another ZIP.
                    row = outbox.find_by_idempotency_key(draft_key)
                    if row is None:
                        self.results.put(("not_saved", None))
                        phase = "archive"
                        with tempfile.TemporaryDirectory(prefix="capture-", dir=self.storage_dir) as directory:
                            path = Path(directory) / "feedback.zip"
                            build_archive(path, files, app_version=snapshot["app_version"],
                                          bundle_id=snapshot["bundle_id"], run_id=snapshot["run_id"],
                                          omitted=snapshot["omitted"], truncated=snapshot["truncated"],
                                          capture_started_at=snapshot["capture_started_at"],
                                          capture_completed_at=snapshot["capture_completed_at"])
                            phase = "enqueue"
                            row = outbox.enqueue(path, app_version=snapshot["app_version"],
                                                 consent_policy_version=CONSENT_VERSION,
                                                 idempotency_key=draft_key)
                    outbox_id = row["outbox_id"]
                else:
                    outbox_id = pending_id
                self.results.put(("sealed", outbox_id))
                phase = "submit"
                kwargs = {"sender": self.sender} if self.sender is not None else {}
                receipt = submit_pending(outbox, outbox_id, self.endpoint, **kwargs)
                self.results.put(("received", receipt))
            except Exception as exc:
                self.results.put(("failed", _failure(phase, exc)))

        try:
            self.worker = threading.Thread(target=work, name="feedback-upload", daemon=True)
            self.worker.start()
        except Exception as exc:
            self.results.put(("failed", _failure("worker_start", exc)))
            self._poll()
            return
        self.poll.start()

    def _poll(self):
        # Only stop after observing a finished worker before draining its queue.
        # It may publish the final result between an empty check and thread exit.
        finished = self.worker is None or not self.worker.is_alive()
        while not self.results.empty():
            kind, value = self.results.get_nowait()
            if kind == "not_saved":
                self.storage_uncertain = False
            elif kind == "sealed":
                self.storage_uncertain = False
                self.current_id = value
                self.history.blockSignals(True)
                index = self.history.findData(value)
                if index < 0:
                    self.history.addItem("本次反馈 · " + value[:8], value)
                    index = self.history.count() - 1
                self.history.setCurrentIndex(index)
                self.history.blockSignals(False)
                self.status.setText("正在上传；未收到校验回执前，不代表 GitHub 已收件。")
            elif kind == "received":
                self.stage = "received"
                self.upload_button.setText("已上传")
                self.status.setText("GitHub 已收件：%s\n下载和分析在维护者电脑进行，不代表已分析或已修复。" % value["case_id"])
            else:
                if value["phase"] in ("enqueue", "recovery"):
                    self.storage_uncertain = True
                self.stage = ("pending" if self.current_id else
                              "uncertain" if self.storage_uncertain else "draft")
                self.last_error = value["category"]
                self.last_failure_phase = value["phase"]
                self.failure_count += 1
                logging.getLogger(__name__).warning(
                    "feedback failure: phase=%s category=%s queued=%s",
                    self.last_failure_phase, self.last_error, self.current_id is not None)
                self.status.setText(
                    "上传未完成或结果未知。反馈已进入本机待提交队列；重试使用原反馈编号。"
                    if self.current_id else
                    "保存结果未知。草稿仍在当前窗口，退出可能丢失；重试会先核对原记录。"
                    if self.stage == "uncertain" else
                    "反馈包尚未保存。草稿仅保留在当前窗口，退出程序会丢失；请重试。")
                self.upload_button.setText("核对并重试" if self.stage == "uncertain" else "重试上传")
        if finished:
            self.worker = None
            self.poll.stop()
            self.history.setEnabled(True)
            self.consent.setEnabled(self.stage != "received")
            self.description.setReadOnly(self.current_id is not None or self.stage == "uncertain")
            self._update_enabled()

    def closeEvent(self, event):
        # Closing hides the dialog, not the durable submission. No cancellation
        # promise is made after bytes may have left the machine.
        super().closeEvent(event)


def feedback_diagnostics(owner):
    dialog = vars(owner).get("_feedback_dialog")
    if dialog is None:
        return {"opened": False}
    state = vars(dialog)
    return {"opened": True, "stage": state.get("stage"),
            "failure_count": state.get("failure_count", 0),
            "last_error": state.get("last_error", ""),
            "last_failure_phase": state.get("last_failure_phase", ""),
            "storage_uncertain": state.get("storage_uncertain", False),
            "outbox_ready": state.get("outbox") is not None}
