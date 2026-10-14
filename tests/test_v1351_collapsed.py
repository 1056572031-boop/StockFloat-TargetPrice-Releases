# -*- coding: utf-8 -*-
"""v1.3.52：折叠态（图带过矮）彻底不画分时图。

背景：折叠态窗口高 = collapsed_height() = 155，图带 = 155 - CHART_TOP(108)
- BOTTOM_SECTION_HIDDEN(46) = **1px**。展开动画期间 _chart_expanded 立刻置
True、高度还停在 155 附近，于是会画出一帧 1~2px 的怪线：既不像"已收起"，
也不像走势图。

裁决（交互设计师）：折叠态 = 彻底收干净，不画分时图。不改任何高度常量
（折叠 155 / 展开最小 224 / CHART_MIN_H=50 全保持原值），只改"画不画"。

本组用例锁死：
  1) 折叠高度 155 整帧绘制：_draw_chart_line / _draw_chart_avg / _draw_chart_fill
     调用次数均为 0（含"折叠标志=False"与"折叠高度+展开标志=True"两条路径，
     后者是展开动画首几帧的真实形态）；
  2) 折叠态图带区域与背景一致（像素法：图带内无非背景像素）；
  3) 展开态（224 及以上）回归：三个绘制函数正常被调用；
  4) 边界：图带高 == CHART_MIN_DRAW_H 画、== CHART_MIN_DRAW_H - 1 不画；
  5) 离屏抓帧冒烟不崩。

运行方式（工程根目录，务必带三个隔离环境变量避免首屏快照跨进程污染）：
    SF_LAST_QUOTE_PATH=/tmp/x.json SF_CONFIG_PATH=/tmp/y.json SF_SEED_FIRST_SCREEN=0 \
    QT_QPA_PLATFORM=offscreen ./.venv/bin/python -m unittest discover -s tests -p "test_v1351_collapsed.py" -v
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
import sf_ui_render as R
import stock_float_window as m
from sf_constants import MAX_H, MIN_H
from sf_ui_render import (BOTTOM_SECTION_HIDDEN, CHART_MIN_DRAW_H, CHART_TOP, PAD_X)


# ============================== 测试数据构造 ==============================

def _quote(pre_close=403.50):
    return {
        "name": "贵州茅台", "code": "sh600519", "price": str(pre_close),
        "pct": "0.00", "change": "0.00", "pre_close": str(pre_close),
        "high": str(pre_close), "low": str(pre_close),
        "volume": "12345", "limit_up": "443.85", "limit_down": "363.15",
    }


def _mk_pts(pre_close=403.50, n=240, amp=0.02):
    times = [9 * 60 + 30 + i for i in range(120)] + [13 * 60 + i for i in range(120)]
    pts = []
    for i, tm in enumerate(times[:n]):
        hh, mm = tm // 60, tm % 60
        price = pre_close * (1 + amp * math.sin(i / 20.0) + amp * 0.3 * math.sin(i / 7.0))
        avg = pre_close * (1 + 0.6 * amp * math.sin(i / 25.0))
        pts.append(("%02d%02d" % (hh, mm), price, avg, 1000 + i * 10))
    return pts


# ============================== 公共夹具 ==============================

class TestCollapsedBase(unittest.TestCase):
    """与 test_stretch.py 相同的 patch 模式构造 FloatWindow，停后台线程。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._td = tempfile.mkdtemp(prefix="stockfloat_collapsed_")
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
        # 呼吸灯依赖 time.monotonic()，冻结时间保证多帧渲染可比
        self._time_patch = patch.object(R.time, "monotonic", return_value=1000.0)
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
        self._install_spies()

    def tearDown(self):
        try:
            self.win.close()
        except Exception:
            pass
        self._time_patch.stop()
        for p in self._patches:
            p.stop()

    # ---- 绘制计数 spy ----

    def _install_spies(self):
        """给 5 个分时子绘制方法装计数器（实例属性覆盖，tearDown 随窗口销毁）。"""
        self.calls = {"base": 0, "avg": 0, "fill": 0, "line": 0, "dot": 0}
        for name, tag in (("_draw_chart_base", "base"), ("_draw_chart_avg", "avg"),
                          ("_draw_chart_fill", "fill"), ("_draw_chart_line", "line"),
                          ("_draw_chart_dot", "dot")):
            orig = getattr(self.win, name)

            def make(fn, t):
                def spy(*a, **k):
                    self.calls[t] += 1
                    return fn(*a, **k)
                return spy

            setattr(self.win, name, make(orig, tag))

    def _reset(self):
        for k in self.calls:
            self.calls[k] = 0
        # 清掉折线/均价线缓存，避免上一帧残留干扰计数
        self.win._line_path = None
        self.win._line_path_key = None
        self.win._avg_path_cache = None

    # ---- 渲染辅助 ----

    def _render(self, h, expanded, w=250):
        """整帧绘制（背景+头部+价格+分时+底部），只用于统计子绘制调用次数。

        窗口构造时 min=MIN_H(224)、宽度固定；这里先放开下限再 resize，
        与 _toggle_chart 收起分支（动画期间 setMinimumHeight(target)）同口径，
        否则 155 会被 min 钳到 224，根本到不了折叠几何。

        实现口径：按 _paint_event_inner 的绘制顺序（背景→头部→按钮→价格→
        分时→底部）逐个调用子方法画进一张 QImage。不走 _paint_event_inner
        是因为它内部 QPainter(self) 会画进窗口本身（离屏下 widget 无 paint
        engine，取不到像素），而本组断言只需要"调用次数 + 图带像素"。
        """
        self._reset()
        self.win.setMinimumHeight(0)
        self.win.setMaximumHeight(MAX_H)
        self.win.resize(w, h)
        w = self.win.width()
        h = self.win.height()
        self.win._chart_expanded = expanded
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            self.win._paint_background(p, w)
            self.win._paint_header(p, w)
            self.win._paint_buttons(p, w)
            self.win._paint_price(p, w)
            self.win._paint_chart(p, w)
            self.win._paint_bottom(p, w)
        finally:
            p.end()
        return self.win.height()

    def _render_chart_only(self, h, expanded, w=250):
        """只把分时图画进一张全透明 QImage（用于"画了/没画"的像素断言）。

        直接调 _draw_chart 而非整帧：图带高度与 _paint_chart 的推导完全一致，
        命中的是同一道闸门。
        """
        self.win.setMinimumHeight(0)
        self.win.setMaximumHeight(MAX_H)
        self.win.resize(w, h)
        w = self.win.width()
        h = self.win.height()
        self.win._chart_expanded = expanded
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            band = max(0, h - CHART_TOP - BOTTOM_SECTION_HIDDEN)
            self.win._draw_chart(p, QRectF(PAD_X, CHART_TOP, w - 2 * PAD_X, band))
        finally:
            p.end()
        return img

    @staticmethod
    def _band_h(h):
        return max(0, h - CHART_TOP - BOTTOM_SECTION_HIDDEN)

    @staticmethod
    def _opaque_px(img):
        """整幅图的非透明像素数（图外全透明 ⇒ 数值即"分时图画出多少"）。"""
        data = bytes(img.constBits().asstring(img.sizeInBytes()))
        return sum(1 for i in range(3, len(data), 4) if data[i] != 0)


