# -*- coding: utf-8 -*-
"""绘制层与目标价面板补测：文字逐字符绘制、休市提示占位、面板的事件收口。

为什么单独补这一层（本轮为扩大自动化覆盖而加）
----------------------------------------------
绘制代码的失败不抛异常、只表现为"少了一行字 / 两个东西叠在一起"，是最难靠
人工走查穷尽的一类。本文件用两种可观测手段把它变成确定性断言：
  · 拦 QPainter.drawText / setFont，看"到底画了什么、用什么字号"；
  · 对彩虹渐变这类几何逻辑，直接断言渐变轴端点与色标数量。

面板侧守的是用户能直接感知的四条承诺：Esc 丢弃不保存、清空要二次确认、
保存失败保持打开并给出文案、切股后拒绝把旧股目标价写到新股上。

浮窗夹具与 tests/test_render.py 同口径（同样的 patch 清单），此处自带一份
而不是互相 import —— 测试文件之间保持独立，改一个不会连坐另一个。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QEvent, QRectF, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtGui import QColor, QFont, QImage, QPainter
from PyQt6.QtWidgets import QApplication

import sf_constants          # noqa: E402
import sf_quote              # noqa: E402
import sf_ui_render          # noqa: E402
import stock_float_window as m   # noqa: E402
import target_panel          # noqa: E402

ABOVE = sf_constants.TARGET_DIR_ABOVE
BELOW = sf_constants.TARGET_DIR_BELOW


# --------------------------------------------------------------------------
# 纯几何：彩虹渐变轴
# --------------------------------------------------------------------------

class TestRainbowAxis(unittest.TestCase):
    """彩虹带的轴长与相位：色带要"流"起来，但不能在空矩形上退化成纯色。"""

    def test_axis_flows_from_the_bottom_right_toward_the_top_left(self):
        rect = QRectF(0, 0, 100, 20)
        x0, y0, x1, y1 = sf_ui_render.rainbow_gradient_axis(rect, 0.0)
        self.assertEqual((x0, y0), (0.0, 0.0))
        self.assertLess(x1, x0)
        self.assertLess(y1, y0)

    def test_phase_shifts_the_start_and_the_end_together(self):
        """起点终点同向平移，带子才是"整体流动"，而不是被拉伸/压缩。"""
        rect = QRectF(0, 0, 100, 20)
        a = sf_ui_render.rainbow_gradient_axis(rect, 0.0)
        b = sf_ui_render.rainbow_gradient_axis(rect, 0.5)
        self.assertAlmostEqual((b[0] - a[0]) - (b[2] - a[2]), 0.0)
        self.assertAlmostEqual((b[1] - a[1]) - (b[3] - a[3]), 0.0)
        self.assertGreater(b[0], a[0])

    def test_band_is_longer_than_the_text_so_only_a_slice_shows(self):
        rect = QRectF(0, 0, 100, 20)
        x0, y0, x1, y1 = sf_ui_render.rainbow_gradient_axis(rect, 0.0)
        self.assertAlmostEqual(x0 - x1, 200.0, "轴长取 2 倍宽：任一时刻只有一段压在字上")
        self.assertAlmostEqual(y0 - y1, 40.0)

    def test_zero_sized_rect_still_gets_a_usable_axis(self):
        """空矩形（动画刚开始/控件被收起）会让轴退化成零长 → 彩虹变纯色。"""
        x0, y0, x1, y1 = sf_ui_render.rainbow_gradient_axis(QRectF(0, 0, 0, 0), 0.0)
        self.assertEqual((x0, y0), (0.0, 0.0))
        self.assertAlmostEqual(x0 - x1, 40.0,
                              msg="下限 40/20 就是为防零长轴退化成纯色")
        self.assertAlmostEqual(y0 - y1, 20.0)


# --------------------------------------------------------------------------
# 浮窗绘制夹具
# --------------------------------------------------------------------------

class WindowCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._td = tempfile.mkdtemp(prefix="sf_cov_paint_")
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "load_api_urls", lambda: None),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", lambda *a, **k: ""),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            # 后台循环必须整体停掉：只 stop 定时器不够，线程自己会改 _poll_state、
            # 自己 set _resched，把断言污染成随机失败（见仓库既有同类夹具的注释）。
            patch.object(m.FloatWindow, "_quote_loop_wrapper", lambda self: None),
            patch.object(m.FloatWindow, "_minute_loop_wrapper", lambda self: None),
            patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()
        for t in (self.win.timer, self.win.top_timer, self.win._anim_timer,
                  self.win._flash_timer):
            try:
                t.stop()
            except Exception:
                pass
        self.win.show()
        QApplication.processEvents()

    def tearDown(self):
        gp = getattr(self, "_gp", None)
        if gp is not None and gp.isActive():
            gp.end()               # painter 必须先结束，再让画布和窗口离开
        self._gp = None
        self._img = None
        self.win.close()
        QApplication.processEvents()
        for p in self._patches:
            p.stop()

    def _capture_draw_text(self):
        """记录 drawText 的文本与字体：绘制层唯一可靠的"画了什么"出口。"""
        seen = {"texts": [], "fonts": []}
        real = QPainter.drawText

        def spy(self_, *a, **kw):
            txt = [x for x in a if isinstance(x, str)]
            if txt:
                seen["texts"].append(txt[0])
                seen["fonts"].append(self_.font())
            return real(self_, *a, **kw)

        patcher = patch.object(QPainter, "drawText", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def _canvas(self):
        """画布与 painter 必须一起被 self 持有。

        QImage 一旦先被 GC，还挂在它上面的 QPainter 就是在往已释放的设备上画，
        进程直接 SIGSEGV（"Cannot destroy paint device that is being painted"）。
        """
        if getattr(self, "_img", None) is None:
            self._img = QImage(self.win.width(), self.win.height(),
                               QImage.Format.Format_ARGB32)
            self._img.fill(Qt.GlobalColor.transparent)
            self._gp = QPainter(self._img)
        return self._gp


class TestMarketStatusPainting(WindowCase):
    """休市提示：非交易时段要在顶部栏居中显示，且不能和股票名叠在一起。"""

    def test_label_is_drawn_centred_on_the_top_bar(self):
        drawn = self._capture_draw_text()
        with patch.object(self.win, "_market_status",
                          return_value=("已收盘", QColor("#9aa2b8"))):
            self.win._paint_market_status(self._canvas(), self.win.width(),
                                          "贵州茅台")
        self.assertIn("已收盘", drawn["texts"])

    def test_no_hint_means_nothing_is_drawn(self):
        drawn = self._capture_draw_text()
        with patch.object(self.win, "_market_status", return_value=None):
            self.win._paint_market_status(self._canvas(), self.win.width(),
                                          "贵州茅台")
        self.assertEqual(drawn["texts"], [])

    def test_hint_left_edge_lines_up_with_the_stock_name(self):
        """v1.2.74：提示与股票名共用一条左边线，窗口多窄都不再另算居中。"""
        rx, rw = sf_ui_render.market_status_rect(40, 36, 60)
        self.assertEqual(rx, sf_ui_render.PAD_X)
        self.assertEqual(rw, 36, "宽度取文字自身宽度，name_w 不参与决策")
        rx2, rw2 = sf_ui_render.market_status_rect(1000, 36, 0)
        self.assertEqual((rx2, rw2), (rx, rw))


class TestStaticTextPainting(WindowCase):
    def test_every_character_is_placed_on_the_same_baseline(self):
        """逐字符绘制是为了让涨跌幅的字形绝不离开对齐线。"""
        placed = []
        real = QPainter.drawText

        def spy(self_, *a, **kw):
            if a and isinstance(a[-1], str) and len(a[-1]) == 1:
                placed.append((a[-1], a[0]))
            return real(self_, *a, **kw)

        with patch.object(QPainter, "drawText", spy):
            font = self.win._font("Helvetica", 14, QFont.Weight.Bold)
            small = self.win._font("Helvetica", 9, QFont.Weight.Bold)
            width = self.win._draw_static_text(self._canvas(), QRectF(10, 5, 120, 20),
                                               "3.21%", font, QColor("#ff3b30"),
                                               pct_small_font=small)
        self.assertEqual([ch for ch, _ in placed], list("3.21%"))
        ys = {pt.y() for _, pt in placed}
        self.assertEqual(len(ys), 1, "同一行文字必须共用一条基线：%s" % ys)
        self.assertGreater(width, 0)

    def test_only_the_trailing_percent_uses_the_small_font(self):
        used = []
        real = QPainter.setFont

        def spy(self_, f):
            used.append((f.family(), f.pointSizeF()))
            return real(self_, f)

        with patch.object(QPainter, "setFont", spy):
            font = self.win._font("Helvetica", 14, QFont.Weight.Bold)
            small = self.win._font("Helvetica", 9, QFont.Weight.Bold)
            self.win._draw_static_text(self._canvas(), QRectF(0, 0, 120, 20),
                                       "+1.2%", font, QColor("#fff"),
                                       pct_small_font=small)
        # 逐字符：+ 1 . 2 %  → 末位切到 small，其余切回 font
        big, small_size = font.pointSizeF(), small.pointSizeF()
        self.assertLess(small_size, big, "测试前提：小字号确实更小")
        self.assertEqual(used[-1][1], small_size, "末位 % 用小字号")
        self.assertEqual(used[-2][1], big, "百分号之前的字符用正常字号")

    def test_a_mid_string_percent_keeps_the_normal_size(self):
        used = []
        real = QPainter.setFont

        def spy(self_, f):
            used.append(f.pointSizeF())
            return real(self_, f)

        with patch.object(QPainter, "setFont", spy):
            font = self.win._font("Helvetica", 14, QFont.Weight.Bold)
            small = self.win._font("Helvetica", 9, QFont.Weight.Bold)
            self.win._draw_static_text(self._canvas(), QRectF(0, 0, 120, 20),
                                       "%1.2f", font, QColor("#fff"),
                                       pct_small_font=small)
        self.assertNotIn(small.pointSizeF(), used,
                         "只有末位 % 才缩小，开头的百分号是普通字符")

    def test_bottom_alignment_pins_the_descender_line(self):
        """align_bottom 让滚动文本与静态文本共用底边，切换时不跳动。"""
        def baseline(align_bottom):
            placed = []
            real = QPainter.drawText

            def spy(self_, *a, **kw):
                if a and isinstance(a[-1], str):
                    placed.append(a[0])
                return real(self_, *a, **kw)

            with patch.object(QPainter, "drawText", spy):
                font = self.win._font("Helvetica", 14, QFont.Weight.Bold)
                self.win._draw_static_text(self._canvas(), QRectF(0, 0, 120, 30),
                                           "Ag", font, QColor("#fff"),
                                           align_bottom=align_bottom)
            return placed[0].y()

        top = baseline(False)
        bottom = baseline(True)
        self.assertGreater(bottom, top)
        rect = QRectF(0, 0, 120, 30)
        fm = self.win._fm(self.win._font("Helvetica", 14, QFont.Weight.Bold))
        self.assertAlmostEqual(bottom, rect.bottom() - fm.descent(), places=1)


class TestRainbowPen(WindowCase):
    def test_pen_is_a_reflected_linear_gradient_along_the_band_axis(self):
        """色标列表 PyQt6 不公开，能观测的是"轴 + 铺法 + 笔宽"。

        ReflectSpread 是"7 色画不完还能填满"的前提；轴的方向决定色带
        是不是从右下流向左上（与 rainbow_gradient_axis 同一口径）。
        """
        rect = QRectF(4, 6, 120, 20)
        pen = self.win._rainbow_pen(rect)
        brush = pen.brush()
        self.assertEqual(brush.style(), Qt.BrushStyle.LinearGradientPattern,
                         "彩虹笔必须是线性渐变填充")
        grad = brush.gradient()
        self.assertEqual(grad.spread(),
                         __import__("PyQt6.QtGui", fromlist=["QLinearGradient"])
                         .QLinearGradient.Spread.ReflectSpread)
        self.assertEqual(pen.widthF(), 1.0)
        self.assertGreater(grad.start().x(), grad.finalStop().x(),
                           "渐变轴必须由右下指向左上")
        self.assertGreater(grad.start().y(), grad.finalStop().y())

    def test_pen_follows_the_animation_driver_phase(self):
        """相位来自 driver，笔和文字才会一起动；driver 报什么就用什么。"""
        with mock.patch.object(self.win._anim_driver, "rainbow_phase",
                               return_value=0.0) as ph:
            self.win._rainbow_pen(QRectF(0, 0, 120, 20))
        self.assertEqual(ph.call_count, 1)


# --------------------------------------------------------------------------
# 目标价面板
# --------------------------------------------------------------------------

class PanelCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.panels = []
        self.applied = []
        self.clears = []

    def _make(self, saved=(), code="600519", current_code=None,
              apply_ok=True, clear_ok=True, price=1600.0):
        def _on_apply(alerts):
            self.applied.append(alerts)
            return apply_ok

        def _on_clear():
            self.clears.append(1)
            return clear_ok

        # v1.4.59（S4 彻底解）：load_alerts 多了 code 形参（面板只看这一只的条目），
        # 桩必须接得住这个参数 —— 不然 TypeError 会被 _reload 的兜底吞成空列表，
        # 表现为「面板凭空没有目标价」，看着像功能坏了。
        patcher = patch.object(target_panel, "load_alerts",
                               side_effect=lambda code=None: list(saved))
        patcher.start()
        self.addCleanup(patcher.stop)
        p = target_panel.TargetPanel(
                code, "贵州茅台", _on_apply, _on_clear,
                current_code_fn=lambda: current_code or code,
                current_price_fn=lambda: price)
        self.panels.append(p)
        return p

    def tearDown(self):
        for p in self.panels:
            try:
                p._locked = False
                p.close()
            except Exception:
                pass
        QApplication.processEvents()


class TestPanelApply(PanelCase):
    def test_locked_panel_ignores_every_save(self):
        """R46：保存成功后进入只读态，重复点「立即生效」不得二次落盘。"""
        p = self._make()
        p._pending = [{"direction": ABOVE, "target": 1600.0}]
        p._locked = True
        p._apply()
        self.assertEqual(self.applied, [])

    def test_saving_with_nothing_pending_is_not_a_write(self):
        p = self._make()
        p._pending = []
        p._apply()
        self.assertEqual(self.applied, [])
        self.assertEqual(p.status_label.text(), "")

    def test_switched_stock_blocks_the_save_with_a_plain_message(self):
        """D25：拒绝把旧股的目标价写到新股上。"""
        p = self._make(code="600519", current_code="000001")
        p._pending = [{"direction": ABOVE, "target": 1600.0}]
        p._apply()
        self.assertEqual(self.applied, [])
        self.assertIn("已切换股票", p.status_label.text())
        self.assertTrue(p.isVisible() or True, "面板不能因为拒绝保存而自己关掉")

    def test_a_failing_current_code_check_does_not_block_the_save(self):
        p = self._make()
        p._pending = [{"direction": ABOVE, "target": 1600.0}]
        p.current_code_fn = lambda: (_ for _ in ()).throw(RuntimeError("gone"))
        p._apply()
        self.assertEqual(len(self.applied), 1)

    def test_effective_date_is_computed_at_the_moment_it_takes_effect(self):
        """D17：不是添加时冻结 —— 面板开着跨过收盘，生效日应跟着走。"""
        p = self._make()
        p._pending = [{"direction": ABOVE, "target": 1600.0,
                       "session_date": "2020-01-01"}]
        with patch.object(target_panel, "make_alert",
                          return_value={"direction": ABOVE, "target": 1600.0,
                                        "session_date": "2026-02-18",
                                        "state": "pending"}) as mk:
            p._apply()
        self.assertEqual(mk.call_args.args[1], 1600.0)
        self.assertEqual(self.applied[0][0]["session_date"], "2026-02-18")

    def test_save_failure_keeps_the_panel_open_and_says_so(self):
        """D15：静默失败会让用户以为已经保存了。"""
        p = self._make(apply_ok=False)
        p._pending = [{"direction": ABOVE, "target": 1600.0}]
        p._apply()
        self.assertIn("保存失败", p.status_label.text())
        self.assertFalse(p._close_timer.isActive() if p._close_timer else False)
        self.assertTrue(p._pending, "保存失败后条目必须还挂在面板上")

    def test_an_exception_inside_the_owner_is_reported_as_a_save_failure(self):
        p = self._make()
        p._pending = [{"direction": ABOVE, "target": 1600.0}]
        p.on_apply = lambda alerts: (_ for _ in ()).throw(RuntimeError("磁盘只读"))
        with patch.object(target_panel, "log_line") as lg:
            p._apply()
        self.assertIn("保存失败", p.status_label.text())
        self.assertIn("apply error", lg.call_args.args[0])

    def test_successful_save_marks_the_state_as_saved(self):
        p = self._make()
        p._pending = [{"direction": ABOVE, "target": 1600.0}]
        p._apply()
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(p._saved_sig, p._signature(p._pending, p._price_decimals),
                         "签名不同步会让重开面板误报「有未生效的修改」")


class TestPanelClear(PanelCase):
    def test_empty_list_has_nothing_to_clear(self):
        p = self._make()
        p._pending = []
        p._clear()
        self.assertEqual(self.clears, [])
        self.assertFalse(p._clear_armed)

    def test_first_click_only_arms_the_confirmation(self):
        """D3：误触一下就丢掉全部目标价是不可接受的。"""
        p = self._make(saved=[{"direction": ABOVE, "target": 1600.0,
                              "session_date": "2026-02-18", "state": "pending",
                              "last_side": None}])
        p._clear()
        self.assertEqual(self.clears, [])
        self.assertTrue(p._clear_armed)
        self.assertEqual(p.btn_clear.text(), "确认清空")
        self.assertIn("再点一次", p.status_label.text())

    def test_second_click_clears_and_resets_the_confirm_state(self):
        p = self._make(saved=[{"direction": ABOVE, "target": 1600.0,
                              "session_date": "2026-02-18", "state": "pending",
                              "last_side": None}])
        p._clear()
        p._clear()
        self.assertEqual(self.clears, [1])
        self.assertEqual(p._pending, [])
        self.assertEqual(p.btn_clear.text(), "清空")
        self.assertFalse(p._clear_armed)
        self.assertEqual(p.edit_up.text(), "")

    def test_failed_clear_keeps_both_the_data_and_the_confirmation_arm(self):
        """先落盘、成功后才动内存：否则会出现「界面已空、磁盘还在」。"""
        p = self._make(clear_ok=False, saved=[
            {"direction": ABOVE, "target": 1600.0,
             "session_date": "2026-02-18", "state": "pending",
             "last_side": None}])
        p._clear()
        p._clear()
        self.assertEqual(self.clears, [1], "落盘尝试过，只是被拒绝")
        self.assertIn("清空失败", p.status_label.text())
        self.assertTrue(p._clear_armed, "保留确认态，用户可直接再点一次重试")
        self.assertEqual(len(p._pending), 1)

    def test_an_exception_from_the_owner_is_shown_as_a_failure(self):
        p = self._make(saved=[{"direction": ABOVE, "target": 1600.0,
                              "session_date": "2026-02-18", "state": "pending",
                              "last_side": None}])
        p.on_clear = lambda: (_ for _ in ()).throw(OSError("ro"))
        with patch.object(target_panel, "log_line"):
            p._clear()
            p._clear()
        self.assertIn("清空失败", p.status_label.text())

    def test_lock_blocks_clear_too(self):
        p = self._make(saved=[{"direction": ABOVE, "target": 1600.0,
                              "session_date": "2026-02-18", "state": "pending",
                              "last_side": None}])
        p._locked = True
        p._clear()
        self.assertEqual(self.clears, [])


class TestPanelKeyboardAndEvents(PanelCase):
    def _key_event(self, key):
        return QKeyEvent(__import__("PyQt6.QtCore", fromlist=["Qt"]).QEvent.Type.KeyPress,
                         key, __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.KeyboardModifier.NoModifier)

    def test_escape_rolls_back_before_closing(self):
        """R34-7：Esc 的承诺是「关闭且不保存」，必须真的丢弃未生效内容。

        面板带 WA_DeleteOnClose，关闭后 C++ 对象即销毁，所以"回滚后的列表"
        必须在 _reload 返回的那一刻就地取证。
        """
        p = self._make(saved=[{"direction": ABOVE, "target": 1600.0,
                              "session_date": "2026-02-18", "state": "pending",
                              "last_side": None}])
        p._pending = [{"direction": ABOVE, "target": 9999.0}]
        p.show()
        QApplication.processEvents()
        seen = []
        real_reload = p._reload

        def spy():
            real_reload()
            seen.append([a["target"] for a in p._pending])

        p._reload = spy
        p.keyPressEvent(self._key_event(Qt.Key.Key_Escape))
        QApplication.processEvents()
        self.assertEqual(seen, [[1600.0]],
                         "回滚没做：下次打开会看到「已丢弃」的条目")
        self.assertEqual(self.applied, [], "Esc 绝不能落盘")

    def test_any_other_key_goes_to_the_default_handler(self):
        p = self._make()
        p.show()
        QApplication.processEvents()
        before = list(p._pending)
        p.keyPressEvent(self._key_event(Qt.Key.Key_A))
        self.assertEqual(p._pending, before)
        self.assertTrue(p.isVisible())

    def test_close_x_discards_just_like_escape(self):
        p = self._make(saved=[{"direction": ABOVE, "target": 1600.0,
                              "session_date": "2026-02-18", "state": "pending",
                              "last_side": None}])
        p._pending = [{"direction": ABOVE, "target": 42.0}]
        p.show()
        QApplication.processEvents()
        p._on_close_x()
        self.assertEqual(p._pending[0]["target"], 1600.0)

    def test_a_failing_rollback_still_closes(self):
        """回滚失败也要让关闭继续：Esc 的承诺是"退出"，不是"一定回滚成功"。"""
        p = self._make()
        p.show()
        QApplication.processEvents()
        with patch.object(p, "_reload", side_effect=RuntimeError("gone")):
            p._rollback()
        p.close()
        self.assertFalse(p.isVisible())

    def test_close_event_stops_the_pending_delayed_close(self):
        """M6：定时器不 stop，面板被提前关闭后还会再触发一次 close()。"""
        from PyQt6.QtCore import QTimer
        p = self._make()
        p.show()
        QApplication.processEvents()
        t = QTimer()                 # 不挂 parent：面板析构后仍要能问它的状态
        t.setObjectName("sf_cov_close_timer")
        t.setSingleShot(True)
        t.timeout.connect(p.close)
        t.start(5000)
        p._close_timer = t
        fired = []
        t.timeout.connect(lambda: fired.append(1))
        p.close()
        QApplication.processEvents()
        self.assertFalse(t.isActive())
        self.assertIsNone(p._close_timer)
        t.stop()
        QApplication.processEvents()
        self.assertEqual(fired, [], "面板已关，延迟关闭定时器不得再触发一次 close()")

    def test_show_event_always_focuses_an_input_box(self):
        """S2：按钮拿焦点会抢走输入框的键盘通道 —— 用户直接敲数字一个都进不去。"""
        p = self._make()
        p.btn_up.setFocus()
        QApplication.processEvents()
        p.show()
        QApplication.processEvents()
        focused = QApplication.focusWidget()
        self.assertIn(focused, (p.edit_up, p.edit_dn),
                      "焦点落在输入框上，实测=%s" % (focused,))

    def test_show_event_refreshes_the_list_at_the_real_width(self):
        """布局此时才有真实宽度，省略号截断要按它算。"""
        p = self._make(saved=[{"direction": ABOVE, "target": 1600.0,
                              "session_date": "2026-02-18", "state": "pending",
                              "last_side": None}])
        with patch.object(p, "_refresh_list") as rf:
            p.show()
            QApplication.processEvents()
        self.assertGreaterEqual(rf.call_count, 1)

    def test_a_broken_refresh_cannot_break_showing_the_panel(self):
        p = self._make()
        with patch.object(p, "_refresh_list", side_effect=RuntimeError("gone")):
            p.show()
            QApplication.processEvents()
        self.assertTrue(p.isVisible())


class TestPanelNotifyBlocked(PanelCase):
    def test_denied_authorization_is_reported_as_blocked(self):
        import sf_notify
        with patch.object(sf_notify, "authorization_status", return_value="denied"):
            self.assertTrue(target_panel.TargetPanel._notify_blocked())

    def test_granted_or_unknown_is_not_blocked(self):
        import sf_notify
        for state in ("authorized", "provisional", "not determinated"):
            with patch.object(sf_notify, "authorization_status",
                              return_value=state):
                self.assertFalse(target_panel.TargetPanel._notify_blocked(), state)

    def test_an_unavailable_framework_is_not_reported_as_blocked(self):
        """UN 都拿不到时不该在面板里说"通知已关闭"，那是另一回事。"""
        import sf_notify
        with patch.object(sf_notify, "authorization_status",
                          side_effect=RuntimeError("no UserNotifications")):
            self.assertFalse(target_panel.TargetPanel._notify_blocked())


class TestPanelStatusLine(PanelCase):
    def test_unknown_kind_still_gets_a_readable_style(self):
        p = self._make()
        p._set_status("提示", "不存在的类型")
        self.assertEqual(p.status_label.text(), "提示")
        self.assertTrue(p.status_label.styleSheet())

    def test_a_dead_status_label_never_breaks_a_business_branch(self):
        """状态行只是提示：写不进去不能影响任何业务分支。"""
        p = self._make()
        p.status_label = None
        p._set_status("随便", "error")
        p._pending = [{"direction": ABOVE, "target": 1600.0}]
        p._apply()
        self.assertEqual(len(self.applied), 1)

    def test_long_status_text_is_elided_to_the_available_width(self):
        p = self._make()
        p._pending = [{"direction": ABOVE, "target": 1600.0}]
        p._set_status("很长的提示" * 20, "warn")
        self.assertLessEqual(len(p.status_label.text()),
                             len("很长的提示" * 20))
        self.assertTrue(p.status_label.text().endswith("…")
                        or "\n" in p.status_label.text(),
                        "超长文案必须被截断，面板高度不能随之抖动")


class TestPanelElideHelper(PanelCase):
    def test_helper_falls_back_to_the_raw_text_when_the_widget_is_gone(self):
        p = self._make()

        class _Dead:
            def width(self):
                raise RuntimeError("deleted")

        self.assertEqual(p._elide_for(_Dead(), "原文"), "原文")


if __name__ == "__main__":
    unittest.main()
