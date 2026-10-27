# -*- coding: utf-8 -*-
"""实时股价 v1.2.32（自 v1.2.30/v1.2.31 沿革）两项优化回归测试。

需求1：无有效行情一律显示 "--" —— price 为 0/非法且无分时数据可兜底时，
       _apply_quote 丢弃本条（_disp_price/_disp_pct 保持 None 或上一有效值），
       渲染层无数据分支输出 "--"，绝不显示 0/0.00/0%；停牌状态仍保留灰色徽标；
       有 minute_points 兜底（_quote_zero_fallback）则正常显示兜底价。
需求2：分钟分时拉取失败短期快速重试 —— _fetch_minute 返回显式成败信号；
       _minute_loop 失败后以 MINUTE_RETRY_INTERVAL 快速重试，成功恢复常规周期，
       持续失败超过 MINUTE_RETRY_MAX 次回落到常规周期（不空转刷屏）。
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


_QUOTE_OK = _quote(price="410.00", pct="1.61", change="6.50", volume="12345")


class _WinBase(unittest.TestCase):
    """公共夹具：真窗口、真业务路径，但不为无关断言启动常驻 worker。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._runtime_starts = []

        def block_runtime_start(controller, quote_target, minute_target):
            self.assertEqual(controller.threads(), (None, None),
                             "夹具构造前不应已有行情或分时 worker")
            self._runtime_starts.append((controller, quote_target, minute_target))

        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: True),
            patch.object(sf_quote, "http_get", lambda *a, **k: ""),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch.object(m.QuoteRuntimeController, "start", block_runtime_start),
            patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.win = m.FloatWindow()
        self.assertEqual(len(self._runtime_starts), 1,
                         "窗口构造必须把两条常驻循环交给运行时控制器")
        controller, quote_target, minute_target = self._runtime_starts[0]
        self.assertIs(controller, self.win._quote_runtime)  # discipline-ok(R5): 接线断言必须取窗口持有的运行时控制器
        self.assertEqual(quote_target.__name__, "_quote_loop_wrapper")
        self.assertEqual(minute_target.__name__, "_minute_loop_wrapper")
        self.assertEqual(controller.threads(), (None, None),
                         "本文件不测后台抓取，不应真的创建 worker")
        self.win.running = False
        self.win._wake.set()
        self.win.timer.stop()
        self.win.top_timer.stop()
        self.win._anim_timer.stop()
        self.win._flash_timer.stop()
        self.win.show()

    def tearDown(self):
        self.win._exit_persisted = True  # discipline-ok(R5): §6.11 规定关窗前必须阻止真实配置落盘
        self.win.close()


# =====================================================================
# 需求1：无有效行情一律显示 "--"
# =====================================================================
class TestDashInsteadOfZero(_WinBase):
    """price 为 0/非法且无分时兜底 → 不落价格，渲染走 "--" 分支。"""

    def test_zero_without_fallback_drops_price(self):
        """无有效行情：注入 price=0 且无 minute_points 兜底 → _disp_price 保持 None。"""
        with patch.object(m, "_is_trading_time", return_value=True):
            self.win._apply_quote(_quote(price="0.00", pct="0.00"))
        self.assertIsNone(self.win._disp_price, "无有效行情时应保持价格未落地（渲染 --）")
        self.assertIsNone(self.win._disp_pct, "无有效行情时涨跌幅应保持未落地（渲染 --）")
        self.assertIsNotNone(self.win.quote, "首条 0 值行情仍记录 quote 供停牌徽标识别")

    def test_invalid_price_without_trading_time_drops(self):
        """非交易时段 + price=0 且无分时兜底：同样不落价格（兜底失效时维持 None）。"""
        self.win._apply_quote(_quote(price="0.00", pct="0.00"))
        self.assertIsNone(self.win._disp_price)

    def test_zero_keeps_previous_valid_price(self):
        """已有有效行情后再收到 price=0（无兜底）：保持上一有效价，绝不被 0 覆盖。"""
        with patch.object(m, "_is_trading_time", return_value=True):
            self.win._apply_quote(dict(_QUOTE_OK))
        self.assertEqual(self.win._disp_price, 410.00)
        with patch.object(m, "_is_trading_time", return_value=True):
            self.win._apply_quote(_quote(price="0.00", pct="0.00"))
        self.assertEqual(self.win._disp_price, 410.00, "0 值行情不得覆盖已有有效价格")
        self.assertEqual(self.win._disp_pct, 1.61)

    def test_zero_with_minute_fallback_shows_fallback_price(self):
        """有分时兜底：price=0 但 minute_points 非空 → 显示兜底价（配合 1.2.28 语义）。"""
        self.win.minute_points = [("0930", "410.00"), ("1000", "410.50")]
        with patch.object(m, "_is_trading_time", return_value=True):
            self.win._apply_quote(_quote(price="0.00", pct="0.00", pre_close="403.50"))
        self.assertIsNotNone(self.win._disp_price)
        self.assertAlmostEqual(self.win._disp_price, 410.5, places=2,
                               msg="有分时兜底应显示兜底价而非 --")
        expect_pct = (410.5 - 403.5) / 403.5 * 100
        self.assertAlmostEqual(self.win._disp_pct, expect_pct, places=2)

    def test_valid_zero_pct_still_displayed(self):
        """有效行情平盘（price>0 且 pct=0）：0% 是有效值，正常显示不被误杀。"""
        with patch.object(m, "_is_trading_time", return_value=True):
            self.win._apply_quote(_quote(price="410.00", pct="0.00", change="0.00"))
        self.assertEqual(self.win._disp_price, 410.00)
        self.assertEqual(self.win._disp_pct, 0.00, "有效平盘 0% 应正常显示")

    def test_render_without_data_is_dash(self):
        """渲染分支开关：无有效价格时 if 分支条件为假（走 else "--"），且可无异常渲染。"""
        with patch.object(m, "_is_trading_time", return_value=True):
            self.win._apply_quote(_quote(price="0.00", pct="0.00"))
        self.win._need_redraw = True
        self.win.update()
        QApplication.processEvents()
        img = self.win.grab().toImage()
        QApplication.processEvents()
        self.assertFalse(
            self.win.quote and self.win._disp_price is not None and self.win._disp_pct is not None,
            "应走无数据分支渲染 --")
        self.assertGreater(img.width(), 0)


