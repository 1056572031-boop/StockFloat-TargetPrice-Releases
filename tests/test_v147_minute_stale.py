# -*- coding: utf-8 -*-
"""v1.4.7 分时盘中容错回归：短暂故障不再闪白屏，滞后态不冒充实时。

背景：分时图经常"加载中…"——根因是 _fetch_minute 在盘中失败/空返回时
直接投递空点集清空当日曲线。本次改为投递 minute_stale 信号：
  - 屏幕已有**当日**曲线 → 保留 + 渲染层压暗 + "数据暂缺 · 重试中"，呼吸点冻结；
  - 数据跨日或当日从未取到 → 仍清空（防把昨日全量走势冒充当日进度）；
  - 任一成功回包立即恢复实时态。
断言全部走行为：队列消息、状态字段、离屏渲染像素，不读源码字符串。
"""

import datetime
import math
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

import sf_quote
import sf_ui_render
import stock_float_window as m
import targets as _targets
from sf_ui_render import BOTTOM_SECTION_HIDDEN, CHART_TOP, PAD_X


def _today():
    # 必须按**模块属性**取函数，不能 `from targets import _beijing_now`：
    # 那样名字在导入那一刻就绑成真实函数，setUp 里打在 targets 上的 patch 够不着它，
    # 于是断言两侧看的不是同一个"今天"。本机是北京时区、两边同一天，永远复现不了；
    # UTC runner 上每天 16:00~24:00 UTC 这段窗口里，注入的固定时钟是 UTC 日期、
    # 这里返回北京日期，差一天必红（2026-10-10 实测 '2026-10-10' != '2026-10-11'）。
    return _targets._beijing_now().strftime("%Y-%m-%d")  # discipline-ok(R5): 固定时钟必须打在产品真正查找的那个名字上（targets 的模块属性），它没有公开别名；不打这里就等于没注入（本次的缺陷正是这么来的）


def _quote(pre_close=403.50):
    return {
        "name": "贵州茅台", "code": "600519", "price": str(pre_close),
        "pct": "0.00", "change": "0.00", "pre_close": str(pre_close),
        "high": str(pre_close), "low": str(pre_close),
        "volume": "12345", "limit_up": "443.85", "limit_down": "363.15",
    }


def _mk_pts(pre_close=403.50, n=120, amp=0.02):
    times = [9 * 60 + 30 + i for i in range(120)]
    pts = []
    for i, tm in enumerate(times[:n]):
        hh, mm = tm // 60, tm % 60
        price = pre_close * (1 + amp * math.sin(i / 20.0))
        avg = pre_close * (1 + 0.6 * amp * math.sin(i / 25.0))
        pts.append(("%02d%02d" % (hh, mm), price, avg, 1000 + i * 10))
    return pts


