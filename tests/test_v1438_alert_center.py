# -*- coding: utf-8 -*-
"""v1.4.38：检查更新相关的窗口一律落在**屏幕正中**（用户 2026-10-08 要求）。

分四层，从『不需要屏幕』到『必须真屏幕』：

  1. 纯几何 `centered_origin` —— 任何机器都能跑。守三件事：窗口中心 == 屏可用区
     中心；窗口比可用区还大时不外溢（宁可贴边也要让按钮可点）；副屏的负坐标照样居中。
  2. 选屏登记 `set_screen_anchor` / `target_screen` —— 脏值不得把弹窗弄丢。
  3. 真窗口落点（子进程 position 模式）—— 守的是『AppKit 会不会在我们摆完之后
     又按自己的版式挪回去』，这一条只有真屏幕能回答。
  4. 产品侧接线 —— 四个更新弹窗调用点有没有都把选屏锚点交给弹窗模块；
     原生弹窗不可用时的 Qt 兜底框同样居中。

改前实测（本机 1470x956 单屏，真弹出来量）：「正在检查更新」的窗口中心比屏幕可用区
中心偏左 384pt、偏上 178pt；带长更新说明的那版偏左 254pt、偏上 70pt。
"""

import os
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtGui import QGuiApplication                 # noqa: E402
from PyQt6.QtWidgets import QApplication                # noqa: E402

import sf_quote                                         # noqa: E402
import sf_system_alert as s                             # noqa: E402
import sf_update                                        # noqa: E402
import stock_float_window as m                          # noqa: E402

try:
    from _base import SFCase
except ImportError:                                      # pragma: no cover
    from tests._base import SFCase

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHILD = ROOT / "tests" / "_alert_child.py"
TIMEOUT_SECONDS = 120
# 原点用 round() 取整到整数 pt，1pt 以内算正中；Qt 兜底框用整除，多给 1px 余量。
CENTER_TOLERANCE_PT = 1.0
CENTER_TOLERANCE_PX = 2