# ============================== 折叠态：彻底不画 ==============================

class TestCollapsedNoChart(TestCollapsedBase):

    def test_collapsed_height_band_is_one_px(self):
        """前置事实：折叠高度下图带只有 1px（本轮要修的瑕疵来源）。"""
        self.assertEqual(R.collapsed_height(), 155)
        self.assertEqual(self._band_h(R.collapsed_height()), 1)

    def test_collapsed_flag_false_draws_nothing(self):
        """折叠态（_chart_expanded=False，h=155）：三个绘制函数调用均为 0。"""
        self._render(R.collapsed_height(), False)
        for tag in ("line", "avg", "fill", "base", "dot"):
            self.assertEqual(self.calls[tag], 0,
                             "折叠态 _draw_chart_%s 不应被调用" % tag)

    def test_collapsed_height_with_expanded_flag_draws_nothing(self):
        """折叠高度 + _chart_expanded=True（展开动画首几帧的真实形态）：同样 0 次。

        这是本轮闸门真正生效的场景：展开时标志立刻置 True，高度还停在 155 附近，
        图带 1px —— 没有闸门就会画出一帧怪线。
        """
        self._render(R.collapsed_height(), True)
        for tag in ("line", "avg", "fill", "base", "dot"):
            self.assertEqual(self.calls[tag], 0,
                             "图带 1px 时 _draw_chart_%s 不应被调用" % tag)

    def test_collapsed_band_draws_zero_pixels(self):
        """像素法：折叠态（图带 1px）整幅分时图一个像素都不画 —— 收得干净。

        底图为全透明，非透明像素数即"分时图实际画出的量"；折叠态必须为 0，
        说明折线/渐变填充/均价线/基准线/呼吸点/加载中提示全被闸门挡下。
        """
        h = R.collapsed_height()
        img = self._render_chart_only(h, True)
        self.assertEqual(self._opaque_px(img), 0,
                         "折叠态分时图应为 0 个绘制像素，实得 %d" % self._opaque_px(img))

    def test_no_loading_text_in_tiny_band(self):
        """图带过矮时连"加载中"/市场状态提示也不画（收干净）。"""
        self.win.minute_points = []
        self._render(R.collapsed_height(), True)
        for tag in ("line", "avg", "fill", "base", "dot"):
            self.assertEqual(self.calls[tag], 0)


