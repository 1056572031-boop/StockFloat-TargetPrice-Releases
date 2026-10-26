# -*- coding: utf-8 -*-
"""点击分时图热区切换收/展：必须由**真点那个坐标**证明（2026-10-23，"发布流程补真实点击" A 档之二）。

由来：收展这条线在套件里一直是"直调函数"测的 —— `test_bugfix_148.py` 直接
`self.win._toggle_chart()`，`test_coverage_window_interactions.py` 用一个
`_chart_hit=lambda pos: True` 的替身宿主喂假事件。两者都证明"切换函数会切"，
没有一个证明"**用户点在分时图那块地方，主窗自己会切**"。而这条线真正的实现是
`mousePressEvent`/`mouseReleaseEvent` 里的三段判定（热区命中 → 未越过系统拖动阈值
→ 松开仍在热区），热区边界与阈值判定都住在主窗里，替身宿主一次都没碰到。

本文件把这三段判定用 QTest 真事件、真坐标各测一遍，断言只看可观测事实：
窗口高度变了没有、窗口原点动了没有。不读 `_chart_expanded` 作为结论。

离屏（offscreen）可跑，不需要真屏幕。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import sf_quote
import stock_float_window as m
from tests._base import SFCase

TOP_BAND_Y = 40          # 顶部按钮行与热区起点（Y_PRICE=68 / CHART_TOP=102）之间的空白
HOT_ZONE_INSET = 8       # 取热区内一点，避开边界
LAYOUT_SETTLE_MSG = "点击后 2 秒内收展动画必须落定"


def _fake_http_get(url, **kw):
    """离线行情/分时 mock：主备源都给合法数据，避免用例真联网。"""
    if "hq.sinajs.cn" in url:
        return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
    if "/appstock/app/minute/query?" in url:
        return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                '"data":["0930 1510.0 100 1000", "1000 1511.0 200 2000"]}}}}')
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


class ChartClickToggleCase(SFCase):
    def setUp(self):
        super().setUp()
        self.ensure_qapp()
        tmp = tempfile.mkdtemp(prefix="sf_chart_click_")
        self.cfg_path = os.path.join(tmp, "config.json")
        self.log_path = os.path.join(tmp, "debug.log")
        # §6.14：窗口不是被测对象的旁观者，构造它的全部代价都是夹具的责任。
        # 落盘路径全部改道临时目录，帧驱动/行情线程不起，图标 worker 不碰包体。
        patches = [
            mock.patch.object(m, "load_config",
                              return_value=("sh600519", 100, 100, 200, "translucent")),
            mock.patch.object(m, "save_config", lambda *a, **k: True),
            mock.patch.object(sf_quote, "http_get", _fake_http_get),
            mock.patch.object(m, "start_icon_worker", lambda: None),
            mock.patch.object(m.FloatWindow, "_force_top", lambda self: None),
            mock.patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            mock.patch.object(m.FloatWindow, "_quote_loop_wrapper", lambda self: None),
            mock.patch.object(m.FloatWindow, "_minute_loop_wrapper", lambda self: None),
            mock.patch.object(m, "CONFIG_PATH", self.cfg_path),
            mock.patch.object(m, "CONFIG_BACKUP_PATH", self.cfg_path + ".bak"),
            mock.patch.object(m, "LAST_QUOTE_PATH", os.path.join(tmp, "last_quote.json")),
            mock.patch.object(m, "LOG_PATH", self.log_path),
        ]
        for p in patches:
            self.track_patch(p)
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()  # discipline-ok(R5): 夹具要放开唤醒闸，FloatWindow 没有公开开关
        for timer in (self.win.timer, self.win.top_timer, self.win._flash_timer):  # discipline-ok(R5): 停常驻节拍是夹具职责，这几个句柄都只有私有的
            timer.stop()
        # 注意：**不**停 _anim_timer —— 收展是 200ms 逐帧 resize，把它停了就等于
        # 断言"点了没反应"是合理的。落定判定见 _wait_layout_done。
        self.win.show()
        QApplication.processEvents()

    def tearDown(self):
        # §6.11：关窗必须发生在 patch 还活着的时候，否则 closeEvent 的落盘走真实实现
        self.win.close()
        QApplication.processEvents()
        super().tearDown()

    # ---------- 辅助 ----------
    def _hot_point(self):
        """在当前状态下取分时图热区内一点（用布局常量定位，不借被测判定反推）。"""
        top = m.CHART_TOP if self.win.height() > m.Y_PRICE + m.CHART_TOP else m.Y_PRICE
        return QPoint(m.W // 2, top + HOT_ZONE_INSET)

    def _wait_layout_done(self, timeout_sec=2.0):
        """把收展动画泵到落定：轮询定时器状态，不猜固定时长（§6 第 3 条）。"""
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if not self.win._anim_timer.isActive():  # discipline-ok(R5): 判"动画落定"只有这个句柄
                return True
            QTest.qWait(10)
        return False

    def _click(self, pos):
        QTest.mousePress(self.win, Qt.MouseButton.LeftButton, pos=pos)
        QTest.mouseRelease(self.win, Qt.MouseButton.LeftButton, pos=pos)

    # ---------- 用例 ----------
    def test_点热区收起再点展开_高度回到原值(self):
        pos = self._hot_point()
        self.assertTrue(self.win._chart_hit(pos),  # discipline-ok(R5): 前提检查，不是结论
                        "前提：这个点必须落在主窗自己的分时图热区里")
        h_expanded = self.win.height()
        self._click(pos)
        self.assertTrue(self._wait_layout_done(), LAYOUT_SETTLE_MSG)
        h_collapsed = self.win.height()
        self.assertLess(h_collapsed, h_expanded,
                        "点在分时图热区上，窗口应当收起（高度变小）")

        # 收起态的热区起点换到股价行，重新取点后再点一次必须展回原高度
        pos_again = self._hot_point()
        self.assertTrue(self.win._chart_hit(pos_again))  # discipline-ok(R5): 同上，前提检查
        self._click(pos_again)
        self.assertTrue(self._wait_layout_done(), LAYOUT_SETTLE_MSG)
        self.assertEqual(self.win.height(), h_expanded,
                         "展开高度应回到上次展开的那一档（记忆展开高度）")

    def test_热区上方按下不切换分时图(self):
        h0 = self.win.height()
        pos = QPoint(m.W // 2, TOP_BAND_Y)
        self.assertFalse(self.win._chart_hit(pos),  # discipline-ok(R5): 前提检查
                         "前提：顶部空白不在分时图热区内")
        self._click(pos)
        self.assertTrue(self._wait_layout_done(), LAYOUT_SETTLE_MSG)
        self.assertEqual(self.win.height(), h0, "热区外的一次点击不该把分时图收掉")

    def test_在热区按下后拖过阈值只拖窗不收展(self):
        """v1.2.6 那条"避免拖拽误触"的守卫：越过系统拖动阈值后必须转成拖窗口。"""
        pos = self._hot_point()
        h0 = self.win.height()
        origin = self.win.pos()
        drift = QApplication.startDragDistance() + 20
        moved = QPoint(pos.x(), pos.y() + drift)
        QTest.mousePress(self.win, Qt.MouseButton.LeftButton, pos=pos)
        QTest.mouseMove(self.win, moved)
        # 松开点仍取"热区内"的那个局部坐标：如果切换仍然发生，说明阈值判定没生效
        QTest.mouseRelease(self.win, Qt.MouseButton.LeftButton, pos=moved)
        self.assertTrue(self._wait_layout_done(), LAYOUT_SETTLE_MSG)
        self.assertEqual(self.win.height(), h0,
                         "拖过阈值的这次手势只是搬窗口，不该顺手收起分时图")
        self.assertNotEqual(self.win.pos(), origin, "同一次手势应把窗口搬走")

    def test_按下与松开之间没送移动事件_位移超阈值也不收展(self):
        """松开侧的静止判定必须自己成立。

        与上一条的分工：`test_在热区按下后拖过阈值只拖窗不收展` 里 move 事件真的到了，
        `mouseMoveEvent` 先把 `_press_chart` 清掉，于是 `mouseReleaseEvent` 里那道
        "按下点与松开点位移 < 系统拖动阈值"的守卫**根本没被执行到** —— 实测把它删掉，
        那一条照旧判绿。本条故意不送 move 事件（macOS 原生置顶/跨 Space 偶发丢手势就是
        这个形状），只让按下与松开相隔几十像素，逼这条守卫单独出面。
        """
        press = QPoint(m.W // 2, m.CHART_TOP + 3)
        release = QPoint(m.W // 2, self.win.height() - m.BOTTOM_SECTION_HIDDEN - 3)
        self.assertTrue(self.win._chart_hit(press) and self.win._chart_hit(release),  # discipline-ok(R5): 前提检查
                        "前提：按下与松开都落在热区内，差别只在位移")
        self.assertGreaterEqual((release - press).manhattanLength(),
                                QApplication.startDragDistance(),
                        "前提：两点间距必须已经越过系统拖动阈值")
        h0 = self.win.height()
        QTest.mousePress(self.win, Qt.MouseButton.LeftButton, pos=press)
        QTest.mouseRelease(self.win, Qt.MouseButton.LeftButton, pos=release)
        self.assertTrue(self._wait_layout_done(), LAYOUT_SETTLE_MSG)
        self.assertEqual(self.win.height(), h0,
                         "一次没有移动过程的大位移不该被当成'点击收展'")


if __name__ == "__main__":
    unittest.main()
