# -*- coding: utf-8 -*-
"""实时股价 v1.2.32 两项 BUG 修复回归测试。

BUG1（拉伸顶部背景层撕裂/错位）：
    sf_glass.py GlassMixin._set_glass_mask 重建 CAShapeLayer 圆角 path/frame 时未禁用
    CoreAnimation 隐式动画（约 0.25s 插值追赶），拉伸瞬间玻璃层从旧圆角向新圆角平滑
    过渡，而主窗 Qt 圆角瞬时重绘 → 顶部背景层撕裂/错位，稳定后收敛对齐。
    修复：CATransaction.setDisableActions_(True) 包裹 setFrame_/setPath_；_sync_glass_frame
    的 setFrame_display_ 同源问题一并包裹。测试验证：
      - 源码级：两方法均含 CATransaction 禁用隐式动画包裹；
      - 功能级：_set_glass_mask 重建后 mask 的 frame 值正确、(begin/commit/setDisableActions) 配对执行。

BUG2（非交易时段股价显示 0 元、涨跌幅显示 0%）：
    以 v1.2.30 逻辑为基础加固三条链：
      a) _paint_price 有效分支追加 _disp_price > 0 守卫——任何路径把 _disp_price 置 0，
         渲染层也绝不画 0/0.00，一律落入 "--" 分支（停牌仍保留灰色徽标）；
      b) _apply_quote 在 new_price<=0 且无分时兜底时不更新 _disp_price/_disp_pct
         （保持 None 或上一有效值）；_quote_zero_fallback 非交易时段用分时最后一笔兜底；
      c) _paint_bottom 直绘 raw quote["high"]/["low"] 的 0 值是隐藏画 0 路径，已过滤为 "--"。

运行方式（在 StockFloat 目录）：
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

import stock_float_window as m
import sf_glass
import sf_quote
import sf_ui_render
from sf_ui_render import bottom_rows


def _quote(**over):
    q = {"name": "贵州茅台", "code": "600519", "price": "0.00", "pct": "0.00",
         "change": "0.00", "pre_close": "403.50", "high": "403.50", "low": "403.50",
         "volume": "0", "limit_up": "443.85", "limit_down": "363.15"}
    q.update(over)
    return q


_QUOTE_OK = _quote(price="410.00", pct="1.61", change="6.50", volume="12345")


class _WinBase(unittest.TestCase):
    """公共夹具：与 test_v130 相同的 patch 模式，构造后停掉后台线程。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", lambda *a, **k: ""),
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
        for _t in (getattr(self.win, "_quote_thread", None),
                   getattr(self.win, "_minute_thread", None)):
            if _t is not None:
                _t.join(timeout=1.5)
        self.win.show()

    def tearDown(self):
        self.win.close()
        for p in self._patches:
            p.stop()


