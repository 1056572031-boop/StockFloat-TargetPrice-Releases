# -*- coding: utf-8 -*-
"""主窗编辑入口与更新流程补测：开关式弹窗、引用回收、下载进度合流与交接失败。

为什么单独补这一层（本轮为扩大自动化覆盖而加）
----------------------------------------------
这几段都是"用户点一下就会走到、坏了就是一次可见故障"的路径：
点编辑按钮没反应 / 二次点击不关窗 / 更新下载框不关 / 装不上却没话说。
其中"下载进度按整数百分比合流"和"launcher 失败要留在当前版本"两条
此前只能靠人工点出来，本文件把它们变成确定性断言。

浮窗夹具与 tests/test_render.py 同口径（同样一份 patch 清单），测试文件之间
不互相 import，改一个不会连坐另一个。
"""
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

import sf_quote              # noqa: E402
import sf_update             # noqa: E402
import stock_float_window as m   # noqa: E402

try:
    from _base import wait_for
except ImportError:           # pragma: no cover - 包模式导入
    from tests._base import wait_for


class WindowCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._td = tempfile.mkdtemp(prefix="sf_cov_win_")
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "load_api_urls", lambda: None),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", lambda *a, **k: ""),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch.object(m.FloatWindow, "_quote_loop_wrapper", lambda self: None),
            patch.object(m.FloatWindow, "_minute_loop_wrapper", lambda self: None),
            patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()
        for t in (self.win.timer, self.win.top_timer, self.win._anim_timer,
                  self.win._flash_timer):
            try:
                t.stop()
            except Exception:
                pass
        self.win.show()
        QApplication.processEvents()

    def tearDown(self):
        if self.win._editor is not None:
            try:
                self.win._editor._closed = True
                self.win._editor.close()
            except Exception:
                pass
        self.win.close()
        QApplication.processEvents()
        patch.stopall()
        for p in self._patches:
            try:
                p.stop()
            except Exception:
                pass


# --------------------------------------------------------------------------
# 编辑弹窗：开关式、引用回收
# --------------------------------------------------------------------------

class TestOpenEditor(WindowCase):
    def test_first_click_opens_the_editor_anchored_to_the_main_window(self):
        self.win.open_editor()
        ed = self.win._editor
        self.assertIsNotNone(ed, "点编辑没反应 = 换股入口整个没了")
        self.assertTrue(ed.isVisible())
        self.assertIsNotNone(ed.on_closed,
                             "关闭回调没挂上，主窗会一直持有已关弹窗的引用")

    def test_second_click_closes_it_in_place(self):
        """开关行为：已打开时再点编辑应当原地关闭，而不是重复建一个窗。"""
        with patch.object(m, "EditorWindow", wraps=m.EditorWindow) as mk:
            self.win.open_editor()
            first = self.win._editor
            self.assertTrue(first.isVisible())
            self.win.open_editor()
            self.assertEqual(mk.call_count, 1, "第二次点击又建了一个编辑窗")
        self.assertFalse(first.isVisible(), "第二次点击必须把旧窗原地关掉")

    def test_closing_the_editor_clears_the_reference(self):
        """on_closed 是主路径：引用不清，下次 open_editor 会对着已死对象判断可见性。"""
        self.win.open_editor()
        ed = self.win._editor
        ed._close()          # ✕ / Esc / 替换成功都走这里，同步通知主窗
        self.assertIsNone(self.win._editor)
        self.assertFalse(ed.isVisible())

    def test_the_identity_check_protects_a_newer_window(self):
        """_clear 只在当前引用还是"自己"时才清空：否则旧窗销毁会误清新窗。"""
        self.win.open_editor()
        old = self.win._editor
        old._closed = True
        new = mock.Mock()
        self.win._editor = new
        cb = old.on_closed
        cb()
        self.assertIs(self.win._editor, new, "旧弹窗的清理回调把新弹窗引用抹掉了")

    def test_a_dangling_reference_is_replaced_not_fatal(self):
        """C++ 对象已被删（悬空引用）时 isVisible() 抛 RuntimeError，必须照旧开新窗。"""
        dangling = mock.Mock()
        dangling.isVisible.side_effect = RuntimeError("deleted C++ object")
        dangling.destroyed.disconnect.side_effect = RuntimeError("gone")
        dangling.deleteLater.side_effect = RuntimeError("gone")
        self.win._editor = dangling
        self.win.open_editor()
        self.assertIsNot(self.win._editor, dangling)
        self.assertIsNotNone(self.win._editor)

    def test_a_broken_editor_construction_leaves_no_half_open_window(self):
        with patch.object(m, "EditorWindow", side_effect=RuntimeError("no glass")):
            with patch.object(m, "log_line") as lg:
                self.win.open_editor()
        self.assertIsNone(self.win._editor)
        self.assertIn("open_editor error", lg.call_args.args[0])

    def test_the_editor_gets_the_main_window_width(self):
        """弹窗与主窗同宽：硬编码 230 会在主窗自适应变宽后明显窄一截。

        注：绑定方法每次访问都是新对象，比较身份要用 __func__ / __self__。
        """
        with patch.object(m, "EditorWindow") as ed:
            self.win.open_editor()
        args = ed.call_args.args
        self.assertEqual(args[3], self.win.width())
        self.assertEqual(args[0], self.win.code)
        self.assertIs(args[1].__func__, m.FloatWindow.apply_stock)
        self.assertIs(args[1].__self__, self.win)


