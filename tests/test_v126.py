# -*- coding: utf-8 -*-
"""实时股价 v1.2.26 回归测试（保留项：逐位滚动「跳出边界」）。

逐位滚动「跳出边界」：滚动位垂直滑动时旧/新字符越出字符条带，与上下行文字
错位重叠。修复：_draw_roll_text 将滚动绘制裁剪到「字符行条带」
[baseline-ascent, baseline+descent]，滑出部分被裁掉，绝不越界。
—— 测试：验证设置了 clip 矩形且等于字符行条带。

（v1.2.26 原「悬停版本号果冻抖动」回归测试已随悬停版本号功能移除而删除，
  版本号现为右键菜单灰色禁用项，从 Info.plist 读取。）

运行方式（在 StockFloat 目录）：
    python3 -m unittest tests.test_v126 -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QFont, QImage, QPainter
from PyQt6.QtWidgets import QApplication

import stock_float_window as m
import sf_quote
from sf_ui_render import NUM_FONT


def _fake_http_get(url, **kw):
    if "hq.sinajs.cn" in url:
        return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
    if "/appstock/app/minute/query?" in url:
        return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                '"data":["0930 1510.0 100 1000", "1000 1511.0 200 2000"]}}}}')
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


_QUOTE = {"name": "贵州茅台", "code": "600519", "price": "410.00", "pct": "1.61",
          "change": "6.50", "pre_close": "403.50", "high": "412.00", "low": "400.00",
          "volume": "12345", "limit_up": "443.85", "limit_down": "363.15"}


class _WinBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", _fake_http_get),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
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
        self.win.show()
        self.win._apply_quote(dict(_QUOTE))
        self.win._anim_timer.stop()

    def tearDown(self):
        self.win.close()
        QApplication.processEvents()
        for p in self._patches:
            p.stop()


class TestRollClip(_WinBase):
    """BUG1：逐位滚动裁剪到字符行条带，杜绝越界。"""

    def test_roll_draw_sets_clip_to_glyph_band(self):
        """_draw_roll_text 应设置 clip 矩形。v1.3.65 起条带底边从
        baseline+descent 收紧到 baseline（数字静止时的可见底边线）：
        实测滚动中的字符此前最多探到 baseline+descent 才被裁掉，
        造成滚动位相对静止位"沉底"（老板多轮截图同源）。
        条带 = [baseline-ascent, baseline]，高度 = fm.ascent()。"""
        clips = []
        real = QPainter.setClipRect

        def _spy(self_, *args, **kw):
            # 取 QRectF（可能以 rect= 关键字或位置参数传入）
            rect = kw.get("rect")
            if rect is None:
                rect = args[0]
            clips.append(rect)
            return real(self_, *args, **kw)

        f = self.win._font(NUM_FONT, 27, QFont.Weight.Bold)
        fm = self.win._fm(f)
        s = "410.00"
        img = QImage(400, 100, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        try:
            with patch.object(QPainter, "setClipRect", _spy):
                self.win._draw_roll_text(p, QRectF(0, 0, 300, 60), s, f,
                                         QColor("white"), roll_key="wtest")
        finally:
            p.end()
        self.assertEqual(len(clips), 1,
                         "应恰好设置一次 clip，实际 %d 次" % len(clips))
        r = clips[0]
        baseline = (60 - fm.height()) / 2.0 + fm.ascent()   # align_bottom=False 基线
        self.assertAlmostEqual(r.top(), baseline - fm.ascent(), delta=0.6,
                               msg="clip 顶部应等于 baseline-ascent")
        self.assertAlmostEqual(r.height(), fm.ascent(), delta=0.6,
                               msg="v1.3.65: clip 高度应为 fm.ascent()（底边钉在 baseline）")

    def test_roll_clip_restored_after_draw(self):
        """滚动绘制结束后应 restore，clip 不残留到后续绘制。"""
        f = self.win._font(NUM_FONT, 27, QFont.Weight.Bold)
        s = "410.00"
        img = QImage(400, 100, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        try:
            self.win._draw_roll_text(p, QRectF(0, 0, 300, 60), s, f,
                                     QColor("white"), roll_key="wtest2")
            # 绘制返回后 painter 的 clip 应已 restore 清除（默认无裁剪，bounding 为空）
            br = p.clipBoundingRect()
        finally:
            p.end()
        self.assertTrue(br.isEmpty(),
                        "绘制结束后 clip 应被 restore 清空，实际: %r" % (br,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