# =====================================================================
# BUG1：拉伸顶部背景层撕裂/错位 —— CATransaction 禁用隐式动画
# =====================================================================
class TestBug1GlassTransaction(_WinBase):
    """CATransaction 包裹 mask 几何变更，消除隐式动画插值追赶。"""

    def _render_paint(self):
        self.win._need_redraw = True
        self.win.update()
        QApplication.processEvents()
        img = self.win.grab().toImage()
        QApplication.processEvents()
        return img

    def test_set_glass_mask_src_uses_catransaction(self):
        """源码级防回归：_set_glass_mask 必须用 CATransaction 禁隐式动画包裹 frame/path。"""
        import inspect
        src = inspect.getsource(sf_glass.GlassMixin._set_glass_mask)
        self.assertIn("CATransaction.setDisableActions_(True)", src,
                      "mask 几何变更必须禁用 CoreAnimation 隐式动画")
        self.assertIn("CATransaction.commit()", src, "begin 后必须 commit（含异常路径 finally）")
        self.assertIn("finally:", src, "commit 必须放 finally，异常也不留未闭合事务")
        # 创建/重建两处 setFrame_ 与 setPath_ 都必须位于禁用事务内
        self.assertGreaterEqual(src.count(".setFrame_("), 2, "创建+重建各需同步 mask frame")
        self.assertIn(".setPath_(path)", src)

    def test_sync_glass_frame_src_uses_catransaction(self):
        """源码级防回归：_sync_glass_frame 的 setFrame_display_ 同样禁隐式动画。"""
        import inspect
        src = inspect.getsource(sf_glass.GlassMixin._sync_glass_frame)
        self.assertIn("CATransaction.setDisableActions_(True)", src,
                      "玻璃窗 frame 同步也必须禁用隐式动画（拉伸拖影同源）")
        self.assertIn("setFrame_display_", src)

    def test_set_glass_mask_applies_frame_and_path(self):
        """功能级：_set_glass_mask 重建后 mask frame 值正确、CATransaction 配对执行。"""
        import types
        from unittest.mock import MagicMock
        mix = sf_glass.GlassMixin()
        mix._glass_mask_layer = None
        fake_vev = MagicMock()
        mask_layer = MagicMock()
        # 注入假 Quartz 模块（PyObjC 的 Quartz 为命名空间模块，patch 其属性不可靠）：
        # 函数内 `from Quartz import CGRectMake, CAShapeLayer, CATransaction` 会命中假模块，
        # 调用序列完全可断言；不影响真实 Quartz（仅本 with 作用域内替换 sys.modules）。
        fake_q = types.ModuleType("Quartz")
        fake_q.CATransaction = tx = MagicMock()
        fake_q.CAShapeLayer = cs = MagicMock()
        fake_q.CGRectMake = MagicMock(side_effect=lambda x, y, w, h: (x, y, w, h))
        fake_q.CGPathCreateWithRoundedRect = MagicMock(return_value=object())

        with patch.dict(sys.modules, {"Quartz": fake_q}):
            cs.layer.return_value = mask_layer
            mix._set_glass_mask(fake_vev, 240, 120)

        # mask layer 被创建 + 保存，两处 frame 同步 + 一次 path
        self.assertIsNotNone(mix._glass_mask_layer, "mask layer 应被创建并保存")
        self.assertIs(mix._glass_mask_layer, mask_layer)
        self.assertEqual(mask_layer.setFrame_.call_count, 2,
                         "创建分支 + 公共重建各同步一次 frame")
        for call in mask_layer.setFrame_.call_args_list:
            self.assertEqual(call[0][0], (0, 0, 240, 120),
                             "mask frame 值必须为 (0,0,w,h)")
        self.assertEqual(mask_layer.setPath_.call_count, 1, "每次重建都要设置一次圆角 path")
        # layer mask 挂载
        fake_vev.setWantsLayer_.assert_called_once_with(True)
        fake_vev.layer.return_value.setMask_.assert_called_once_with(mask_layer)
        # CATransaction 时序：begin 先于 setDisableActions_(True)，commit 收尾（各一次配对）
        seq = [c[0] for c in tx.mock_calls]
        self.assertIn("begin", seq)
        self.assertIn("commit", seq)
        self.assertLess(seq.index("begin"), seq.index("commit"),
                        "begin 必须先于 commit")
        tx.setDisableActions_.assert_called_once_with(True)


