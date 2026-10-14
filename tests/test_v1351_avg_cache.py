# -*- coding: utf-8 -*-
"""v1.3.52：分时均价线 QPainterPath 绘制缓存。

背景：_draw_chart_avg 此前每帧遍历 m["n"] 个点重建 QPainterPath（240 次
moveTo/lineTo），实测 0.49ms/帧、占整块重绘 28%。坐标数组 xs/na 早已有缓存
（_chart_pts_cache，v1.2.20），但 path 本身没有 —— 本轮按折线 _line_path
的同一模式给 path 也加上缓存。

本组用例锁死四件事：
  1) 同数据 + 同 rect 连续两帧 → 第二帧命中缓存（不重建 path）；
  2) 走真实刷新路径（换 minute_points + _line_ver += 1）→ 缓存失效重建；
  3) 只改 rect 高度 → 缓存失效（path 存的是绝对 y = top + na*h）；
  4) 均价全 None / 首尾均为 None 的脏数据 → 不崩、不产生错误命中。

运行方式（工程根目录）：
    QT_QPA_PLATFORM=offscreen ./.venv/bin/python -m unittest discover -s tests -p "test_v1351_avg_cache.py" -v
"""
import math
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

import sf_quote
import sf_ui_render
import stock_float_window as m
from sf_ui_render import BOTTOM_SECTION_HIDDEN, CHART_TOP, PAD_X


# ============================== 测试数据构造 ==============================

def _quote(pre_close=403.50):
    return {
        "name": "贵州茅台", "code": "sh600519", "price": str(pre_close),
        "pct": "0.00", "change": "0.00", "pre_close": str(pre_close),
        "high": str(pre_close), "low": str(pre_close),
        "volume": "12345", "limit_up": "443.85", "limit_down": "363.15",
    }


def _mk_pts(pre_close=403.50, n=240, amp=0.02):
    """分时点 (hhmm, price, avg, vol)：9:30~11:30 + 13:00~15:00 共 240 分钟。"""
    times = [9 * 60 + 30 + i for i in range(120)] + [13 * 60 + i for i in range(120)]
    pts = []
    for i, tm in enumerate(times[:n]):
        hh, mm = tm // 60, tm % 60
        price = pre_close * (1 + amp * math.sin(i / 20.0) + amp * 0.3 * math.sin(i / 7.0))
        avg = pre_close * (1 + 0.6 * amp * math.sin(i / 25.0))
        pts.append(("%02d%02d" % (hh, mm), price, avg, 1000 + i * 10))
    return pts


# ============================== 公共夹具 ==============================