class _WinBase(unittest.TestCase):
    """与 test_v130/test_bugfix_148 相同的 patch 模式，构造后停掉后台线程。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        # 固定时钟：消除跨午夜失败风险（项目测试规范 §6 第 4 条）。
        # 同时 patch 被测模块（stock_float_window）和测试辅助函数（_today → targets）
        # 两处引用的 _beijing_now，确保断言两侧看到同一个"今天"。
        import targets as _targets_mod
        _fixed_now = datetime.datetime(2026, 1, 5, 10, 0)
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", lambda *a, **k: ""),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
            patch.object(m, "_beijing_now", return_value=_fixed_now),
            patch.object(_targets_mod, "_beijing_now", return_value=_fixed_now),
            patch.object(sf_quote, "_beijing_now", return_value=_fixed_now),
            patch.object(sf_quote, "is_holiday", return_value=False),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()
        self.win.timer.stop()
        self.win.top_timer.stop()
        self.win._anim_timer.stop()
        self.win._flash_timer.stop()
        for _t in (getattr(self.win, "_quote_thread", None),
                   getattr(self.win, "_minute_thread", None)):
            if _t is not None:
                _t.join(timeout=1.5)
        self._drain_queue()
        self.win.show()

    def tearDown(self):
        self.win.close()
        for p in self._patches:
            p.stop()

    def _drain_queue(self):
        import queue as _queue
        while True:
            try:
                self.win.q.get_nowait()
            except _queue.Empty:
                return

    def _queue_items(self):
        items = []
        while True:
            try:
                items.append(self.win.q.get_nowait())
            except Exception:
                return items


class TestFetchMinuteSignal(_WinBase):
    """拉取线程侧：盘中失败投递 minute_stale，不再投递空点集。"""

    def test_exception_pushes_stale_not_clear(self):
        with patch.object(m, "fetch_minute", side_effect=RuntimeError("net down")), \
             patch.object(self.win, "_is_trading_time", return_value=True):
            self.assertFalse(self.win._fetch_minute())
        self.assertEqual(self._queue_items(), [("minute_stale", self.win.code)],
                         "盘中失败必须投递 stale 信号，而不是清空曲线的空点集")

    def test_empty_during_trading_pushes_stale(self):
        with patch.object(m, "fetch_minute", return_value=[]), \
             patch.object(self.win, "_is_trading_time", return_value=True):
            self.assertFalse(self.win._fetch_minute())
        self.assertEqual(self._queue_items(), [("minute_stale", self.win.code)])

    def test_success_pushes_points(self):
        pts = _mk_pts(n=5)
        with patch.object(m, "fetch_minute", return_value=pts), \
             patch.object(self.win, "_is_trading_time", return_value=True):
            self.assertTrue(self.win._fetch_minute())
        self.assertEqual(self._queue_items(), [("minute", (self.win.code, pts))])

    def test_off_market_failure_pushes_nothing(self):
        """非交易时段失败不投递：收盘后保留既有走势属正常，无需滞后态。"""
        with patch.object(m, "fetch_minute", side_effect=RuntimeError("net down")), \
             patch.object(self.win, "_is_trading_time", return_value=False):
            self.assertFalse(self.win._fetch_minute())
        self.assertEqual(self._queue_items(), [])


class TestStaleHandling(_WinBase):
    """主线程侧状态机：当日保留、跨日清空、迟到信号丢弃。"""

    def test_today_curve_kept_and_marked_stale(self):
        pts = _mk_pts(n=10)
        self.win._apply_minute_points(pts)
        self.win._handle_minute_stale(self.win.code)
        self.assertEqual(self.win.minute_points, pts, "当日曲线必须保留（不再闪白屏）")
        self.assertTrue(self.win._minute_stale)

    def test_cross_day_curve_cleared(self):
        """屏幕上是历史日数据时失败必须清空 —— 防把昨日全量走势冒充当日进度。"""
        self.win.minute_points = _mk_pts(n=10)
        self.win._minute_day = "2000-01-01"
        ver_before = self.win._line_ver
        self.win._handle_minute_stale(self.win.code)
        self.assertEqual(self.win.minute_points, [])
        self.assertEqual(self.win._line_ver, ver_before + 1, "清空必须失效折线缓存")
        self.assertFalse(self.win._minute_stale)

    def test_stale_for_other_code_ignored(self):
        pts = _mk_pts(n=10)
        self.win._apply_minute_points(pts)
        self.win._handle_minute_stale("sz000001")
        self.assertEqual(self.win.minute_points, pts)
        self.assertFalse(self.win._minute_stale, "切股后的迟到失败不得污染新股票")

    def test_apply_points_marks_today_and_clears_stale(self):
        self.win._minute_stale = True
        self.win._apply_minute_points(_mk_pts(n=3))
        self.assertEqual(self.win._minute_day, _today())
        self.assertFalse(self.win._minute_stale, "成功回包即恢复实时态")

    def test_helper_today_follows_the_injected_clock(self):
        """夹具的前提：_today() 必须读到 setUp 注入的那个时钟，而不是真实墙钟。

        这条不测产品，测的是"我的固定时钟有没有真的生效"。它原先没生效
        （`from targets import _beijing_now` 在导入那一刻就把真函数绑走了，
        打在 targets 上的 patch 够不着），于是上一条在 UTC runner 上每天
        16:00~24:00 UTC 那八小时必红，而本机（北京时区）永远是绿的。
        """
        sentinel = datetime.datetime(2001, 2, 3, 10, 0)
        with patch.object(_targets, "_beijing_now", return_value=sentinel):
            self.assertEqual(_today(), "2001-02-03",
                             "_today() 绕过了注入的时钟 —— 断言两侧不是同一个「今天」")


class TestPollWiring(_WinBase):
    """队列 → 状态 的接线：成功与失败按到达顺序生效。"""

    def test_success_then_stale_keeps_curve(self):
        pts = _mk_pts(n=8)
        self.win.running = True   # _poll 有 running 门控（与 test_bugfix_148 同法）
        self.win.q.put(("minute", (self.win.code, pts)))
        self.win.q.put(("minute_stale", self.win.code))
        with patch.object(self.win, "_apply_icons", lambda: None):
            self.win._poll()
        self.assertEqual(self.win.minute_points, pts)
        self.assertTrue(self.win._minute_stale)

    def test_stale_then_success_recovers(self):
        pts = _mk_pts(n=8)
        self.win.running = True
        self.win.q.put(("minute_stale", self.win.code))
        self.win.q.put(("minute", (self.win.code, pts)))
        with patch.object(self.win, "_apply_icons", lambda: None):
            self.win._poll()
        self.assertEqual(self.win.minute_points, pts)
        self.assertFalse(self.win._minute_stale, "同批里成功在后，失败态必须被覆盖")

    def test_minimized_branch_also_handles_stale(self):
        pts = _mk_pts(n=8)
        self.win._apply_minute_points(pts)
        self.win.running = True
        self.win._minimized = True
        try:
            with patch.object(self.win, "_apply_icons", lambda: None):
                self.win.q.put(("minute_stale", self.win.code))
                self.win._poll()
        finally:
            self.win.running = False
            self.win._minimized = False
        self.assertEqual(self.win.minute_points, pts)
        self.assertTrue(self.win._minute_stale)


def _chart_image(win, w=250, h=206):
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(PAD_X, CHART_TOP, w - 2 * PAD_X, h - CHART_TOP - BOTTOM_SECTION_HIDDEN)
    win._draw_chart(p, rect)
    p.end()
    return img


def _mean_brightness(img):
    data = bytes(img.constBits().asstring(img.sizeInBytes()))
    total = 0
    count = 0
    for i in range(0, len(data), 4):     # ARGB32 小端：B G R A
        b, g, r = data[i], data[i + 1], data[i + 2]
        total += r + g + b
        count += 1
    return total / max(count * 3, 1)


class TestStaleRendering(_WinBase):
    """滞后态保留原亮度曲线，状态在图外标签中展示。"""

    def setUp(self):
        super().setUp()
        self.win.quote = _quote()
        self.win.minute_points = _mk_pts()
        self._time_patch = patch.object(sf_ui_render.time, "monotonic",
                                        return_value=1000.0)
        self._time_patch.start()
        self.addCleanup(self._time_patch.stop)

    def test_stale_preserves_chart_brightness(self):
        self.win._minute_stale = False
        fresh = _chart_image(self.win)
        self.win._minute_stale = True
        stale = _chart_image(self.win)
        # 呼吸点仍冻结为最小态，因此容许端点附近的局部差异，不能压暗整图。
        self.assertGreaterEqual(_mean_brightness(stale), _mean_brightness(fresh) * 0.95)
        data = bytes(stale.constBits().asstring(stale.sizeInBytes()))
        lum = {data[i:i + 3] for i in range(0, len(data), 4)}
        self.assertGreater(len(lum), 8, "折线/渐变必须仍可见，不得变成纯色死图")

    def test_fresh_render_untouched(self):
        """非滞后态渲染不得出现遮罩：与 stale=False 的默认路径一致（幂等）。"""
        self.win._minute_stale = False
        a = _chart_image(self.win)
        b = _chart_image(self.win)
        self.assertEqual(bytes(a.constBits().asstring(a.sizeInBytes())),
                         bytes(b.constBits().asstring(b.sizeInBytes())))


if __name__ == "__main__":
    unittest.main()
