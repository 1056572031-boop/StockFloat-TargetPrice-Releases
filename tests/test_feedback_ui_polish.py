"""反馈窗口 UI 与上传失败取证的判据（2026-10-26 用户报：记录窗不置顶/样式丑/上传后不关、上传失败无从查证）。"""

import io
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea, QToolButton, QWidget

from sf_feedback_capture import MAX_DESCRIPTION
from sf_feedback_ui import (
    FeedbackDialog, FeedbackHistoryDialog, _COUNTER_SHOW_AT, _DEFAULT_SIZE, _REOPEN_TAIL, _TITLE_BUTTON_H,
    _UploadTiming, _failure_detail, _failure_hint, _failure_kind, _timing_line, feedback_diagnostics,
)
from sf_feedback_ui import FeedbackOutbox, build_archive
from tests._base import SFCase
from tests.test_feedback_dialog import _FeedbackCase
from tests.test_feedback_pipeline import MemoryGitHub, snapshot
from sf_feedback_github import GitHubInbox
from types import SimpleNamespace


# 记录窗默认 400x380；40 条记录若把窗口撑开，高度会远超这个量级。
MAX_HISTORY_HEIGHT = 500


class _Host(QWidget):
    """代替主窗：只记录“被要求抬层的是哪个窗口”。"""

    def __init__(self):
        super().__init__()
        self.raised = []

    def _raise_tool_window(self, tool):
        self.raised.append(tool)


class _DialogCase(_FeedbackCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.inbox = GitHubInbox(MemoryGitHub())
        self.host = self.track_widget(_Host())
        self.dialog = self.track_widget(FeedbackDialog(
            self.host, snapshot=snapshot(), storage_dir=self.temporary.name,
            endpoint="https://receiver.example/v1/feedback",
            sender=lambda url, envelope, path: self.inbox.accept(envelope, Path(path).read_bytes())))
        self.dialog.show()
        self.assertTrue(QTest.qWaitForWindowExposed(self.dialog, 1000))

    def finish(self):
        QTest.qWait(20)
        for _ in range(250):
            if self.dialog.worker is None:
                return
            QTest.qWait(20)
            self.dialog._poll()  # discipline-ok(R5): deliver queued worker results deterministically
        self.fail("worker did not finish")

    def open_history(self):
        self.dialog.history_button.click()
        QApplication.processEvents()
        history = self.dialog._history_dialog  # discipline-ok(R5): the secondary window is only reachable through the dialog
        self.track_widget(history)
        return history


class HistoryWindowBehavior(_DialogCase):
    def test_history_is_a_frameless_always_on_top_tool_window(self):
        history = self.open_history()
        flags = history.windowFlags()
        for flag in (Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint, Qt.WindowType.Tool):
            self.assertTrue(flags & flag, "记录窗缺少窗口标志 %s：回到带系统标题栏的普通窗" % flag.name)
        self.assertTrue(history.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))

    def test_opening_history_raises_it_through_the_same_tool_layer_as_the_feedback_dialog(self):
        history = self.open_history()
        self.assertIn(history, self.host.raised, "记录窗没有被抬到工具层，会沉在已抬层的反馈窗下面")

    def test_history_opens_centered_over_the_feedback_dialog(self):
        history = self.open_history()
        offset = history.frameGeometry().center() - self.dialog.frameGeometry().center()
        screen_area = history.screen().availableGeometry()
        fits = (history.frameGeometry().width() <= screen_area.width()
                and history.frameGeometry().height() <= screen_area.height())
        self.assertTrue(fits)
        # 允许被夹在屏幕可用区内而偏移；没被夹时中心必须重合。
        if screen_area.contains(history.frameGeometry()) and screen_area.contains(self.dialog.frameGeometry()):
            self.assertLessEqual(abs(offset.x()) + abs(offset.y()), 2, "记录窗应摆在反馈窗中心，实测偏移 %s" % offset)

    def test_uploading_closes_the_history_window(self):
        history = self.open_history()
        self.assertTrue(history.isVisible())
        self.dialog.description.setPlainText("history must not linger")
        self.dialog.upload_button.click()
        QApplication.processEvents()
        self.assertFalse(history.isVisible(), "点上传后反馈窗隐藏了，记录窗却留在屏幕上")
        self.finish()

    def test_closing_the_feedback_dialog_closes_the_history_window(self):
        history = self.open_history()
        self.dialog.close()
        QApplication.processEvents()
        self.assertFalse(history.isVisible())

    def test_history_can_be_reopened_after_it_was_closed_by_the_parent(self):
        first = self.open_history()
        self.dialog.hide()
        QApplication.processEvents()
        self.assertFalse(first.isVisible())
        self.dialog.show()
        self.dialog.history_button.click()
        QApplication.processEvents()
        self.assertTrue(first.isVisible(), "再次点“记录”应复用并重新显示同一个记录窗")


class _Outbox:
    def __init__(self, count):
        base = {"app_version": "1.4.104", "updated_at": "2026-10-26T06:25:00+00:00"}
        self.rows = [dict(base, outbox_id="id%03d" % index, case_id="case-%03d" % index,
                          received_at="2026-10-26T06:25:11+00:00",
                          status="receipt_persisted" if index % 3 else "response_unknown")
                     for index in range(count)]

    def history(self, limit=100, include_cleaned=True):
        return [dict(row) for row in self.rows if include_cleaned or row["status"] != "cleaned"]

    def cleanup_local(self, outbox_id):
        # 与真实 FeedbackOutbox 一致：清理后记录还在库里，只是状态变成 cleaned。
        for row in self.rows:
            if row["outbox_id"] == outbox_id:
                row["status"] = "cleaned"


