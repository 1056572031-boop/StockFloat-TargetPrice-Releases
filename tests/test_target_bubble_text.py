# -*- coding: utf-8 -*-
"""TargetBubble（效果图 B 版）：尺寸规则、小三角与定位、配色、三列文字底线、分组细线、柔影。

只用公开接口（setRows / rows / body_rect_global / show_below / is_flipped /
caret_center_global_x）和类常量，不访问私有成员。不构造主窗、不启动 worker。
行数据怎么排见 test_bell_bubble_rows.py，和主窗的接线见 test_target_bubble_live.py。
"""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import sf_ui_render
import stock_float_window as m
from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QFont, QFontInfo, QImage

from stock_float_window import TargetBubble
from targets import bell_bubble_rows
from tests._base import SFCase

B = TargetBubble
_QAPP = None
CMP = [
    {"direction": "above", "target": 61.3, "state": "pending"},
    {"direction": "below", "target": 58.5, "state": "pending"},
    {"direction": "above", "target": 60.5, "state": "hit"},
]
SEP_BLOCK = B.SEP_GAP_TOP + 1 + B.SEP_GAP_BOTTOM


def _near(c, rgb, tol=40):
    return c.alpha() > 200 and all(abs(a - b) <= tol for a, b in
                                   zip((c.red(), c.green(), c.blue()), rgb))


def _rgb(qcolor):
    return (qcolor.red(), qcolor.green(), qcolor.blue())


