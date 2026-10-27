# -*- coding: utf-8 -*-
"""拉伸回归自动化测试（v1.2.20 / v1.2.21）

把「悬浮窗拉伸」纳入自动化测试流程，覆盖历史回归点：
  1) v1.2.20 分时坐标缓存：拉伸高度逐帧变化时，命中缓存渲染 == 强制重算渲染
     （逐像素一致，防缓存错位/花屏回归）；
  2) v1.2.20 数据刷新缓存失效：_line_ver 变更后渲染随新数据更新，不残留旧缓存；
  3) 全高度（MIN_H~MAX_H）离屏渲染不崩溃、分时区确有内容、图底不压底部数据行；
  4) v1.2.20 玻璃层圆角 mask 节流：60ms 内不重复重建，force_mask 强制重建一次；
  5) v1.2.21 盘后分时点过滤：收盘后 15:00~15:30 腾讯盘后点不再保留为折线
     （修复右端 20+ 点重叠竖线导致的"乱七八糟"）。
"""
import json
import math
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

import sf_quote
import sf_ui_render
import stock_float_window as m
from sf_constants import GLASS_MASK_THROTTLE, MAX_H, MIN_H
from sf_ui_render import BOTTOM_BASE_HIDDEN, BOTTOM_SECTION_HIDDEN, CHART_TOP, PAD_X


# ============================== 测试数据构造 ==============================

def _quote(pre_close=403.50):
    return {
        "name": "贵州茅台", "code": "600519", "price": str(pre_close),
        "pct": "0.00", "change": "0.00", "pre_close": str(pre_close),
        "high": str(pre_close), "low": str(pre_close),
        "volume": "12345", "limit_up": "443.85", "limit_down": "363.15",
    }


def _mk_pts(pre_close=403.50, n=240, amp=0.02):
    """构造分时点 (hhmm, price, avg, vol)：9:30~11:30 + 13:00~15:00 共 240 分钟。"""
    times = [9 * 60 + 30 + i for i in range(120)] + [13 * 60 + i for i in range(120)]
    pts = []
    for i, tm in enumerate(times[:n]):
        hh, mm = tm // 60, tm % 60
        price = pre_close * (1 + amp * math.sin(i / 20.0) + amp * 0.3 * math.sin(i / 7.0))
        avg = pre_close * (1 + 0.6 * amp * math.sin(i / 25.0))
        pts.append(("%02d%02d" % (hh, mm), price, avg, 1000 + i * 10))
    return pts


def _img_bytes(img):
    """QImage 原始字节（ARGB32，含 alpha），按实际大小截取。"""
    return bytes(img.constBits().asstring(img.sizeInBytes()))


def _pixel_diff(a, b):
    """两图不同字节数（含 alpha）。同尺寸下逐像素等价，C 级 bytes 比较较快。"""
    if a.size() != b.size():
        return -1
    ba = _img_bytes(a)
    bb = _img_bytes(b)
    return sum(1 for x, y in zip(ba, bb) if x != y)


def _content_px(img):
    """非全透明像素数：分时区确有绘制内容（折线/均价线/基准线）。"""
    count = 0
    data = _img_bytes(img)
    for i in range(0, len(data), 4):
        if data[i + 3] != 0:          # alpha 通道非 0
            count += 1
    return count


# ============================== 公共夹具 ==============================