class HistoryLayout(_DialogCase):
    def test_many_rows_scroll_instead_of_stretching_the_window(self):
        history = self.track_widget(FeedbackHistoryDialog(_Outbox(40), self.dialog))
        history.show()
        QApplication.processEvents()
        scroll = history.findChild(QScrollArea)
        self.assertIsNotNone(scroll, "记录最多 100 条，必须放在滚动区里")
        self.assertTrue(scroll.isVisible())
        body = history.rows.parentWidget()
        self.assertIs(scroll.widget(), body, "记录行必须装在滚动区的内容里，而不是直接铺进窗口布局")
        self.assertGreater(body.height(), scroll.viewport().height(), "40 条记录的内容应高于可视区")
        bar = scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), bar.minimum(), "40 条记录应该可以滚动")
        self.assertLessEqual(history.height(), MAX_HISTORY_HEIGHT,
                             "窗口高度不应被记录条数撑开，实测 %d" % history.height())

    def test_each_record_is_a_card_with_the_case_id_and_its_state(self):
        history = self.track_widget(FeedbackHistoryDialog(_Outbox(3), self.dialog))
        cards = [w for w in history.findChildren(QWidget) if w.objectName() == "feedbackRow"]
        self.assertEqual(len(cards), 3)
        text = "\n".join(label.text() for label in history.findChildren(QLabel))
        self.assertIn("case-001", text)
        self.assertIn("等待确认，可重试", text)
        self.assertIn("已收件，本机资料待清理", text)
        self.assertEqual(len(history.findChildren(QToolButton)), 2, "只有“已收件、待清理”的两条有清理按钮")

    def test_waiting_records_stand_out_from_settled_ones(self):
        history = self.track_widget(FeedbackHistoryDialog(_Outbox(3), self.dialog))
        texts = {label.text() for label in history.findChildren(QLabel)}
        waiting = next(t for t in texts if "等待确认" in t)
        settled = next(t for t in texts if "已收件，本机资料待清理" in t)
        self.assertIn("font-weight:bold", waiting.split("等待确认")[0].rsplit("<span", 1)[-1])
        self.assertNotIn("font-weight:bold", settled.split("已收件")[0].rsplit("<span", 1)[-1])

    def test_case_id_text_is_escaped_in_the_rich_label(self):
        outbox = _Outbox(1)
        outbox.rows[0]["case_id"] = "<b>x</b>&<script>"
        history = self.track_widget(FeedbackHistoryDialog(outbox, self.dialog))
        raw = "\n".join(label.text() for label in history.findChildren(QLabel))
        self.assertNotIn("<script>", raw)
        self.assertIn("&lt;script&gt;", raw)

    def test_empty_and_unavailable_notes_are_centered_and_dim(self):
        history = self.track_widget(FeedbackHistoryDialog(_Outbox(0), self.dialog))
        note = next(l for l in history.findChildren(QLabel) if l.objectName() == "feedbackEmpty")
        self.assertEqual(note.text(), "还没有反馈记录。")
        self.assertTrue(note.alignment() & Qt.AlignmentFlag.AlignHCenter)

    def test_an_unreadable_history_says_so_and_leaves_a_trace(self):
        class Broken(_Outbox):
            def history(self, limit=100, include_cleaned=True):
                raise RuntimeError("database disk image is malformed")
        with patch("sf_feedback_ui.log_swallow") as swallowed:
            history = self.track_widget(FeedbackHistoryDialog(Broken(0), self.dialog))
        note = next(l for l in history.findChildren(QLabel) if l.objectName() == "feedbackEmpty")
        self.assertEqual(note.text(), "本机反馈记录暂时不可用。")
        self.assertEqual(swallowed.call_count, 1, "记录读不出来必须在 debug.log 里留一行（§6 第 9 条），不能只在界面上说一句")
        self.assertIn("FeedbackHistoryDialog.refresh", swallowed.call_args.args[0])


class HistoryCloseButton(_DialogCase):
    """用户 2026-10-27：反馈记录窗的关闭入口也换成右上角 ✕（与反馈窗、主窗同一枚控件）。"""

    def make_history(self):
        history = self.track_widget(FeedbackHistoryDialog(_Outbox(3), self.dialog))
        history.show()
        QApplication.processEvents()
        return history

    def test_the_close_entry_is_the_shared_main_window_button(self):
        from sf_editor import MainCloseButton
        button = self.make_history().close_button
        self.assertIsInstance(button, MainCloseButton, "记录窗的关闭键必须是主窗同款控件，不能另画一种叉号")
        self.assertEqual(button.text(), "", "关闭图标是画出来的，不能用字体字符顶替")
        self.assertEqual((button.width(), button.height()), (20, 20))
        self.assertIsNotNone(button._close_img, "必须加载主窗正在用的那份 close SVG")  # discipline-ok(R5): 图标是否真加载只能看这一个成员

    def test_the_close_button_sits_in_the_top_right_corner_above_the_list(self):
        history = self.make_history()
        close = history.close_button.geometry()
        self.assertEqual(history.width() - 1 - close.right(), 20, "关闭键应贴着 20pt 右边距")
        self.assertGreaterEqual(close.top(), 20, "关闭键不能越过 20pt 上边距")
        self.assertLessEqual(close.bottom(), history.scroll_area.geometry().top(),
                             "关闭键属于标题行，不能压进记录列表")

    def test_the_title_stays_on_the_window_midline(self):
        history = self.make_history()
        center = history.title.geometry().center().x()
        self.assertLessEqual(abs(center - history.width() // 2), 1,
                             "标题必须仍在整窗中线，实测标题中心 %d / 窗宽 %d" % (center, history.width()))

    def test_the_bottom_row_has_no_text_close_button_any_more(self):
        history = self.make_history()
        texts = [b.text() for b in history.findChildren(QPushButton)]
        self.assertNotIn("关闭", texts, "底部那枚「关闭」文字按钮应已被右上角 ✕ 取代")
        bottom = history.scroll_area.geometry().bottom()
        stray = [b for b in history.findChildren(QPushButton) if b.geometry().top() > bottom]
        self.assertEqual(stray, [], "记录列表下方不应再有任何按钮行")

    def test_focus_never_lands_on_the_close_button(self):
        button = self.make_history().close_button
        self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.NoFocus, "焦点落到 ✕ 后，敲空格或回车会直接关窗")
        self.assertFalse(button.autoDefault())

    def test_clicking_the_close_button_hides_the_history_window(self):
        history = self.make_history()
        self.assertTrue(history.isVisible())
        history.close_button.click()
        QApplication.processEvents()
        self.assertFalse(history.isVisible())

    def test_escape_still_closes_the_history_window(self):
        history = self.make_history()
        QTest.keyClick(history, Qt.Key.Key_Escape)
        QApplication.processEvents()
        self.assertFalse(history.isVisible(), "去掉底部按钮后，键盘退出路径仍须可用")


