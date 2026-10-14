# -*- coding: utf-8 -*-
"""SearchThread 迟到回包关闭链路回归测试。"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QObject, pyqtSignal

from sf_editor import EditorWindow, _zombie_threads


class _StubSearchThread(QObject):
    """仅模拟搜索线程的信号和运行状态，不创建 QWidget。"""

    done = pyqtSignal(list)
    finished = pyqtSignal()

    def __init__(self, running=False):
        super().__init__()
        self._running = running

    def isRunning(self):
        return self._running


class _EditorDouble:
    """以最小接口驱动 EditorWindow 的生产关闭/回包方法，隔离 QWidget 生命周期。"""

    def __init__(self, applied):
        self.applied = applied
        self._closed = False
        self._search_seq = 0
        self._search_thread = None
        self._anchor = object()
        self.on_closed = None
        self.closed = False
        self.hints = []

    def close(self):
        self.closed = True

    def _detach_search_thread(self):
        EditorWindow._detach_search_thread(self)

    def _close(self):
        EditorWindow._close(self)

    def _on_search_done(self, results, seq=0):
        EditorWindow._on_search_done(self, results, seq)

    def _is_dup(self, code):
        return False

    def _clear_candidates(self):
        pass


class TestCloseDisconnectsDone(unittest.TestCase):
    def setUp(self):
        self.applied = []
        self.ed = _EditorDouble(self.applied)

    def tearDown(self):
        st = self.ed._search_thread
        if st is not None:
            try:
                st.done.disconnect()
            except TypeError:
                pass
            if st in _zombie_threads:
                _zombie_threads.remove(st)

    def test_close_disconnects_done_signal(self):
        st = _StubSearchThread(running=True)
        self.ed._search_thread = st
        st.done.connect(lambda results: None)
        st.done.disconnect()
        st.done.connect(self.ed._on_search_done)
        self.assertEqual(st.receivers(st.done), 1)
        self.ed._close()
        self.assertEqual(st.receivers(st.done), 0)
        self.assertTrue(self.ed.closed)

    def test_late_done_after_close_not_applied(self):
        st = _StubSearchThread(running=True)
        self.ed._search_thread = st
        st.done.connect(lambda results: EditorWindow._on_search_done(self.ed, results))
        self.ed._close()
        st.done.emit([("sh000001", "平安银行")])
        self.assertEqual(self.applied, [])


class TestClosedGuard(unittest.TestCase):
    def test_on_search_done_returns_when_closed(self):
        ed = _EditorDouble([])
        ed._closed = True
        EditorWindow._on_search_done(ed, [("sh000001", "平安银行")])
        self.assertEqual(ed.applied, [])

    def test_closed_guard_source_level(self):
        src = inspect.getsource(EditorWindow._on_search_done)
        self.assertIn("getattr(self, '_closed', False)", src)


class TestNormalFlow(unittest.TestCase):
    def test_open_window_unique_result_still_applies(self):
        applied = []
        ed = _EditorDouble(applied)
        ed.on_apply = applied.append
        EditorWindow._on_search_done(ed, [("sh000001", "平安银行")])
        self.assertEqual(applied, ["sh000001"])
        self.assertTrue(ed._closed)
        self.assertTrue(ed.closed)


class TestZombieRegistry(unittest.TestCase):
    def test_close_registers_running_thread_in_zombie_table(self):
        ed = _EditorDouble([])
        st = _StubSearchThread(running=True)
        ed._search_thread = st
        st.done.connect(ed._on_search_done)
        ed._close()
        self.assertIn(st, _zombie_threads)
        self.assertEqual(st.receivers(st.done), 0)
        _zombie_threads.remove(st)


if __name__ == "__main__":
    unittest.main(verbosity=2)
