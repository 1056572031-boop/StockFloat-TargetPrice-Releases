# -*- coding: utf-8 -*-
"""v1.2.95 走查回归：宽度自适应失效 + autowidth 日志刷屏（P0）。

缺陷根因
--------
``stock_float_window._bottom_texts()`` 直接调用 ``bottom_rows(self.quote)``，
但本模块**从未导入** ``bottom_rows``（它定义在 ``sf_ui_render.py``）。运行时
抛 ``NameError``，被 ``_auto_width()`` 里的 ``except Exception`` 吞掉并
``log_line("autowidth error: %r")``。两个后果：

  1. ``_content_min_width()`` 永远走不到 → 窗口宽度停在默认值，长股票名/大数字
     被裁切或留白异常（实测长文本 quote 下宽度 200 vs 应得 227）。
  2. ``_auto_width()`` 由 ``_poll`` 每轮调用（交易时段 ~50ms/轮），异常 100%
     必抛 → **20 条/秒**错误日志，实测用户 debug.log 三代轮转里该类错误
     12.3 万条，把真故障日志全挤掉。

907 个测试全绿却没发现，原因是：
  - 该路径被 ``except`` 吞掉，不抛到测试层；
  - **此前 tests/ 下没有任何一个测试覆盖 ``_auto_width`` / ``_bottom_texts``**
    （可自行 grep 验证），宽度自适应逻辑属于零覆盖区域。

本文件建立的回归防线（任一被破坏即失败）
----------------------------------------
  A. 模块确实绑定了 ``bottom_rows``（快速失败，给出可操作的报错）；
  B. ``_bottom_texts()`` 不抛异常且返回 (row1, row2) × 2 段 × (标签,数字,颜色)；
  C. ``_auto_width()`` 连续多轮不产生任何 "autowidth error" 日志；
  D. 宽度随内容单调变化：长文本 > 短文本，且短文本能收敛回窄宽度。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

import stock_float_window as m
import sf_quote


def _quote(**over):
    """与 test_v132 同源的 quote 夹具字段集。"""
    q = {"name": "贵州茅台", "code": "600519", "price": "0.00", "pct": "0.00",
         "change": "0.00", "pre_close": "403.50", "high": "403.50",
         "low": "403.50", "volume": "0",
         "limit_up": "443.85", "limit_down": "363.15"}
    q.update(over)
    return q


# 短文本：窄价、短名 —— 期望收敛到较窄宽度
_SHORT = _quote(name="ABC", code="1", price="1.00", pct="0.01", change="0.01",
                pre_close="1.00", high="1.01", low="0.99", volume="1",
                limit_up="1.10", limit_down="0.90")
# 长文本：长名 + 5 位价格 + 6 位高低点 —— 期望明显撑宽
_LONG = _quote(name="贵州茅台贵州茅台贵州茅台", code="600519",
               price="1888.88", pct="188.88", change="1888.88",
               pre_close="1000.00", high="188888.88", low="177777.77",
               volume="999999999", limit_up="188888.88", limit_down="111111.11")


class _WinBase(unittest.TestCase):
    """公共夹具：构造 FloatWindow 并停掉后台线程/定时器（同 test_v132 模式）。"""

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

    def tearDown(self):
        self.win.close()
        for p in self._patches:
            p.stop()

    # -- 辅助 ----------------------------------------------------------
    def _width_for(self, quote):
        """给定 quote，跑一次 _auto_width 后返回窗口宽度。"""
        self.win.quote = dict(quote)
        self.win._auto_width()
        return self.win.width()

    def _collect_autowidth_errors(self, quote, periods=20):
        """模拟 N 个轮询周期，收集 _auto_width 产生的日志文本。"""
        logged = []
        orig = m.log_line
        m.log_line = lambda *a, **k: logged.append(a[0] if a else "")
        try:
            self.win.quote = dict(quote)
            for _ in range(periods):
                self.win._auto_width()
        finally:
            m.log_line = orig
        return [x for x in logged if "autowidth error" in x]


class TestBottomRowsImport(_WinBase):
    """A：模块必须绑定 bottom_rows —— 缺失会让 B/C/D 全线崩，这里先给清晰报错。"""

    def test_module_binds_bottom_rows(self):
        self.assertTrue(
            hasattr(m, "bottom_rows"),
            "stock_float_window 必须导入 bottom_rows（定义在 sf_ui_render）；"
            "缺失会让 _bottom_texts() 抛 NameError 并被 _auto_width 静默吞掉，"
            "表现为宽度永不自适应 + 每轮询周期一条错误日志（~20 条/秒）")


class TestBottomTexts(_WinBase):
    """B：_bottom_texts() 不抛异常且结构正确。"""

    def test_bottom_texts_returns_two_rows_of_two_segments(self):
        self.win.quote = _quote(price="410.00", pct="1.61", change="6.50",
                                high="1888.88", low="1777.77", volume="123456")
        texts = self.win._bottom_texts()          # 缺导入时此行抛 NameError
        self.assertIsNotNone(texts, "有 quote 时 _bottom_texts() 不得返回 None")
        self.assertEqual(len(texts), 2, "应返回 (row1, row2) 两行")
        row1, row2 = texts
        for tag, row in (("row1", row1), ("row2", row2)):
            self.assertEqual(len(row), 2, "%s 应为 2 段" % tag)
            for seg in row:
                self.assertEqual(len(seg), 3,
                                 "%s 每段须为 (标签, 数字, 颜色) 三元组，实际 %r"
                                 % (tag, seg))

    def test_bottom_texts_matches_shared_source(self):
        """与 sf_ui_render.bottom_rows 同源 —— 防止再次出现双份实现漂移。"""
        q = _quote(price="410.00", high="1888.88", low="1777.77")
        self.win.quote = dict(q)
        self.assertEqual([(s[0], s[1]) for s in self.win._bottom_texts()[0]],
                         [(s[0], s[1]) for s in m.bottom_rows(q)[0]],
                         "_bottom_texts 必须薄委托给 sf_ui_render.bottom_rows（唯一实现）")


class TestAutoWidthNoLogFlood(_WinBase):
    """C：_auto_width 连续多轮不得产生 autowidth error（否则 20 条/秒刷屏）。"""

    def test_no_autowidth_error_over_many_poll_cycles(self):
        errs = self._collect_autowidth_errors(_LONG, periods=20)
        self.assertEqual(
            errs, [],
            "20 个轮询周期内不应有任何 autowidth error（否则交易时段 20 条/秒刷屏），"
            "实际 %d 条，首条：%s" % (len(errs), errs[0] if errs else ""))

    def test_no_autowidth_error_when_quote_minimal(self):
        """极简 quote（缺 pre_close/hi/lo 边界）同样不得刷日志。"""
        errs = self._collect_autowidth_errors({"name": "X", "code": "1"}, periods=20)
        self.assertEqual(errs, [], "极简 quote 下也不得刷 autowidth error，实际 %r" % (errs[:2],))


class TestAutoWidthAdapts(_WinBase):
    """D：宽度真的随内容变化 —— 长文本撑宽、短文本收敛。"""

    def test_long_and_short_content_keep_fixed_compact_width(self):
        """用户指定主界面整体缩短 20%，内容变化不得再把窗口反向撑宽。"""
        self.assertEqual(self._width_for(_SHORT), m.W)
        self.assertEqual(self._width_for(_LONG), m.W)

    def test_width_converges_to_compact_width(self):
        """若旧状态留下更宽窗口，收到行情后应收敛回固定窄版宽度。"""
        self.win.setFixedWidth(m.W + 40)
        self.assertEqual(self._width_for(_LONG), m.W)


if __name__ == "__main__":
    unittest.main()
