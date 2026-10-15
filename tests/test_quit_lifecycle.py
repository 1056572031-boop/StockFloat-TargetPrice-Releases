# -*- coding: utf-8 -*-
"""实时股价 v1.2.95 退出生命周期回归：毛玻璃安全下屏 + 后台线程统一停止。

锁定的四条修复（防止回退）：

1. `_persist_on_exit` 对原生 NSWindow 只 orderOut_、绝不 close()。
   本项目用 NSVisualEffectView/NSWindow 做毛玻璃，历史 4 次 objc_release
   SIGSEGV：close() 让 AppKit 释放后 Python 包装对象再 release 即 EXC_BAD_ACCESS。
   Cmd+Q / Dock 右键退出**只**走 aboutToQuit → _persist_on_exit，所以这条路径
   上的违规调用在实际使用中必然命中。
2. `_bg_effect` 退出时复位（此前只在注入成功/失败两处置位，退出路径从不复位）。
3. `quit()` 里那段对 _glass_window 的收尾是 100% 不可达死代码，已删除。
4. `_shutdown_threads()` 为后台线程清理的唯一实现，并挂到 aboutToQuit：
   Cmd+Q（QApplication.quit）/ Dock 退出（app.exit(0)）都不经过 quit()，
   此前三条 daemon 循环与编辑器 SearchThread 完全无人停止。

运行方式（在 StockFloat 目录）：
    QT_QPA_PLATFORM=offscreen ./.venv/bin/python3 -m pytest tests/test_quit_lifecycle.py -q
"""
import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sf_quote
import stock_float_window as m


def _fake_http_get(url, **kw):
    """离线行情/分时 mock，避免测试访问网络。"""
    if "hq.sinajs.cn" in url:
        return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
    if "/appstock/app/minute/query?" in url:
        return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                '"data":["0930 1510.0 100 1000", "1000 1511.0 200 2000"]}}}}')
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


class _FakeNSWindow(object):
    """模拟原生 NSWindow：记录 orderOut_ / close 调用。"""

    def __init__(self):
        self.calls = []

    def orderOut_(self, arg):
        self.calls.append(("orderOut", arg))

    def close(self):
        self.calls.append(("close",))

    def setReleasedWhenClosed_(self, flag):
        self.calls.append(("setReleasedWhenClosed", flag))


class _FakeThread(object):
    """模拟 threading.Thread：只关心 is_alive / join。"""

    def __init__(self, alive=True):
        self.joins = []
        self._alive = alive

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self.joins.append(timeout)
        self._alive = False


class _FakeSearchThread(object):
    """模拟 sf_editor.SearchThread（QThread）：只关心 isRunning / wait。"""

    def __init__(self, running=True):
        self.waits = []
        self._running = running

    def isRunning(self):
        return self._running

    def wait(self, ms=None):
        self.waits.append(ms)
        self._running = False
        return True