# =====================================================================
# 需求2：分钟分时拉取失败短期快速重试（_fetch_minute 返回语义）
# =====================================================================
class TestFetchMinuteReturn(_WinBase):
    """_fetch_minute 的显式成败信号（供 _minute_loop 状态机消费）。"""

    def test_success_returns_true(self):
        with patch.object(m, "fetch_minute", return_value=[("0930", "410.0")]), \
             patch.object(m, "_is_trading_time", return_value=True):
            self.assertTrue(self.win._fetch_minute())

    def test_empty_returns_false(self):
        with patch.object(m, "fetch_minute", return_value=[]), \
             patch.object(m, "_is_trading_time", return_value=True):
            self.assertFalse(self.win._fetch_minute())

    def test_exception_returns_false(self):
        with patch.object(m, "fetch_minute", side_effect=RuntimeError("net down")), \
             patch.object(m, "_is_trading_time", return_value=True):
            self.assertFalse(self.win._fetch_minute())

    def test_reentrant_returns_none(self):
        """防重入：上一请求未完成时跳过判定，返回 None（不触发重试窗口）。

        抢的是 `_minute_busy`（防重入守卫），不是 `_minute_lock`（数据锁）——
        两把锁的分工与判据见 tests/test_minute_recovery_latency.py。"""
        acquired = self.win._minute_busy.acquire(blocking=False)
        try:
            self.assertIsNone(self.win._fetch_minute())
        finally:
            if acquired:
                self.win._minute_busy.release()


class _ControlledLoopMixin:
    """可控时钟下驱动 _minute_loop，返回每次 fetch 的时间戳序列。

    用迭代式假时钟精确模拟秒表：time.monotonic() 每轮从 times 序列取下一时间点，
    fetch 次数达到 len(results) 即停止。points 间隔即是调用时刻间隔，
    可直接区分常规周期（如 1.0s）与快速重试窗口（2.5s）。

    v1.2.72：假时钟目标由 time.time() 改为 time.monotonic() —— 到期判定已换用
    单调时钟（墙钟会被 NTP/改表拨回，回拨时 `now-last` 为负会静默停拉）。
    本夹具驱动的是循环内的到期判定，故必须打在循环实际读取的那个时钟上。
    """

    def _drive_minute_loop(self, win, times, results, minute_interval=1.0):
        it = iter(times)
        cur = {"v": times[0]}
        fetch_times = []
        slept = {"n": 0}

        def fake_now():
            try:
                cur["v"] = next(it)
            except StopIteration:
                pass  # 时钟点用尽：停在末值（由 fetch 数控制退出）
            return cur["v"]

        res_q = list(results)
        _boolify = {"ok": True, "fail": False}

        def fake_fetch():
            fetch_times.append(cur["v"])
            idx = len(fetch_times) - 1
            scalar = res_q[idx] if idx < len(res_q) else True
            # 统一映射为布尔：避免 "fail" 为真值字符串被 if result: 判为成功
            return _boolify.get(scalar, False) if scalar is not None else False

        def fake_sleep(sec):
            slept["n"] += 1
            if len(fetch_times) >= len(res_q) or slept["n"] > 5000:
                win.running = False
                return False
            return True

        win._minute_interval = minute_interval
        win.running = True
        with patch.object(m.time, "monotonic", side_effect=fake_now), \
             patch.object(win, "_is_trading_time", return_value=True), \
             patch.object(win, "_fetch_minute", side_effect=fake_fetch), \
             patch.object(win, "_sleep", side_effect=fake_sleep):
            win._minute_loop()
        return fetch_times