class TargetBubbleCase(SFCase):
    def setUp(self):
        global _QAPP
        super().setUp()
        _QAPP = SFCase.ensure_qapp()
        sf_ui_render.load_bundled_fonts()
        # 度量类断言必须自己把内置字体注册上，并当场验证真的解析到了 D-DIN，
        # 否则是在靠「别的模块先注册过」这个副作用跑（AGENTS §5.2 像素判据一条）。
        self.assertEqual(QFontInfo(QFont(sf_ui_render.NUM_FONT)).family(), sf_ui_render.NUM_FONT)
        self.area = QRect(0, 0, 800, 600)
        self.track_patch(patch.object(B, "_available_rect",
                                      staticmethod(lambda anchor: self.area)))
        self.bubble = self.track_widget(B())

    # ---- 辅助 ----
    def fill(self, alerts=CMP, price=60.0, decimals=2):
        pending, hit = bell_bubble_rows(alerts, price, decimals)
        self.bubble.setRows(pending, hit)

    def body(self):
        return self.bubble.body_rect_global()

    def image(self):
        img = QImage(self.bubble.size(), QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        self.bubble.render(img)
        return img

    def local_caret_x(self):
        origin = self.bubble.mapToGlobal(QPoint(0, 0)).x()
        return int(round(self.bubble.caret_center_global_x())) - origin

    @staticmethod
    def band(img, x0, x1, y0, y1):
        return [img.pixelColor(x, y) for y in range(y0, y1) for x in range(x0, x1)]

    def row_top(self, index, *, after_sep=False):
        """第 index 行（组内从 0 起）的本地 y；after_sep=True 表示已触发组的行（前面有 2 行待触发 + 细线）。"""
        top = B.MARGIN_TOP + B.PAD_TOP + B.HEADER_H + B.HEADER_GAP
        if after_sep:
            top += 2 * B.ROW_H + SEP_BLOCK + B.HEADER_H + B.HEADER_GAP
        return top + index * B.ROW_H

    # ---- 数据与尺寸 ----
    def test_setRows只在内容真变了才报变化(self):
        pending, hit = bell_bubble_rows(CMP, 60.0, 2)
        self.assertTrue(self.bubble.setRows(pending, hit))
        self.assertFalse(self.bubble.setRows(pending, hit), "同样的内容不该触发重排")
        self.assertEqual(self.bubble.rows()["pending"], pending)
        self.assertEqual(self.bubble.rows()["hit"], hit)
        pending2, hit2 = bell_bubble_rows(CMP, 61.0, 2)
        self.assertTrue(self.bubble.setRows(pending2, hit2), "百分比变了就是变了")

    def test_高度由组标题_行数_细线三样相加(self):
        one = [CMP[0]]
        self.fill(one)
        h1 = self.body().height()
        self.assertEqual(h1, B.PAD_TOP + B.PAD_BOTTOM + B.HEADER_H + B.HEADER_GAP + B.ROW_H)
        self.fill(CMP[:2])
        self.assertEqual(self.body().height() - h1, B.ROW_H, "多一行就多一个行高")
        self.fill(CMP)
        self.assertEqual(self.body().height() - h1,
                         B.ROW_H + SEP_BLOCK + B.HEADER_H + B.HEADER_GAP + B.ROW_H,
                         "再加一个已触发组：多一个标题、一行、一道细线占位")

    def test_没有任何百分比时整列收起(self):
        # 价位取得足够长，让内容宽度超过最小宽度，宽度差才反映第三列
        long_alert = [{"direction": "above", "target": 123456.78, "state": "pending"}]
        self.fill(long_alert, price=None)
        narrow = self.body().width()
        self.fill(long_alert, price=100000.0)
        wide = self.body().width()
        self.assertGreater(wide - narrow, B.COL_GAP + B.PCT_GAP, "有百分比时要多出第三列的宽度")

    def test_短内容撑不到最小宽度时取最小宽度(self):
        self.fill([CMP[0]], price=None)
        self.assertEqual(self.body().width(), B.MIN_CONTENT_W + 2 * B.PAD_X)

    def test_清空后收回占位_再填同样内容画面完全一致(self):
        self.fill()
        filled_h, filled_img = self.body().height(), self.image()
        self.bubble.setRows([], [])
        self.assertLess(self.body().height(), filled_h, "清空必须收回旧行、标题与细线的占位")
        self.assertNotEqual(self.image(), filled_img)
        self.fill()
        self.assertEqual(self.body().height(), filled_h)
        self.assertEqual(self.image(), filled_img, "清空再填回，不能留下旧画面的残影")

    # ---- 小三角与定位 ----
    def test_小三角朝向铃铛_尖端处有填充_尖端之外是空的(self):
        self.fill()
        self.bubble.show_below(QPoint(400, 40), 20)
        img, cx = self.image(), self.local_caret_x()
        self.assertFalse(self.bubble.is_flipped())
        top = B.MARGIN_TOP
        inside = img.pixelColor(cx, top - 2).alpha()
        beyond = img.pixelColor(cx, top - 6).alpha()
        beside = img.pixelColor(cx + 6, top - 2).alpha()
        self.assertGreaterEqual(inside, 200, "三角内部应是本体的颜色")
        self.assertLess(beyond, 60, "尖端之外不该有填充（只可能有极淡的柔影）")
        self.assertLess(beside, 60, "三角很窄，两侧不该有填充")

    def test_下方放不下时整块翻到铃铛上方_三角朝下(self):
        self.fill()
        self.area = QRect(0, 0, 800, 300)
        icon_top = 270
        self.bubble.show_below(QPoint(400, icon_top + 12), icon_top)
        self.assertTrue(self.bubble.is_flipped())
        body = self.body()
        self.assertEqual(body.bottom() + 1, icon_top - B.GAP_TO_ICON)
        img, cx = self.image(), self.local_caret_x()
        bottom = B.MARGIN_TOP + body.height()
        inside = img.pixelColor(cx, bottom + 2).alpha()
        beside = img.pixelColor(cx + 6, bottom + 2).alpha()
        self.assertGreaterEqual(inside, 200)
        self.assertGreater(inside - beside, 100, "翻转后三角应出现在本体下沿")

    def test_居中时本体与小三角都对准锚点(self):
        self.fill()
        self.bubble.show_below(QPoint(400, 40), 20)
        body = self.body()
        self.assertLessEqual(abs(body.center().x() + 0.5 - 400), 1.0)
        self.assertEqual(body.top(), 40 + B.GAP_TO_ICON)
        self.assertLessEqual(abs(self.bubble.caret_center_global_x() - 400), 0.5)

    def test_贴右缘时本体向内收拢_三角留在锚点下直到被圆角挡住(self):
        self.fill()
        self.bubble.show_below(QPoint(770, 40), 20)
        body = self.body()
        self.assertEqual(body.right() + 1, self.area.right() + 1 - B.SCREEN_EDGE)
        self.assertLess(body.center().x() + 0.5, 770, "本体确实被往左收拢了")
        shift = self.bubble.caret_center_global_x() - 770
        self.assertLessEqual(abs(shift), 1.0, "锚点离本体右缘还有 CARET_INSET 以上，三角应仍然对着它")
        self.bubble.show_below(QPoint(799, 40), 20)
        body = self.body()
        self.assertLessEqual(self.bubble.caret_center_global_x(), body.right() + 1 - B.CARET_INSET + 0.5,
                             "锚点靠到屏幕边之外时，三角要躲开本体圆角")

    def test_贴左缘时同理(self):
        self.fill()
        self.bubble.show_below(QPoint(10, 40), 20)
        body = self.body()
        self.assertEqual(body.left(), self.area.left() + B.SCREEN_EDGE)
        self.assertGreaterEqual(self.bubble.caret_center_global_x(), body.left() + B.CARET_INSET - 0.5)

    def test_不给铃铛上沿就永不翻转_上方也放不下时留在下方(self):
        self.fill()
        self.area = QRect(0, 0, 800, 200)
        self.bubble.show_below(QPoint(400, 190), None)
        self.assertFalse(self.bubble.is_flipped())
        self.area = QRect(0, 0, 800, 150)
        self.bubble.show_below(QPoint(400, 140), 128)
        self.assertFalse(self.bubble.is_flipped(), "上方同样放不下就别翻，免得整块跑到屏幕外")

    def test_翻转状态每次摆放都重新判定(self):
        self.fill()
        self.area = QRect(0, 0, 800, 300)
        self.bubble.show_below(QPoint(400, 282), 270)
        self.assertTrue(self.bubble.is_flipped())
        self.area = QRect(0, 0, 800, 900)
        self.bubble.show_below(QPoint(400, 282), 270)
        self.assertFalse(self.bubble.is_flipped(), "空间够了就该回到下方，不能沿用上一次的翻转")

    # ---- 配色、文字底线、圆点、细线、柔影 ----
    def row_pixels(self, img, y):
        x0 = B.MARGIN_X + B.PAD_X
        return self.band(img, x0, B.MARGIN_X + self.body().width() - B.PAD_X, y, y + B.ROW_H)

    def test_涨红跌绿_已触发统一灰(self):
        self.fill()
        self.bubble.show_below(QPoint(400, 40), 20)
        img = self.image()
        red, green = _rgb(sf_ui_render.RED), _rgb(sf_ui_render.GREEN)
        grey = _rgb(B.HIT_TEXT_COLOR)
        up, down = self.row_pixels(img, self.row_top(0)), self.row_pixels(img, self.row_top(1))
        hit = self.row_pixels(img, self.row_top(0, after_sep=True))
        self.assertTrue(any(_near(c, red) for c in up), "涨到那一行应是涨色")
        self.assertFalse(any(_near(c, green) for c in up))
        self.assertTrue(any(_near(c, green) for c in down), "跌到那一行应是跌色")
        self.assertFalse(any(_near(c, red) for c in down))
        self.assertTrue(any(_near(c, grey) for c in hit), "已触发统一灰")
        self.assertFalse(any(_near(c, red) or _near(c, green) for c in hit),
                         "已触发不保留红绿，哪怕它原来是涨到")

    def test_价位与百分比共用一条文字底线(self):
        self.fill([CMP[0]])
        self.bubble.show_below(QPoint(400, 40), 20)
        img = self.image()
        y0 = self.row_top(0)
        x0, x1 = B.MARGIN_X + B.PAD_X, B.MARGIN_X + self.body().width() - B.PAD_X

        def is_ink(c):
            return c.alpha() > 120 and c.red() > 200 and c.green() < 140

        cols = [x for x in range(x0, x1)
                if any(is_ink(img.pixelColor(x, y)) for y in range(y0, y0 + B.ROW_H))]
        clusters, start, prev = [], None, None
        for x in cols:
            if start is None:
                start = x
            elif x - prev > 4:
                clusters.append((start, prev))
                start = x
            prev = x
        clusters.append((start, prev))
        self.assertEqual(len(clusters), 3, "方向词、价位、百分比应各成一簇：%r" % (clusters,))

        def bottom(cluster):
            return max(y for x in range(cluster[0], cluster[1] + 1)
                       for y in range(y0, y0 + B.ROW_H) if is_ink(img.pixelColor(x, y)))

        self.assertLessEqual(abs(bottom(clusters[1]) - bottom(clusters[2])), 1,
                             "价位与百分比的数字应落在同一条底线上")

    def test_组标题前的圆点_待触发金黄_已触发灰蓝(self):
        self.fill()
        self.bubble.show_below(QPoint(400, 40), 20)
        img = self.image()
        x0 = B.MARGIN_X + B.PAD_X
        pend_y = B.MARGIN_TOP + B.PAD_TOP
        hit_y = self.row_top(0, after_sep=True) - B.HEADER_H - B.HEADER_GAP
        pend = self.band(img, x0, x0 + 6, pend_y, pend_y + B.HEADER_H)
        hit = self.band(img, x0, x0 + 6, hit_y, hit_y + B.HEADER_H)
        self.assertTrue(any(_near(c, _rgb(B.PENDING_DOT_COLOR)) for c in pend))
        self.assertFalse(any(_near(c, _rgb(B.PENDING_DOT_COLOR)) for c in hit))
        self.assertTrue(any(_near(c, _rgb(B.HIT_DOT_COLOR)) for c in hit))
        self.assertFalse(any(_near(c, _rgb(B.HIT_DOT_COLOR)) for c in pend))

    def test_两组之间有一道左右留边的细线(self):
        self.fill()
        self.bubble.show_below(QPoint(400, 40), 20)
        img = self.image()
        sep_y = B.MARGIN_TOP + B.PAD_TOP + B.HEADER_H + B.HEADER_GAP + 2 * B.ROW_H + B.SEP_GAP_TOP

        def light(x, y):
            c = img.pixelColor(x, y)
            return c.red() + c.green() + c.blue()

        mid = B.MARGIN_X + self.body().width() // 2
        self.assertGreater(light(mid, sep_y), light(mid, sep_y - 2), "细线那一行应比上下的本体底色亮")
        pad_x = B.MARGIN_X + B.PAD_X - 3
        self.assertEqual(light(pad_x, sep_y), light(pad_x, sep_y - 2), "细线左右各留 PAD_X，不通栏")

    def test_柔影向外逐像素衰减_且没被透明外圈裁出硬边(self):
        self.fill()
        self.bubble.show_below(QPoint(400, 40), 20)
        img = self.image()
        x = B.MARGIN_X + 20
        below = [img.pixelColor(x, B.MARGIN_TOP + self.body().height() + d).alpha()
                 for d in range(1, B.MARGIN_BOTTOM)]
        self.assertGreater(below[0], 40, "贴着本体下沿应有可见的影")
        self.assertEqual(below, sorted(below, reverse=True), "影只能越往外越淡")
        w, h = img.width(), img.height()
        ring = max([img.pixelColor(0, y).alpha() for y in range(h)]
                   + [img.pixelColor(w - 1, y).alpha() for y in range(h)]
                   + [img.pixelColor(xx, 0).alpha() for xx in range(w)]
                   + [img.pixelColor(xx, h - 1).alpha() for xx in range(w)])
        self.assertLessEqual(ring, 8, "窗口最外一圈几乎全透明，否则影在窗口边上被切出硬边")


class TargetBubbleScreenLookupCase(SFCase):
    """真实的屏幕查询路径（上面的用例把它换成了固定区域）。

    取不到屏幕信息时不收拢也不翻转；查询抛错要留一行痕迹，不能静默。
    锚点放在真实可用区域右缘内 2px：能收拢的话本体右沿必被拉回，不收拢则必伸出区域之外。
    """

    def setUp(self):
        global _QAPP
        super().setUp()
        _QAPP = SFCase.ensure_qapp()
        sf_ui_render.load_bundled_fonts()
        self.bubble = self.track_widget(B())
        pending, hit = bell_bubble_rows(CMP, 60.0, 2)
        self.bubble.setRows(pending, hit)
        self.area = m.QGuiApplication.primaryScreen().availableGeometry()
        self.anchor = QPoint(self.area.right() - 2, self.area.top() + 40)
        self.icon_top = self.area.top() + 20

    def test_真实屏幕下贴右缘的气泡被拉回可用区域(self):
        self.bubble.show_below(self.anchor, self.icon_top)
        self.assertEqual(self.bubble.body_rect_global().right() + 1,
                         self.area.right() + 1 - B.SCREEN_EDGE)

    def test_取屏幕信息抛错时不收拢也不翻转_并留下一行痕迹(self):
        fake = Mock()
        fake.screenAt.side_effect = RuntimeError("boom")
        with patch.object(m, "QGuiApplication", fake), patch.object(m, "log_swallow") as swallowed:
            self.bubble.show_below(self.anchor, self.icon_top)
        self.assertGreater(self.bubble.body_rect_global().right() + 1, self.area.right() + 1,
                           "取不到屏幕就没法知道边在哪，宁可不动也不能乱挪")
        self.assertFalse(self.bubble.is_flipped())
        labels = [c.args[0] for c in swallowed.call_args_list]
        self.assertIn("stock_float_window.TargetBubble._available_rect", labels)

    def test_系统没有任何屏幕时同样不收拢_且不当成错误(self):
        fake = Mock()
        fake.screenAt.return_value = None
        fake.primaryScreen.return_value = None
        with patch.object(m, "QGuiApplication", fake), patch.object(m, "log_swallow") as swallowed:
            self.bubble.show_below(self.anchor, self.icon_top)
        self.assertGreater(self.bubble.body_rect_global().right() + 1, self.area.right() + 1)
        self.assertFalse(self.bubble.is_flipped())
        swallowed.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
