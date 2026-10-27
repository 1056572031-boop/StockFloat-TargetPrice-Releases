# -*- coding: utf-8 -*-
"""冷启动右上角四按钮「先放大再缩小」回归测试。

现象（用户 2026-09-22 报告）：每次启动程序，主界面右上方的四个按钮会先突然
放大、随后缩回正常大小。

根因：顶部按钮图标有**两套画法** —— 自绘兜底 与 内嵌 SVG。SVG 位图原先由后台
线程（sf_icons._icon_worker）投递，窗口先露出、图标后到位，于是首帧只能画自绘
兜底造型，图标就绪后同一位置换成另一套更小的造型。实测墨迹尺寸：
    edit  铅笔 13×13px → 齿轮 8×8px
    close 粗叉 13×13px → 细叉 8×8px
    min   横线 11×2px  →  横线 8×2px
    bell  两种画法都是 8×8px（同一行，整排一起跳）
后台投递延迟约 150~300ms（要先 import AppKit ≈122ms，再画 512px 应用图标），
足够被肉眼看到。

修复：三个按钮 SVG 改为**首帧之前**在 GUI 线程同步渲染（sf_icons.button_pixmaps，
实测约 5ms），后台只保留真正昂贵的应用图标。

用例口径（遵循 AGENTS.md 第 6 节）：只断言抓帧像素这类公开输出 ——
不断言版本号、不读源码做字符串断言、不用 sleep 猜时长、不依赖现在几点。

"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QColor, QImage, QPainter

import sf_constants
import sf_icons
import sf_quote
import sf_ui_render as R
import stock_float_window as m

_ICONS = ("edit", "close", "min")        # 走 SVG 的三个按钮（铃铛一直是自绘）
_ALPHA_CUT = 20                          # 判定"这里画上了东西"的 alpha 阈值


def _svg_image(kind):
    """按**生产同款参数**现取某个按钮的 SVG 位图（修复前后都存在的公开函数）。

    v1.4.59（走查 S8）：出图边长不再是裸 20 —— 绘制框是 12pt，Retina 上要 24 个设备像素，
    所以生产参数是「按常量算出的设备像素 + setDevicePixelRatio」。这里跟着改成同源，
    否则本用例比的是两种口径，测出来的是夹具差异而不是"首帧即终态"。
    """
    px = int(round(sf_constants.BTN_ICON_PT * sf_constants.BTN_ICON_DEVICE_RATIO))
    img = sf_icons.svg_pixmap(sf_icons._ICON_SVG[kind], px)
    if img is not None:
        img.setDevicePixelRatio(sf_constants.BTN_ICON_DEVICE_RATIO)
    return img


def _blank(w, h):
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    return img


def _ink_box(img, x0, y0, x1, y1):
    """返回 [x0,y0,x1,y1] 范围内的墨迹外接框 (宽, 高)；无像素返回 (0, 0)。"""
    lo_x = lo_y = hi_x = hi_y = None
    for y in range(max(0, y0), min(img.height(), y1)):
        for x in range(max(0, x0), min(img.width(), x1)):
            if img.pixelColor(x, y).alpha() > _ALPHA_CUT:
                lo_x = x if lo_x is None else min(lo_x, x)
                hi_x = x if hi_x is None else max(hi_x, x)
                lo_y = y if lo_y is None else min(lo_y, y)
                hi_y = y if hi_y is None else max(hi_y, y)
    if hi_x is None:
        return (0, 0)
    return (hi_x - lo_x + 1, hi_y - lo_y + 1)


class _TopButtonBase(unittest.TestCase):
    """构造"刚建好、后台图标线程从未启动"的主窗 —— 冷启动首帧的现场。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])
        R.load_bundled_fonts()

    def setUp(self):
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", lambda *a, **k: ""),
            # 后台图标线程整体关掉：首帧必须自身就是终态，不靠后台补投
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()
        for name in ("timer", "top_timer", "_anim_timer", "_flash_timer"):
            getattr(self.win, name).stop()
        for _t in (getattr(self.win, "_quote_thread", None),
                   getattr(self.win, "_minute_thread", None)):
            if _t is not None:
                _t.join(timeout=1.5)
        self._drain_icon_queue()

    def tearDown(self):
        self.win.close()
        for p in reversed(self._patches):
            p.stop()

    # ---- 公开工具：抓帧 / 量墨迹 / 模拟"图标就绪" ----
    def _drain_icon_queue(self):
        try:
            while True:
                sf_icons._ICON_QUEUE.get_nowait()
        except Exception:
            pass

    def _paint_buttons(self):
        """只画顶部按钮区到透明画布。直调 _paint_buttons 而不走 paintEvent：
        后者有 try/except 兜底，会吞掉异常只剩一行日志（AGENTS.md 3.1 已记）。"""
        w, h = self.win.width(), self.win.height()
        img = _blank(w, h)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.win._paint_buttons(p, w)
        p.end()
        return img

    def _band_pixels(self, img):
        """按钮所在那一行的完整像素指纹（含 alpha），用于逐像素比较。"""
        out = []
        y0 = max(0, R.BTN_ICON_Y - 4)
        y1 = min(img.height(), R.BTN_ICON_Y + R.BTN_ICON_SZ + 4)
        for y in range(y0, y1):
            for x in range(img.width() - 110, img.width()):   # 四个按钮都在右区
                c = img.pixelColor(x, y)
                out.append((x, y, c.red(), c.green(), c.blue(), c.alpha()))
        return out

    def _icon_ink(self, img, kind):
        """按 BTN_ICON_* 常量定位某个按钮的墨迹外接框尺寸。"""
        x0 = img.width() - R.BTN_ICON_X[kind]
        return _ink_box(img, x0 - 4, R.BTN_ICON_Y - 4,
                        x0 + R.BTN_ICON_SZ + 4, R.BTN_ICON_Y + R.BTN_ICON_SZ + 4)

    def mark_icons_ready(self):
        """走生产入口 _apply_icons 把 SVG 图标"投到位"，模拟后台就绪的那一刻。

        刻意用 sf_icons.svg_pixmap 现取（修复前后都在的公开函数），不依赖
        button_images —— 这样同一份用例在修复前也会真的判红，而不是 import 报错。
        """
        for kind in _ICONS:
            img = _svg_image(kind)
            if img is not None and not img.isNull():
                sf_icons._ICON_QUEUE.put((kind, img))
        self.win._apply_icons()
        QApplication.processEvents()

    def svg_available(self):
        return all(_svg_image(k) is not None and not _svg_image(k).isNull()
                   for k in _ICONS)

    def paint_with_fallback_icons(self):
        """临时退回自绘兜底画法（绘制侧唯一读取的就是这三个属性，没有公开开关），
        抓一帧"修复前首帧长什么样"，随后还原。"""
        saved = (self.win._edit_pm, self.win._close_pm, self.win._min_pm)
        self.win._edit_pm = self.win._close_pm = self.win._min_pm = None
        try:
            return self._paint_buttons()
        finally:
            (self.win._edit_pm, self.win._close_pm,
             self.win._min_pm) = saved

    def band_diff(self, img_a, img_b):
        """顶部按钮行的差异摘要：不同像素数 + 每个按钮的墨迹尺寸变化。
        失败信息必须一眼看出"哪个按钮从多大变成多大"，而不是甩几万条像素。"""
        pa, pb = self._band_pixels(img_a), self._band_pixels(img_b)
        diffs = [(a, b) for a, b in zip(pa, pb) if a != b]
        sizes = "；".join(
            "%s %dx%d→%dx%d" % (k,
                                 self._icon_ink(img_a, k)[0], self._icon_ink(img_a, k)[1],
                                 self._icon_ink(img_b, k)[0], self._icon_ink(img_b, k)[1])
            for k in _ICONS)
        where = ("首个差异 x=%d y=%d" % (diffs[0][0][0], diffs[0][0][1])) if diffs else ""
        return len(diffs), sizes, where


