# -*- coding: utf-8 -*-
"""数字定宽占位回归（走查 30 轮 S8）。

守的两件事，都是"用户一眼看得境"的那种：

1. **同位数的价格，横向占位必须恒定。** D-DIN 的数字原本是比例数字（27pt 实测
   `1`=13px、`7`=16px、`8`=19px），逐位滚动时每一位的左边界随位值变化，价格每跳
   一次，右侧涨跌幅与徽标就整体横移（实测最多 20px）。现在数字位一律占
   `max(advance('0'..'9'))` 的格宽、字形在格内居中，整串宽度只由位数决定。
2. **布局量的必须是"这一帧真正会画出来的串"。** 滚动预置期间绘制用目标串
   （"100.00"），旧实现却按插值中间串（"99.98"）量宽，位数一跨价格就压住涨跌幅
   约 8px、持续 2~3 帧。

断言口径：不重新实现布局，而是包住产品自己调的 `price_pct_layout`，读它**实际收到
的参数与返回的位置**（§6 第 5 条：看内部状态的理由是"要证明量宽侧与绘制侧同口径"，
而这两侧的合流点就在这个调用上）。

"""
import itertools
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtGui import QFont, QFontMetrics, QImage, QPainter, QColor
from PyQt6.QtWidgets import QApplication

import sf_quote
import sf_ui_render as R
import stock_float_window as m
from sf_ui_render import (H_PRICE, NUM_FONT, Y_PRICE, digit_cell_width, tabular_advance)
from tests._base import SFCase

_FAKE_HTTP = ('var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,'
              '1520.0,1480.0";')


def _fake_http_get(url, **kw):
    if "hq.sinajs.cn" in url:
        return _FAKE_HTTP
    if "/appstock/app/minute/query?" in url:
        return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                '"data":["0930 1510.0 100 1000", "1000 1511.0 200 2000"]}}}}')
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


_QUOTE = {"name": "贵州茅台", "code": "600519", "price": "410.00", "pct": "1.61",
          "change": "6.50", "pre_close": "403.50", "high": "412.00", "low": "400.00",
          "volume": "12345", "limit_up": "443.85", "limit_down": "363.15"}