# --------------------------------------------------------------------------
# 更新：后台下载与进度合流
# --------------------------------------------------------------------------

class TestUpdateDownload(WindowCase):
    def setUp(self):
        super().setUp()
        self.win._update_downloading = False
        self.win._update_cancel = None
        self.win._quitting = False
        self.queued = []
        patch.object(self.win, "_q_put",
                     side_effect=lambda item: self.queued.append(item)).start()
        patch.object(self.win, "_open_download_progress_box").start()
        self.update = sf_update.UpdateInfo(
            version="9.9.9", notes="n",
            archive_url="https://github.com/o/r/a.zip",
            archive_size=100, archive_sha256="a" * 64)

    def test_a_second_click_while_downloading_is_ignored(self):
        """重复起两轮下载会抢同一个临时文件，也会让进度框被开两次。"""
        gate = threading.Event()
        with patch.object(sf_update, "download_update",
                          side_effect=lambda *a, **k: gate.wait(3.0)):
            self.win._download_update_background(self.update)
            started = self.win._update_downloading
            self.win._download_update_background(self.update)
        self.assertTrue(started)
        gate.set()
        self.win._update_downloading = False

    def test_downloading_is_refused_while_quitting(self):
        self.win._quitting = True
        self.win._download_update_background(self.update)
        self.assertFalse(self.win._update_downloading)

    def test_progress_is_coalesced_to_one_event_per_percent(self):
        """8 路分片每 1MB 回调一次：洪峰会把一次性的完成事件挤掉。"""
        captured = {}

        def fake_download(info, cancel_event=None, progress_callback=None):
            captured["cb"] = progress_callback
            return "/tmp/StockFloatTargetPrice-9.9.9.zip"

        with patch.object(sf_update, "download_update", side_effect=fake_download):
            self.win._download_update_background(self.update)
            self.assertTrue(wait_for(lambda: "cb" in captured, timeout=3.0))
            cb = captured["cb"]
            # 8 路分片每 1MB 回调一次：同一个百分比内会来几十次
            for _ in range(60):
                cb(50, 100)
            for _ in range(60):
                cb(51, 100)
            cb(100, 100)                    # 收尾（100%）恒放行，不能被合流吃掉
            self.assertIn(("update_progress", (50, 50, 100)), self.queued)
            self.assertIn(("update_progress", (51, 51, 100)), self.queued)
        progress = [q[1] for q in self.queued if q[0] == "update_progress"]
        self.assertEqual(len(progress), 3,
                         "两个百分比 + 一次收尾 = 3 条，实测 %d 条" % len(progress))
        self.assertEqual(progress[-1], (100, 100, 100), "最后一条必须是 100%")
        self.assertIn("update_download", [q[0] for q in self.queued])

    def test_zero_total_does_not_divide_by_zero(self):
        captured = {}

        def fake_download(info, cancel_event=None, progress_callback=None):
            captured["cb"] = progress_callback
            return "/tmp/a.zip"

        with patch.object(sf_update, "download_update", side_effect=fake_download):
            self.win._download_update_background(self.update)
            self.assertTrue(wait_for(lambda: "cb" in captured, timeout=3.0))
            captured["cb"](0, 0)
        kinds = [q[0] for q in self.queued]
        self.assertIn("update_progress", kinds)
        self.assertEqual([q for q in self.queued if q[0] == "update_progress"][0][1],
                         (0, 0, 0))

    def test_a_download_failure_is_queued_for_the_ui_instead_of_lost(self):
        boom = sf_update.UpdateError("无法连接更新服务")
        with patch.object(sf_update, "download_update", side_effect=boom):
            self.win._download_update_background(self.update)
            self.assertTrue(wait_for(
                lambda: any(q[0] == "update_download" for q in self.queued),
                timeout=3.0, msg="失败也必须回报给 UI，否则进度框永不收尾"))
        payload = [q[1] for q in self.queued if q[0] == "update_download"][0]
        self.assertIs(payload[2], boom)
        self.assertIsNone(payload[1])