class TestFirstFrameIsFinal(_TopButtonBase):
    """修复守的不变量：首帧就是终态，右上角不再有第二次绘制变化。"""

    def test_frame_after_icons_ready_is_identical_to_first_frame(self):
        first = self._paint_buttons()
        self.mark_icons_ready()
        after = self._paint_buttons()
        n, sizes, where = self.band_diff(first, after)
        self.assertEqual(n, 0,
                         "图标就绪后右上角有 %d 个像素不同（%s），墨迹尺寸 %s —— "
                         "就是用户看到的「启动时按钮先放大再缩小」" % (n, where, sizes))

    def test_first_frame_is_not_the_oversized_self_drawn_form(self):
        """首帧必须已经是 SVG 造型，而不是放大的自绘兜底（否则上一条相等断言
        会在"两边都是兜底"时假绿）。两帧都出自同一条绘制路径，可直接逐像素比。"""
        if not self.svg_available():
            self.skipTest("QtSvg 不可用，本机两种画法本来就是同一版兜底")
        first = self._paint_buttons()
        fallback = self.paint_with_fallback_icons()
        n, sizes, where = self.band_diff(fallback, first)
        self.assertGreater(n, 0, "首帧与自绘兜底逐像素相同 —— 按钮图标没有首帧前备好")
        for kind in _ICONS:
            self.assertLessEqual(
                self._icon_ink(first, kind)[0], self._icon_ink(fallback, kind)[0],
                "%s 首帧比兜底造型还宽，量级方向不对" % kind)
        self.assertGreater(self._icon_ink(fallback, "close")[0],
                           self._icon_ink(first, "close")[0],
                           "兜底 close 不再明显偏大，本用例失去鉴别力")

    def test_bell_in_the_same_row_never_moves(self):
        before = self._icon_ink(self._paint_buttons(), "bell")
        self.mark_icons_ready()
        self.assertEqual(before, self._icon_ink(self._paint_buttons(), "bell"),
                         "铃铛随图标就绪发生了变化：同一排按钮会一起跳")


class TestDegradedPathKeepsButtonsVisible(_TopButtonBase):
    """QtSvg 缺失时仍要画出按钮（自绘兜底），不能白屏。"""

    def test_self_drawn_fallback_still_paints_every_button(self):
        if not self.svg_available():
            self.skipTest("QtSvg 不可用，无参照物")
        img = self.paint_with_fallback_icons()
        self.assertTrue(all(self._icon_ink(img, k)[0] > 0 for k in _ICONS),
                        "自绘兜底没画出图标：降级路径右上角会空白")

    def test_button_pixmaps_degrades_to_none_without_crashing(self):
        if not hasattr(sf_icons, "button_pixmaps"):
            self.skipTest("本版本还没有 button_pixmaps（首帧前同步渲染入口）")
        with patch.object(sf_icons, "button_images",
                          return_value={k: None for k in sf_icons._BUTTON_ICON_KEYS}):
            self.assertEqual(sf_icons.button_pixmaps(),
                             {k: None for k in sf_icons._BUTTON_ICON_KEYS})


if __name__ == "__main__":
    unittest.main(verbosity=2)