class _QuitCase(unittest.TestCase):
    """创建离屏主窗的公共夹具（与 test_v165._WinCase 同构）。"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", _fake_http_get),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        self.win._anim_timer.stop()
        self.win._rainbow_timer.stop()

    def tearDown(self):
        # 每个用例都会构造真实 FloatWindow。先解除 QApplication 对该窗口的全局持有，
        # 再收敛线程，避免 coverage 放大 Qt/Cocoa 析构时序竞争。
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            for signal, callback in (
                    (app.aboutToQuit, self.win._persist_on_exit),
                    (app.aboutToQuit, self.win._shutdown_threads),
                    (app.applicationStateChanged, self.win._on_app_state),
                    (app.applicationStateChanged, self.win._check_updates_after_resume),
                    (app.primaryScreenChanged, self.win._on_screen_changed),
                    (app.screenAdded, self.win._on_screen_added),
                    (app.screenRemoved, self.win._on_screen_changed)):
                try:
                    signal.disconnect(callback)
                except (TypeError, RuntimeError):
                    pass
        for timer in (self.win._anim_timer, self.win._rainbow_timer):
            try:
                timer.stop()
            except Exception:
                pass
        try:
            self.win._shutdown_threads()
        except Exception:
            pass
        try:
            self.win.close()
            self.win.deleteLater()
        except Exception:
            pass
        QApplication.processEvents()
        for p in self._patches:
            p.stop()


class TestGlassSafeOnExit(_QuitCase):
    """1+2：退出时毛玻璃只下屏不 close，且 _bg_effect 复位。"""

    def test_persist_on_exit_never_closes_native_window(self):
        w = self.win
        gw = _FakeNSWindow()
        w._glass_window = gw
        w._bg_effect = True
        w._persist_on_exit()
        self.assertIn(("orderOut", None), gw.calls, "退出必须把毛玻璃子窗下屏")
        self.assertNotIn(("close",), gw.calls,
                         "对原生 NSWindow 调 close() 会触发 objc_release SIGSEGV")
        self.assertIsNone(w._glass_window)
        self.assertFalse(w._bg_effect, "退出必须复位 _bg_effect")

    def test_persist_on_exit_clears_effect_even_without_glass(self):
        """没有玻璃窗时 _bg_effect 也必须复位（此前退出路径从不复位）。"""
        w = self.win
        w._glass_window = None
        w._bg_effect = True
        w._persist_on_exit()
        self.assertFalse(w._bg_effect)

    def test_persist_on_exit_stops_rainbow_animation(self):
        """目标价命中后的彩虹动画不能在退出阶段继续触发重绘。"""
        w = self.win
        w._rainbow_timer.start()
        self.assertTrue(w._rainbow_timer.isActive(), "测试前彩虹动画必须已启动")
        w._persist_on_exit()
        self.assertFalse(w._rainbow_timer.isActive(),
                         "退出必须停止彩虹动画，避免退出尾声继续刷新窗口")


class TestSystemCloseExits(_QuitCase):
    def test_close_event_runs_full_shutdown_and_requests_app_quit(self):
        w = self.win
        calls = []
        with patch.object(w, "_shutdown_threads", side_effect=lambda: calls.append("threads")), \
             patch.object(w, "_persist_on_exit", side_effect=lambda: calls.append("persist")), \
             patch.object(m.QApplication, "quit") as app_quit:
            w.close()
        self.assertEqual(calls, ["threads", "persist"])
        app_quit.assert_called_once()


class TestShutdownThreads(_QuitCase):
    """4：_shutdown_threads 停止后台线程，且幂等。"""

    def _install_fakes(self, w):
        w.running = True
        w._wake = threading.Event()
        w._quote_thread = _FakeThread()
        w._minute_thread = _FakeThread()
        return w._quote_thread, w._minute_thread

    def test_threads_stopped_and_joined(self):
        w = self.win
        qt, mt = self._install_fakes(w)
        with patch.object(m._NET_POOL, "close_all") as pool:
            w._shutdown_threads()
        self.assertFalse(w.running, "必须置 running=False 让后台循环退出")
        self.assertTrue(w._wake.is_set(), "必须 _wake.set() 唤醒长睡的线程")
        self.assertEqual(qt.joins, [1.5], "行情线程应被 join(1.5)")
        self.assertEqual(mt.joins, [1.5], "分时线程应被 join(1.5)")
        pool.assert_called_once()

    def test_search_thread_waited(self):
        w = self.win
        self._install_fakes(w)
        st = _FakeSearchThread()
        w._editor = type("_Ed", (), {"_search_thread": st})()
        try:
            w._shutdown_threads()
            self.assertEqual(st.waits, [500],
                             "SearchThread 必须被有界等待，否则退出时报 "
                             "QThread: Destroyed while thread is still running")
            self.assertLessEqual(sum(st.waits), 500,
                                 "等待上限必须 ≤500ms —— v1.2.x 曾因主线程长等 "
                                 "导致退出卡 UI，超时部分由 quit() 的 pending "
                                 "后台线程兜底，不得在此硬扛")
        finally:
            w._editor = None

    def test_idempotent(self):
        """连调两次：第二次直接返回，join 只发生一次。"""
        w = self.win
        qt, mt = self._install_fakes(w)
        w._shutdown_threads()
        w._shutdown_threads()
        self.assertEqual(qt.joins, [1.5], "幂等失效：join 被调用了多次")
        self.assertEqual(mt.joins, [1.5], "幂等失效：join 被调用了多次")

    def test_skips_dead_and_current_thread(self):
        w = self.win
        w.running = True
        w._wake = threading.Event()
        dead = _FakeThread(alive=False)
        w._quote_thread = dead
        w._minute_thread = None
        w._shutdown_threads()
        self.assertEqual(dead.joins, [], "已结束的线程不应再 join")


class TestAboutToQuitWiring(_QuitCase):
    """4-接线：aboutToQuit 必须挂上 _shutdown_threads（Cmd+Q 唯一通知点）。"""

    def test_shutdown_connected_to_about_to_quit(self):
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        w = self.win
        try:
            app.aboutToQuit.disconnect(w._shutdown_threads)
        except (TypeError, RuntimeError):
            self.fail("aboutToQuit 未连接 _shutdown_threads —— Cmd+Q/Dock 退出时"
                      "后台线程无人停止")
        else:
            # 断开成功即证明已连接，立即恢复，避免影响后续用例
            try:
                app.aboutToQuit.connect(w._shutdown_threads)
            except Exception:
                pass

    def test_shutdown_disconnects_resume_update_callback(self):
        """临时窗口退出后不得被 QApplication 的状态信号继续持有。"""
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        w = self.win
        w._shutdown_threads()
        with self.assertRaises((TypeError, RuntimeError)):
            app.applicationStateChanged.disconnect(w._check_updates_after_resume)

    def test_quit_reuses_single_implementation(self):
        """quit() 不得再自带一份线程清理：调用它必须走到 _shutdown_threads。"""
        w = self.win
        calls = []
        real = w._shutdown_threads
        w._shutdown_threads = lambda: (calls.append(1), real())[0]
        with patch.object(m.QApplication, "quit"):
            with patch.object(w, "close", lambda: None):
                w.quit()
        self.assertEqual(len(calls), 1, "quit() 必须复用 _shutdown_threads，不得另写一份")

    def test_single_implementation_in_source(self):
        """源码里 _wake.set() / join(timeout=1.5) 只能各出现一处。"""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "stock_float_window.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(src.count("t.join(timeout=1.5)"), 1,
                         "线程 join 必须只有 _shutdown_threads 一处实现")
        self.assertEqual(src.count("_NET_POOL.close_all()"), 1,
                         "连接池关闭必须只有 _shutdown_threads 一处实现")


if __name__ == "__main__":
    unittest.main()
