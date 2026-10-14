# -*- coding: utf-8 -*-
"""v1.4.20 退出收尾回归：quit() 之前不得留下任何待销毁的 Qt 窗口。

线上现场（2026-09-23，iMac / macOS 27，v1.4.18）：用户点「立即更新」，下载完成后
程序**必崩**。崩溃报告主线程栈（自内向外）：

    QThreadStorageData::get            ← 读空指针
    QOpenGLContext::currentContext
    QSurface::~QSurface / QWindow::~QWindow
    QWidget::destroy / QWidget::~QWidget
    sipQMessageBox::~sipQMessageBox    ← 被销毁的是下载进度兜底框
    QObject::event(DeferredDelete)
    QCoreApplicationPrivate::sendPostedEvents
    __cxa_finalize_ranges / exit       ← 已经在 C++ 静态析构阶段
    -[NSApplication terminate:]
    libqcocoa
    meth_QCoreApplication_quit         ← 起点就是 QApplication.quit()

两条事实拼出根因：

1. **macOS 上 QApplication.quit() 不是"让 exec() 返回"**，Qt 的 cocoa 插件把它转成
   -[NSApplication terminate:] → exit()。进程不回 Python 侧收尾，直接跑静态析构。
2. v1.4.17 给下载进度兜底框写的收口是 close() + **deleteLater()**。这条延迟删除
   事件没人冲刷，一直排到 exit() 阶段才执行；那时主线程的 QThreadStorage 已被更早
   的 atexit 例程销毁，销毁带 QWindow 的框就是空指针。

所以修复是"退出前把待删对象就地兑现"，两道防线各自独立生效（实测任撤一道仍能守住，
两道都在才不怕后续新增 deleteLater 的调用点）：
  · _close_update_flow_box 改用 sip.delete 同步销毁；
  · quit() 在 QApplication.quit() 之前调 _flush_deferred_deletions() 兜底冲刷。

为什么必须子进程 + 真 cocoa：崩溃在 Qt 的 cocoa 插件里，离屏插件（全量套件的
QT_QPA_PLATFORM=offscreen）走的是另一套后端，同进程复现不了也验证不了修复。
子进程实现见 tests/_exit_child.py。会短暂抢一次焦点，窗口都挪在屏幕外。
"""

import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHILD = ROOT / "tests" / "_exit_child.py"
TIMEOUT_SECONDS = 120


