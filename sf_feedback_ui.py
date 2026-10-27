"""Explicit feedback upload with sealed diagnostics and persistent retry."""

from __future__ import annotations

import html
import json
import logging
import queue
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QGuiApplication, QPainter
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QScrollArea, QToolButton, QVBoxLayout, QWidget,
)

from sf_feedback import FeedbackProtocolError, build_archive
from sf_feedback_capture import (
    CONSENT_VERSION, MAX_DESCRIPTION, package_files, read_upload_archive, validate_capture_context, validate_upload_receipt,
)
from sf_feedback_outbox import FeedbackOutbox, OutboxBusy
from sf_feedback_bundle import FULL_CONSENT_VERSION, FULL_V3_CONSENT_VERSION, read_diagnostics
from sf_feedback import utc_now
from sf_feedback_versions import PAYLOAD_V3, is_full_payload
from sf_logging import feature_enabled, log_line, log_swallow
from sf_feedback_transport import endpoint_url, server_reply, submit_pending
from sf_glass import GlassMixin, paint_popup_surface, popup_stylesheet, prepare_popup_surface
from sf_editor import MainCloseButton
from sf_constants import FG_BRIGHT_HEX, FG_DISABLED_HEX, FG_HINT_HEX, FG_TEXT_HEX

_BLOCKING_ERRORS = {"configuration_or_storage_unavailable", "configuration_unavailable",
                    "capture_failed", "local_package_invalid"}
# 弹窗尺寸（2026-10-22 用户要求：输入框的宽、高各缩为原来的一半）。
# 原来窗口 540x500、输入框实测 500x383；四周留白与标题、按钮行（共 117pt）不变，
# 所以输入框 250x192 对应窗口 290x309。最小尺寸同理：原 440x410 → 保住按钮行放得下。
_DEFAULT_SIZE = (290, 309)
_MIN_SIZE = (270, 290)
_TITLE_BUTTON_H = 24    # 标题行「记录」按钮的高度（样式表加内边距之前的实测值）
# 标题行里控件与标题之间的水平间隙（整窗用 12，这一行刻意紧一档）。
_TITLE_ROW_GAP = 8
# 本进程已经“第一次看过”的反馈存储目录；见 FeedbackDialog.__init__ 里的恢复说明。
_RECOVERED_STORES: set[str] = set()
# 描述写到多少字开始显示字数（上限的 90%）。
_COUNTER_SHOW_AT = int(MAX_DESCRIPTION * 0.9)
_FULL_UPLOAD_HINT = "发送完整脱敏诊断资料，包含运行日志、设置和功能状态；会去除已识别的凭据与身份字段，不采集摄像头画面。"
_V3_UPLOAD_HINT = ("发送完整脱敏诊断资料，包含运行日志、设置和功能状态，以及运行环境、权限状态、反馈偏好和本次反馈上下文；"
                   "会去除已识别的凭据与身份字段，不采集摄像头画面或截图。")
_LEGACY_UPLOAD_HINT = "按原资料提交旧版反馈；不会重新采集或添加完整诊断内容。"


def _upload_hint(payload_version: int) -> str:
    if payload_version == PAYLOAD_V3:
        return _V3_UPLOAD_HINT
    if is_full_payload(payload_version):
        return _FULL_UPLOAD_HINT
    return _LEGACY_UPLOAD_HINT


def _new_payload_version(diagnostic_capture) -> int:
    return PAYLOAD_V3 if diagnostic_capture is not None and feature_enabled("feedback_v3") else 2 if diagnostic_capture is not None else 1


def _failure(phase, exc):
    for kind, category in ((HTTPError, "HTTPError"), (URLError, "URLError"),
                           (TimeoutError, "TimeoutError"), (PermissionError, "PermissionError"),
                           (FileNotFoundError, "FileNotFoundError"), (OSError, "OSError"),
                           (sqlite3.Error, "SQLiteError"), (FeedbackProtocolError, "FeedbackProtocolError"),
                           (ValueError, "ValueError"), (RuntimeError, "RuntimeError")):
        if isinstance(exc, kind):
            return {"phase": phase, "category": category}
    return {"phase": phase, "category": "unexpected_error"}


# 服务端回的错误码只收这种形状（小写字母、数字、下划线），其余一律丢弃：
# 日志与诊断包里不许出现异常原文或服务端返回的自由文本。
_SERVER_CODE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_ERROR_BODY_PEEK = 2048


def _failure_detail(exc, started):
    """上传失败的可查证据：HTTP 状态码、服务端错误码、耗时。

    之前失败只记 "HTTPError" 一个类别，分不出是服务端限流（429）、同一反馈仍在处理（409）、
    还是上游超时（503），也看不出用户等了多久。这里只取三样白名单信息，不碰异常原文。
    """
    detail = {"elapsed_sec": round(max(0.0, time.monotonic() - started), 1)}
    if isinstance(exc, HTTPError):
        detail["http_status"] = int(exc.code)
        # 响应体只能读一次；与队列写失败原因共用 transport 的缓存读取，谁先读都不会让另一个拿到空。
        detail.update(server_reply(exc))
    elif isinstance(exc, URLError):
        reason = exc.reason
        # 只认异常对象的类名（超时单列）；reason 是纯文本时类型名是 "str"，没有诊断意义。
        name = type(reason).__name__
        detail["network_reason"] = ("timeout" if isinstance(reason, TimeoutError)
                                    else name if isinstance(reason, BaseException) and name.isidentifier()
                                    else "unknown")
    elif isinstance(exc, TimeoutError):
        # 连上之后读响应超时抛的是裸 TimeoutError，不是 URLError，之前认不出来。
        detail["network_reason"] = "timeout"
    return detail