class FailureEvidence(_DialogCase):
    def http_error(self, status, body=b""):
        return HTTPError("https://synthetic.invalid", status, "secret reason", {}, io.BytesIO(body))

    def test_detail_keeps_status_code_and_the_whitelisted_server_code(self):
        detail = _failure_detail(self.http_error(429, b'{"error":"daily_budget"}'), 0.0)
        self.assertEqual(detail["http_status"], 429)
        self.assertEqual(detail["server_code"], "daily_budget")
        self.assertIn("elapsed_sec", detail)

    def test_detail_never_carries_free_text_from_the_server_or_the_exception(self):
        for body in (b'{"error":"Password=secret value"}', b'{"error":"UPPER"}', b'{"error":12}',
                     b"not json", b"", b'{"error":"' + b"a" * 80 + b'"}'):
            with self.subTest(body=body[:20]):
                detail = _failure_detail(self.http_error(503, body), 0.0)
                self.assertNotIn("server_code", detail)
                self.assertNotIn("secret", repr(detail))

    def test_unreadable_response_body_still_yields_the_status(self):
        error = self.http_error(502)
        error.read = Mock(side_effect=OSError("closed"))
        self.assertEqual(_failure_detail(error, 0.0)["http_status"], 502)

    def test_timeout_is_told_apart_from_other_network_failures(self):
        self.assertEqual(_failure_detail(URLError(TimeoutError("secret")), 0.0)["network_reason"], "timeout")
        self.assertEqual(_failure_detail(URLError(ConnectionRefusedError("secret")), 0.0)["network_reason"],
                         "ConnectionRefusedError")
        self.assertEqual(_failure_detail(URLError("plain text reason"), 0.0)["network_reason"], "unknown")

    def test_hints_only_cover_known_situations(self):
        self.assertIn("1~2 分钟", _failure_hint({"http_status": 409}))
        self.assertIn("1~2 分钟", _failure_hint({"server_code": "submission_in_progress"}))
        self.assertIn("较忙", _failure_hint({"http_status": 429}))
        self.assertIn("较忙", _failure_hint({"server_code": "daily_byte_budget"}))
        self.assertIn("超时", _failure_hint({"network_reason": "timeout"}))
        self.assertIn("暂时不可用", _failure_hint({"http_status": 503}))
        self.assertEqual(_failure_hint({}), "")
        self.assertEqual(_failure_hint({"http_status": 400}), "", "认不出的情况不编原因")

    def fail_with(self, error):
        self.dialog.sender = Mock(side_effect=error)
        with patch("sf_system_alert.show_alert") as alerts, patch("sf_feedback_ui.log_line") as log:
            self.dialog.description.setPlainText("failure evidence")
            self.dialog.upload_button.click()
            self.finish()
        return alerts, log

    def test_a_failed_upload_leaves_a_line_in_the_debug_log_with_status_and_elapsed(self):
        alerts, log = self.fail_with(self.http_error(409, b'{"error":"submission_in_progress"}'))
        matched = [call for call in log.call_args_list if "feedback failure" in call.args[0]]
        self.assertEqual(len(matched), 1, "失败必须写进 debug.log（标准库 logging 没有 handler，不落盘）")
        line = matched[0].args[0]
        for token in ("phase=submit", "category=HTTPError", "http_status=409",
                      "server_code=submission_in_progress", "elapsed_sec="):
            self.assertIn(token, line)
        self.assertNotIn("secret", line)
        # 级别必须显式给出：log_line 的默认级别是 INFO，漏写就会被日志级别过滤掉。
        self.assertEqual(matched[0].args[1:], ("WARNING",))
        timing = feedback_diagnostics(SimpleNamespace(_feedback_dialog=self.dialog))["last_upload_timing"]
        self.assertEqual(timing["outcome"], "failed")
        self.assertEqual(timing["phase"], "submit")
        self.assertIn("network_ms", timing)
        self.assertNotIn("secret", repr(timing))

    def test_the_failure_popup_adds_the_hint_for_a_known_situation(self):
        alerts, _log = self.fail_with(self.http_error(429, b'{"error":"receiver_busy"}'))
        self.assertIn("较忙", alerts.call_args.kwargs["info"])
        self.assertIn("反馈已保存到本机", alerts.call_args.kwargs["info"])

    def test_an_unrecognized_failure_adds_no_made_up_reason(self):
        alerts, _log = self.fail_with(self.http_error(404, b'{"error":"not_found"}'))
        info = alerts.call_args.kwargs["info"]
        self.assertEqual(info, "反馈已保存到本机，重试会使用原反馈编号。\n" + _REOPEN_TAIL)

    def test_diagnostics_export_carries_the_last_failure_detail(self):
        self.fail_with(self.http_error(503, b'{"error":"submission_unconfirmed"}'))
        exported = feedback_diagnostics(SimpleNamespace(_feedback_dialog=self.dialog))
        self.assertEqual(exported["last_failure_detail"]["http_status"], 503)
        self.assertEqual(exported["last_failure_detail"]["server_code"], "submission_unconfirmed")
        self.assertEqual(feedback_diagnostics(SimpleNamespace(_feedback_dialog=SimpleNamespace())
                                              )["last_failure_detail"], {})

    def test_successful_upload_records_four_timing_segments_in_log_and_diagnostics(self):
        with patch("sf_system_alert.show_alert"), patch("sf_feedback_ui.log_line") as log:
            self.dialog.description.setPlainText("timing evidence")
            self.dialog.upload_button.click()
            self.finish()
        timing = feedback_diagnostics(SimpleNamespace(_feedback_dialog=self.dialog))["last_upload_timing"]
        self.assertEqual(timing["outcome"], "received")
        self.assertFalse(timing["resumed"])
        # 这个夹具是旧式反馈（没有诊断采集），所以没有 capture_ms：不执行就不出现，不补 0。
        # 带采集的四段齐全判据在 test_feedback_full_bundle 里。
        for name in ("pack_ms", "enqueue_ms", "network_ms", "other_ms", "total_ms"):
            self.assertIsInstance(timing[name], int, name)
            self.assertGreaterEqual(timing[name], 0, name)
        self.assertNotIn("capture_ms", timing)
        lines = [call for call in log.call_args_list if "feedback upload timing:" in call.args[0]]
        self.assertEqual(len(lines), 1, "上传成功也必须留下可供诊断包对照的分段耗时")
        for token in ("outcome=received", "resumed=0", "pack_ms=", "enqueue_ms=", "network_ms=", "total_ms="):
            self.assertIn(token, lines[0].args[0])
        self.assertNotIn("capture_ms=", lines[0].args[0])
        self.assertEqual(lines[0].args[1:], ("INFO",))

    def test_each_segment_is_charged_only_to_its_own_step(self):
        # 用假时钟：打包、入队、网络三步各自推进固定秒数，其余步骤不推进。
        # 段名错位、漏记、重复计入，都会让下面的精确数字对不上。
        now = [1000.0]
        real_build, real_enqueue = build_archive, FeedbackOutbox.enqueue

        def build(*args, **kwargs):
            result = real_build(*args, **kwargs)
            now[0] += 3.0
            return result

        def enqueue(outbox, *args, **kwargs):
            result = real_enqueue(outbox, *args, **kwargs)
            now[0] += 2.0
            return result

        def sender(_url, envelope, path):
            now[0] += 7.0
            return self.inbox.accept(envelope, Path(path).read_bytes())

        self.dialog.sender = sender
        with patch("sf_system_alert.show_alert"), patch("sf_feedback_ui.log_line"), \
                patch("sf_feedback_ui._clock", lambda: now[0]), \
                patch("sf_feedback_ui.build_archive", build), \
                patch.object(FeedbackOutbox, "enqueue", enqueue):
            self.dialog.description.setPlainText("attribution")
            self.dialog.upload_button.click()
            self.finish()
        timing = feedback_diagnostics(SimpleNamespace(_feedback_dialog=self.dialog))["last_upload_timing"]
        self.assertEqual((timing["pack_ms"], timing["enqueue_ms"], timing["network_ms"]), (3000, 2000, 7000))
        self.assertNotIn("capture_ms", timing)
        self.assertEqual(timing["total_ms"], 12000, "总数必须等于各段之和加其他")
        self.assertEqual(timing["other_ms"], 0)

    def test_timing_object_sums_a_segment_entered_twice_and_keeps_time_of_a_failing_step(self):
        now = [50.0]
        with patch("sf_feedback_ui._clock", lambda: now[0]):
            timing = _UploadTiming(resumed=False)
            for step in (1.5, 2.5):
                with timing.segment("capture"):
                    now[0] += step
            with self.assertRaises(OSError):
                with timing.segment("pack"):
                    now[0] += 4.0
                    raise OSError("synthetic")
            now[0] += 1.0
            result = timing.result("failed", "archive")
        self.assertEqual(result["capture_ms"], 4000)
        self.assertEqual(result["pack_ms"], 4000, "失败的那一步也要记下已花掉的时间")
        self.assertNotIn("enqueue_ms", result)
        self.assertEqual((result["other_ms"], result["total_ms"]), (1000, 9000))
        self.assertEqual(_timing_line(result),
                         "feedback upload timing: outcome=failed phase=archive resumed=0 "
                         "capture_ms=4000 pack_ms=4000 other_ms=1000 total_ms=9000")

    def test_retry_records_only_the_new_network_segment(self):
        sender = self.dialog.sender
        self.dialog.sender = Mock(side_effect=TimeoutError())
        with patch("sf_system_alert.show_alert"), patch("sf_feedback_ui.log_line"):
            self.dialog.description.setPlainText("retry timing")
            self.dialog.upload_button.click()
            self.finish()
        self.assertEqual(self.dialog.stage, "pending")
        self.dialog.sender = sender
        with patch("sf_system_alert.show_alert"), patch("sf_feedback_ui.log_line"):
            self.dialog.upload_button.click()
            self.finish()
        timing = feedback_diagnostics(SimpleNamespace(_feedback_dialog=self.dialog))["last_upload_timing"]
        self.assertEqual(timing["outcome"], "received")
        self.assertTrue(timing["resumed"])
        self.assertIn("network_ms", timing)
        self.assertNotIn("capture_ms", timing)
        self.assertNotIn("pack_ms", timing)
        self.assertNotIn("enqueue_ms", timing)