# =====================================================================
# BUG2：非交易时段股价/涨跌幅显示 0 —— 渲染层守卫 + 隐藏路径堵死
# =====================================================================
class TestBug2ZeroGuard(_WinBase):
    """渲染层不画 0：_paint_price >0 守卫 + _paint_bottom 0 值过滤为 --。"""

    def _render_paint(self):
        self.win._need_redraw = True
        self.win.update()
        QApplication.processEvents()
        img = self.win.grab().toImage()
        QApplication.processEvents()
        return img

    def test_paint_price_src_has_positive_guard(self):
        """源码级防回归：_paint_price 有效分支必须含 _disp_price > 0 守卫。"""
        import inspect
        src = inspect.getsource(m.FloatWindow._paint_price)
        self.assertIn("self._disp_price > 0", src,
                      "渲染层必须对 0 值做最终拦截（堵死绕过 _apply_quote 的路径）")

    def test_bottom_src_filters_zero_high_low(self):
        """底部 high/low 为 0/非法时必须输出 "--"（v1.2.72：源码断言 → 行为断言）。

        原实现用 inspect.getsource 断言 _paint_bottom 源码里含
        'hi_txt = ' / '"最高点", hi_txt' 等字符串。这类**源码级断言**有两个问题：
          1. 重构必碎 —— v1.2.72 把底部文本下沉为 bottom_rows() 后即失败；
          2. 它只能证明"源码里有这行字"，**不能证明行为正确**（字符串在也
             可能走不到）。改为直接调用 bottom_rows() 断言实际输出。
        """
        rows = bottom_rows(dict(_quote(name="贵州茅台", code="600519",
                                       pre_close="403.50", high="0", low="0")))
        self.assertIsNotNone(rows, "有 quote 时必须返回两行文本")
        row1, row2 = rows
        self.assertEqual([s[0] for s in row1], ["最大涨幅", "最高点"])
        self.assertEqual([s[0] for s in row2], ["最大跌幅", "最低点"],
                         "S3 行2 顺序须为 最大跌幅→最低点")
        for label, num, _color in row2:
            self.assertEqual(num, "--",
                             "%s 在 high/low 为 0 时必须显示 --，实际 %r" % (label, num))

    def test_apply_quote_zero_no_fallback_keeps_none(self):
        """a) price=0 且无分时兜底：_disp_price/_disp_pct 保持 None（渲染走 --）。"""
        with patch.object(m, "_is_trading_time", return_value=True):
            self.win._apply_quote(_quote(price="0.00", pct="0.00"))
        self.assertIsNone(self.win._disp_price, "0 值且无兜底不得落价格（渲染 --）")
        self.assertIsNone(self.win._disp_pct)

    def test_halt_suspension_zero_keeps_quote_for_badge(self):
        """b) 停牌（price=0）：不落价格但记录 quote 供停牌灰色徽标；渲染无异常。"""
        with patch.object(m, "_is_trading_time", return_value=True), \
             patch.object(m, "market_status", return_value=None):
            self.win._apply_quote(_quote(price="0.00", pct="0.00", volume="0"))
        self.assertIsNone(self.win._disp_price)
        self.assertIsNotNone(self.win.quote, "停牌首条 0 值行情需记录 quote 供徽标识别")
        # 渲染过程不抛异常即可（无有效价格分支画 --）
        img = self._render_paint()
        self.assertGreater(img.width(), 0)

    def test_empty_minute_no_data_same_as_zero(self):
        """b) 空分时（minute_points 为空）：price=0 依旧不落价格。"""
        self.win.minute_points = []
        with patch.object(m, "_is_trading_time", return_value=True):
            self.win._apply_quote(_quote(price="0.00", pct="0.00"))
        self.assertIsNone(self.win._disp_price)

    def test_normal_minute_fallback_shows_price(self):
        """b) 分时兜底正常：非交易时段用分时最后一笔兜底价显示（无 0 元）。"""
        self.win.minute_points = [("0930", "409.00"), ("1459", "409.80")]
        with patch.object(m, "_is_trading_time", return_value=False):
            self.win._apply_quote(_quote(price="0.00", pct="0.00", pre_close="403.50"))
        self.assertIsNotNone(self.win._disp_price)
        self.assertAlmostEqual(self.win._disp_price, 409.80, places=2,
                               msg="非交易时段应显示分时最后一笔兜底价")
        expect_pct = (409.8 - 403.5) / 403.5 * 100
        self.assertAlmostEqual(self.win._disp_pct, expect_pct, places=2)

    def test_normal_quote_still_renders(self):
        """d) 正常行情（price>0）：有效分支正常渲染，不被守卫误删。"""
        with patch.object(m, "_is_trading_time", return_value=True):
            self.win._apply_quote(dict(_QUOTE_OK))
        self.assertEqual(self.win._disp_price, 410.00)
        self.assertEqual(self.win._disp_pct, 1.61)
        img = self._render_paint()
        self.assertGreater(img.width(), 0)

    def test_zero_disp_forced_skips_roll_text(self):
        """c) 隐路径堵死：即便有人绕过 _apply_quote 把 _disp_price 塞 0，
           渲染层也走 "--" 分支（有效分支的逐位滚动不触发）。"""
        calls = []
        orig = self.win._draw_roll_text
        self.win.quote = dict(_quote(name="贵州茅台", code="600519"))
        self.win._disp_price = 0.0
        self.win._disp_pct = 0.0
        with patch.object(self.win, "_draw_roll_text",
                          side_effect=lambda *a, **k: calls.append(k.get("roll_key"))):
            self._render_paint()
        self.assertNotIn("price", calls, "_disp_price=0 时不得渲染价格滚动（必须走 -- 分支）")
        self.assertNotIn("pct", calls)

    def test_bottom_zero_high_low_renders_dash(self):
        """c) 底部隐藏路径：quote 的 high/low 为 0 时，输出 -- 而非 0。

        v1.2.72：原实现断言 _paint_bottom 源码含 "hi_txt = "，属源码级断言
        （重构必碎且不能证明行为）。改为断言 bottom_rows() 的实际输出。
        """
        self.win.quote = dict(_quote(name="贵州茅台", code="600519",
                                     pre_close="403.50", high="0", low="0"))
        self.win._disp_price = None
        self.win._disp_pct = None
        rows = bottom_rows(self.win.quote)
        self.assertIsNotNone(rows)
        for label, num, _color in rows[1]:
            self.assertEqual(num, "--", "0 值 high/low 必须渲染为 --，实际 %r" % num)
        img = self._render_paint()
        self.assertGreater(img.width(), 0, "high/low=0 时底部仍可正常渲染")


if __name__ == "__main__":
    unittest.main()