def _spawn(mode):
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "cocoa"        # 父套件是 offscreen，这里必须覆盖
    proc = subprocess.run([sys.executable, str(CHILD), mode],
                          capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                          cwd=str(ROOT), env=env)
    markers = {}
    for line in (proc.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            markers[key.strip()] = value.strip()
    return proc, markers


def _describe(proc, markers):
    return ("退出码=%s（-11=SIGSEGV）标记=%s stderr=%s"
            % (proc.returncode, markers, (proc.stderr or "")[-400:]))


class ExitTeardownCocoaTest(unittest.TestCase):
    """真 cocoa：退出链路必须干净收尾，不得被信号杀掉。"""

    @classmethod
    def setUpClass(cls):
        if sys.platform != "darwin":
            raise unittest.SkipTest("崩溃发生在 Qt 的 macOS cocoa 插件里")
        try:
            proc, markers = _spawn("probe")
        except subprocess.TimeoutExpired:
            raise unittest.SkipTest("窗口预检超时：本机可能没有窗口服务")
        if markers.get("probe") != "ok":
            raise unittest.SkipTest(
                "窗口预检未通过（无窗口服务？SSH/CI）：%s"
                % _describe(proc, markers))

    def test_update_download_quit_does_not_crash(self):
        """线上崩溃原样：下载完成 → 收口 Qt 兜底进度框 → quit()。"""
        proc, markers = _spawn("update_download_quit")
        self.assertNotIn("error", markers, "子进程抛异常: %s" % markers.get("error"))
        self.assertEqual(markers.get("box"), "QMessageBox",
                         "必须真的走 Qt 自绘兜底框，否则这条用例没覆盖到崩溃现场")
        self.assertEqual(markers.get("quit_called"), "True",
                         "用例没跑到 quit()，等于没测")
        self.assertEqual(proc.returncode, 0,
                         "退出被信号打断 = 静态析构阶段还在销毁窗口：%s"
                         % _describe(proc, markers))

    def test_plain_quit_still_works(self):
        """常规退出（没有进度框）不能被这次修复改坏。"""
        proc, markers = _spawn("plain_quit")
        self.assertNotIn("error", markers, "子进程抛异常: %s" % markers.get("error"))
        self.assertEqual(markers.get("quit_called"), "True")
        self.assertEqual(proc.returncode, 0, _describe(proc, markers))


class _Host:
    """够 _close_update_flow_box 读写的属性面，不建真窗口。"""

    def __init__(self):
        self._update_check_dialog = None
        self._update_message_dialog = None
        self._update_prompt_dialog = None
        self._update_flow_box = None
        self._update_flow_progress = None
        self._update_flow_cancel = None


class ExitTeardownLogicTest(unittest.TestCase):
    """离屏可跑：锁住"同步销毁 + 退出前冲刷"这两道防线本身。"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import stock_float_window
        self.m = stock_float_window

    def test_close_update_flow_box_destroys_qt_box_immediately(self):
        """兜底框必须就地销毁，不能留 deleteLater 排队到 exit() 之后。"""
        from PyQt6 import sip
        from PyQt6.QtWidgets import QMessageBox
        box = QMessageBox()
        box.show()
        host = _Host()
        host._update_flow_box = box
        self.m.FloatWindow._close_update_flow_box(host)
        self.assertTrue(sip.isdeleted(box),
                        "C++ 侧还活着就说明仍是排队销毁：macOS 上 quit() 直接 exit()，"
                        "这条延迟删除会在静态析构阶段销毁 QWindow 并 SIGSEGV")
        self.assertIsNone(host._update_flow_box)
        self.assertIsNone(host._update_flow_progress)
        self.assertIsNone(host._update_flow_cancel)

    def test_close_update_flow_box_is_idempotent_after_delete(self):
        """重复收口不得二次触碰已销毁的底层对象。"""
        from PyQt6.QtWidgets import QMessageBox
        host = _Host()
        host._update_flow_box = QMessageBox()
        self.m.FloatWindow._close_update_flow_box(host)
        try:
            self.m.FloatWindow._close_update_flow_box(host)
        except Exception as exc:                       # noqa: BLE001
            self.fail("重复收口必须安全，实际抛了 %r" % (exc,))

    def test_native_handle_is_not_treated_as_qt_object(self):
        """原生弹窗句柄只有 close()，没有 Qt 析构面：不能被 sip.delete 碰。"""
        closed = []

        class _Handle:
            def close(self):
                closed.append("close")

        host = _Host()
        host._update_flow_box = _Handle()
        self.m.FloatWindow._close_update_flow_box(host)
        self.assertEqual(closed, ["close"])
        self.assertIsNone(host._update_flow_box)

    def test_flush_deferred_deletions_only_runs_on_main_thread(self):
        """工作线程没有 Qt 事件循环，替别人冲刷排队事件是错的。"""
        import threading
        self.assertTrue(self.m._flush_deferred_deletions(),
                        "主线程必须真的执行一次冲刷")
        results = []
        t = threading.Thread(target=lambda: results.append(
            self.m._flush_deferred_deletions()))
        t.start()
        t.join(5)
        self.assertEqual(results, [False], "非主线程必须直接返回，不触碰事件队列")

    def test_flush_actually_destroys_a_queued_widget(self):
        """冲刷必须真的兑现排队的 deleteLater，而不是空转。"""
        from PyQt6 import sip
        from PyQt6.QtWidgets import QWidget
        w = QWidget()
        w.show()
        w.deleteLater()
        self.assertFalse(sip.isdeleted(w), "deleteLater 之后、事件循环之前不该销毁")
        self.m._flush_deferred_deletions()
        self.assertTrue(sip.isdeleted(w),
                        "冲刷之后 C++ 对象必须已经没了：否则它会活到 exit() 阶段")


if __name__ == "__main__":
    unittest.main()