class TitleRowCloseButton(_DialogCase):
    """用户 2026-10-27 要求：反馈窗的关闭入口改成与主窗右上角那枚一致的 ✕，不再在底部占一格。

    三条都要钉住：
    ① 用的是**同一份**关闭控件（`sf_editor.MainCloseButton`：主窗同款 close SVG + 同款悬停
       底板令牌），不是再画第四种关闭键；
    ② 底部那枚「关闭」文字按钮真的没了，标题行的居中标题没被新按钮挤偏；
    ③ 焦点不会落到它身上（否则写反馈时敲空格/回车直接关窗），键盘退出走 Esc。
    """

    def bare_widgets(self):
        return [c for c in self.dialog.findChildren(QWidget)
                if type(c) is QWidget and c.parentWidget() is self.dialog]

    def test_the_dialog_has_no_invisible_placeholder_widget_left(self):
        self.assertEqual(self.bare_widgets(), [],
                         "标题行又出现了裸 QWidget 占位块：那一格可能被画成一块实心黑，居中请改用 addSpacing")

    def test_the_close_entry_is_the_main_windows_shared_button(self):
        from sf_editor import MainCloseButton
        button = self.dialog.close_button
        self.assertIsInstance(button, MainCloseButton, "关闭键必须是主窗同款控件，不能各画一种叉号")
        self.assertEqual(button.text(), "", "关闭图标不能用字体字符（✕ 是画出来的，不是打字）")
        self.assertEqual((button.width(), button.height()), (20, 20))
        self.assertIsNotNone(button._close_img, "必须加载主窗正在用的那份 close SVG")  # discipline-ok(R5): 图标是否真加载只能看这一个成员

    def test_the_close_button_sits_in_the_top_right_corner(self):
        close = self.dialog.close_button.geometry()
        self.assertEqual(self.dialog.width() - 1 - close.right(), 20, "关闭键应贴着 20pt 右边距")
        self.assertEqual(close.top(), self.dialog.history_button.geometry().top()
                         + (self.dialog.history_button.height() - close.height()) // 2,
                         "关闭键与「记录」在同一行内垂直居中")
        self.assertLessEqual(close.bottom(), self.dialog.description.geometry().top(),
                             "关闭键属于标题行，不能掉到正文区")

    def test_the_history_button_moved_to_the_top_left_corner(self):
        """用户 2026-10-27：记录挪到左上角，✕ 独占右上角。"""
        history = self.dialog.history_button.geometry()
        self.assertEqual(history.left(), 20, "「记录」应贴着 20pt 左边距")
        self.assertLess(history.right(), self.dialog.close_button.geometry().left(),
                        "「记录」不能再压在关闭键右侧")

    def test_the_upload_button_is_centered_in_the_bottom_row(self):
        """用户 2026-10-27：底部只剩这一枚按钮，摆正中。"""
        center = self.dialog.upload_button.geometry().center().x()
        self.assertLessEqual(abs(center - self.dialog.width() // 2), 1,
                             "「上传反馈」必须居中，实测中心 %d / 窗宽 %d" % (center, self.dialog.width()))

    def test_the_bottom_row_no_longer_has_a_text_close_button(self):
        texts = [b.text() for b in self.dialog.findChildren(QPushButton) if b.parentWidget() is self.dialog]
        self.assertNotIn("关闭", texts, "底部那枚「关闭」文字按钮应已被右上角 ✕ 取代")
        self.assertIn("上传反馈", texts)

    def test_the_title_is_still_centered_with_two_buttons_on_the_right(self):
        center = self.dialog.title.geometry().center().x()
        self.assertLessEqual(abs(center - self.dialog.width() // 2), 1,
                             "标题必须仍在整窗中线，实测标题中心 %d / 窗宽 %d" % (center, self.dialog.width()))

    def test_the_input_box_size_the_user_fixed_on_2026_10_22_is_unchanged(self):
        """加一枚 ✕ 不许把用户钉死的输入框尺寸带跑（见 _DEFAULT_SIZE 那段注释）。"""
        box = self.dialog.description.geometry()
        self.assertEqual((box.width(), box.height()), (250, 188),
                         "输入框尺寸变了：标题行/底部行的改动挤到了正文区")
        self.assertEqual((self.dialog.width(), self.dialog.height()), _DEFAULT_SIZE,
                         "整窗尺寸不再是 _DEFAULT_SIZE")
        self.assertEqual(self.dialog.history_button.height(), _TITLE_BUTTON_H,
                         "标题行被新按钮撑高，正文区就会跟着缩")

    def test_focus_never_lands_on_the_close_button(self):
        button = self.dialog.close_button
        self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.NoFocus,
                         "焦点落到 ✕ 后，写反馈时敲空格就会关窗")
        self.assertFalse(button.autoDefault(), "回车必须仍然触发「上传反馈」，不能触发关闭")

    def test_clicking_the_close_button_hides_the_dialog(self):
        self.assertTrue(self.dialog.isVisible())
        self.dialog.close_button.click()
        QApplication.processEvents()
        self.assertFalse(self.dialog.isVisible())


class CounterAndNewEntry(_DialogCase):
    def type_chars(self, count):
        self.dialog.description.setPlainText("字" * count)
        QApplication.processEvents()

    def test_counter_stays_out_of_the_way_until_the_limit_is_near(self):
        self.type_chars(_COUNTER_SHOW_AT - 1)
        self.assertTrue(self.dialog.counter.isHidden())
        self.type_chars(_COUNTER_SHOW_AT)
        self.assertFalse(self.dialog.counter.isHidden())
        self.assertEqual(self.dialog.counter.text(), "%d / %d" % (_COUNTER_SHOW_AT, MAX_DESCRIPTION))

    def test_over_the_limit_says_by_how_much_and_blocks_upload(self):
        self.type_chars(MAX_DESCRIPTION + 7)
        self.assertFalse(self.dialog.counter.isHidden())
        self.assertIn("已超出 7 字", self.dialog.counter.text())
        self.assertFalse(self.dialog.upload_button.isEnabled())
        self.type_chars(10)
        self.assertTrue(self.dialog.counter.isHidden(), "删回去以后提示要消失")
        self.assertTrue(self.dialog.upload_button.isEnabled())

    def fail_once(self):
        self.dialog.sender = Mock(side_effect=URLError("synthetic offline"))
        with patch("sf_system_alert.show_alert"), patch("sf_feedback_ui.log_line"):
            self.dialog.description.setPlainText("stuck feedback")
            self.dialog.upload_button.click()
            self.finish()

    def test_new_entry_is_offered_only_when_a_pending_feedback_holds_the_window(self):
        self.assertTrue(self.dialog.new_button.isHidden(), "空白草稿不需要“新建反馈”")
        self.fail_once()
        self.assertEqual(self.dialog.stage, "pending")
        self.assertFalse(self.dialog.new_button.isHidden(), "失败后正文只读，必须有出路")

    def test_new_entry_returns_to_an_editable_blank_draft_and_keeps_the_old_one(self):
        self.fail_once()
        stuck = self.dialog.current_id
        self.dialog.new_button.click()
        QApplication.processEvents()
        self.assertIsNone(self.dialog.current_id)
        self.assertEqual(self.dialog.stage, "draft")
        self.assertFalse(self.dialog.description.isReadOnly())
        self.assertEqual(self.dialog.description.toPlainText(), "")
        self.assertTrue(self.dialog.new_button.isHidden())
        self.assertIsNotNone(self.dialog.outbox.get(stuck), "放下的反馈不能被删")

    def test_button_says_uploading_while_the_upload_is_in_flight(self):
        gate = threading.Event()

        def slow(url, envelope, path):
            gate.wait(5)
            return self.inbox.accept(envelope, Path(path).read_bytes())

        self.dialog.sender = slow
        self.dialog.description.setPlainText("slow upload")
        self.dialog.upload_button.click()
        during = self.dialog.upload_button.text()
        gate.set()
        self.finish()
        self.assertEqual(during, "上传中…", "上传期间重开窗口要能看出正在上传")
        self.assertEqual(self.dialog.upload_button.text(), "已上传")

    def test_history_button_keeps_its_height_so_the_input_box_is_not_squeezed(self):
        self.assertEqual(self.dialog.history_button.height(), _TITLE_BUTTON_H)


class RejectedVersusRetryable(FailureEvidence):
    def test_a_409_that_waiting_cannot_fix_does_not_say_wait(self):
        for code in ("remote_binding", "identity_conflict"):
            hint = _failure_hint({"server_code": code, "http_status": 409})
            self.assertNotIn("1~2 分钟", hint, code)
            self.assertIn("拒收", hint, code)
        self.assertIn("1~2 分钟", _failure_hint({"server_code": "lease_superseded", "http_status": 409}))

    def test_an_unknown_409_gets_no_made_up_reason(self):
        self.assertEqual(_failure_hint({"server_code": "some_future_conflict", "http_status": 409}), "",
                         "不认识的 409 不知道等一会儿有没有用，不能编“可能仍在处理”")
        self.assertIn("1~2 分钟", _failure_hint({"http_status": 409}), "没有错误码的 409（旧版服务端）沿用原提示")

    def test_permanent_and_full_codes_the_worker_really_sends(self):
        self.assertEqual(_failure_kind({"server_code": "zip_members", "http_status": 422}), "rejected")
        self.assertEqual(_failure_kind({"server_code": "never_heard_of_it", "http_status": 422}), "rejected")
        self.assertEqual(_failure_kind({"server_code": "never_heard_of_it", "http_status": 413}), "rejected")
        self.assertEqual(_failure_kind({"server_code": "inbox_capacity", "http_status": 503}), "full")
        self.assertEqual(_failure_kind({"http_status": 503}), "retry")

    def test_failure_kinds(self):
        self.assertEqual(_failure_kind({"server_code": "diagnostic_member"}), "rejected")
        self.assertEqual(_failure_kind({"server_code": "case_budget"}), "full")
        self.assertEqual(_failure_kind({"server_code": "submission_unconfirmed"}), "retry")
        self.assertEqual(_failure_kind({"http_status": 503}), "retry")
        self.assertEqual(_failure_kind({}), "retry")

    def test_a_rejected_feedback_does_not_promise_that_retrying_helps(self):
        alerts, _log = self.fail_with(self.http_error(422, b'{"error":"diagnostic_member"}'))
        info = alerts.call_args.kwargs["info"]
        self.assertIn("拒收", info)
        self.assertIn("导出诊断包", info)
        self.assertNotIn(_REOPEN_TAIL, info, "被拒收的反馈再重试没有意义，不能再劝用户重试")

    def test_a_full_service_is_not_called_busy(self):
        alerts, _log = self.fail_with(self.http_error(429, b'{"error":"case_budget"}'))
        info = alerts.call_args.kwargs["info"]
        self.assertIn("存储已满", info)
        self.assertNotIn("较忙", info, "容量是永久上限，不是“稍后再试”")

    def test_a_retryable_failure_tells_the_user_how_to_come_back(self):
        alerts, _log = self.fail_with(self.http_error(503, b'{"error":"submission_unconfirmed"}'))
        info = alerts.call_args.kwargs["info"]
        self.assertTrue(info.endswith(_REOPEN_TAIL))
        self.assertIn("暂时不可用", info)

    def test_a_bare_socket_timeout_is_recognised_as_a_timeout(self):
        self.assertEqual(_failure_detail(TimeoutError("secret"), 0.0)["network_reason"], "timeout")
        self.assertIn("超时", _failure_hint({"network_reason": "timeout"}))


class BlockingReasonAndRestore(_DialogCase):
    def test_a_blocking_reason_is_shown_not_only_put_in_a_tooltip(self):
        broken = self.track_widget(FeedbackDialog(
            snapshot=snapshot(), storage_dir=self.temporary.name, endpoint="http://not-allowed.example"))
        broken.show()
        QApplication.processEvents()
        self.assertEqual(broken.last_error, "configuration_or_storage_unavailable")
        self.assertFalse(broken.upload_button.isEnabled())
        self.assertTrue(broken.status.isVisible(), "按钮灰着却没有任何可见原因")
        self.assertIn("尚未就绪", broken.status.text())

    def test_the_status_line_stays_hidden_when_nothing_blocks_upload(self):
        self.assertFalse(self.dialog.status.isVisible())

    def test_a_restored_packet_that_fails_to_apply_ends_in_the_unreadable_state(self):
        # 读回的包校验不过时 _apply_pending 会抛；之前异常会逃出轮询，界面永远停在“读取中”。
        self.dialog.results.put(("restored", (self.dialog.current_id, {}, {})))
        with patch("sf_system_alert.show_alert"), patch("sf_feedback_ui.log_line"):
            self.dialog._poll()  # discipline-ok(R5): the poll slot is the unit under test
        self.assertEqual(self.dialog.last_error, "local_package_invalid")
        self.assertEqual(self.dialog.stage, "pending")
        self.assertEqual(self.dialog.last_failure_phase, "restore")


class OutboxOrphanCount(SFCase):
    def test_orphans_are_judged_against_every_record_not_just_the_latest_hundred(self):
        import sf_feedback as protocol
        from sf_feedback_outbox import FeedbackOutbox, read_diagnostics

        with tempfile.TemporaryDirectory(prefix="orphans-", dir="/tmp") as directory:
            root = Path(directory)
            store = FeedbackOutbox(root / "outbox.sqlite3", root / "objects")
            for index in range(103):
                archive = root / ("src-%d.zip" % index)
                protocol.build_archive(archive, {"state.json": "fixture-%d" % index}, app_version="fixture",
                                       bundle_id="com.skyrocket.app", run_id="run%d" % index)
                store.enqueue(archive, app_version="fixture", consent_policy_version="fixture")
            summary = read_diagnostics(root / "outbox.sqlite3", root / "objects")
            self.assertEqual(summary["recent"], 100)
            self.assertEqual(summary["orphan_objects"], 0, "第 101 条以后的正常文件被误报成孤儿")
            (root / "objects" / "stray.zip").write_bytes(b"x")
            self.assertEqual(read_diagnostics(root / "outbox.sqlite3", root / "objects")["orphan_objects"], 1)


class CleanupIsImmediate(_DialogCase):
    """用户 2026-10-27 要求：点“清理”直接清理，不再二次确认；清理后这条记录不再展示。"""

    def received_history(self):
        self.dialog.description.setPlainText("done")
        self.dialog.upload_button.click()
        self.finish()
        return self.open_history()

    def cards(self, history):
        # refresh() 用 deleteLater 换掉旧行：先处理延迟删除，才数得到刷新后的真实卡片。
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        return [w for w in history.findChildren(QWidget) if w.objectName() == "feedbackRow"]

    def test_one_click_cleans_and_the_record_disappears(self):
        history = self.received_history()
        outbox_id = self.dialog.current_id
        archive = Path(self.dialog.outbox.get(outbox_id)["archive_path"])
        self.assertEqual(len(self.cards(history)), 1)
        self.assertEqual(history.findChild(QToolButton).text(), "清理")
        history.findChild(QToolButton).click()
        self.assertFalse(archive.exists(), "点一下就要清掉本机资料，不能还要再点一次")
        self.assertEqual(self.dialog.outbox.get(outbox_id)["status"], "cleaned")
        self.assertEqual(self.cards(history), [], "清理后这条记录不能再展示")
        self.assertEqual(history.findChildren(QToolButton), [])
        self.assertIn("还没有反馈记录", "\n".join(label.text() for label in history.findChildren(QLabel)))

    def test_a_cleaned_record_stays_gone_after_reopening_the_window(self):
        history = self.received_history()
        history.findChild(QToolButton).click()
        history.hide()
        self.dialog.history_button.click()
        QApplication.processEvents()
        self.assertEqual(self.cards(history), [], "重新打开记录窗，已清理的记录不能回来")

    def test_a_second_click_on_the_same_button_is_not_logged_as_a_failure(self):
        # 快速连点：旧按钮在延迟删除之前仍可点，第二下打在已清理的记录上。界面结果是对的，
        # 但不能留下带堆栈的警告——分析器会把警告行当作候选线索。
        history = self.received_history()
        outbox_id = self.dialog.current_id
        button = history.findChild(QToolButton)
        with self.assertNoLogs("sf_feedback_ui", level="WARNING"):
            button.click()
            button.click()
        self.assertEqual(self.dialog.outbox.get(outbox_id)["status"], "cleaned")
        self.assertEqual(self.cards(history), [])

    def test_a_real_cleanup_failure_is_still_logged_as_a_warning(self):
        # 对照：只有“已清理/被占用”这类状态变化才静默，真正的失败（磁盘、权限）仍要留痕。
        history = self.received_history()
        button = history.findChild(QToolButton)
        with patch.object(self.dialog.outbox, "cleanup_local", side_effect=OSError("disk full")):
            with self.assertLogs("sf_feedback_ui", level="WARNING"):
                button.click()
        self.assertEqual(len(self.cards(history)), 1, "清理失败时这条记录要留在列表里，方便重试")

    def test_cleaning_one_record_leaves_the_others_in_place(self):
        history = self.track_widget(FeedbackHistoryDialog(_Outbox(3), self.dialog))
        buttons = history.findChildren(QToolButton)
        self.assertEqual(len(buttons), 2, "夹具里有两条已收件记录")
        buttons[0].click()
        remaining = self.cards(history)
        text = "\n".join(label.text() for label in history.findChildren(QLabel))
        self.assertEqual(len(remaining), 2)
        self.assertNotIn("case-001", text, "被清理的那一条必须消失")
        self.assertIn("case-000", text)
        self.assertIn("case-002", text)


class DialogRecoveryAndSweep(_FeedbackCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory(prefix="dialog-recover-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.track_patch(patch("tempfile.gettempdir", return_value=str(self.root)))

    def seed_submitting(self, seconds=300):
        import sf_feedback as protocol
        from sf_feedback_outbox import FeedbackOutbox
        source = self.root / "src.zip"
        protocol.build_archive(source, {"state.json": "fixture"}, app_version="fixture",
                               bundle_id="com.skyrocket.app", run_id="fixture")
        outbox = FeedbackOutbox(self.root / "feedback" / "outbox.sqlite3", self.root / "feedback" / "objects")
        row = outbox.enqueue(source, app_version="fixture", consent_policy_version="fixture")
        outbox.claim(row["outbox_id"], kind="submit", statuses=("locally_verified",), target_status="submitting",
                     lease_seconds=seconds)
        return outbox, row["outbox_id"]

    def open_dialog(self):
        with patch("sf_system_alert.show_alert"):
            dialog = self.track_widget(FeedbackDialog(
                snapshot=snapshot(), storage_dir=self.root / "feedback", endpoint="https://receiver.example/v1/feedback"))
        return dialog

    def test_a_submission_left_by_a_killed_process_is_recovered_on_first_look(self):
        outbox, outbox_id = self.seed_submitting()
        with patch("sf_feedback_ui._RECOVERED_STORES", set()):
            self.open_dialog()
        row = outbox.get(outbox_id)
        self.assertEqual((row["status"], row["last_error"]), ("response_unknown", "process_interrupted"))

    def test_the_second_look_in_the_same_process_leaves_a_live_lease_alone(self):
        with patch("sf_feedback_ui._RECOVERED_STORES", set()):
            self.open_dialog()
            outbox, outbox_id = self.seed_submitting()
            self.open_dialog()
        self.assertEqual(outbox.get(outbox_id)["status"], "submitting", "本进程自己的上传不能被当成遗留回收")

    def test_stale_scratch_is_swept_and_reported_on_first_look(self):
        import os
        import time
        stale = self.root / "feedback" / "capture-old"
        stale.mkdir(parents=True)
        stamp = time.time() - 7200
        os.utime(stale, (stamp, stamp))
        with patch("sf_feedback_ui._RECOVERED_STORES", set()), patch("sf_feedback_ui.log_line") as log:
            self.open_dialog()
        self.assertFalse(stale.exists())
        self.assertTrue(any("moved=1" in call.args[0] for call in log.call_args_list))

    def test_the_second_look_does_not_sweep_again(self):
        import os
        import time
        with patch("sf_feedback_ui._RECOVERED_STORES", set()):
            self.open_dialog()
            stale = self.root / "feedback" / "capture-late"
            stale.mkdir(parents=True)
            stamp = time.time() - 7200
            os.utime(stale, (stamp, stamp))
            self.open_dialog()
        self.assertTrue(stale.exists(), "清扫只在本进程第一次打开时做；之后可能正有别的流程在用这个目录")

    def test_a_recovery_that_fails_is_retried_on_the_next_look(self):
        with patch("sf_feedback_ui._RECOVERED_STORES", set()) as seen:
            with patch("sf_feedback_ui.FeedbackOutbox.recover_interrupted", side_effect=RuntimeError("database is locked")):
                dialog = self.open_dialog()
            self.assertEqual(seen, set(), "回收没成功就不能登记为已看过，否则被杀进程留下的行要等租约过期才出现")
            self.assertEqual(dialog.last_error, "configuration_or_storage_unavailable")
            outbox, outbox_id = self.seed_submitting()
            self.open_dialog()
        self.assertEqual(outbox.get(outbox_id)["status"], "response_unknown")

    def test_a_failed_sweep_is_reported_as_a_warning(self):
        with patch("sf_feedback_ui._RECOVERED_STORES", set()), patch("sf_feedback_ui.log_line") as log, \
                patch("sf_feedback_ui.FeedbackOutbox.sweep_scratch", return_value=(0, 2)):
            self.open_dialog()
        warned = [call for call in log.call_args_list if "failed=2" in call.args[0]]
        self.assertEqual(len(warned), 1)
        self.assertEqual(warned[0].args[1], "WARNING")


class DialogEdges(_DialogCase):
    def test_a_history_window_destroyed_underneath_is_forgotten_not_fatal(self):
        class Gone:
            def isVisible(self):
                raise RuntimeError("wrapped C/C++ object has been deleted")
        self.dialog._history_dialog = Gone()  # discipline-ok(R5): simulating a destroyed Qt object needs the handle itself
        try:
            self.dialog._close_history()  # discipline-ok(R5): the method under test
            self.assertIsNone(self.dialog._history_dialog)  # discipline-ok(R5): dropping the stale handle is the observable
        finally:
            # tearDown 会关窗，hideEvent 里同一个异常若还在，Qt 会直接中止整个测试进程、
            # 同批其他结果一起丢；用例体内先摘掉假句柄，守卫被拆时也只是这一条普通失败。
            self.dialog._history_dialog = None  # discipline-ok(R5): same handle, restored before teardown

    def test_starting_a_new_feedback_is_refused_while_an_upload_runs(self):
        self.dialog.sender = Mock(side_effect=URLError("synthetic offline"))
        with patch("sf_system_alert.show_alert"), patch("sf_feedback_ui.log_line"):
            self.dialog.description.setPlainText("stuck")
            self.dialog.upload_button.click()
            self.finish()
        stuck = self.dialog.history.currentData()
        self.assertIsNotNone(stuck, "失败后窗口应停在那条待处理反馈上")
        alive = Mock()
        alive.is_alive.return_value = True
        self.dialog.worker = alive
        self.dialog._start_new()  # discipline-ok(R5): the button is hidden while busy, so only a direct call reaches the guard
        self.assertEqual(self.dialog.history.currentData(), stuck)
        self.dialog.worker = None

    def test_a_failure_whose_evidence_cannot_be_collected_is_still_a_failure(self):
        self.dialog.sender = Mock(side_effect=URLError("synthetic offline"))
        with patch("sf_system_alert.show_alert"), patch("sf_feedback_ui.log_line"), \
                patch("sf_feedback_ui._failure_detail", side_effect=RuntimeError("evidence broken")):
            self.dialog.description.setPlainText("evidence failure")
            self.dialog.upload_button.click()
            self.finish()
        self.assertEqual(self.dialog.last_error, "URLError")
        self.assertEqual(self.dialog.last_failure_detail, {})
        self.assertEqual(self.dialog.stage, "pending")

    def test_new_server_codes_get_their_own_wording(self):
        self.assertIn("今天的上传次数已用完", _failure_hint({"server_code": "daily_client_budget"}))
        self.assertIn("约 30 秒", _failure_hint({"server_code": "receiver_busy", "http_status": 429, "retry_after_sec": 30}))
        self.assertIn("较忙", _failure_hint({"server_code": "receiver_busy", "http_status": 429}))
        self.assertIn("上游超时", _failure_hint({"server_code": "upstream_timeout", "http_status": 503}))
        self.assertEqual(_failure_kind({"server_code": "archive_invalid"}), "rejected")
        self.assertIn("现在重试多半仍会失败", _failure_hint({"server_code": "archive_invalid"}))