class TestAvgCacheBase(unittest.TestCase):
    """与 test_stretch.py 相同的 patch 模式构造 FloatWindow，停后台线程。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._td = tempfile.mkdtemp(prefix="stockfloat_avg_cache_")
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "load_api_urls", lambda: None),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", lambda *a, **k: ""),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        # 呼吸灯依赖 time.monotonic()，冻结时间保证两帧渲染可比
        self._time_patch = patch.object(sf_ui_render.time, "monotonic", return_value=1000.0)
        self._time_patch.start()
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()
        self.win.timer.stop()
        self.win.top_timer.stop()
        self.win._anim_timer.stop()
        self.win._flash_timer.stop()
        self.win.minute_points = _mk_pts()
        self.win.quote = _quote()

    def tearDown(self):
        try:
            self.win.close()
        except Exception:
            pass
        self._time_patch.stop()
        for p in self._patches:
            p.stop()

    # ---- 渲染辅助 ----

    def _rect(self, h=100):
        return QRectF(PAD_X, CHART_TOP, 250.0, float(h))

    def _draw_avg(self, rect):
        """只画均价线一帧（隔离掉填充/折线，便于精确统计重建次数）。"""
        img = QImage(300, 200, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        mm = self.win._chart_metrics(self.win.minute_points, rect)
        self.win._draw_chart_avg(p, rect, mm)
        p.end()
        return self.win._avg_path_cache

    def _count_rebuilds(self, rect, times=2):
        """连续画 times 帧，返回 (重建次数, 每帧的缓存 key, 每帧的 path 对象)。

        重建次数 = _chart_pts_cache 被调用次数：命中 path 缓存时 _draw_chart_avg
        会完全跳过坐标计算，故该计数可精确反映 path 是否被重建。
        """
        calls = []
        keys = []
        paths = []
        orig = self.win._chart_pts_cache

        def counting(rect_, mm):
            calls.append(1)
            return orig(rect_, mm)

        self.win._chart_pts_cache = counting
        try:
            for _ in range(times):
                cache = self._draw_avg(rect)
                keys.append(cache[0])
                paths.append(cache[1])
        finally:
            del self.win._chart_pts_cache   # 还原为类方法
        return len(calls), keys, paths


# ============================== 缓存命中 / 失效 ==============================

class TestAvgPathCacheHit(TestAvgCacheBase):

    def test_second_frame_hits_cache(self):
        """同数据 + 同 rect：第一帧重建，第二帧命中（不重建 path、key 不变）。"""
        rect = self._rect(100)
        builds, keys, paths = self._count_rebuilds(rect, times=2)
        self.assertEqual(builds, 1, "连续两帧只应重建一次 path，实得 %d 次" % builds)
        self.assertEqual(keys[0], keys[1], "命中帧的缓存 key 不应变化")
        self.assertIs(paths[0], paths[1], "命中帧应复用同一个 QPainterPath 对象")

    def test_cache_tuple_shape(self):
        """缓存元组存在且结构为 (key, path, 末点下标, 末点均价)。"""
        rect = self._rect(100)
        cache = self._draw_avg(rect)
        self.assertIsInstance(cache, tuple)
        self.assertEqual(len(cache), 4, "缓存元组应为 (key, path, last_i, last_avg)")
        self.assertIsInstance(cache[0], tuple)
        # 末点均价 = 最后一个分时点的均价
        self.assertAlmostEqual(cache[3], self.win.minute_points[-1][2], places=6)
        self.assertEqual(cache[2], len(self.win.minute_points) - 1)

    def test_key_contains_geometry_and_version(self):
        """键必须含 _line_ver 与 rect 的 x/y/w/h（防数据与几何变化时不失效）。"""
        rect = self._rect(100)
        key = self._draw_avg(rect)[0]
        self.assertEqual(key[0], self.win._line_ver)
        self.assertEqual(key[-4:], (rect.x(), rect.y(), rect.width(), rect.height()))


class TestAvgPathCacheInvalidate(TestAvgCacheBase):

    def test_data_refresh_invalidates(self):
        """真实刷新路径（换 minute_points + _line_ver += 1）→ 缓存失效重建。"""
        rect = self._rect(100)
        self._draw_avg(rect)
        k_before = self.win._avg_path_cache[0]
        path_before = self.win._avg_path_cache[1]

        self.win.minute_points = _mk_pts(pre_close=420.00, amp=0.03)
        self.win.quote = _quote(420.00)
        self.win._line_ver += 1

        builds, keys, paths = self._count_rebuilds(rect, times=1)
        self.assertEqual(builds, 1, "数据刷新后必须重算 path")
        self.assertNotEqual(k_before, self.win._avg_path_cache[0],
                            "数据刷新后缓存 key 必须变化")
        self.assertIsNot(path_before, self.win._avg_path_cache[1],
                         "数据刷新后必须换成新的 QPainterPath")

    def test_rect_height_change_invalidates(self):
        """只改 rect 高度 → 失效（path 存绝对 y = top + na*h）。"""
        r1 = self._rect(100)
        self._draw_avg(r1)
        k1 = self.win._avg_path_cache[0]
        path1 = self.win._avg_path_cache[1]

        r2 = self._rect(130)      # 仅高度变化
        builds, keys, paths = self._count_rebuilds(r2, times=1)
        self.assertEqual(builds, 1, "rect 高度变化后必须重算 path")
        self.assertNotEqual(k1, self.win._avg_path_cache[0])
        self.assertIsNot(path1, self.win._avg_path_cache[1])

    def test_rect_xy_change_invalidates(self):
        """只平移 rect（x/y 变、尺寸不变）→ 同样失效（绝对坐标）。"""
        r1 = QRectF(PAD_X, CHART_TOP, 250.0, 100.0)
        self._draw_avg(r1)
        k1 = self.win._avg_path_cache[0]

        r2 = QRectF(PAD_X + 7, CHART_TOP + 5, 250.0, 100.0)
        self._draw_avg(r2)
        self.assertNotEqual(k1, self.win._avg_path_cache[0])


# ============================== 脏数据健壮性 ==============================

class TestAvgPathCacheDirty(TestAvgCacheBase):

    def test_all_avg_none_no_crash(self):
        """均价全为 None：不崩、不抛异常，路径为空（画不出均线但整图不受影响）。"""
        self.win.minute_points = [("%02d%02d" % (9 + i // 60, 30 + i), 10.0 + i * 0.01, None, 100)
                                  for i in range(30)]
        self.win._line_ver += 1
        rect = self._rect(100)
        cache = self._draw_avg(rect)
        self.assertIsInstance(cache, tuple)
        self.assertTrue(cache[1].isEmpty(), "全 None 均价应得到空 path")
        self.assertEqual(cache[2], -1)
        self.assertIsNone(cache[3])
        # 再画一帧仍不崩
        self._draw_avg(rect)

    def test_first_last_none_middle_differs(self):
        """首尾均为 None、仅中间均价不同的两份数据，缓存键必须不同（防退化误命中）。"""
        mk = lambda mid: [("0930", 10.0, None, 100), ("0931", 10.1, mid, 100),
                          ("0932", 10.2, None, 100)]
        rect = self._rect(100)

        self.win.minute_points = mk(10.05)
        self.win._line_ver += 1
        k_a = self._draw_avg(rect)[0]

        self.win.minute_points = mk(99.99)
        self.win._line_ver += 1
        k_b = self._draw_avg(rect)[0]

        self.assertNotEqual(k_a, k_b,
                            "首尾同为 None 的退化数据必须靠中间均价区分，否则会画错图")

    def test_empty_minute_points_no_crash(self):
        """空分时数据：_draw_chart_avg 不应抛异常（加载中分支已提前返回，此处防直调）。"""
        self.win.minute_points = []
        self.win._line_ver += 1
        rect = self._rect(100)
        img = QImage(300, 200, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        try:
            mm = self.win._chart_metrics([], rect)
            self.win._draw_chart_avg(p, rect, mm)
        finally:
            p.end()


# ============================== 离屏冒烟 ==============================

class TestAvgCacheSmoke(TestAvgCacheBase):

    def test_offscreen_frame_no_crash(self):
        """离屏抓一帧整图（含均价线）不崩溃，且分时区确有内容。"""
        w, h = 250, 206
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            rect = QRectF(PAD_X, CHART_TOP, w - 2 * PAD_X,
                          h - CHART_TOP - BOTTOM_SECTION_HIDDEN)
            self.win._draw_chart(p, rect)
        finally:
            p.end()
        # 命中缓存路径下也应画出内容（均价线/折线/填充）：统计非透明像素
        data = bytes(img.constBits().asstring(img.sizeInBytes()))
        non_empty = sum(1 for i in range(3, len(data), 4) if data[i] != 0)
        self.assertGreater(non_empty, 0, "离屏帧为空白")

    def test_repeated_frames_pixel_identical(self):
        """同数据同 rect 连续两帧（第二帧命中缓存）渲染结果逐像素一致。"""
        w, h = 250, 206
        rect = QRectF(PAD_X, CHART_TOP, w - 2 * PAD_X,
                      h - CHART_TOP - BOTTOM_SECTION_HIDDEN)

        def frame():
            img = QImage(w, h, QImage.Format.Format_ARGB32)
            img.fill(0)
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            try:
                self.win._draw_chart(p, rect)
            finally:
                p.end()
            return bytes(img.constBits().asstring(img.sizeInBytes()))

        a = frame()
        b = frame()          # 第二帧：均价线/折线均命中缓存
        self.assertEqual(a, b, "命中缓存的第二帧与首帧渲染结果不一致")


if __name__ == "__main__":
    unittest.main()
