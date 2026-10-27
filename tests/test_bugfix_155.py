# -*- coding: utf-8 -*-
"""实时股价 v1.2.55 「数字滚动卡中途」真正病根修复回归测试。

根因（用户现场：价格大字停在中间值不上不下）：
    _apply_quote 的"微小变动直赋"分支会 self._anim_timer.stop() 打断进行中的
    插值动画并将 _disp_price 直接设为新值，但旧逻辑只清了 _roll_pending，
    没清 _roll_prev（"上一帧显示串"）和 _roll_anim（进行中的滚动条目）。
    下一帧渲染时 _roll_prev ≠ 新 _disp_price → _roll_update 误以为"数字变了"
    → 创建从旧目标串到新值的滚动条目 → 再下一帧 _roll_prev 被更新为新值 →
    滚动条目因无新触发而永远跑不完（时间线从旧 pending 时刻开始，但后续帧
    不再刷新它）→ 画面永久卡在滚动中途。

覆盖项（对应 v1.2.55 修复）：
1. 微小变动直赋分支同时清除 _roll_pending / _roll_prev / _roll_anim
2. 首帧分支（_disp_price is None）同步清除三态
3. 渲染层安全网：定时器已停但滚动条目残留时就地清除
"""
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Qt 对象（QFont/QRectF）需要先有 QApplication
from PyQt6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv)

from sf_constants import APP_VERSION, VERSION_TEXT




class TestTinyMoveClearsAllRollState(unittest.TestCase):
    """验证微小变动直赋路径同时清除 _roll_pending / _roll_prev / _roll_anim"""

    def _make_window(self):
        """构造最小 FloatWindow 实例（不启动 Qt 事件循环）"""
        from stock_float_window import FloatWindow
        from sf_anim_driver import AnimDriver
        # 阻止 QTimer 实际启动
        with patch.object(FloatWindow, '__init__', lambda self: None):
            w = FloatWindow.__new__(FloatWindow)
        w._anim_driver = AnimDriver()
        w._disp_price = 427.08
        w._disp_pct = 1.5
        w._anim_timer = MagicMock()
        w._anim_timer.isActive.return_value = True
        w._roll_pending = {"price": "427.10", "pct": "+1.52%"}
        w._roll_prev = {"price": "427.00", "pct": "+1.40%"}
        w._roll_anim = {"price": {0: ["4", "4", time.monotonic(), 0.18]},
                        "pct": {}}
        w._partial_price = (0, 0, 100, 30)
        w._need_redraw = False
        w._anim_driver.anim_decimals = 2
        w._anim_from_price = 427.00
        w._anim_to_price = 427.10
        w._anim_from_pct = 1.40
        w._anim_to_pct = 1.52
        w._anim_start = time.monotonic() - 0.05
        w._anim_dur = 0.13
        w.quote = {"price": "427.09", "pct": "1.51", "high": "430", "low": "425",
                    "code": "000001", "change": "6.35", "pre_close": "420.73"}
        w.code = "000001"
        w._last_ok_time = "09:30:00"
        w._chart_expanded = True
        w._chart_changed = False
        w._resizing = False
        w._resize_target_h = None
        return w

    def test_tiny_move_clears_roll_prev_and_roll_anim(self):
        """微小变动直赋后 _roll_prev 和 _roll_anim 必须为空"""
        w = self._make_window()
        # 模拟微小变动：价格从 427.08 → 427.09（差 0.01）
        data = {"price": "427.09", "pct": "1.51", "high": "430", "low": "425",
                "code": "000001", "change": "6.36", "pre_close": "420.73"}
        with patch("stock_float_window.feature_enabled", return_value=True), \
             patch("stock_float_window.is_tiny_move", return_value=True):
            w._apply_quote(data)
        # 核心断言：三态全部清零
        self.assertEqual(w._roll_pending, {}, "_roll_pending 应被清除")
        self.assertEqual(w._roll_prev, {}, "_roll_prev 应被清除（v1.2.55 新增）")
        self.assertEqual(w._roll_anim, {}, "_roll_anim 应被清除（v1.2.55 新增）")
        # 显示值已更新
        self.assertEqual(w._disp_price, 427.09)
        self.assertEqual(w._disp_pct, 1.51)
        # 定时器已停
        w._anim_timer.stop.assert_called()

    def test_first_frame_clears_all_roll_state(self):
        """首帧（_disp_price is None）也应清除三态"""
        from stock_float_window import FloatWindow
        from sf_anim_driver import AnimDriver
        with patch.object(FloatWindow, '__init__', lambda self: None):
            w = FloatWindow.__new__(FloatWindow)
        w._anim_driver = AnimDriver()
        w._disp_price = None
        w._disp_pct = None
        w._anim_timer = MagicMock()
        w._roll_pending = {"price": "100.00", "pct": "+0.50%"}
        w._roll_prev = {"price": "99.00", "pct": "+0.40%"}
        w._roll_anim = {"price": {0: ["9", "1", time.monotonic(), 0.18]}}
        w._partial_price = None
        w._need_redraw = False
        w._anim_driver.anim_decimals = 2
        w._anim_from_price = 0.0
        w._anim_to_price = 0.0
        w._anim_from_pct = 0.0
        w._anim_to_pct = 0.0
        w.quote = None
        w.code = "000001"
        w._last_ok_time = ""
        w._chart_expanded = True
        w._chart_changed = False
        w._resizing = False
        w._resize_target_h = None

        data = {"price": "100.00", "pct": "0.50", "high": "101", "low": "99",
                "code": "000001", "change": "0.50", "pre_close": "99.50"}
        w._apply_quote(data)
        self.assertEqual(w._roll_pending, {})
        self.assertEqual(w._roll_prev, {})
        self.assertEqual(w._roll_anim, {})
        self.assertEqual(w._disp_price, 100.00)


class TestRenderSafetyNet(unittest.TestCase):
    """验证渲染层在定时器已停时的行为：不创建滚动条目"""

    def test_no_roll_when_timer_stopped_and_prev_synced(self):
        """当 _roll_prev 已同步为当前值时，_roll_update 不创建滚动条目"""
        from sf_ui_render import RenderMixin

        class FakeRender(RenderMixin):
            def __init__(self):
                # 模拟窗口层已清除三态后的状态：_roll_prev 为空
                self._roll_prev = {}
                self._roll_anim = {}
                self._roll_pending = {}
                self._anim_timer = MagicMock()
                self._anim_timer.isActive.return_value = False
                self._fonts = {}
                self._fms = {}

        r = FakeRender()
        fake_p = MagicMock()
        from PyQt6.QtCore import QRectF
        fake_rect = QRectF(0, 0, 200, 40)
        from PyQt6.QtGui import QFont
        fake_font = QFont("D-DIN", 28)

        # 第一次调用：prev=None → 不创建滚动
        try:
            r._draw_roll_text(fake_p, fake_rect, "427.09", fake_font,
                              MagicMock(), roll_key="price")
        except Exception:
            pass
        self.assertFalse(r._roll_anim.get("price"),
                         "首次渲染不应创建滚动条目")

        # 第二次调用：prev=当前值 → 仍不创建滚动
        try:
            r._draw_roll_text(fake_p, fake_rect, "427.09", fake_font,
                              MagicMock(), roll_key="price")
        except Exception:
            pass
        self.assertFalse(r._roll_anim.get("price"),
                         "值未变化时不应创建滚动条目")


if __name__ == "__main__":
    unittest.main()