class TestTabularWidths(SFCase):   # §6 第 7 条：新用例走登记式基类
    """纯度量层：定宽格与串宽函数（不需要窗口）。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])
        R.load_bundled_fonts()

    def _fm(self, size=R.FONT_PRICE_MAX):
        return QFontMetrics(QFont(NUM_FONT, size, QFont.Weight.Bold))

    def test_cell_is_the_widest_digit(self):
        fm = self._fm()
        cell = digit_cell_width(fm)
        self.assertEqual(cell, max(fm.horizontalAdvance(d) for d in "0123456789"))
        # 格宽必须容得下每一位数字，否则定宽占位会让字形互相压字
        for d in "0123456789":
            self.assertLessEqual(fm.horizontalAdvance(d), cell, "数字 %s 宽于格" % d)

    def test_same_shape_strings_share_one_width(self):
        """同位数、同非数字字符的串，量宽必须只有一个取值 —— 这是"不横移"的全部依据。"""
        fm = self._fm()
        cell = digit_cell_width(fm)
        widths = {tabular_advance(fm, "".join(t) + ".00", cell)
                  for t in itertools.product("01358", repeat=3)}
        self.assertEqual(len(widths), 1, "同形状价格量出了多种宽度: %r" % sorted(widths))

    def test_natural_widths_actually_differ(self):
        """反向确认：不改口径时宽度确实会跳。若这条也"相等"，说明上面的断言是空的。"""
        fm = self._fm()
        self.assertNotEqual(fm.horizontalAdvance("111.11"),
                            fm.horizontalAdvance("888.88"),
                            "字体已自带等宽数字？那 S8 的定宽口径可以退役")

    def test_non_digits_keep_natural_advance(self):
        """小数点/正负号/百分号不参与定宽 —— 它们的位置不随位值变化。"""
        fm = self._fm()
        cell = digit_cell_width(fm)
        self.assertEqual(tabular_advance(fm, ".", cell), fm.horizontalAdvance("."))
        self.assertEqual(tabular_advance(fm, "+%", cell),
                         fm.horizontalAdvance("+") + fm.horizontalAdvance("%"))


class TestPriceRowLayout(SFCase):
    """价格行：涨跌幅/徽标的横位置不随位值滑动，且量的是真正画出来的串。"""

    @classmethod
    def setUpClass(cls):
        # 本进程必须自己持有 QApplication 引用（否则建 FloatWindow 直接崩）
        cls._app = cls.ensure_qapp()
        # 与产品启动同口径：先注册内置字体，否则这一帧是**替代字体**画出来的，
        # 上面几条断言就变成"对任何字体都成立"的空断言（离屏实测会打
        # `missing font family "D-DIN"` 警告，那就是没注册的信号）。
        cls._fams = R.load_bundled_fonts()

    def setUp(self):
        super().setUp()
        self.track_patch(patch.object(m, "load_config",
                                      return_value=("sh600519", 100, 100, 200, "translucent")))
        self.track_patch(patch.object(m, "save_config", return_value=True))
        self.track_patch(patch.object(sf_quote, "http_get", _fake_http_get))
        self.track_patch(patch.object(m, "start_icon_worker", lambda: None))
        self.track_patch(patch.object(m.FloatWindow, "_force_top", lambda self: None))
        self.track_patch(patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None))
        self.win = self.track_widget(m.FloatWindow())
        self.win.show()
        self.win._apply_quote(dict(_QUOTE))   # discipline-ok(R5): 喂一帧行情只能走 _apply_quote，公开入口是网络回调
        self.win._anim_timer.stop()   # discipline-ok(R5): 停掉真实动画定时器，否则逐位滚动自己推进（同 test_v126 口径）
        self.recorded = []
        # 包住产品自己调的布局函数：记录入参与返回，断言"量宽侧"的真实口径
        real = R.price_pct_layout

        def spy(width, measure, pct_w, **kw):
            lay = real(width, measure, pct_w, **kw)
            self.recorded.append({"width": width, "pct_w": pct_w, "price_w": lay["price_w"],
                                  "size": lay["size"], "pct_x": lay["pct_x"],
                                  "price_x": lay["price_x"]})
            return lay

        self.track_patch(patch.object(R, "price_pct_layout", spy))

    def tearDown(self):
        # §6 第 11 条：关窗必须发生在 patch 还活着的时候，否则落盘走真实实现
        self.win._exit_persisted = True   # discipline-ok(R5): §6 第 11 条：关窗前必须关掉落盘幂等标记，否则写用户 config.json
        self.win.close()
        super().tearDown()

    def _paint(self, price, pct, pending=None, animating=False):
        """按给定显示值画一帧价格行，返回 (图像, 本次布局记录)。"""
        self.recorded.clear()
        self.win._disp_price = price   # discipline-ok(R5): 价格显示值是渲染层输入，产品没有公开 setter
        self.win._disp_pct = pct   # discipline-ok(R5): 同上：涨跌幅显示值
        self.win._roll_pending = dict(pending or {})   # discipline-ok(R5): 滚动预置目标串是 S8-B 的触发条件，只能直接置
        if animating:
            # 插值动画进行中的样子：定时器活着，_draw_roll_text 才会认预置目标串
            self.win._anim_timer = _ActiveTimer()   # discipline-ok(R5): 替换定时器只为让它'看起来在跑'，渲染层只读 isActive()
        img = QImage(self.win.width(), self.win.height(), QImage.Format.Format_ARGB32)
        img.fill(QColor("#000000"))
        p = QPainter(img)
        self.win._paint_price(p, self.win.width())   # discipline-ok(R5): 直调 _paint_price 画到自建 QImage —— 与 tools/verify_bottom_gap_px.py 同口径，paintEvent 的兜底会吞异常
        p.end()
        return img, (self.recorded[-1] if self.recorded else None)

    def test_pct_x_is_same_for_same_digit_count(self):
        """核心一条：`111.11` 与 `888.88` 的涨跌幅起点必须一模一样。"""
        _, lay_a = self._paint(111.11, 1.61)
        _, lay_b = self._paint(888.88, 1.61)
        self.assertIsNotNone(lay_a)
        self.assertIsNotNone(lay_b)
        self.assertEqual(lay_a["pct_x"], lay_b["pct_x"],
                         "同位数价格的涨跌幅起点不同 → 数字一跳整行横移（S8 复发）")
        self.assertEqual(lay_a["price_w"], lay_b["price_w"])

    def test_price_ink_stays_inside_its_advance_box(self):
        """字形在格内居中后，可见墨水不得越出这一串的定宽占位盒（越界=压到涨跌幅）。"""
        img, lay = self._paint(888.88, 1.61)
        self.assertIsNotNone(lay["pct_x"], "该宽度下涨跌幅被丢弃，断言无意义")
        fm = QFontMetrics(QFont(NUM_FONT, lay["size"], QFont.Weight.Bold))
        box_right = lay["price_x"] + tabular_advance(fm, "888.88", digit_cell_width(fm))
        # 只看涨跌幅起点左侧的墨水 —— 那才是价格大字自己的可见范围
        cols = [c for c in _ink_columns(img, Y_PRICE, Y_PRICE + H_PRICE)
                if c < lay["pct_x"]]
        self.assertTrue(cols, "价格行没画出来，断言无意义")
        self.assertLessEqual(max(cols), box_right,
                             "价格墨水越出占位盒 %dpx" % (max(cols) - box_right))

    def test_layout_measures_the_string_actually_drawn(self):
        """滚动预置期间：布局要按目标串量宽，不能按插值中间串（旧实现压字 8px 的根因）。"""
        _, lay_target = self._paint(100.00, 1.61)
        _, lay_mid = self._paint(99.98, 1.61, pending={"price": "100.00"}, animating=True)
        self.assertEqual(lay_mid["pct_x"], lay_target["pct_x"],
                         "中间串量宽、目标串绘制 → 两者占位不同，价格会压住涨跌幅")


class _ActiveTimer:
    """替身：只回答"动画定时器还活着"这一件事（渲染层只用到 isActive）。"""

    def isActive(self):
        return True

    def start(self):
        pass

    def stop(self):
        pass


def _ink_columns(img, y0, y1):
    """返回 [y0,y1) 行带内有非背景墨水的列号 —— 用来量"看得见的字到哪儿为止"。"""
    cols = set()
    for y in range(max(0, int(y0)), min(img.height(), int(y1))):
        for x in range(img.width()):
            if (img.pixel(x, y) & 0x00FFFFFF) != 0:
                cols.add(x)
    return sorted(cols)


if __name__ == "__main__":
    unittest.main(verbosity=2)
