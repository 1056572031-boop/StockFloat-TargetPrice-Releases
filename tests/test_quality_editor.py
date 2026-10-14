"""Search intent, completion feedback and real GUI responsiveness regressions."""
import threading
from unittest import mock

from PyQt6.QtCore import QPoint, QThread, Qt, QTimer
from PyQt6.QtWidgets import QApplication
import sf_editor
import sf_quote
from tests._base import SFCase, wait_for


def search_thread(editor):
    field = "_search_thread"
    return getattr(editor, field)


class EditorQualityTests(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()

    def make_editor(self):
        applied = []
        editor = sf_editor.EditorWindow("sh600519", applied.append, QPoint(0, 0), 200)
        editor.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.addCleanup(editor.close)
        return editor, applied

    def test_changed_text_invalidates_already_queued_unique_result(self):
        editor, applied = self.make_editor()
        with mock.patch.object(sf_editor.SearchThread, "start"):
            editor.entry.setText("pingan")
            editor.do_search()
            thread = search_thread(editor)
            editor.entry.setText("ningde")
            thread.done.emit([("sz000001", "Ping An")], thread.seq)
        self.assertEqual(applied, [])
        self.assertEqual(editor.entry.text(), "ningde")

    def test_multiple_candidates_end_the_loading_state(self):
        editor, _ = self.make_editor()
        with mock.patch.object(sf_editor.SearchThread, "start"):
            editor.entry.setText("bank")
            editor.do_search()
            thread = search_thread(editor)
            thread.done.emit([("sz000001", "A"), ("sh600036", "B")], thread.seq)
        self.assertEqual(editor.cand_box.count(), 2)
        self.assertEqual(editor.hint.text(), "")

    def test_etf_verification_runs_off_gui_and_ui_timers_continue(self):
        editor, applied = self.make_editor()
        entered, release = threading.Event(), threading.Event()
        gui_calls = []

        def probe(code):
            gui_calls.append(QThread.currentThread() == self.app.thread())
            entered.set()
            release.wait(3)
            return {"name": "ETF", "code": code}

        with mock.patch("sf_universe.match_confident", return_value=[]), \
                mock.patch("sf_universe.ensure_ready"), \
                mock.patch.object(sf_quote, "fetch_quote", side_effect=probe):
            editor.entry.setText("510300")
            editor.do_search()
            thread = search_thread(editor)
            try:
                self.assertTrue(entered.wait(2))
                callbacks = []
                QTimer.singleShot(0, lambda: callbacks.append(True))
                QApplication.processEvents()
                self.assertEqual(callbacks, [True])
                self.assertEqual(applied, [])
            finally:
                release.set()
                thread.wait(3000)
            def complete():
                QApplication.processEvents()
                return bool(applied)
            self.assertTrue(wait_for(complete))
        self.assertEqual(gui_calls, [False])
        self.assertEqual(applied, ["sh510300"])

    def test_dictionary_miss_can_be_confirmed_by_live_quote(self):
        with mock.patch("sf_universe.match_confident", return_value=[]), \
                mock.patch("sf_universe.ensure_ready"), \
                mock.patch.object(sf_quote, "fetch_quote",
                                  return_value={"name": "New stock", "code": "600999"}):
            self.assertEqual(sf_editor.verify_bare_code("600999"), [("sh600999", "New stock")])