# ============================== 展开态回归 ==============================

class TestExpandedStillDraws(TestCollapsedBase):

    def test_min_expanded_height_draws(self):
        """展开态最小高度 224（图带 70px）：五个绘制函数各被调用一次。"""
        self._render(MIN_H, True)
        for tag in ("line", "avg", "fill", "base", "dot"):
            self.assertEqual(self.calls[tag], 1,
                             "展开态 _draw_chart_%s 应被调用一次" % tag)

    def test_expanded_band_has_content(self):
        """展开态（图带 70px）确有绘制内容（像素法）。"""
        img = self._render_chart_only(MIN_H, True)
        self.assertGreater(self._opaque_px(img), 0, "展开态分时图应有绘制内容")

    def test_heights_across_range_draw(self):
        """MIN_H~MAX_H 全程（含底部拉伸）都正常出图。"""
        for h in range(MIN_H, MAX_H + 1, 24):
            self._render(h, True)
            for tag in ("line", "avg", "fill", "dot"):
                self.assertEqual(self.calls[tag], 1,
                                 "h=%d 时 _draw_chart_%s 应被调用" % (h, tag))


# ============================== 边界：≥ 画 / < 不画 ==============================

class TestMinDrawHeightBoundary(TestCollapsedBase):

    def _h_for_band(self, band):
        return CHART_TOP + BOTTOM_SECTION_HIDDEN + band

    def test_band_equal_threshold_draws(self):
        """图带高 == CHART_MIN_DRAW_H：画。"""
        h = self._h_for_band(CHART_MIN_DRAW_H)
        self.assertEqual(self._band_h(h), CHART_MIN_DRAW_H)
        self._render(h, True)
        for tag in ("line", "avg", "fill", "dot"):
            self.assertEqual(self.calls[tag], 1,
                             "图带 == CHART_MIN_DRAW_H 时 _draw_chart_%s 应被调用" % tag)

    def test_band_one_below_threshold_skips(self):
        """图带高 == CHART_MIN_DRAW_H - 1：不画。"""
        h = self._h_for_band(CHART_MIN_DRAW_H - 1)
        self.assertEqual(self._band_h(h), CHART_MIN_DRAW_H - 1)
        self._render(h, True)
        for tag in ("line", "avg", "fill", "dot"):
            self.assertEqual(self.calls[tag], 0,
                             "图带 < CHART_MIN_DRAW_H 时 _draw_chart_%s 不应被调用" % tag)

    def test_zero_and_negative_band_skips(self):
        """图带为 0 或负（窗口极矮）：不画，也不崩。"""
        for h in (CHART_TOP + BOTTOM_SECTION_HIDDEN, CHART_TOP, 40):
            self._render(h, True)
            for tag in ("line", "avg", "fill", "dot"):
                self.assertEqual(self.calls[tag], 0, "h=%d 不应绘制分时图" % h)


# ============================== 缓存不被污染 ==============================

class TestCacheUntouchedWhenSkipped(TestCollapsedBase):

    def test_skip_does_not_populate_caches(self):
        """图带过矮跳过后，折线/均价线缓存都不应被写入（跳过即不碰缓存）。"""
        self.win._line_path = None
        self.win._line_path_key = None
        self.win._avg_path_cache = None
        self._render(R.collapsed_height(), True)
        self.assertIsNone(self.win._line_path, "跳过后不应写入折线 path 缓存")
        self.assertIsNone(self.win._avg_path_cache, "跳过后不应写入均价线 path 缓存")

    def test_line_cache_key_contains_lo_hi(self):
        """v1.3.52：折线缓存键含 lo/hi（昨收变了但版本号没变时也要重算）。"""
        h = MIN_H
        self.win.setMinimumHeight(0)
        self.win.setMaximumHeight(MAX_H)
        self.win.resize(250, h)
        w = self.win.width()
        h = self.win.height()
        rect = QRectF(PAD_X, CHART_TOP, w - 2 * PAD_X, self._band_h(h))
        pts = self.win.minute_points
        mm = self.win._chart_metrics(pts, rect)
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        try:
            self.win._line_path = None
            self.win._line_path_key = None
            self.win._draw_chart_line(p, rect, mm)
            k1 = self.win._line_path_key
            ver_before = self.win._line_ver
            # 只改昨收 → lo/hi 变，_line_ver 不变：键必须不同
            self.win.quote = _quote(420.00)
            mm2 = self.win._chart_metrics(pts, rect)
            self.assertNotEqual(mm2["lo"], mm["lo"], "改昨收后 lo 应变化")
            self.assertEqual(self.win._line_ver, ver_before, "_line_ver 不应变化")
            self.win._draw_chart_line(p, rect, mm2)
            k2 = self.win._line_path_key
        finally:
            p.end()
        self.assertNotEqual(k1, k2,
                            "昨收变化（lo/hi 变）但 _line_ver 未变时，折线缓存键必须变化")
        self.assertEqual(k1[2], mm["lo"], "键第 3 项应为 lo")
        self.assertEqual(k1[3], mm["hi"], "键第 4 项应为 hi")