class TestHandleUpdateDownload(WindowCase):
    def setUp(self):
        super().setUp()
        self.win._update_downloading = True
        self.win._update_cancel = threading.Event()
        self.win._quitting = False
        self.messages = []
        self.quits = []
        patch.object(self.win, "_show_update_message",
                     side_effect=lambda *a: self.messages.append(a)).start()
        patch.object(self.win, "_close_update_flow_box").start()
        patch.object(self.win, "quit",
                     side_effect=lambda *a, **k: self.quits.append(1)).start()
        self.update = sf_update.UpdateInfo(
            version="9.9.9", notes="", archive_url="https://github.com/o/r/a.zip",
            archive_size=1, archive_sha256="b" * 64)

    def test_the_progress_box_and_flags_are_closed_on_every_outcome(self):
        """完成事件先复位状态再判断退出中：否则进度框留着、下载锁死。"""
        self.win._handle_update_download(self.update, None,
                                         sf_update.UpdateError("x"))
        self.assertFalse(self.win._update_downloading)
        self.assertIsNone(self.win._update_cancel)
        self.win._close_update_flow_box.assert_called_once()

    def test_a_failed_download_says_what_failed_and_keeps_the_current_version(self):
        self.win._handle_update_download(self.update, None, OSError("net down"))
        self.assertEqual(len(self.messages), 1)
        title, text, detail = self.messages[0]
        self.assertEqual(title, "更新未完成")
        self.assertIn("下载或校验失败", text)
        self.assertIn("当前版本未受影响", detail)
        self.assertEqual(self.quits, [], "下载失败绝不能退出程序")

    def test_a_failed_handoff_says_so_instead_of_quitting_silently(self):
        """装不上却 quit = 用户看到的是程序消失，更新没发生。"""
        with patch.object(sf_update, "launch_installer",
                          side_effect=OSError("helper missing")):
            self.win._handle_update_download(
                self.update, "/tmp/a.zip", None)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("无法启动安装助手", self.messages[0][1])
        self.assertEqual(self.quits, [])

    def test_a_successful_handoff_launches_then_quits(self):
        with patch.object(sf_update, "launch_installer") as launch:
            self.win._handle_update_download(self.update, "/tmp/a.zip", None)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args, ("/tmp/a.zip", "b" * 64, "9.9.9"))
        self.assertRegex(launch.call_args.kwargs["handoff_id"], r"[a-f0-9]{32}")
        self.assertEqual(self.quits, [1])
        self.assertEqual(self.messages, [])

    def test_nothing_happens_after_the_user_started_quitting(self):
        """退出流程里再弹"更新未完成"会让收口被模态框挡住。"""
        self.win._quitting = True
        self.win._handle_update_download(self.update, None, OSError("late"))
        self.assertEqual(self.messages, [], "退出中不该再弹任何框")
        self.assertEqual(self.quits, [])
        self.assertFalse(self.win._update_downloading)


if __name__ == "__main__":
    unittest.main()
