# -*- coding: utf-8 -*-
"""v1.4.59（S8 第三条）：底部两行"整段凭空消失"改成缩字号。

现场（30 轮走查报告，本机离屏真字体度量复核）：窗口宽被用户明确定死为 184
（sf_constants.W，"原 200px 缩短 20%"，_content_min_width 恒返回 W），而布局对放不下的
段判 fits=False、绘制侧 `continue` —— 默认股茅台当日高 1688.88 / 低 1666.66（两段都是
7 字符）时「最低点 1666.66」**整段不画**，没有省略号也没有提示，用户看到的就是少了一个数。
报告给了两个改法：让 min_width 真撑宽（与用户那条定宽决定冲突），放不下时缩字号
（与"宽度固定"一致）—— 本轮走后者。

判据口径：先给"能不能失败"的对照（同一帧把缩号关掉，差出来的像素就是被找回来的那一段），
再给"缩到几档、什么时候不再缩"的纯函数断言。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QRectF          # noqa: E402
from PyQt6.QtGui import QFont, QFontDatabase, QFontMetrics, QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import sf_constants as C                 # noqa: E402
import sf_ui_render as R                 # noqa: E402
import sf_quote                          # noqa: E402
import stock_float_window as m           # noqa: E402
from sf_logging import reset_log_throttle   # noqa: E402

try:
    from _base import SFCase             # noqa: E402
except ImportError:                       # pragma: no cover
    from tests._base import SFCase        # noqa: E402

FONT_READY = False


def _fonts():
    """真字体度量：内置 D-DIN 必须先注册，否则退化成系统字体、宽度全变。"""
    global FONT_READY
    if not FONT_READY:
        R.load_bundled_fonts()
        FONT_READY = True


def _fm_of(family, size, bold=False):
    return QFontMetrics(QFont(family, size, QFont.Weight.Bold if bold else QFont.Weight.Normal))


def _rows(high, low, pre_close):
    return R.bottom_rows({"price": str(high), "pct": "0.00", "change": "0.00",
                          "high": str(high), "low": str(low), "pre_close": str(pre_close),
                          "name": "贵州茅台", "code": "sh600519", "volume": "1"})


def _fit(width, high, low, pre_close):
    _fonts()
    fm_label = _fm_of(R.FONT, R.BOTTOM_LABEL_PT)

    def fm_num_at(delta):
        return _fm_of(R.NUM_FONT, R.FONT_CODE + delta, bold=True)

    row1, row2 = _rows(high, low, pre_close)
    segs1, segs2, shrink, min_w = R.bottom_rows_fitting(
        width, row1, row2, fm_label, fm_num_at,
        R.PAD_X, R.BOTTOM_INNER_GAP, R.BOTTOM_GAP)
    dropped = [(s["label"], s["num"]) for segs in (segs1, segs2) for s in segs
               if not s["fits"]]
    return segs1, segs2, shrink, min_w, dropped


class TestFittingIsPureFunction(SFCase):
    """bottom_rows_fitting 的四条契约。"""

    @classmethod
    def setUpClass(cls):
        # QFontMetrics 要问字体库，字体库要问 QGuiApplication —— 没有它时
        # horizontalAdvance 直接 SIGSEGV（本机实测 rc=139，一行断言都跑不到）。
        # 引用必须挂在类上持有：局部变量一出栈，QApplication 被析构，后面的用例就崩。
        cls._app = QApplication.instance() or QApplication([])

    def test_previous_close_clamps_only_percentages_and_keeps_real_prices(self):
        # 高开全天在昨收之上、低开全天在昨收之下、跨昨收、完全持平。
        cases = ((105, "110.50", "102.25", "+10.50%", "0.00%"),
                 (95, "98.50", "90.25", "0.00%", "-9.75%"),
                 (102, "110.50", "90.25", "+10.50%", "-9.75%"),
                 (100, "100", "100", "0.00%", "0.00%"))
        for opening, high, low, up, down in cases:
            with self.subTest(opening=opening):
                quote = dict(open=opening, high=high, low=low, pre_close="100")
                rows = R.bottom_rows(quote)
                self.assertEqual([[s[0] for s in row] for row in rows],
                                 [["最大涨幅", "最高点"], ["最大跌幅", "最低点"]])
                self.assertEqual([[s[1] for s in row] for row in rows],
                                 [[up, R._fmt_hl(high)], [down, R._fmt_hl(low)]])  # discipline-ok(R5): preserve the existing price-format contract independently of percentage clamping
                for row, pct in zip(rows, (up, down)):
                    expected_color = R.FG_DIM if pct == "0.00%" else (R.RED if pct.startswith("+") else R.GREEN)
                    self.assertEqual(row[0][2], expected_color)
                self.assertEqual(quote["high"], high)
                self.assertEqual(quote["low"], low)

    def test_unknown_previous_close_never_uses_open_or_hides_prices(self):
        for pre_close in (None, "", "bad", "NaN", "inf", "-inf", "0", "-10"):
            with self.subTest(pre_close=pre_close):
                quote = dict(high="110.50", low="90.25", open="100")
                if pre_close is not None:
                    quote["pre_close"] = pre_close
                rows = R.bottom_rows(quote)
                self.assertEqual([[s[1] for s in row] for row in rows],
                                 [["--", "110.5"], ["--", "90.25"]])
                self.assertEqual([row[0][2] for row in rows], [R.FG_DIM, R.FG_DIM])

    def test_open_changes_do_not_affect_rows(self):
        quote = dict(high="110.50", low="90.25", pre_close="100")
        expected = R.bottom_rows(quote)
        for opening in (None, "", "bad", "NaN", "inf", "0", "-10", "80", "120"):
            with self.subTest(opening=opening):
                self.assertEqual(R.bottom_rows(quote | {"open": opening}), expected)

    def test_invalid_extreme_does_not_hide_the_other_price(self):
        for key in ("high", "low"):
            for value in (None, "bad", "NaN", "inf", "0", "-1"):
                with self.subTest(key=key, value=value):
                    rows = R.bottom_rows(dict(pre_close="100", high="110", low="90") | {key: value})
                    invalid, valid = (rows[0], rows[1]) if key == "high" else (rows[1], rows[0])
                    self.assertEqual([s[1] for s in invalid], ["--", "--"])
                    self.assertNotIn("--", [s[1] for s in valid])

    def test_common_quote_keeps_the_default_size(self):
        """绝大多数行情（尾 0 被 _fmt_hl 归一掉）根本不该触发缩号 —— 界面不许无故变样。"""
        _segs1, _segs2, shrink, _mw, dropped = _fit(C.W, 1830.00, 1810.00, 1800.0)
        self.assertEqual(shrink, 0, "普通行情被无谓缩了号：%r" % (_mw,))
        self.assertEqual(dropped, [])

    def test_long_pair_recovers_the_segment(self):
        """S3 高/低长价仍须完整保留；数字可按既有两档缩号。"""
        _segs1, _segs2, shrink, mw, dropped = _fit(C.W, 1688.88, 1666.66, 1660.0)
        self.assertEqual(dropped, [], "「最低点 1666.66」仍然整段不画（需宽 %d > %d）"
                         % (mw, C.W))
        self.assertLess(shrink, 0, "放得下却没缩号，说明这条判据没走到修复分支")

    def test_shrink_is_capped_and_does_not_lie(self):
        """窄到放不下时：只许缩到下限（两档），并且如实报告还有段没放下。

        这条是"闸门不许自己说谎"的对应面 —— 如果它偷偷多缩几档、或把丢段说成没丢，
        上面那条"最低点回来了"就变成永远绿的自证。
        """
        _s1, _s2, shrink, _mw, dropped = _fit(60, 1688.88, 1666.66, 1660.0)
        self.assertEqual(shrink, min(R.BOTTOM_NUM_SHRINK_STEPS),
                         "缩号越过了下限（%d）：字号会小过状态徽标，层级读不出来" % shrink)
        self.assertTrue(dropped, "60px 宽里明明放不下，却说全都画了")

    def test_floor_size_still_below_badge_size_is_refused(self):
        """下限必须高于徽标字号 BADGE_FONT_SIZE，否则"缩到读不出"这件事没人拦。"""
        self.assertGreaterEqual(R.FONT_CODE + min(R.BOTTOM_NUM_SHRINK_STEPS),
                                R.BADGE_FONT_SIZE,
                                "底部数字的下限比徽标还小，这个档位本身就不该存在")


class _FontSpy(QPainter):
    """记录 setFont 真实入参的画笔（(字体族, 字号) 列表）。

    QPainter 由测试自己构造并传给被测方法，所以这里的 setFont 会被 Python 侧
    覆盖到 —— 记的是被测对象实际做了什么，不是它"打算做什么"。
    """

    def __init__(self, image):
        super().__init__(image)
        self.fonts = []
        self.texts = []

    def drawText(self, *args):
        self.texts.append(args[-1])
        return super().drawText(*args)

    def setFont(self, font):
        self.fonts.append((font.family(), font.pointSize()))
        return super().setFont(font)


class TestRecoveredSegmentIsRealInk(SFCase):
    """像素对照：被"找回来"的那一段必须真的有墨水。

    为什么纯函数断言不够：segs 里 fits=True 只证明布局认为放得下，不证明绘制侧用了
    同一份字号 —— 而"按 8pt 排、按 9pt 画"恰恰是这类修复最容易留下的错（画出去就等于
    又消失一次，还更难查）。做法与 A26 填充判据同一条路子：同一帧画两遍，第二遍把
    缩号关掉，差出来的像素就是被找回来的那一段。
    """

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        _fonts()
        reset_log_throttle()
        self._td = tempfile.mkdtemp(prefix="sf_bottom_fit_")
        for p in (
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, C.MIN_H, "translucent")),
            patch.object(m, "load_api_urls", lambda: None),
            patch.object(m, "save_config", lambda *a, **k: True),
            patch.object(sf_quote, "http_get", lambda *a, **k: ""),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
        ):
            self.track_patch(p)
        self.win = m.FloatWindow()
        self.track_widget(self.win)
        self.win.running = False
        self.win._wake.set()   # discipline-ok(R5): 不停它，绘制线程会往这张帧里塞帧
        # discipline-ok(R5): 这些是窗口自己的线程/动画件，offscreen 下没有公开入口能停
        for t in (self.win.timer, self.win.top_timer,          # discipline-ok(R5): 停表只能取窗口自有件
                  self.win._anim_timer,                          # discipline-ok(R5): 同上（动画/闪烁两个定时器）
                  self.win._flash_timer):   # discipline-ok(R5): 同上（停掉闪烁定时器，画面才可比）
            t.stop()
        self.win.quote = {"name": "贵州茅台", "code": "sh600519", "price": "1688.88",
                          "pct": "0.53", "change": "8.88", "pre_close": "1680.00",
                          "high": "1688.88", "low": "1666.66", "volume": "12345"}

    def _render_bottom_spy(self):
        """用子类化的 QPainter 记录 _paint_bottom 真正设过的号码字号。"""
        img = QImage(self.win.width(), self.win.height(), QImage.Format.Format_ARGB32)
        img.fill(0)
        p = _FontSpy(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.win._paint_bottom(p, self.win.width())   # discipline-ok(R5): AGENTS §3.1 明令直调此入口
        p.end()
        return p

    @staticmethod
    def _number_font_sizes(spy):
        """只取数字字体（NUM_FONT）的字号集合，中文标签字体不参与判断。"""
        return {size for family, size in spy.fonts if family == R.NUM_FONT}

    def test_shrink_leaves_an_audible_need_width(self):
        """`min_width` 的消费者必须还在：缩过号就得留下一条「需要多宽」。

        为什么单独钉这一条：主窗把第 4 个返回值接成 `_min_w`，看名字像死值 —— 本轮
        复核我就据此误判过一次"算了没人用"，而它实际是 sf_ui_render.py:1201 那条
        缩档日志里的数。没有这条判据，下一个人真会把它删掉，于是"为什么少了一个数"
        又变成事后无从回答的事。
        """
        import sf_ui_render as _m
        reset_log_throttle()
        rec = []
        with patch.object(_m, "log_line_throttled",
                          lambda key, msg, *a, **k: rec.append((key, msg))):
            self._render_bottom()
        hit = [msg for key, msg in rec if key == "bottom_num_shrunk"]
        self.assertTrue(hit, "默认股 1688.88 在 184px 宽下必然走缩档，却没留下缩档日志")
        self.assertIn("需要", hit[0],
                      "日志要说清需宽，否则只能知道缩了号、不知道该不该放宽：%s" % hit[0])

    def _render_bottom(self, disable_shrink=False):
        h = self.win.height()
        img = QImage(self.win.width(), h, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        orig = None
        if disable_shrink:
            orig = R.bottom_rows_fitting

            def only_default(*a, **k):
                """退回修复前的行为：只按现字号排一次，放不下就丢段。"""
                from renderer import _bottom_layout
                fm_label = _fm_of(R.FONT, R.BOTTOM_LABEL_PT)
                fm_num = _fm_of(R.NUM_FONT, R.FONT_CODE, bold=True)
                s1, s2, mw = _bottom_layout(a[0], a[1], a[2], fm_label, fm_num,
                                            pad_x=a[5], inner_gap=a[6], gap=a[7],
                                            slack=k.get("slack", 8), base=0)
                return s1, s2, 0, mw
            R.bottom_rows_fitting = only_default
        try:
            self.win._paint_bottom(p, self.win.width())   # discipline-ok(R5): 同上，必须直调此入口
        finally:
            if orig is not None:
                R.bottom_rows_fitting = orig
        p.end()
        return img

    def _row2_ink(self, img):
        """底部第二行那条横带里的不透明像素数（最高点/最低点所在行）。"""
        top = img.height() - R.BOTTOM_BASE_HIDDEN - R.FONT_CODE - 4
        bottom = img.height() - 2
        n = 0
        for y in range(max(0, top), min(img.height(), bottom)):
            for x in range(img.width()):
                if img.pixelColor(x, y).alpha() > 40:
                    n += 1
        return n

    def test_recovered_segment_has_real_ink(self):
        with_shrink = self._render_bottom()
        without = self._render_bottom(disable_shrink=True)
        a = self._row2_ink(with_shrink)
        b = self._row2_ink(without)
        self.assertGreater(b, 0, "对照帧一行都没画出来 —— 夹具或行带取错了，两条数都不算数")
        self.assertGreater(a, b,
                           "修复后底部第二行的墨水反而没变多（%d → %d）："
                           "说明那段文本只是「布局认为放得下」，绘制侧并没有真的画出来"
                           % (b, a))
        # 找回一整段「最低点 1666.66」至少应当带来可感知的一截墨水
        self.assertGreater(a - b, a * 0.15,
                           "只多了 %.1f%% 的墨水，不像是找回了整段文本" % (100.0 * (a - b) / a))

    def test_drawn_number_font_follows_the_shrink(self):
        """布局缩了档，绘制必须用同一档 —— 这条只有靠"看见 setFont 的真参数"才判得了。

        为什么不用像素判：实测把"按 8pt 排、按 9pt 画"这个错种回去，底部行的墨水
        只是变多、越界像素是 0（8pt 排完还剩 ~4px 余量，9pt 画出去刚好没顶到边界），
        像素口径看不见。而 QPainter 是本用例自己造好传进去的，所以可以子类化它
        把 setFont 的真实入参记下来 —— 记的是被测对象实际做了什么。
        """
        sizes = self._number_font_sizes(self._render_bottom_spy())
        self.assertIn(R.FONT_CODE - 2, sizes,
                      "S3 长价缩到 7pt，绘制必须使用同一档号码字体：%s" % sorted(sizes))

    def test_s3_draws_all_labels_and_long_prices_in_real_fonts(self):
        spy = self._render_bottom_spy()
        self.assertEqual(spy.texts, ["最大涨幅", "+0.53%", "最高点", "1688.88",
                                     "最大跌幅", "-0.79%", "最低点", "1666.66"])
        label_sizes = {size for family, size in spy.fonts if family == R.FONT}
        self.assertEqual(label_sizes, {8}, "S3 仅底部中文标签使用 8pt")
        self.assertEqual(R.FONT_CODE, 9, "代码行和底部默认数字字号不能一起缩小")

    def test_common_quote_draws_at_the_default_size_only(self):
        """反向对照：普通行情不许无谓缩号（界面不许因为这次改动变样）。"""
        self.win.quote = dict(self.win.quote, high="1830.00", low="1810.00",
                              pre_close="1800.00")
        sizes = self._number_font_sizes(self._render_bottom_spy())
        self.assertEqual(sizes, {R.FONT_CODE},
                         "普通行情的底部数字字号不只有默认档：%s" % sorted(sizes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