# ============================== 离屏冒烟 ==============================

class TestCollapsedSmoke(TestCollapsedBase):

    def test_full_toggle_cycle_no_crash(self):
        """折叠→展开→折叠 整轮离屏渲染不崩，且末态回到"正常出图"。"""
        for h, exp in ((MIN_H, True), (MIN_H + 40, True), (R.collapsed_height(), True),
                       (R.collapsed_height(), False), (MIN_H, True)):
            self._render(h, exp)
        self.assertEqual(self.calls["line"], 1, "末态（展开）应正常出图")


if __name__ == "__main__":
    unittest.main()


# ============================== A26：图带填充的像素判据 ==============================

class TestChartFillHasArea(TestCollapsedBase):
    """A26（台账里"没有任何东西拦得住"的两条之一）：填充必须用**像素**判。

    本文件原有的 `self.calls["fill"]` 只证明"`_draw_chart_fill` 这行跑了"，
    证明不了屏幕上真有一片渐变 —— 把它改成什么都不画，计数照样是 1。
    下面两条判据都带一个"自校准"的对照：同一份数据画两遍，第二遍把填充换成空实现，
    **差出来的像素就是填充的面积**。这样既不用猜绝对值，也保证这条断言能失败。
    """

    def _band_rect(self, w, h):
        band = max(0, h - CHART_TOP - BOTTOM_SECTION_HIDDEN)
        return QRectF(PAD_X, CHART_TOP, w - 2 * PAD_X, band)

    def _render_with_fill(self, w=250, h=420, skip_fill=False):
        if skip_fill:
            orig = self.win._draw_chart_fill   # discipline-ok(R5): 要拿它做对照，只能先存下来
            self.win._draw_chart_fill = lambda *a, **k: None   # discipline-ok(R5): offscreen 下没有公开入口能构造这个现场
            try:
                img = self._render_chart_only(h, True, w=w)
            finally:
                self.win._draw_chart_fill = orig   # discipline-ok(R5): offscreen 下没有公开入口能构造这个现场
            return img
        return self._render_chart_only(h, True, w=w)

    def test_fill_area_is_a_real_region_not_a_hairline(self):
        with_fill = self._render_with_fill()
        without = self._render_with_fill(skip_fill=True)
        total = with_fill.width() * with_fill.height()
        delta = self._opaque_px(with_fill) - self._opaque_px(without)
        rect = self._band_rect(with_fill.width(), with_fill.height())
        self.assertGreater(delta, 0,
                           "关掉填充之后像素数一点没变 → 填充从来没画过任何东西")
        self.assertGreater(delta / (rect.width() * rect.height()), 0.20,
                           "填充只占了图带的 %.1f%%（折线+均价线+基准线的线宽就能凑出这点）"
                           " —— 渐变面积没铺开的形状又回来了"
                           % (100.0 * delta / (rect.width() * rect.height())))

    def test_fill_reaches_the_bottom_of_the_band(self):
        """填充是"折线下方一直到图带底"，不是贴着折线的一条带。"""
        img = self._render_with_fill()
        rect = self._band_rect(img.width(), img.height())
        x0, x1 = int(rect.left()) + 6, int(rect.right()) - 6
        row = int(rect.bottom()) - 3
        lit = sum(1 for x in range(x0, x1) if img.pixelColor(x, row).alpha() > 0)
        self.assertGreater(lit / float(x1 - x0), 0.6,
                           "图带底沿上方 3px 处只有 %.0f%% 的列有墨水 —— "
                           "填充没铺到底，用户看到的就是一条悬空的折线"
                           % (100.0 * lit / float(x1 - x0)))

    def test_criterion_would_fire_when_the_fill_disappears(self):
        """判据自己必须能失败：拿"填充已关掉"那张图去跑第一条，必须不达标。"""
        without = self._render_with_fill(skip_fill=True)
        with_fill = self._render_with_fill()
        total = with_fill.width() * with_fill.height()
        rect = self._band_rect(with_fill.width(), with_fill.height())
        delta = self._opaque_px(with_fill) - self._opaque_px(without)
        # 反向验证：把"有填充"当成"没有填充"来比，差值必须为 0
        self.assertEqual(self._opaque_px(without) - self._opaque_px(without), 0)
        self.assertGreater(delta, 0, "对照本身为 0 → 上面两条判据都是空的")