# 服务端明确拒收内容、重试不会改变结果的错误码（来自 services/feedback-worker 的 Rejection）。
_REJECTED_CODES = frozenset({
    "request_headers", "envelope_limit", "body_limit", "request_size", "description_limit",
    "diagnostic_member", "diagnostic_members", "diagnostic_missing_category", "diagnostic_export",
    "diagnostic_inventory", "diagnostic_secret", "diagnostic_inflated_limit", "diagnostic_size",
    "diagnostic_v3_missing", "diagnostic_v3_mixed", "diagnostic_v3_snapshot", "diagnostic_local_header",
    "zip_member", "zip_members", "inflated_limit", "identity_conflict", "remote_binding", "archive_invalid"})
# 服务端明确说“内容有问题”的 HTTP 状态：请求体过大、内容校验不过。具体错误码有三十多个，
# 逐个列会漏（审查时就漏了 zip_members），所以按状态兜底；列表只是更早、更明确的一层。
_REJECTED_STATUSES = frozenset({413, 422})
# 服务端容量已满：同样不是“稍后再试”能解决的（案件数、资产数是永久上限）。
_CAPACITY_CODES = frozenset({"case_budget", "inbox_asset_capacity", "inbox_capacity"})
# 409 里真正“等一会儿就好”的只有这两种；其余 409（身份冲突、远端校验和不符）等多久都一样。
_WAIT_CODES = frozenset({"submission_in_progress", "lease_superseded"})


def _failure_kind(detail):
    """失败属于哪一类：rejected（内容被拒）/ full（服务端容量满）/ retry（可以再试）。"""
    code = detail.get("server_code", "")
    if code in _CAPACITY_CODES:
        return "full"
    if code in _REJECTED_CODES or detail.get("http_status") in _REJECTED_STATUSES:
        return "rejected"
    return "retry"


def _failure_hint(detail):
    """把状态码翻成用户能照着做的一句话；认不出的返回空串，不编原因。"""
    code, status = detail.get("server_code", ""), detail.get("http_status")
    kind = _failure_kind(detail)
    if kind == "full":
        return "接收服务的存储已满，再试也不会成功。请用右键菜单“导出诊断包”，把文件发给维护者。"
    if kind == "rejected":
        return "这份反馈被接收服务拒收，现在重试多半仍会失败。请用右键菜单“导出诊断包”，把文件发给维护者；问题修复后可再回来重试。"
    if code in _WAIT_CODES or (status == 409 and not code):
        return "上一次提交可能仍在处理，请等 1~2 分钟再点重试。"
    if code == "daily_client_budget":
        return "这台电脑今天的上传次数已用完，请明天再试；已保存的反馈不会丢。"
    if code in ("receiver_busy", "daily_budget", "daily_byte_budget") or status == 429:
        wait = detail.get("retry_after_sec")
        if code == "receiver_busy" and wait:
            return "接收服务当前较忙，请约 %d 秒后再试。" % wait
        return "接收服务当前较忙或今日次数已满，请稍后再试。"
    if code == "upstream_timeout":
        return "接收服务连接上游超时，请稍后重试；已保存的反馈不会丢。"
    if detail.get("network_reason") == "timeout":
        return "连接接收服务超时，请检查网络后重试。"
    if status is not None and status >= 500:
        return "接收服务暂时不可用，请稍后重试。"
    return ""


# 失败后反馈窗保持隐藏；可重试时告诉用户怎么回来，不要让他自己猜。
_REOPEN_TAIL = "可在右键菜单“反馈问题”里重新打开，继续重试。"


# 记录页每个状态的说明文字，以及是否属于“还没落定”（待提交/上传中/待确认）。
# 落定的（已收件）用淡色，没落定的用亮色加粗，一眼分得出哪几条还需要处理。
_HISTORY_STATUS = {
    "locally_verified": ("待提交", True),
    "submitting": ("正在上传，稍后可重试", True),
    "response_unknown": ("等待确认，可重试", True),
    "receipt_persisted": ("已收件，本机资料待清理", False),
    "cleanup_pending": ("已收件，等待本机清理", False),
    # 已清理的记录不在记录页展示（history(include_cleaned=False)），所以这里没有 cleaned。
}
_HISTORY_STYLE = (
    "QLabel{font-size:12px;} QLabel#feedbackTitle{font-size:14px;font-weight:bold;}"
    "QLabel#feedbackEmpty{color:%s;padding:24px 0;}"
    "QWidget#feedbackRow{background:rgba(255,255,255,0.06);"
    "border:1px solid rgba(154,163,181,0.22);border-radius:8px;}"
    "QToolButton{background:rgba(255,255,255,0.10);color:%s;border:none;border-radius:6px;"
    "padding:4px 10px;font-size:12px;} QToolButton:hover{background:rgba(255,255,255,0.18);}"
    "QScrollArea{background:transparent;border:none;}"
    "QScrollBar:vertical{background:transparent;width:8px;margin:2px 0;}"
    "QScrollBar::handle:vertical{background:rgba(154,163,181,0.40);border-radius:4px;min-height:24px;}"
    "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
    "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
    % (FG_HINT_HEX, FG_TEXT_HEX))


def _history_time(value):
    """ISO 时间转成本机时区的“月-日 时:分”；解析不了就原样显示，不丢信息。"""
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value or "")