def _spawn(mode):
    proc = subprocess.run([sys.executable, str(CHILD), mode],
                          capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                          cwd=str(ROOT))
    markers = {}
    for line in (proc.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            markers[key.strip()] = value.strip()
    return proc, markers


class CenteredOriginTest(SFCase):
    """纯几何：正中、超尺寸不外溢、副屏负坐标。"""

    def test_window_center_equals_visible_area_center(self):
        cases = (
            ((0.0, 60.0, 1470.0, 863.0), (260.0, 176.0)),   # 「正在检查更新」实测尺寸
            ((0.0, 60.0, 1470.0, 863.0), (520.0, 391.0)),   # 长更新说明（会长高）
            ((0.0, 0.0, 1920.0, 1080.0), (420.0, 96.0)),
            ((-1920.0, 0.0, 1920.0, 1050.0), (260.0, 176.0)),  # 排在主屏左侧的副屏
        )
        for visible, size in cases:
            with self.subTest(visible=visible, size=size):
                x, y = s.centered_origin(visible, size)
                cx = x + size[0] / 2.0
                cy = y + size[1] / 2.0
                self.assertAlmostEqual(visible[0] + visible[2] / 2.0, cx,
                                       delta=CENTER_TOLERANCE_PT,
                                       msg="水平没居中：窗口中心 x=%s" % cx)
                self.assertAlmostEqual(visible[1] + visible[3] / 2.0, cy,
                                       delta=CENTER_TOLERANCE_PT,
                                       msg="垂直没居中：窗口中心 y=%s" % cy)

    def test_oversized_window_stays_inside_the_visible_area(self):
        """比可用区还高的框（超长说明 + 进度条）不得把按钮顶到 Dock 底下。"""
        visible = (0.0, 0.0, 400.0, 300.0)
        for size in ((260.0, 500.0), (2000.0, 100.0), (2000.0, 900.0)):
            with self.subTest(size=size):
                x, y = s.centered_origin(visible, size)
                self.assertGreaterEqual(x, visible[0], "左边缘越出可用区")
                self.assertGreaterEqual(y, visible[1], "下边缘越出可用区（按钮会够不到）")
                self.assertLessEqual(x, visible[0] + max(0.0, visible[2] - size[0]))
                self.assertLessEqual(y, visible[1] + max(0.0, visible[3] - size[1]))


@unittest.skipIf(s.AppKit is None, "本机无 AppKit，选屏这条路径不存在")
class ScreenAnchorTest(SFCase):
    """选屏：登记过的锚点要用上，脏值也不能把弹窗弄丢。"""

    def tearDown(self):
        # 锚点是模块级状态：不清就会带进下一个用例（甚至下一个测试文件）。
        s.set_screen_anchor(None, None)
        super().tearDown()

    def test_anchor_inside_a_screen_selects_that_screen(self):
        screen = s.target_screen()
        self.assertIsNotNone(screen, "一块屏都没有 = 这台机器不该跑这条断言")
        frame = screen.frame()
        s.set_screen_anchor(frame.origin.x + frame.size.width / 2.0,
                            frame.origin.y + frame.size.height / 2.0)
        self.assertEqual(screen, s.target_screen(),
                         "锚点落在某块屏内，就必须选那块屏")

    def test_dirty_anchor_still_yields_a_screen(self):
        """脏锚点（配置里飘出来的坐标、刚拔掉的副屏）要退回鼠标/主屏，不能不弹。"""
        s.set_screen_anchor(10 ** 9, -10 ** 9)
        self.assertIsNotNone(s.target_screen())
        s.set_screen_anchor("不是数字", None)
        self.assertIsNone(s.screen_anchor(), "取值非法应清空登记，而不是留个脏值")
        self.assertIsNotNone(s.target_screen())


@unittest.skipIf(s.AppKit is None, "本机无 AppKit")
class RealAlertPositionTest(SFCase):
    """真窗口、真屏幕：量用户实际看见的落点。"""

    @classmethod
    def setUpClass(cls):
        try:
            proc, markers = _spawn("probe")
        except subprocess.TimeoutExpired:
            raise unittest.SkipTest("弹窗预检超时：本机可能没有窗口服务")
        if markers.get("probe") != "ok":
            raise unittest.SkipTest(
                "弹窗预检未通过（无窗口服务？）probe=%s rc=%s"
                % (markers.get("probe"), proc.returncode))

    def test_update_dialogs_land_in_the_screen_center(self):
        proc, markers = _spawn("position")
        self.assertNotIn("error", markers, "子进程抛异常: %s" % markers.get("error"))
        self.assertEqual(markers.get("position"), "ok",
                         "弹窗落点不在正中（改前实测偏左 384pt、偏上 178pt）：%s｜%s"
                         % (markers.get("problems"), markers))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class UpdateDialogWiringTest(SFCase):
    """产品侧接线：每一次检查更新相关的弹窗，都要先说清『弹在哪块屏』。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
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
        ]
        for p in self._patches:
            self.track_patch(p)
        self.win = self.track_widget(m.FloatWindow())
        self.win.running = False
        self.win._wake.set()  # discipline-ok(R5): 线程唤醒事件没有公开入口，夹具必须置位否则行情线程空转
        for t in (self.win.timer, self.win.top_timer,  # discipline-ok(R5): 四个定时器只能逐个停，没有统一开关
                  self.win._anim_timer,  # discipline-ok(R5): 同上
                  self.win._flash_timer):  # discipline-ok(R5): 同上
            try:
                t.stop()
            except Exception:
                pass
        self.win.show()
        QApplication.processEvents()
        s.set_screen_anchor(None, None)
        self.shown = []

    def tearDown(self):
        self.win._close_update_flow_box()  # discipline-ok(R5): 收口更新框是内部生命周期方法，公开路径只有点按钮
        s.set_screen_anchor(None, None)
        # 关窗与停 patch 交给 SFCase：它按『先停 patch 再关窗』的顺序收口，
        # 顺序反了会走被 mock 掉的析构路径（见 tests/_base.py 的注释）。
        super().tearDown()

    def _fake_show_alert(self, **kwargs):
        """在弹窗真正要出现的这一刻，查一次选屏锚点登记好了没有。"""
        self.shown.append((kwargs.get("title"), s.screen_anchor()))
        return None

    def _area_center(self, widget):
        scr = (QGuiApplication.screenAt(self.win.frameGeometry().center())
               or QGuiApplication.primaryScreen())
        return scr.availableGeometry().center(), widget.frameGeometry().center()

    def test_message_prompt_and_progress_all_pick_a_screen(self):
        """三条更新文案路径（已是最新 / 发现新版 / 检查失败）共用同一个出口。"""
        with patch.object(s, "show_alert", self._fake_show_alert):
            self.win._show_update_message("检查更新", "当前已是最新版本。")  # discipline-ok(R5): 要量的就是这条内部出口有没有登记选屏
            update = sf_update.UpdateInfo(
                version="9.9.9", notes="修了一些问题",
                archive_url="https://github.com/o/r/a.zip",
                archive_size=100, archive_sha256="a" * 64)
            self.win._prompt_update(update)  # discipline-ok(R5): 同上（新版确认框）
        self.assertEqual(len(self.shown), 2)
        for title, anchor in self.shown:
            with self.subTest(title=title):
                self.assertIsNotNone(
                    anchor, "弹窗没登记选屏：副屏上点检查更新，弹窗会跑到另一块屏")

    def test_manual_check_reports_checking_dialog_on_screen(self):
        """手动检查更新的第一帧提示框（用户截图里那一次）同样要登记选屏。

        为什么先用 patch.object 把并发闸门摆成『空闲』：FloatWindow 一建好就会起
        一次启动后的静默自检，那个网络线程可能还在飞，手动检查会被
        `_update_checking` 那道闸门直接挡掉 —— 与本轮改动无关的前置条件，
        用 patch 而不是直接写属性，避免测试代码依赖内部字段名。
        """
        queued = []
        for name, value in (("_update_checking", False),
                            ("_update_downloading", False),
                            ("_update_transition_pending", False),
                            ("_update_pending", None)):
            self.track_patch(patch.object(self.win, name, value))
        with patch.object(s, "show_alert", self._fake_show_alert), \
                patch.object(sf_update, "check_for_update", lambda *a, **k: None), \
                patch.object(self.win, "_q_put", side_effect=queued.append):
            self.win._check_updates_background(manual=True)  # discipline-ok(R5): 入口方法没有公开触发点
            for _ in range(200):
                if queued:
                    break
                QApplication.processEvents()
        self.assertEqual([t for t, _a in self.shown], ["软件更新"],
                         "手动检查必须先弹『正在检查更新』")
        self.assertIsNotNone(self.shown[0][1],
                             "检查中提示框没登记选屏：副屏上点检查更新会跑错屏")

    def test_qt_fallback_progress_box_is_centered_too(self):
        """原生弹窗不可用时的 Qt 自绘兜底框：同样落在屏正中。"""
        with patch.object(s, "show_progress_alert", lambda **k: None):
            box = self.win._open_download_progress_box()  # discipline-ok(R5): 下载框由下载线程间接打开，测试里直接开这一层
        self.assertIsNotNone(box, "兜底路径必须仍然给出一个框")
        QApplication.processEvents()
        area_center, box_center = self._area_center(box)
        self.assertLessEqual(abs(box_center.x() - area_center.x()), CENTER_TOLERANCE_PX,
                             "兜底下载框没居中：x=%s" % box_center.x())
        self.assertLessEqual(abs(box_center.y() - area_center.y()), CENTER_TOLERANCE_PX,
                             "兜底下载框没居中：y=%s" % box_center.y())


if __name__ == "__main__":
    unittest.main()