class TestMinuteLoopFastRetry(_WinBase, _ControlledLoopMixin):
    def test_failure_fast_retry_then_recover(self):
        """常规 10s 周期失败后进入 2.5s 快速重试，成功即恢复 10s 常规周期。"""
        times = [1000.0, 1010.0, 1020.0, 1022.5, 1025.0, 1027.5,
                 1060.0, 1070.0, 1080.0]
        results = ["ok", "fail", "fail", "fail", "fail", "ok",
                   "ok", "ok", "ok"]
        fetch_times = self._drive_minute_loop(self.win, times, results)
        self.assertEqual(len(fetch_times), len(results))
        deltas = [round(b - a, 2) for a, b in zip(fetch_times, fetch_times[1:])]
        self.assertEqual(deltas[0], 10.0, "常规周期首拉 10s")
        self.assertEqual(deltas[1:5], [10.0, 2.5, 2.5, 2.5],
                         "失败后进入 2.5s 快速重试窗口")
        self.assertGreaterEqual(deltas[5], 10.0,
                                "成功恢复后回落常规周期（不再 2.5s 快速重试）")
        self.assertEqual(deltas[6], 10.0, "恢复正常 10s 常规周期")

    def test_failure_exhausts_retry_then_normal_interval(self):
        """持续失败耗尽快速重试窗口后回落到常规周期（避免空转刷屏）。"""
        # 前 6 次在 2.5s 快速重试窗口内（1 次触发 + 5 次重试），随后回落 1s 常规
        times = [1000.0, 1002.5, 1005.0, 1007.5, 1010.0, 1012.5,
                 1013.5, 1014.5, 1015.5, 1016.5]
        results = ["fail"] * 10
        fetch_times = self._drive_minute_loop(self.win, times, results)
        deltas = [round(b - a, 2) for a, b in zip(fetch_times, fetch_times[1:])]
        # 快速重试窗口内（触发 + MINUTE_RETRY_MAX 次重试）：间隔 2.5s
        self.assertEqual(deltas[0:5], [2.5, 2.5, 2.5, 2.5, 2.5],
                         "快速重试窗口间隔应为 2.5s")
        # 窗口耗尽后回落到常规周期（不再以 2.5s 高频刷屏）
        self.assertEqual(deltas[5], 1.0,
                         "超过快速重试上限后应回落常规周期（1s）")
        self.assertEqual(deltas[6], 1.0, "回落常规周期后保持 1s")

    def test_success_resets_retry_state(self):
        """断连恢复后：持续失败状态清零，下一次失败可再次进入快速重试窗口。"""
        # 失败*3 → 成功*2(常规1s) → 失败*4 → 成功：恢复后再次失败可重入 2.5s 窗口
        times = [1000.0, 1002.5, 1005.0, 1007.5, 1008.5, 1009.5,
                 1012.0, 1014.5, 1017.0, 1019.5]
        results = ["fail", "fail", "fail", "ok", "ok",
                   "fail", "fail", "fail", "fail", "ok"]
        fetch_times = self._drive_minute_loop(self.win, times, results)
        deltas = [round(b - a, 2) for a, b in zip(fetch_times, fetch_times[1:])]
        # 第一段快速重试窗口（失败后）2.5s
        self.assertEqual(deltas[0:3], [2.5, 2.5, 2.5], "首段快速重试窗口")
        # 成功恢复后回到常规周期
        self.assertEqual(deltas[3], 1.0, "成功后恢复常规周期（1s）")
        self.assertEqual(deltas[4], 1.0, "常规周期保持 1s")
        # 再次失败可重新进入快速重试窗口
        self.assertEqual(deltas[5:7], [2.5, 2.5], "再次失败重新进入快速重试窗口")

    def test_long_pause_backoff_is_interruptible(self):
        """长暂停退避收到退出信号后不得再拉取分时数据。"""
        sched = unittest.mock.Mock()
        sched.step.return_value = (True, True, 0.1)
        self.win.running = True
        with patch.object(m, "QuoteLoopSchedule", return_value=sched), \
             patch.object(self.win, "_sleep", return_value=False) as sleep, \
             patch.object(self.win, "_fetch_minute") as fetch:
            self.win._minute_loop()
        sleep.assert_called_once_with(1.0)
        fetch.assert_not_called()

    def test_minute_loop_src_keeps_off_market_constant(self):
        """源码级防回归：非交易时段仍回落 POLL_INTERVAL_OFF_MARKET，快速重试走常量。"""
        import inspect
        src = inspect.getsource(m.FloatWindow._minute_loop)
        self.assertIn("POLL_INTERVAL_OFF_MARKET", src, "非交易时段必须回落 60 分钟低频")
        self.assertIn("MINUTE_RETRY_INTERVAL", src, "快速重试间隔必须走常量")
        self.assertIn("MINUTE_RETRY_MAX", src, "快速重试上限必须走常量")
        self.assertNotIn("else 60.0", src)
        self.assertIn("self._minute_interval", src, "展开/收起仍按 _minute_interval 常规轮询")


if __name__ == "__main__":
    unittest.main()