def _history_row_text(case_id, stamp, version, status, waiting):
    """记录行的富文本：编号加粗，时间与版本淡色小字，状态单独一行。"""
    version = str(version or "")
    if version[:1].isdigit():
        version = "v" + version
    state_style = ("color:%s;font-weight:bold" % FG_BRIGHT_HEX if waiting else "color:%s" % FG_HINT_HEX)
    return ('<span style="color:%s;font-weight:bold">%s</span><br/>'
            '<span style="color:%s;font-size:11px">%s · %s</span><br/>'
            '<span style="%s">%s%s</span>') % (
        FG_TEXT_HEX, html.escape(str(case_id)), FG_HINT_HEX,
        html.escape(_history_time(stamp)), html.escape(version),
        state_style, "● " if waiting else "", html.escape(status))


class _TitleDragDialog(GlassMixin, QDialog):
    """无边框弹窗基类：按住标题行及其下方 8px 拖动整窗（反馈窗与记录窗共用）。"""

    title: QLabel
    _drag_origin: QPoint | None = None

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


class FeedbackHistoryDialog(_TitleDragDialog):
    """Read-only feedback receipt history; never opens local archives."""

    _prefer_liquid_glass = True

    def __init__(self, outbox, parent=None):
        super().__init__(parent)
        self._init_glass_state()
        self._drag_origin = None
        if isinstance(parent, GlassMixin):
            self.set_surface_owner(parent)
        # 与反馈窗同一套外观：无边框、置顶工具窗、半透明底（原先是带系统标题栏的普通窗，
        # 层级也低于被抬高的反馈窗，点开后会沉到下面）。
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setSizeGripEnabled(True)
        self.setStyleSheet(popup_stylesheet(_HISTORY_STYLE))
        self.outbox = outbox
        self.setWindowTitle("反馈记录")
        self.setMinimumSize(340, 260)
        self.resize(400, 380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        self.title = QLabel("反馈记录", self)
        self.title.setObjectName("feedbackTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(0)
        self.close_button = MainCloseButton(self)
        self.close_button.setFixedSize(20, 20)
        self.close_button.setAccessibleName("关闭反馈记录窗口")
        self.close_button.setToolTip("关闭反馈记录")
        self.close_button.setAutoDefault(False)
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.clicked.connect(self.close)
        # 关闭键在右上角；左侧用同宽的纯布局间距镜像它，标题仍落在整窗中线。
        title_row.addSpacing(self.close_button.width())
        title_row.addWidget(self.title, 1)
        title_row.addWidget(self.close_button)
        layout.addLayout(title_row)
        # 记录最多 100 条，必须能滚动；行容器与视口都保持透明，让毛玻璃露出来。
        body = QWidget(self)
        self.rows = QVBoxLayout(body)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(8)
        self.rows.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setWidget(body)
        viewport = self.scroll_area.viewport()
        if viewport is not None:
            viewport.setAutoFillBackground(False)
        layout.addWidget(self.scroll_area, 1)
        # 关闭入口统一放在标题行右上角；底部不再保留第二枚文字关闭按钮。
        self.refresh()
        prepare_popup_surface(self)

    def present(self, anchor=None):
        """摆在锚点窗口中心（夹在所属屏幕可用区内）后显示并前置。"""
        anchor = anchor or self.parentWidget()
        if anchor is not None and not self.isVisible():
            screen = QGuiApplication.screenAt(anchor.frameGeometry().center()) or QGuiApplication.primaryScreen()
            center = anchor.frameGeometry().center()
            x, y = center.x() - self.width() // 2, center.y() - self.height() // 2
            if screen is not None:
                area = screen.availableGeometry()
                x = max(area.left(), min(x, area.right() - self.width() + 1))
                y = max(area.top(), min(y, area.bottom() - self.height() + 1))
            self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

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

    def refresh(self):
        while self.rows.count():
            item = self.rows.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        try:
            rows = self.outbox.history(include_cleaned=False)
        except Exception as exc:
            log_swallow("sf_feedback_ui.FeedbackHistoryDialog.refresh", exc)
            self._add_empty_note("本机反馈记录暂时不可用。")
            return
        if not rows:
            self._add_empty_note("还没有反馈记录。")
            return
        for row in rows:
            box = QWidget(self)
            box.setObjectName("feedbackRow")
            box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            line = QHBoxLayout(box)
            line.setContentsMargins(12, 10, 12, 10)
            line.setSpacing(10)
            status, waiting = _HISTORY_STATUS.get(row["status"], ("本机记录", False))
            label = QLabel(_history_row_text(row["case_id"] or "本机反馈", row["received_at"] or row["updated_at"],
                                             row["app_version"], status, waiting), box)
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setWordWrap(True)
            line.addWidget(label, 1)
            if row["status"] == "receipt_persisted":
                clean = QToolButton(box)
                clean.setText("清理")
                clean.setToolTip("清理本机诊断资料，清理后这条记录不再显示；不会删除已收件的远端反馈。")
                # 用户 2026-10-27 要求：点“清理”就直接清理，不再二次确认。
                clean.clicked.connect(lambda _=False, ident=row["outbox_id"]: self._cleanup(ident))
                line.addWidget(clean)
            self.rows.addWidget(box)

    def _add_empty_note(self, text):
        note = QLabel(text, self)
        note.setObjectName("feedbackEmpty")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rows.addWidget(note)

    def _cleanup(self, outbox_id):
        try:
            self.outbox.cleanup_local(outbox_id)
        except OutboxBusy:
            # 快速连点时，第二下会打在刚被清理（或正被别处清理）的记录上：行状态已经变了，
            # 不是故障。只刷新让界面回到真实状态；记成带堆栈的警告会把正常连点算进日志线索。
            log_line("feedback local cleanup skipped: record already cleaned or busy", "INFO")
        except Exception:
            logging.getLogger(__name__).warning("feedback local cleanup failed", exc_info=True)
        self.refresh()


class FeedbackDialog(_TitleDragDialog):
    _prefer_liquid_glass = True

    def __init__(self, parent=None, *, snapshot, storage_dir, endpoint, sender=None, capture_again=None,
                 diagnostic_capture=None):
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
            "QToolButton{background:rgba(255,255,255,0.10);color:%s;border:none;border-radius:6px;"
            "padding:4px 10px;font-size:12px;} QToolButton:hover{background:rgba(255,255,255,0.18);}"
            "QCheckBox{font-size:12px;spacing:6px;} QComboBox{font-size:12px;padding:5px 8px;"
            "border:1px solid rgba(154,163,181,0.30);border-radius:6px;}"
            "QComboBox QAbstractItemView{background:rgb(36,36,36);color:%s;}"
            "QCheckBox:disabled{color:%s;}" % (FG_TEXT_HEX, FG_TEXT_HEX, FG_DISABLED_HEX)))
        self.snapshot = snapshot
        self.draft_snapshot = json.loads(json.dumps(snapshot))
        self.capture_again = capture_again
        self.diagnostic_capture = diagnostic_capture
        self.payload_version = _new_payload_version(diagnostic_capture)
        self.full_capture = None
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
        self.last_failure_detail: dict[str, Any] = {}
        self.preview_valid = False
        self.preview_files: dict[str, Any] | None = None
        self.validation_error = ""
        self._feedback_status_dialog = None
        self._history_dialog: FeedbackHistoryDialog | None = None
        self.setWindowTitle("反馈问题")
        self.setMinimumSize(*_MIN_SIZE)
        self.resize(*_DEFAULT_SIZE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        self.title = QLabel("反馈问题", self)
        self.title.setObjectName("feedbackTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        # 这一行的间隙全部走显式 addSpacing，布局自身 spacing 置 0：左右各"占了多少"
        # 就能用同一份数字算出来，标题居中的等式不依赖 Qt 对占位项补不补间隙的隐含行为。
        title_row.setSpacing(0)
        self.history_button = QToolButton(self)
        self.history_button.setText("记录")
        self.history_button.setAccessibleName("查看反馈记录")
        self.history_button.setToolTip("查看反馈记录")
        self.history_button.setFixedWidth(self.history_button.sizeHint().width())
        # 样式表给按钮加了内边距，不钉高度的话标题行会被撑高、输入框随之缩小；
        # 输入框尺寸是用户 2026-10-22 明确要求的（见 _DEFAULT_SIZE），不能被顺带改掉。
        self.history_button.setFixedHeight(_TITLE_BUTTON_H)
        self.history_button.clicked.connect(self._open_history)
        # 用户 2026-10-27 要求：关闭入口改成与主窗右上角那枚一致的图标按钮，不再在
        # 底部占一格「关闭」文字按钮。复用 sf_editor.MainCloseButton —— 它就是主窗
        # 那枚细线叉号（同一份 close SVG + 同一组悬停底板令牌），目标价面板与替换股票
        # 窗已经用它两年，这里不再画第四种关闭键。
        self.close_button = MainCloseButton(self)
        self.close_button.setFixedSize(20, 20)
        self.close_button.setAccessibleName("关闭反馈窗口")
        self.close_button.setToolTip("关闭窗口不会取消正在进行的上传。")
        # 焦点绝不能落到这枚 ✕（同目标价面板 S2 的口径）：它是 QPushButton，一旦
        # Tab 到它，用户在写反馈时敲空格/回车就直接把窗口关掉。键盘退出走 Esc
        # （QDialog 自带 reject → closeEvent → 隐藏），不需要它参与焦点链。
        self.close_button.setAutoDefault(False)
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.clicked.connect(self.close)
        # 用户 2026-10-27：「记录」挪到左上角，✕ 留在右上角，标题仍按整窗中线居中。
        # 两侧控件不等宽（记录 55 vs ✕ 20），所以标题右侧要补一段
        # （记录宽 - ✕宽），左右占位才真的相等；不补的话标题只是"在两枚按钮之间"，
        # 不在窗口的中线上。用 addSpacing 而不是占位控件：那一格里没有任何控件，
        # 就不存在被画成一块实心底的机会。
        title_row.addWidget(self.history_button)
        title_row.addSpacing(_TITLE_ROW_GAP)
        title_row.addWidget(self.title, 1)
        title_row.addSpacing(_TITLE_ROW_GAP + self.history_button.width() - self.close_button.width())
        title_row.addWidget(self.close_button)
        layout.addLayout(title_row)
        self.description = QPlainTextEdit(self)
        document = self.description.document()
        if document is not None:
            # 用户输入的正文左对齐（2026-10-22 用户要求）；标题仍居中。
            text_option = document.defaultTextOption()
            text_option.setAlignment(Qt.AlignmentFlag.AlignLeft)
            document.setDefaultTextOption(text_option)
        self.description.setTabChangesFocus(True)
        self.description.setAccessibleName("问题描述（可选）")
        self.description.setPlaceholderText("告诉我们你遇到的问题或建议，这些信息将用于改进我们的产品。")
        layout.addWidget(self.description, 1)
        # 描述框下的提示行：字数接近或超过上限时才出现；有待处理反馈占着窗口时给出“新建反馈”。
        # 两者平时都隐藏，整行不占位。
        notes = QHBoxLayout()
        notes.setContentsMargins(0, 0, 0, 0)
        self.new_button = QToolButton(self)
        self.new_button.setText("新建反馈")
        self.new_button.setToolTip("放下当前这条待处理反馈，另写一条新的；原反馈仍保留在本机，下次打开会再出现。")
        self.new_button.clicked.connect(self._start_new)
        self.new_button.hide()
        notes.addWidget(self.new_button)
        notes.addStretch()
        self.counter = QLabel(self)
        self.counter.setAccessibleName("字数提示")
        self.counter.hide()
        notes.addWidget(self.counter)
        layout.addLayout(notes)

        # Keep the sealed preview object available to the upload pipeline and tests,
        # but do not expose raw diagnostic content in the feedback window.
        self.preview = QPlainTextEdit(self)
        self.preview.setTabChangesFocus(True)
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName("实际上传内容预览")
        self.preview.hide()
        self.consent = QCheckBox("我已核对上传内容，同意发送诊断信息和填写的描述", self)
        self.consent.hide()
        self.history = QComboBox(self)
        self.history.setAccessibleName("本机待处理反馈")
        self.history.hide()
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.hide()
        # 只在“不能上传且原因必须让用户看见”时显示（见 _update_enabled）；平时隐藏、不占位。
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        # 底部只剩「上传反馈」：关闭入口已挪到右上角那枚与主窗一致的 ✕（见标题行）。
        buttons.addStretch()
        self.upload_button = QPushButton("上传反馈", self)
        self.upload_button.setFixedWidth(max(100, self.upload_button.fontMetrics().horizontalAdvance("核对并重试") + 32))
        self.upload_button.setToolTip(_upload_hint(self.payload_version))
        self.upload_button.setObjectName("ok")
        self.upload_button.setDefault(True)
        self.upload_button.clicked.connect(self._upload)
        buttons.addWidget(self.upload_button)
        # 用户 2026-10-27：底部只剩这一枚按钮，左右各一段等权拉伸把它摆在正中
        # （以前它是贴右的，因为左边还有一枚「关闭」）。
        buttons.addStretch()
        layout.addLayout(buttons)
        self.description.textChanged.connect(self._draft_changed)
        self.history.currentIndexChanged.connect(self._select_pending)
        self.poll = QTimer(self)
        self.poll.setInterval(100)
        self.poll.timeout.connect(self._poll)
        self._draft_changed()
        try:
            self.storage_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.storage_dir.chmod(0o700)
            self.outbox = FeedbackOutbox(self.storage_dir / "outbox.sqlite3", self.storage_dir / "objects")
            # 本进程第一次打开这个目录：此刻不可能有本进程的上传在跑，所有 submitting 都是上个进程
            # 留下的，不必等租约过期（被杀后马上重启，旧行会一直不出现在待处理列表里）。
            key = str(self.storage_dir.resolve())
            first_look = key not in _RECOVERED_STORES
            self.outbox.recover_interrupted(abandoned=first_look)
            # 回收成功之后才登记：数据库此刻被锁住而失败的话，下次打开还要再试一次，
            # 否则被杀进程留下的 submitting 行要一直等到租约过期才出现在待处理列表里。
            _RECOVERED_STORES.add(key)
            if first_look:
                moved, failed = self.outbox.sweep_scratch()
                if moved or failed:
                    log_line("feedback scratch sweep: moved=%d failed=%d" % (moved, failed),
                             "WARNING" if failed else "INFO")
            self.history.addItem("新建反馈", None)
            pending_index = 0
            for row in self.outbox.pending():
                if row["status"] in ("locally_verified", "response_unknown", "receipt_persisted"):
                    created = datetime.fromisoformat(row["created_at"]).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
                    self.history.addItem(created + " · " + row["outbox_id"][:8], row["outbox_id"])
                    if not pending_index and row["status"] != "receipt_persisted":
                        pending_index = self.history.count() - 1
            endpoint_url(endpoint)
            # With no history selector, resume the oldest unsent package directly.
            # Selection restores bytes only; upload still requires an explicit click.
            if pending_index:
                self.history.setCurrentIndex(pending_index)
        except Exception:
            self.last_error = "configuration_or_storage_unavailable"
            self.status.setText("反馈服务或本地存储尚未就绪，暂不能上传。原有“导出诊断包”仍可使用。")
        self._update_enabled()
        self.description.setFocus()
        prepare_popup_surface(self)

    def center_on_screen(self):
        """摆到所属屏幕可用区的正中。

        不能交给 Qt：QDialog 首次显示时会把自己居中在**父窗口**上，而父窗口是屏幕一角
        的小悬浮卡片，实测中心落在 (320,282)，屏幕可用区中心是 (734,464)，
        还因为超出屏幕被夹到最上沿。先 move() 之后 Qt 就不再自己摆位。
        """
        parent = self.parentWidget()
        screen = None
        if parent is not None:
            screen = QGuiApplication.screenAt(parent.frameGeometry().center())
        screen = screen or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.x() + (area.width() - self.width()) // 2,
                  area.y() + (area.height() - self.height()) // 2)

    def present(self):
        """打开或唤回反馈窗。

        刚创建或已被关掉时先居中再显示；已经显示着的只前置，不改用户拖过的位置。
        """
        if not self.isVisible():
            self.center_on_screen()
        self.show()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event):
        super().showEvent(event)
        if self._glass_pending:
            self._glass_pending = False
            QTimer.singleShot(0, self._inject_glass)

    def hideEvent(self, event):
        self._drag_origin = None
        self._close_glass()
        # 记录窗是独立顶层窗，Qt 隐藏父窗时不会带着它走；上传时反馈窗会 hide()，
        # 记录窗于是留在屏幕上。所有“反馈窗消失”的路径都经过这里，统一收起。
        self._close_history()
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

    def _close_history(self):
        dialog = self._history_dialog
        if dialog is None:
            return
        try:
            if dialog.isVisible():
                dialog.close()
        except RuntimeError:
            self._history_dialog = None    # 底层窗口已被销毁：丢掉句柄，下次重建

    def _open_history(self):
        if self.outbox is None:
            return
        dialog = self._history_dialog
        if dialog is None:
            dialog = FeedbackHistoryDialog(self.outbox, self)
            self._history_dialog = dialog
        else:
            dialog.refresh()
        dialog.present(self)
        # 与反馈窗同一条抬层：反馈窗被主窗的工具层级抬高后，没抬层的子窗会沉到它下面。
        # 测试里没有主窗，取不到就跳过。
        raise_tool = getattr(self.parentWidget(), "_raise_tool_window", None)
        if raise_tool is not None:
            raise_tool(dialog)

    def _draft_changed(self):
        if self.current_id is None and self.stage == "draft":
            self.draft_key = None
        text = self.description.toPlainText()
        self._refresh_counter(len(text))
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

    def _refresh_counter(self, length):
        """字数提示：离上限还远时不出现；快到或超过上限时显示，超限给出明确的删减量。

        原先 counter 建了就 hide，超过 4000 字时按钮只是变灰，原因只藏在悬停提示里。
        """
        if self.current_id is not None or length < _COUNTER_SHOW_AT:
            self.counter.hide()
            return
        over = length - MAX_DESCRIPTION
        self.counter.setText("已超出 %d 字，请删减后再上传" % over if over > 0
                             else "%d / %d" % (length, MAX_DESCRIPTION))
        self.counter.setStyleSheet("color:%s;font-size:11px;" % (FG_BRIGHT_HEX if over > 0 else FG_HINT_HEX))
        self.counter.show()

    def _start_new(self):
        """放下当前待处理反馈，回到空白草稿；原反馈不删，下次打开仍会出现。"""
        busy = self.worker is not None and self.worker.is_alive()
        if busy or self.history.count() == 0:
            return
        self.history.setCurrentIndex(0)
        self.description.setFocus()

    def _render_preview(self):
        files = self.preview_files
        if files is not None:
            self.preview.setPlainText(json.dumps(json.loads(files["state.json"]), ensure_ascii=False, indent=2)
                                      + "\n\n--- problem.txt ---\n" + files["problem.txt"])

    def _update_enabled(self):
        busy = self.worker is not None and self.worker.is_alive()
        # 有待处理反馈占着窗口（正文只读）时，给一条出路；正在上传、已收件、包已损坏（那时
        # 上传钮自己就是“新建反馈”）都不需要。
        self.new_button.setVisible(bool(self.outbox is not None and self.current_id is not None and not busy
                                        and self.stage in ("pending", "uncertain")
                                        and self.last_error != "local_package_invalid"))
        # 不能上传的原因（服务或存储没就绪等）必须让人看得见，不能只放进悬停提示。
        self.status.setVisible(bool(self.last_error in _BLOCKING_ERRORS and self.status.text()))
        if self.last_error == "local_package_invalid" and self.outbox is not None and self.current_id is not None:
            self.upload_button.setText("新建反馈")
            self.upload_button.setToolTip("保留原反馈资料，开始新的反馈；不会立即上传。")
            self.upload_button.setEnabled(not busy)
            return
        reason = self.validation_error or (self.status.text() if self.last_error in _BLOCKING_ERRORS else "")
        self.description.setToolTip(self.validation_error)
        self.upload_button.setToolTip(reason or _upload_hint(self.payload_version))
        valid = self.preview_valid and self.preview_files is not None and len(self.description.toPlainText()) <= MAX_DESCRIPTION
        self.upload_button.setEnabled(bool(self.outbox is not None and self.last_error not in _BLOCKING_ERRORS
                                           and valid and not busy
                                           and self.stage != "received"))

    def _show_feedback_status(self, title, text, detail="", buttons=("好",)):
        """Show feedback results in the same native status popup family as updates.

        buttons=() 表示纯进度提示（上传进行中）：不显示任何按钮，由结果提示替换它。
        """
        import sf_system_alert

        previous = self._feedback_status_dialog
        self._feedback_status_dialog = None
        if previous is not None:
            try:
                previous.close()
            except Exception:
                logging.getLogger(__name__).debug("feedback status popup close failed", exc_info=True)
        try:
            self._feedback_status_dialog = sf_system_alert.show_alert(
                title=title,
                text=text,
                info=detail,
                buttons=buttons,
                icon_path=sf_system_alert.bundled_icon_path(),
                center_text=True,    # 反馈结果（含长编号折行）逐行居中；更新说明不传，仍贴左
            )
        except Exception:
            logging.getLogger(__name__).warning("feedback status popup unavailable", exc_info=True)

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
                if is_full_payload(row["envelope"].get("diagnostic_payload_version")):
                    self._restore_full_pending(row)
                    return
                packet = read_upload_archive(row["archive_path"], row["envelope"])
                if row["status"] == "receipt_persisted":
                    validate_upload_receipt(row["receipt"], row["envelope"], allow_legacy_v2=True)
                self._apply_pending(row, packet)
            except Exception:
                self.last_error = "local_package_invalid"
                self.preview_valid = False
                self.preview_files = None
                self.description.clear()
                self.preview.setPlainText("待处理包或回执不符合当前上传协议，禁止提交。")
                self.status.setText("本地待处理反馈损坏或格式不符合要求，禁止提交。请保留诊断包联系维护者。")
        else:
            self.full_capture = None
            self.payload_version = _new_payload_version(self.diagnostic_capture)
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
        self.consent.setEnabled(False)
        self.consent.setChecked(False)
        self.upload_button.setText("已上传" if self.stage == "received" else
                                   "重试上传" if self.stage == "pending" else "上传反馈")
        self._update_enabled()

    def _apply_pending(self, row, packet):
        if row["status"] != "receipt_persisted":
            validate_capture_context(packet["manifest"], packet["state"])
        self.payload_version = row["envelope"]["diagnostic_payload_version"]
        self.snapshot = packet["state"]
        self.description.setPlainText(packet["files"]["problem.txt"])
        self.preview_files = {key: value for key, value in packet["files"].items() if key != "diagnostics.zip"}
        self.preview_valid = True
        self.validation_error = ""
        self._render_preview()
        self.stage = "received" if row["status"] == "receipt_persisted" else "pending"
        self.upload_button.setText("已上传" if self.stage == "received" else "重试上传")
        self.status.setText("GitHub 已收件：" + row["receipt"]["case_id"] if self.stage == "received"
                            else "上次提交尚未确认收件；重试将使用同一反馈包，不会重新采集。")

    def _restore_full_pending(self, row):
        self.stage = "restoring"
        self.history.setEnabled(False)
        self.description.setReadOnly(True)
        self.upload_button.setEnabled(False)
        self.upload_button.setText("读取中")
        self.status.setText("正在读取并校验上次未完成的完整反馈…")
        expected = self.current_id
        def restore():
            try:
                packet = read_upload_archive(row["archive_path"], row["envelope"])
                if row["status"] == "receipt_persisted":
                    validate_upload_receipt(row["receipt"], row["envelope"], allow_legacy_v2=True)
                self.results.put(("restored", (expected, row, packet)))
            except Exception as exc:
                self.results.put(("restore_failed", (expected, type(exc).__name__)))
        try:
            self.worker = threading.Thread(target=restore, name="feedback-restore", daemon=True)
            self.worker.start()
        except Exception as exc:
            self.worker = None
            self.results.put(("restore_failed", (expected, type(exc).__name__)))
            self._poll()
            return
        self.poll.start()

    def _upload(self):
        if self.last_error == "local_package_invalid" and self.current_id is not None:
            self.history.setCurrentIndex(0)
            self.description.setFocus()
            return
        outbox = self.outbox
        if outbox is None or not self.upload_button.isEnabled():
            return
        try:
            if self.preview_files is None:
                raise FeedbackProtocolError("没有已确认的上传预览")
            files = dict(self.preview_files)
            snapshot = json.loads(files["state.json"])
            if self.current_id is None and self.diagnostic_capture is not None and self.full_capture is None:
                started = utc_now()
                snapshot = self.capture_again() if self.capture_again else snapshot
                snapshot["capture_started_at"] = started
                snapshot["omitted"] = ["camera", "screenshots"]
                context = {"trigger": "manual"} if self.payload_version == PAYLOAD_V3 else None
                self.full_capture = {"job": self.diagnostic_capture(context), "bytes": None, "state": snapshot,
                                     "payload_version": self.payload_version}
            capture = self.full_capture if self.current_id is None else None
        except Exception as exc:
            self.results.put(("failed", _failure("prepare", exc)))
            self._poll()
            return
        # Hide without closing: the dialog owns the upload worker and result timer.
        self.hide()
        self.description.setReadOnly(True)
        self.history.setEnabled(False)
        self.consent.setEnabled(False)
        self.upload_button.setEnabled(False)
        self.status.setText("正在生成并校验反馈包…")
        self.upload_button.setText("上传中…")
        self._show_feedback_status("反馈上传中", "正在上传反馈，请稍候。", buttons=())
        self.stage = "submitting"
        pending_id = self.current_id
        self.draft_key = self.draft_key or uuid.uuid4().hex
        draft_key = self.draft_key

        def work():
            phase = "recovery"
            started = time.monotonic()
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
                            if capture is not None:
                                phase = "capture"
                                if capture["bytes"] is None:
                                    diagnostic_path = Path(directory) / "diagnostics.zip"
                                    capture["job"].path = str(diagnostic_path)
                                    capture["job"].finish()
                                    content = diagnostic_path.read_bytes()
                                    diagnostics = read_diagnostics(content)
                                    automatic = diagnostics.get("state.json", {})
                                    if isinstance(automatic, dict) and isinstance(automatic.get("issues"), list):
                                        capture["state"]["summary"]["capture_issue_count"] = len(automatic["issues"])
                                    if (isinstance(automatic, dict) and automatic.get("truncated")
                                            and "automatic_collection" not in capture["state"]["truncated"]):
                                        capture["state"]["truncated"].append("automatic_collection")
                                    if any(isinstance(issue, str) and (": source truncated," in issue or ": text truncated to " in issue)
                                           for issue in diagnostics["export.json"]["issues"]):
                                        capture["state"]["truncated"].append("diagnostic_attachments")
                                    capture["bytes"] = content
                                    capture["state"]["capture_completed_at"] = utc_now()
                                files["diagnostics.zip"] = capture["bytes"]
                                snapshot.update(capture["state"])
                                files["state.json"] = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
                            build_archive(path, files, app_version=snapshot["app_version"],
                                          bundle_id=snapshot["bundle_id"], run_id=snapshot["run_id"],
                                          omitted=snapshot["omitted"], truncated=snapshot["truncated"],
                                          capture_started_at=snapshot["capture_started_at"],
                                          capture_completed_at=snapshot["capture_completed_at"],
                                          diagnostic_payload_version=(capture["payload_version"] if capture else None))
                            read_upload_archive(path)
                            phase = "enqueue"
                            row = outbox.enqueue(path, app_version=snapshot["app_version"],
                                                 consent_policy_version=(FULL_V3_CONSENT_VERSION if capture and capture["payload_version"] == PAYLOAD_V3
                                                                          else FULL_CONSENT_VERSION if capture else CONSENT_VERSION),
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
                failed = _failure(phase, exc)
                try:
                    failed["detail"] = _failure_detail(exc, started)
                except Exception:
                    failed["detail"] = {}    # 取证失败不能吞掉失败本身
                self.results.put(("failed", failed))

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
            if kind == "restored":
                expected, row, packet = value
                if expected == self.current_id:
                    try:
                        self._apply_pending(row, packet)
                    except Exception as exc:
                        # 读回的包校验不过时 _apply_pending 会抛；不能让异常逃出轮询、
                        # 把界面永远留在“读取中”。按读取失败同一条路收口。
                        self._restore_failed(type(exc).__name__)
            elif kind == "restore_failed":
                expected, category = value
                if expected == self.current_id:
                    self._restore_failed(category)
            elif kind == "not_saved":
                self.storage_uncertain = False
            elif kind == "sealed":
                self.storage_uncertain = False
                self.current_id = value
                self.full_capture = None
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
                self._show_feedback_status(
                    "反馈已上传",
                    "反馈已成功提交。",
                    "反馈编号：%s" % value["case_id"],
                )
            else:
                if value["phase"] == "capture" and self.current_id is None:
                    self.full_capture = None
                if value["phase"] in ("enqueue", "recovery"):
                    self.storage_uncertain = True
                self.stage = ("pending" if self.current_id else
                              "uncertain" if self.storage_uncertain else "draft")
                self.last_error = value["category"]
                self.last_failure_phase = value["phase"]
                self.last_failure_detail = dict(value.get("detail") or {})
                self.failure_count += 1
                logging.getLogger(__name__).warning(
                    "feedback failure: phase=%s category=%s queued=%s",
                    self.last_failure_phase, self.last_error, self.current_id is not None)
                # 标准库 logging 在本程序里没有挂 handler，上面那行不会进 debug.log；
                # 这一行才是用户机器上唯一留得下来的失败现场（状态码、服务端错误码、耗时）。
                log_line("feedback failure: phase=%s category=%s queued=%s %s" % (
                    self.last_failure_phase, self.last_error, self.current_id is not None,
                    " ".join("%s=%s" % item for item in sorted(self.last_failure_detail.items()))), "WARNING")
                hint = _failure_hint(self.last_failure_detail)
                retryable = _failure_kind(self.last_failure_detail) == "retry"
                self.status.setText(
                    "上传未完成或结果未知。反馈已进入本机待提交队列；重试使用原反馈编号。"
                    if self.current_id else
                    "保存结果未知。草稿仍在当前窗口，退出可能丢失；重试会先核对原记录。"
                    if self.stage == "uncertain" else
                    "反馈包尚未保存。草稿仅保留在当前窗口，退出程序会丢失；请重试。")
                self._show_feedback_status(
                    "反馈上传未完成",
                    "反馈未能完成上传，请稍后重试。",
                    ("反馈已保存到本机，重试会使用原反馈编号。" if self.current_id else
                     "保存结果暂无法确认，重试会先核对原记录。" if self.storage_uncertain else
                     "当前草稿尚未保存。") + ("\n" + hint if hint else "")
                    + ("\n" + _REOPEN_TAIL if retryable else ""),
                )
                self.upload_button.setText("核对并重试" if self.stage == "uncertain" else "重试上传")
        if finished:
            self.worker = None
            self.poll.stop()
            self.history.setEnabled(True)
            self.consent.setEnabled(False)
            self.description.setReadOnly(self.current_id is not None or self.stage == "uncertain")
            self._update_enabled()

    def _restore_failed(self, category):
        self.last_error = "local_package_invalid"
        self.stage = "pending"
        self.last_failure_phase = "restore"
        self.failure_count += 1
        self.preview_valid = False
        self.preview_files = None
        self.status.setText("完整反馈读取失败，未上传。请保留本地反馈资料联系维护者。")
        self._show_feedback_status("反馈资料读取失败", "未能读取上次反馈，未上传。",
                                   "本机资料仍然保留，请联系维护者排查。")
        logging.getLogger(__name__).warning("feedback restore failed: category=%s", category)
        log_line("feedback restore failed: category=%s" % category, "WARNING")

    def closeEvent(self, event):
        # Closing hides the dialog, not the durable submission. No cancellation
        # promise is made after bytes may have left the machine.
        if self._feedback_status_dialog is not None:
            try:
                self._feedback_status_dialog.close()
            except Exception:
                logging.getLogger(__name__).debug("feedback status popup close failed", exc_info=True)
            self._feedback_status_dialog = None
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
            "last_failure_detail": dict(state.get("last_failure_detail") or {}),
            "storage_uncertain": state.get("storage_uncertain", False),
            "outbox_ready": state.get("outbox") is not None}