class TestStretchBase(unittest.TestCase):
    """与 test_render.py 相同的 patch 模式构造 FloatWindow，停后台线程。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._td = tempfile.mkdtemp(prefix="stockfloat_stretch_")
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
        # v1.2.57：呼吸灯效果使 _draw_chart_dot 依赖 time.monotonic()，
        # 缓存对比测试需冻结时间以确保两次渲染结果逐像素一致
        self._time_patch = patch.object(sf_ui_render.time, "monotonic", return_value=1000.0)
        self._time_patch.start()
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()
        self.win.timer.stop()
        self.win.top_timer.stop()
        self.win._anim_timer.stop()
        self.win._flash_timer.stop()
        self.win.show()
        QApplication.processEvents()

    def tearDown(self):
        self.win.close()
        QApplication.processEvents()
        self._time_patch.stop()
        for p in self._patches:
            p.stop()

    # ---- 渲染辅助 ----
    def _render_chart(self, w=250, h=206, pts=None, quote=None):
        """离屏渲染分时图（任意窗口宽高，不 resize 真实窗口）。"""
        self.win.minute_points = pts if pts is not None else _mk_pts()
        self.win.quote = quote if quote is not None else _quote()
        bottom = BOTTOM_SECTION_HIDDEN
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(PAD_X, CHART_TOP, w - 2 * PAD_X, h - CHART_TOP - bottom)
        self.win._draw_chart(p, rect)
        p.end()
        return img

    def _clear_chart_cache(self):
        self.win._chart_cache_key = None


# ============================== 分时坐标缓存（v1.2.20） ==============================

class TestChartStretchCache(TestStretchBase):
    """拉伸高度变化时缓存命中渲染 == 强制重算渲染；数据刷新正确失效。"""

    def test_cache_hit_equals_recompute(self):
        """同一数据/同一尺寸：命中缓存渲染 == 清缓存重算渲染（逐像素一致）。"""
        img_hit = self._render_chart(w=250, h=206)
        self._clear_chart_cache()
        img_re = self._render_chart(w=250, h=206)
        self.assertEqual(_pixel_diff(img_hit, img_re), 0,
                         "拉伸高度下缓存命中渲染与重算渲染不一致")

    def test_cache_consistent_across_heights(self):
        """多个拉伸高度：每档高度命中缓存 == 重算（防缓存错位/花屏）。"""
        for h in range(MIN_H, MAX_H + 1, 20):
            img_hit = self._render_chart(w=250, h=h)
            self._clear_chart_cache()
            img_re = self._render_chart(w=250, h=h)
            self.assertEqual(_pixel_diff(img_hit, img_re), 0,
                             "h=%d 高度下缓存命中渲染与重算渲染不一致" % h)

    def test_data_refresh_invalidates_cache(self):
        """数据刷新（_line_ver+1、内容变化）后渲染随新数据更新，不残留旧缓存。"""
        q_a = _quote(403.50)
        q_b = _quote(420.00)
        pts_a = _mk_pts(pre_close=403.50)
        pts_b = _mk_pts(pre_close=420.00, amp=0.03)
        img_a = self._render_chart(pts=pts_a, quote=q_a)
        # 模拟真实刷新路径：换数据并递增版本号
        self.win._line_ver += 1
        img_b = self._render_chart(pts=pts_b, quote=q_b)
        # 参考：干净状态下仅渲染数据B
        self._clear_chart_cache()
        img_b_ref = self._render_chart(pts=pts_b, quote=q_b)
        self.assertEqual(_pixel_diff(img_b, img_b_ref), 0,
                         "数据刷新后渲染与干净重算渲染不一致（缓存未正确失效）")
        # 数据B 确实画出了与数据A 不同的新内容
        self.assertNotEqual(_pixel_diff(img_a, img_b), 0,
                            "新旧数据渲染结果应不同")

    def test_multi_height_render_ok(self):
        """全高度采样离屏渲染不崩溃，且分时区确有内容（折线/均价线绘制了）。"""
        for h in range(MIN_H, MAX_H + 1, 17):
            img = self._render_chart(w=250, h=h)
            self.assertGreater(_content_px(img), 0,
                               "h=%d 分时区渲染为空白" % h)

    def test_layout_no_overlap(self):
        """全高度几何断言：分时图底部不压住底部数据行（高/低行）。"""
        for h in range(MIN_H, MAX_H + 1):
            bottom_section = BOTTOM_SECTION_HIDDEN
            chart_h = max(0, h - CHART_TOP - bottom_section)
            chart_bottom = CHART_TOP + chart_h
            bottom_base = BOTTOM_BASE_HIDDEN
            self.assertLessEqual(chart_bottom, h - bottom_base,
                                 "h=%d 分时图底部压到数据行" % h)


# ============================== 玻璃层圆角 mask 节流（v1.2.20） ==============================

class _NSWinStub:
    def frame(self):
        return QRectF(0, 0, 100, 200)


class _NSViewStub:
    def window(self):
        return _NSWinStub()


class _GlassWindowStub:
    def __init__(self):
        self.calls = 0

    def setFrame_display_(self, frame, display):
        self.calls += 1


class TestGlassMaskThrottle(TestStretchBase):
    """玻璃层圆角 mask 重建节流：60ms 内不重复重建，force_mask 强制重建。"""

    def _setup_glass(self):
        gw = _GlassWindowStub()
        self.win._glass_window = gw
        self.win._glass_view = object()
        return gw

    def test_throttle_skips_rebuild_within_window(self):
        """节流窗口内连续 resize：frame 实时同步，但圆角 mask 不重复重建。"""
        gw = self._setup_glass()
        self.win._last_glass_mask_ts = time.monotonic()   # 模拟刚重建过
        # v1.4.33：_sync_glass_frame 取内容窗改走 sf_glass.content_ns_window()，
        # 它第一行就问平台（offscreen 下 winId() 不是 NSView*，真转换是不可捕获的
        # 段错误）。本类要驱动的是"拿到窗口之后"的节流逻辑，故显式声明在 cocoa 上。
        with patch.object(m.FloatWindow, "_set_glass_mask") as mk, \
                patch("objc.objc_object", return_value=_NSViewStub()), \
                patch("sf_glass.native_cocoa_active", lambda: True):
            self.win._sync_glass_frame()
            self.win._sync_glass_frame()
            self.assertEqual(mk.call_count, 0, "节流窗口内不应重复重建圆角 mask")
            self.assertEqual(gw.calls, 2, "frame 仍需实时同步")

    def test_force_mask_rebuilds(self):
        """拉伸结束（force_mask=True）强制立即重建一次圆角 mask。"""
        gw = self._setup_glass()
        self.win._last_glass_mask_ts = time.monotonic()   # 模拟刚重建过
        # v1.4.33：_sync_glass_frame 取内容窗改走 sf_glass.content_ns_window()，
        # 它第一行就问平台（offscreen 下 winId() 不是 NSView*，真转换是不可捕获的
        # 段错误）。本类要驱动的是"拿到窗口之后"的节流逻辑，故显式声明在 cocoa 上。
        with patch.object(m.FloatWindow, "_set_glass_mask") as mk, \
                patch("objc.objc_object", return_value=_NSViewStub()), \
                patch("sf_glass.native_cocoa_active", lambda: True):
            self.win._sync_glass_frame(force_mask=True)
            self.assertEqual(mk.call_count, 1, "force_mask 应强制重建一次圆角 mask")

    def test_rebuild_after_throttle_window(self):
        """超过节流窗口后再 resize：恢复重建圆角 mask。"""
        gw = self._setup_glass()
        self.win._last_glass_mask_ts = time.monotonic() - (GLASS_MASK_THROTTLE + 0.1)
        # v1.4.33：_sync_glass_frame 取内容窗改走 sf_glass.content_ns_window()，
        # 它第一行就问平台（offscreen 下 winId() 不是 NSView*，真转换是不可捕获的
        # 段错误）。本类要驱动的是"拿到窗口之后"的节流逻辑，故显式声明在 cocoa 上。
        with patch.object(m.FloatWindow, "_set_glass_mask") as mk, \
                patch("objc.objc_object", return_value=_NSViewStub()), \
                patch("sf_glass.native_cocoa_active", lambda: True):
            self.win._sync_glass_frame()
            self.assertEqual(mk.call_count, 1, "超过节流窗口应重建圆角 mask")


# ============================== 盘后分时点过滤（v1.2.21） ==============================

def _minute_rows():
    """构造腾讯分钟接口数据：正常交易时段点 + 15:00 后盘后点。"""
    rows = []
    for hm in ["0930", "1000", "1130", "1300", "1400", "1459"]:
        rows.append("%s 1311.89 100 131189.0 1315.00 1305.00" % hm)
    for hm in ["1501", "1506", "1510", "1520", "1530"]:
        rows.append("%s 1304.00 120 156480.0 1306.00 1300.00" % hm)
    return rows


class TestMinuteAfterCloseFilter(unittest.TestCase):
    """v1.2.21 回归：收盘后接口返回的 15:00~15:30 盘后点不得保留为折线点。"""

    def _fetch(self, date_str="20260825"):
        payload = {
            "data": {
                "sh600519": {
                    "data": {"date": date_str, "data": _minute_rows()}
                }
            }
        }
        with patch.object(sf_quote, "http_get", return_value=json.dumps(payload)):
            return sf_quote._fetch_minute_primary("sh600519")

    def test_after_close_discards_post_market_points(self):
        """当日数据（收盘后场景）：1501/1506/1510/1520/1530 盘后点全部丢弃。"""
        pts = self._fetch(date_str="20260825")
        for t, _price, _avg in pts:
            hhmm = t.replace(":", "")
            tm = int(hhmm[:2]) * 60 + int(hhmm[2:4])
            self.assertLessEqual(tm, 900,
                                 "收盘后 15:00 之后盘后点不应保留: %s" % t)

    def test_keeps_regular_points(self):
        """非当日最近交易日数据（非交易时段场景）：正常 9:30~15:00 点保留、盘后点丢弃。"""
        pts = self._fetch(date_str="20260824")
        ts = [t.replace(":", "") for t, _p, _a in pts]
        if pts:                       # 若恰好盘中运行返回空，跳过（见 docstring）
            self.assertIn("0930", ts, "开盘 9:30 点应保留")
            self.assertIn("1459", ts, "尾盘 14:59 点应保留（非盘后点）")
        for t in ts:
            tm = int(t[:2]) * 60 + int(t[2:4])
            self.assertLessEqual(tm, 900, "15:00 后盘后点不应保留: %s" % t)


if __name__ == "__main__":
    unittest.main()
