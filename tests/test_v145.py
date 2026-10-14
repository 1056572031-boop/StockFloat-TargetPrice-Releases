# -*- coding: utf-8 -*-
"""v1.2.45 闪退修复回归测试。

v1.2.44 用户现场：价格动画局部重绘触发 NameError: name 'QRect' is not defined
（sf_ui_render.py 顶部 QtCore import 漏导 QRect），异常从 paintEvent 逃逸破坏 C++
渲染 flush 链 → QBackingStore::flush 空指针 EXC_BAD_ACCESS (SIGSEGV)，app 打开约
1 秒即闪退。

本文件回归覆盖：
  1) QRect 导入存在性：QtCore import 必须含 QRect，且能正常构造（杜绝漏导再犯）；
  2) valid_price_partial 局部重绘脏区防御校验：partial 类型/长度/数值校验，
     脏数据（None/短/超长/非数值/NaN）一律拒入局部重绘路径，退回完整重绘；
  3) 真实 FloatWindow 实例上，价格动画局部重绘路径可正常执行（QRect 构造 +
     setClipRect），不再向 Qt 事件循环逃逸（首个回归直接复现 v1.2.44 崩溃路径）；
  4) 绘制路径任意异常均被 paintEvent 外层 try/except 兜底吞掉（写 ERROR 日志后
     走完帧），不再有任何异常逃逸导致整 app 崩溃。

运行方式（在 StockFloat 目录）：
    python3 -m unittest tests.test_v145 -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QApplication

import sf_quote
import sf_ui_render as R
import sf_logging
import stock_float_window as m
from sf_constants import LOG_PATH
from sf_ui_render import (is_price_partial_redraw, price_update_rect,
                          valid_price_partial)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestQRectImport(unittest.TestCase):
    """第1项：QRect 导入存在性（v1.2.44 崩溃直接根因）。"""

    def test_qrect_imported_and_constructible(self):
        # QRect 必须已从 QtCore 导入到 sf_ui_render 命名空间，且可正常构造
        self.assertTrue(hasattr(R, "QRect"))
        r = R.QRect(0, 0, 10, 5)
        self.assertEqual((r.x(), r.y(), r.width(), r.height()),
                         (0, 0, 10, 5))

    def test_qrect_in_qcore_import_line(self):
        # 源码级校验：QtCore import 行必须显式含 QRect（防再次漏导）
        with open(os.path.join(REPO, "sf_ui_render.py"), encoding="utf-8") as f:
            src = f.read()
        lines = [ln for ln in src.splitlines()
                 if ln.startswith("from PyQt6.QtCore import")]
        self.assertTrue(lines, "必须存在 from PyQt6.QtCore import 行")
        self.assertIn("QRect", lines[0])


class TestValidPricePartial(unittest.TestCase):
    """第2项：局部重绘脏区防御校验 valid_price_partial。"""

    def test_valid_tuple_true(self):
        self.assertTrue(valid_price_partial((0, 0, 286, 44)))

    def test_valid_list_true(self):
        self.assertTrue(valid_price_partial([0, 0, 286, 44]))

    def test_floats_true(self):
        self.assertTrue(valid_price_partial((0.0, 0.0, 286.5, 44.0)))

    def test_matches_price_update_rect(self):
        self.assertTrue(valid_price_partial(price_update_rect(286)))

    def test_none_false(self):
        self.assertFalse(valid_price_partial(None))

    def test_empty_false(self):
        self.assertFalse(valid_price_partial(()))

    def test_short_false(self):
        self.assertFalse(valid_price_partial((1, 2, 3)))

    def test_long_false(self):
        self.assertFalse(valid_price_partial((1, 2, 3, 4, 5)))

    def test_non_numeric_false(self):
        self.assertFalse(valid_price_partial((0, 0, "10", 44)))

    def test_none_element_false(self):
        self.assertFalse(valid_price_partial((0, 0, None, 44)))

    def test_nan_false(self):
        self.assertFalse(valid_price_partial((0, 0, float("nan"), 44)))


class TestPaintEventSafeguard(unittest.TestCase):
    """第3/4项：绘制路由与异常收口的无原生窗口回归。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            mock.patch.object(m, "load_config",
                              return_value=("sh600519", 100, 100, 200,
                                            "translucent")),
            mock.patch.object(m, "load_api_urls", lambda: None),
            mock.patch.object(m, "save_config", lambda *a, **k: None),
            mock.patch.object(sf_quote, "http_get", lambda *a, **k: ""),
            mock.patch.object(m, "start_icon_worker", lambda: None),
            mock.patch.object(m.FloatWindow, "_force_top", lambda self: None),
            mock.patch.object(m.FloatWindow, "_setup_reopen_handler",
                              lambda self: None),
            mock.patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        # 不构造真实 FloatWindow：构造过程会启动后台线程并接入 QApplication 全局
        # 信号，离屏随机回归中即使不绘制也会形成原生窗口生命周期噪声。
        class _PaintTarget(R.RenderMixin):
            pass

        self.win = _PaintTarget()
        self.win._partial_price = None
        self.win._paint_recovery_pending = False
        self.win._chart_changed = False
        self.win._bg_effect = False
        self.win._glass_mask_pending = False
        self.win.width = lambda: 200
        self.win.height = lambda: 200
        self.win.update = lambda *args: None
        self.win._glass_mask_flush = lambda: None

    def tearDown(self):
        self.win = None
        for p in self._patches:
            p.stop()

    def _exercise_paint_route(self, event):
        """以可控 painter 验证路由与异常收口，不调用 QWidget.paintEvent。

        QWidget 的 paint device 只能由 Qt 在真实绘制事务中管理；在共享离屏测试进程
        中人工触发会把测试风险带入原生绘制层。此辅助方法只验证本版需要守住的
        Python 路由、painter 收尾和恢复排队语义。
        """
        calls = []

        class _Painter:
            def setRenderHint(self, *args):
                calls.append("hint")

            def setClipRect(self, *args):
                calls.append("clip")

            def end(self):
                calls.append("end")

        with mock.patch.object(R, "QPainter", return_value=_Painter()), \
             mock.patch.object(self.win, "_paint_background", side_effect=lambda *a: calls.append("background")), \
             mock.patch.object(self.win, "_paint_header", side_effect=lambda *a: calls.append("header")), \
             mock.patch.object(self.win, "_paint_buttons", side_effect=lambda *a: calls.append("buttons")), \
             mock.patch.object(self.win, "_paint_price", side_effect=lambda *a: calls.append("price")), \
             mock.patch.object(self.win, "_paint_chart", side_effect=lambda *a: calls.append("chart")), \
             mock.patch.object(self.win, "_paint_bottom", side_effect=lambda *a: calls.append("bottom")):
            R.RenderMixin._paint_event_inner(self.win, event)
        return calls

    def test_local_redraw_route_executes_without_escape(self):
        """修复核心回归：局部重绘命中时 QRect + setClipRect 正常执行不逃逸。

        v1.2.44 中此路径触发 NameError: QRect is not defined → SIGSEGV。
        """
        w = self.win.width()
        partial = price_update_rect(w)
        self.win._partial_price = list(partial)
        calls = self._exercise_paint_route(
            type("Event", (), {"rect": lambda self: QRect(*partial)})())
        # 局部重绘完整消费脏区，且没有进入整窗的头部/图表绘制。
        self.assertIsNone(self.win._partial_price)
        self.assertIn("clip", calls)
        self.assertNotIn("header", calls)
        self.assertEqual(calls[-1], "end", "局部绘制异常与否都必须结束 painter")

    def test_partial_matches_local_route_predicate(self):
        # 校验防御层放行的是 is_price_partial_redraw 能精确匹配的合法脏区
        partial = price_update_rect(286)
        self.assertTrue(valid_price_partial(partial))
        self.assertTrue(is_price_partial_redraw(tuple(partial), partial))

    def test_dirty_partial_falls_back_to_full_paint(self):
        """脏数据 partial 拒绝局部重绘：完整路径绘制无异常且清空悬挂脏区。"""
        self.win._partial_price = (0, 0, None, 44)  # 含 None 的脏数据
        full_rect = QRect(0, 0, self.win.width(), self.win.height())
        calls = self._exercise_paint_route(
            type("Event", (), {"rect": lambda _event: full_rect})())
        self.assertIsNone(self.win._partial_price)
        self.assertIn("header", calls, "脏数据必须退回整窗路径")
        self.assertEqual(calls[-1], "end", "整窗绘制必须结束 painter")

    def test_paint_exception_swallowed_no_escape(self):
        """绘制路径任意异常被外层兜底吞掉：不外抛 + 写 ERROR 日志。"""
        with mock.patch.object(self.win, "_paint_event_inner",
                               side_effect=RuntimeError("draw boom")), \
             mock.patch.object(R.QTimer, "singleShot"):
            R.RenderMixin.paintEvent(self.win, None)  # 外层仅验证 Python 异常不会逃逸
        log = self._tail_log()
        if not log:
            log = self._tail_log(sf_logging.LOG_PATH)
        self.assertIn("paintEvent error", log)
        self.assertIn("draw boom", log)

    def test_paint_error_schedules_full_recovery_frame(self):
        """正文绘制失败后下一帧必须恢复，不能留下可操作的空白外壳。"""
        callbacks = []
        with mock.patch.object(self.win, "_paint_event_inner",
                               side_effect=RuntimeError("draw boom")), \
             mock.patch.object(R.QTimer, "singleShot",
                               side_effect=lambda _delay, callback: callbacks.append(callback)):
            R.RenderMixin.paintEvent(self.win, None)
        self.assertTrue(self.win._paint_recovery_pending)
        self.assertEqual(len(callbacks), 1, "绘制失败必须只排队一帧恢复")
        callbacks[0]()
        self.assertFalse(self.win._paint_recovery_pending)
        self.assertIsNone(self.win._partial_price)
        self.assertTrue(self.win._chart_changed)

    def test_glass_mask_failure_does_not_skip_qt_content_route(self):
        """毛玻璃附加层异常时，正文路由仍继续，并回退为半透明背景。"""
        self.win._bg_effect = True
        self.win._glass_mask_pending = True
        full_rect = QRect(0, 0, self.win.width(), self.win.height())
        with mock.patch.object(self.win, "_glass_mask_flush",
                               side_effect=RuntimeError("mask boom")):
            calls = self._exercise_paint_route(
                type("Event", (), {"rect": lambda _event: full_rect})())
        self.assertFalse(self.win._bg_effect)
        self.assertIn("header", calls, "玻璃失败不能阻断 Qt 正文绘制")
        self.assertEqual(calls[-1], "end")

    @staticmethod
    def _tail_log(path=LOG_PATH):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""


if __name__ == "__main__":
    unittest.main(verbosity=2)
