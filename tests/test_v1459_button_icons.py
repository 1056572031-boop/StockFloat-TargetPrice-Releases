# -*- coding: utf-8 -*-
"""v1.4.59（走查 S8）：右上三按钮图标在 Retina 上必须逐像素 1:1。

现场与实测口径
--------------
图标源图过去固定 20px 且**没有** setDevicePixelRatio，而绘制框是 12pt
（sf_ui_render.BTN_ICON_SZ）。Retina 上 12pt = 24 个设备像素，于是 Qt 要把 20 的源图
放大到 24 —— 报告说的"三个按钮发虚"就是这个 1.2× 上采样。本机离屏逐像素实测同一笔
绘制调用（drawPixmap 进一张 DPR=2 的 24×24 帧）与"直接按 24 设备像素渲染 SVG"的差：

  20px 且无 DPR（改前）  → 最大通道差 255、平均差 34.9   ← 肉眼可见的糊
  24px + DPR=2（改后）   → 最大通道差 0、平均差 0        ← 逐像素相同

判据就用这个"相同"：改前那条路线在同一个文件里作为**反向对照**跑（它必须差），
所以这条用例是能失败的，不是把期望值调到了实测值上。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import Qt, QRect        # noqa: E402
from PyQt6.QtGui import QImage, QPainter, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import sf_constants as C                  # noqa: E402
import sf_icons as I                      # noqa: E402
import sf_ui_render as R                  # noqa: E402

try:
    from _base import SFCase                   # noqa: E402
except ImportError:                            # 以包形式被 discover 导入时
    from tests._base import SFCase             # noqa: E402



def _paint_like_the_window(pm):
    """把位图按**绘制点的那一句**画进一张 DPR=2 的 24×24 帧，返回该帧。

    绘制点用的是 `p.drawPixmap(rect.toRect(), pm)`（rect 是 12×12 的逻辑框），
    这里复刻同一个调用与同一套渲染提示，量的才是真机上会发生的事。
    """
    canvas = QImage(C.BTN_ICON_PT * 2, C.BTN_ICON_PT * 2, QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.transparent)
    canvas.setDevicePixelRatio(C.BTN_ICON_DEVICE_RATIO)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.drawPixmap(QRect(0, 0, R.BTN_ICON_SZ, R.BTN_ICON_SZ), pm)
    p.end()
    return canvas


def _max_channel_diff(a, b):
    """(最大单通道差, 平均单通道差)。两张帧尺寸必须一致。"""
    assert a.size() == b.size(), "对照帧尺寸不同（%s vs %s），比不出东西" % (a.size(), b.size())
    worst = 0
    total = 0
    n = 0
    for y in range(a.height()):
        for x in range(a.width()):
            pa, pb = a.pixelColor(x, y), b.pixelColor(x, y)
            d = max(abs(pa.red() - pb.red()), abs(pa.green() - pb.green()),
                    abs(pa.blue() - pb.blue()), abs(pa.alpha() - pb.alpha()))
            worst = max(worst, d)
            total += d
            n += 1
    return worst, total / max(n, 1)


class TestButtonIconsArePixelExactOnRetina(SFCase):
    KEYS = ("edit", "close", "min")

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _reference(self, kind):
        """基准：同一张 SVG 直接按**目标设备像素**渲染（不存在任何缩放）。"""
        px = int(round(C.BTN_ICON_PT * C.BTN_ICON_DEVICE_RATIO))
        return I.svg_pixmap(I._ICON_SVG[kind], px)   # discipline-ok(R5): 内嵌 SVG 只有这一份私有表，基准图必须拿同一份源渲染

    def test_shipped_images_are_tagged_with_the_device_ratio(self):
        """出图边长与 DPR 必须成对：只改尺寸不标 DPR，Qt 仍按逻辑 24pt 画（图标大一倍）。"""
        imgs = I.button_images()
        px = int(round(C.BTN_ICON_PT * C.BTN_ICON_DEVICE_RATIO))
        for kind in self.KEYS:
            img = imgs[kind]
            self.assertIsNotNone(img, "%s 图标没取到（QtSvg 缺失才有 None，本机不该发生）" % kind)
            self.assertEqual(img.width(), px, "%s 出图边长应是 %d 设备像素" % (kind, px))
            self.assertEqual(img.devicePixelRatio(), C.BTN_ICON_DEVICE_RATIO,
                             "%s 没标 DPR：逻辑尺寸变成 %spt，比绘制框大 %s 倍"
                             % (kind, img.width() / img.devicePixelRatio(),
                                (img.width() / img.devicePixelRatio()) / R.BTN_ICON_SZ))

    def test_qpixmaps_keep_the_tag(self):
        """QPixmap.fromImage 必须把 DPR 带过去 —— 绘制点拿到的是 QPixmap。"""
        for kind, pm in I.button_pixmaps().items():
            self.assertIsNotNone(pm, "%s 的 QPixmap 没建起来" % kind)
            self.assertEqual(pm.devicePixelRatio(), C.BTN_ICON_DEVICE_RATIO,
                             "%s 转成 QPixmap 后 DPR 丢了" % kind)
            self.assertAlmostEqual(pm.width() / pm.devicePixelRatio(), R.BTN_ICON_SZ,
                                   places=3, msg="%s 的逻辑边长与绘制框对不上" % kind)

    def test_window_paint_matches_direct_device_render(self):
        """正判据：按绘制点那句画出来的帧，与直接按设备像素渲染的 SVG 逐像素相同。"""
        imgs = I.button_images()
        for kind in self.KEYS:
            got = _paint_like_the_window(QPixmap.fromImage(imgs[kind]))
            worst, mean = _max_channel_diff(got, self._reference(kind))
            self.assertEqual(
                worst, 0,
                "%s 图标在 Retina 上有 %d 个像素对不上（平均差 %.1f）—— 又在上采样了"
                % (kind, worst, mean))

    def test_the_old_20px_untagged_shape_would_fail_this_check(self):
        """反向对照：改前那种"20px 且不标 DPR"的位图必须被同一条判据抓到。

        没有这条，上面那条正判据就分不清"我修好了"和"我拿一把永远量不出差的尺子"。
        """
        old_like = QPixmap.fromImage(I.svg_pixmap(I._ICON_SVG["close"], 20))  # discipline-ok(R5): 反向对照必须拿同一份内嵌 SVG 才造得出"改前那块图"
        old_like.setDevicePixelRatio(1.0)          # 改前根本没有 DPR 概念，就是 1
        got = _paint_like_the_window(old_like)
        worst, mean = _max_channel_diff(got, self._reference("close"))
        self.assertGreater(
            worst, 0,
            "上采样的旧形状竟然与直渲逐像素相同 → 这条判据看不见模糊，是道假门")
        self.assertGreater(mean, 5.0,
                           "旧形状的平均差只有 %.2f，判据的灵敏度不够" % mean)


if __name__ == "__main__":
    unittest.main(verbosity=2)
