# -*- coding: utf-8 -*-
"""实时股价 v1.2.34 切股右侧宽度闪动 BUG 回归测试。

BUG（切换股票时悬浮窗右侧宽度闪动一下）：
    apply_stock 切股瞬间将 self.quote 置 None（过渡期）+ update() 触发 _poll，
    _poll 每轮调用 _auto_width → _content_min_width 在 quote 为 None 时只返回
    最小宽 base(W)，随后 setFixedWidth 把窗口临时收窄到最小宽；新股票行情到达
    （_apply_quote 写回 quote）后再放宽到内容所需宽度 —— 右侧边缘"先收后放"闪动。

修复：
    _auto_width 在 quote 为 None（切股过渡期/启动早段）时直接 return，保持当前
    宽度；行情到达后再正常自适应。构造期初始宽度即最小宽 base(W)，因此启动路径
    行为不变。

测试验证：
    - 切股过渡期（quote=None 且当前宽度 > W）：_auto_width 不改变窗口宽度；
    - 行情到位后（quote 有值且内容需要更宽）：_auto_width 正常放宽生效（守卫不误伤）。
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
    q = {"name": "贵州茅台", "code": "600519", "price": "0.00", "pct": "0.00",
         "change": "0.00", "pre_close": "403.50", "high": "403.50", "low": "403.50",
         "volume": "0", "limit_up": "443.85", "limit_down": "363.15"}
    q.update(over)
    return q


_QUOTE_OK = _quote(price="410.00", pct="1.61", change="6.50", high="415.00",
                   low="402.00", volume="12345")


class _WinBase(unittest.TestCase):
    """公共夹具：与 test_v130/v132 相同的 patch 模式，构造后停掉后台线程。"""

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
        for p in self._patches:
            p.stop()


class TestWidthFlashOnSwitchStock(_WinBase):
    """切股过渡期宽度不闪：quote=None 时 _auto_width 保持当前宽度。"""

    def test_transition_keeps_current_width_when_quote_is_none(self):
        """quote=None（切股过渡期）且当前宽度 >> 最小宽：_auto_width 不得收窄。"""
        self.win.quote = None
        # 模拟窗口已因前一股票内容自适应到一个较宽的值，且明显大于最小宽 base(W)
        wide = m.W + 60
        self.win.setFixedWidth(wide)
        self.win._auto_width()
        self.assertEqual(self.win.width(), wide,
                         "切股过渡期必须保持当前宽度，禁止收窄到最小宽 base(W)")

    def test_transition_applies_to_apply_stock_flow(self):
        """apply_stock 清空 quote 后 _auto_width 不改变宽度（端到端切股流程）。"""
        wide = m.W + 60
        self.win.setFixedWidth(wide)
        self.win.quote = _QUOTE_OK
        self.win.code = "sh600519"
        # 规避 apply_stock 内部 _force_fetch 启动真实拉取线程（本测试只验证过渡态）
        with patch.object(m.FloatWindow, "_force_fetch", lambda self: None):
            self.win.apply_stock("sh600519")
        self.assertIsNone(self.win.quote, "apply_stock 应清空 quote（切股过渡期）")
        self.win._auto_width()
        self.assertEqual(self.win.width(), wide,
                         "切股过渡期结束时宽度不得被 _auto_width 收窄")

    def test_normal_auto_width_still_works_with_quote(self):
        """行情到位后（quote 有值）_auto_width 正常放宽生效，守卫不误伤。

        为什么注入 _content_min_width 而不是依赖真实内容宽度：
        _content_min_width 基于 QFontMetrics，而它在 offscreen / cocoa 两个
        Qt 平台下结果不同（cocoa 字体更窄）。原断言假定「贵州茅台 + 410.00
        的内容宽度必然 > 最小宽 W」，但 cocoa 下内容恰好装得进 200，
        _auto_width 依 abs(nw-cur)>=3 的阈值判定不调整 —— 这是**正确行为**，
        属断言过度指定（v1.2.63 发版核查时在 cocoa 下暴露）。
        改为注入确定宽度，只锁定「quote 有效时守卫不阻断放宽」这一契约本身。
        """
        self.win.quote = _QUOTE_OK
        self.win._disp_price = 410.0
        self.win._disp_pct = 1.61
        # 先固定到一个较窄的宽度，保证 need > cur 时必然触发放宽
        self.win.setFixedWidth(m.W)
        with patch.object(m.FloatWindow, "_content_min_width",
                          lambda self: m.W + 40):
            self.win._auto_width()
        self.assertEqual(self.win.width(), m.W + 40,
                         "quote 有效时 _auto_width 应按内容放宽宽度（守卫不误伤）")

    def test_auto_width_keeps_width_when_delta_below_threshold(self):
        """宽度差 < 3px 时不调整（防抖阈值）：守卫之外的另一半契约。"""
        self.win.quote = _QUOTE_OK
        self.win.setFixedWidth(m.W)
        with patch.object(m.FloatWindow, "_content_min_width",
                          lambda self: m.W + 2):
            self.win._auto_width()
        self.assertEqual(self.win.width(), m.W,
                         "宽度差小于阈值(3px)时不应调整，避免边缘抖动")


if __name__ == "__main__":
    unittest.main(verbosity=2)
