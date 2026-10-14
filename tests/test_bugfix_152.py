# -*- coding: utf-8 -*-
"""实时股价 v1.2.52 「数字滚动卡中途」彻底修复回归测试。

根因（用户现场：价格大字冻结在旧值、分时图照常更新）：
    _apply_quote 的"微小变动直赋"分支会 self._anim_timer.stop() 打断进行中的
    插值动画，但不清渲染层预置的逐位滚动目标 _roll_pending —— 渲染层
    _draw_roll_text 在 pending 非空时一律按该目标串绘制，于是价格被永久锁在
    上一次动画的目标值；A股 0.01 元单档跳动受浮点误差影响大多被判为微小变动
    （如 410.08→410.09 差值=0.00999... < 0.01），后续行情全部走直赋分支，
    锁定状态无法自愈。行情轮询 0.1s 周期内几乎必被触发。

覆盖项（对应 v1.2.52 修复）：
1. 微小变动直赋分支清除 _roll_pending（核心泄漏点）
2. 首帧分支（_disp_price is None）防御性清除残留
3. 渲染层兜底：动画定时器已停而 pending 残留 → 绘制时就地清除，显示跟随 _disp_*
4. 兜底清除后滚动动画恢复正常触发（不残留死状态）
5. 正常动画期间（定时器运行中）兜底不误伤 pending

运行方式（在 StockFloat 目录）：
    python3 -m unittest tests.test_bugfix_152 -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

import stock_float_window as m
import sf_quote

_QUOTE = {"name": "贵州茅台", "code": "600519", "price": "410.00", "pct": "1.61",
          "change": "6.50", "pre_close": "403.50", "high": "412.00", "low": "400.00",
          "volume": "12345", "limit_up": "443.85", "limit_down": "363.15"}


def _fake_http_get(url, **kw):
    if "web.ifzq.gtimg.cn" in url:
        return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                '"data":["0930 1510.0 100 1000"]}}}}')
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


class TestRollPendingLeak(unittest.TestCase):
    """v1.2.52：_roll_pending 泄漏修复回归（状态机断言，不依赖像素）。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", _fake_http_get),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()
        self.win.timer.stop()
        self.win.top_timer.stop()
        self.win._flash_timer.stop()
        self.win.show()
        self.win._apply_quote(dict(_QUOTE))   # 首帧：直接显示 410.00
        self.win._anim_timer.stop()
        self._render(410.00, 1.61)            # 建立滚动基线帧

    def tearDown(self):
        self.win._anim_timer.stop()
        self.win.close()
        QApplication.processEvents()
        for p in self._patches:
            p.stop()

    def _render(self, price, pct):
        """强制走一帧真实绘制（用于刷新 _roll_prev / _roll_anim 等渲染层状态）。

        为什么用 grab() 而不是 update() + processEvents()：
        update() 只是"投递"重绘请求，**是否真正派发取决于窗口服务器**。
        offscreen 下重绘同步完成，稳定；但 cocoa 下窗口曝光是异步的，
        全量套件连续创建/销毁窗口时曝光时机存在抖动，paintEvent 可能在本帧
        未执行 —— 实测单独跑本模块 10/10 通过，跑全量套件则约 1/3 概率失败
        （_roll_prev 未被赋值）。grab() 走 render-to-pixmap 路径，不依赖窗口
        曝光，结果确定；代价是每次多一次位图分配，测试规模下可忽略。
        """
        self.win._disp_price = price
        self.win._disp_pct = pct
        self.win.grab()
        QApplication.processEvents()

    def _apply(self, price, pct):
        q = dict(_QUOTE)
        q["price"] = price
        q["pct"] = pct
        self.win._apply_quote(q)

    def test_big_move_sets_pending(self):
        """前置条件：大幅变动（非微小）确实预置滚动目标并启动动画定时器"""
        self._apply("410.80", "1.61")
        self.assertIn("price", self.win._roll_pending)
        self.assertTrue(self.win._anim_timer.isActive())

    def test_tiny_move_clears_roll_pending(self):
        """核心泄漏点：微小变动直赋打断动画时，必须清除 _roll_pending"""
        self._apply("410.80", "1.61")          # 大幅 → 动画 + 预置目标
        # 模拟插值已推进到目标附近（定时器被测试接管，手动停表落值）
        self.win._disp_price = 410.80
        self.win._disp_pct = 1.61
        self.win._anim_timer.stop()
        # 微小变动（价格不动、涨跌幅 +0.002 < ANIM_TINY_PRICE）→ 直赋分支
        self._apply("410.80", "1.612")
        self.assertFalse(self.win._anim_timer.isActive())
        self.assertEqual(self.win._roll_pending, {},
                         "微小变动直赋后必须清除滚动目标，否则渲染层锁旧值")

    def test_tiny_single_tick_float_epsilon_is_tiny(self):
        """现实行情复现：0.01 元单档跳动受浮点误差被判为微小变动
        （410.08→410.09 差值=0.00999... < 0.01），同样不允许残留目标"""
        self.win._disp_price = 410.08
        self.win._disp_pct = 1.61
        self._render(410.08, 1.61)
        self.win._roll_pending = {"price": "410.80", "pct": "+1.61%"}  # 模拟泄漏残留
        self._apply("410.09", "1.61")
        self.assertEqual(self.win._roll_pending, {})
        self.assertEqual(self.win._disp_price, 410.09)

    def test_first_frame_branch_clears_residual_pending(self):
        """首帧分支（_disp_price=None）防御性清除残留目标"""
        self.win._roll_pending = {"price": "999.99"}     # 人为残留
        self.win._disp_price = None
        self.win._disp_pct = None
        self._apply("410.00", "1.61")
        self.assertEqual(self.win._roll_pending, {})

    def test_render_self_heals_stale_pending_when_timer_stopped(self):
        """渲染层兜底：定时器已停而目标残留 → 绘制时就地清除，基线回到实际显示串"""
        self.win._roll_pending = {"price": "999.99"}     # 模拟任何未知泄漏
        self.win._anim_timer.stop()
        self._render(410.30, 1.61)
        self.assertNotIn("price", self.win._roll_pending, "兜底应就地清除残留目标")
        self.assertEqual(self.win._roll_prev.get("price"), "410.30",
                         "清除后滚动基线应跟随实际显示值")

    def test_roll_works_again_after_self_heal(self):
        """兜底清除后滚动动画恢复正常：后续变化正常触发逐位滚动"""
        self.win._roll_pending = {"price": "999.99"}
        self.win._anim_timer.stop()
        self._render(410.30, 1.61)           # 自愈：基线 = "410.30"
        self._render(410.45, 1.61)           # 位值变化 → 应触发滚动
        self.assertEqual(self.win._roll_prev.get("price"), "410.45")
        self.assertIn(4, self.win._roll_anim.get("price", {}),
                      "自愈后滚动动画应能正常重新触发")

    def test_safety_net_does_not_touch_active_animation(self):
        """正常动画期间（定时器运行中）兜底不误伤预置目标"""
        self._apply("410.80", "1.61")        # 启动动画 + 预置目标
        self.assertTrue(self.win._anim_timer.isActive())
        self._render(self.win._disp_price, 1.61)   # 动画期间的一帧绘制
        self.assertIn("price", self.win._roll_pending,
                      "动画运行中不得误清预置目标")
        self.win._anim_timer.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
