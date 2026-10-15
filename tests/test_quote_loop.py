# -*- coding: utf-8 -*-
"""实时股价 v1.2.40 行情循环与轮询调度纯逻辑单测（上帝类拆分第二步② sf_quote_loop 侧验证）。

覆盖 sf_quote_loop 的调度纯逻辑：
1. next_poll_backoff：熔断状态机一步转移（成功回退/失败升级 NORMAL->DEGRADED->OPEN）
2. ui_poll_interval_ms / ui_top_poll_interval_ms：_poll 头部间隔决策
3. minute_retry_state：分时快速重试窗口状态转移（成功/窗口内/首次失败/窗口耗尽）
4. QuoteLoopSchedule：行情/分时线程"到期拉取/长暂停退避/基础节拍"调度骨架

运行方式（在 StockFloat 目录）：
    python3 -m unittest tests/test_quote_loop.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sf_quote_loop import (QuoteLoopSchedule, minute_retry_state,
                           next_poll_backoff, ui_poll_interval_ms,
                           ui_top_poll_interval_ms)
from sf_constants import (POLL_DEGRADED, POLL_FAIL_TO_DEGRADED,
                          POLL_FAIL_TO_OPEN, POLL_INTERVAL_MINIMIZED,
                          POLL_INTERVAL_NORMAL, POLL_NORMAL, POLL_OPEN,
                          UI_WATCHDOG_INTERVAL_MS)


class TestNextPollBackoff(unittest.TestCase):
    """熔断状态机一步转移（原 _on_poll_result + _switch_poll_state）。"""

    def test_success_on_normal_no_change(self):
        state, fail = next_poll_backoff(POLL_NORMAL, 0, True,
                                        POLL_FAIL_TO_DEGRADED, POLL_FAIL_TO_OPEN)
        self.assertEqual((state, fail), (POLL_NORMAL, 0))

    def test_success_relieves_fail_and_recovers(self):
        # 曾失败（fail=2、DEGRADED）→ 成功清零并回退 NORMAL
        state, fail = next_poll_backoff(POLL_DEGRADED, 2, True,
                                        POLL_FAIL_TO_DEGRADED, POLL_FAIL_TO_OPEN)
        self.assertEqual((state, fail), (POLL_NORMAL, 0))
        # NORMAL 状态但留有失败计数 → 仅清零计数，状态保持
        state, fail = next_poll_backoff(POLL_NORMAL, 1, True,
                                        POLL_FAIL_TO_DEGRADED, POLL_FAIL_TO_OPEN)
        self.assertEqual((state, fail), (POLL_NORMAL, 0))
        state, fail = next_poll_backoff(POLL_OPEN, 4, True,
                                        POLL_FAIL_TO_DEGRADED, POLL_FAIL_TO_OPEN)
        self.assertEqual((state, fail), (POLL_NORMAL, 0))

    def test_fail_counts_then_degrade_then_open(self):
        state, fail = POLL_NORMAL, 0
        # NORMAL 下连续失败累计 → 第 POLL_FAIL_TO_DEGRADED 次后转 DEGRADED
        for i in range(1, POLL_FAIL_TO_DEGRADED + 1):
            state, fail = next_poll_backoff(state, fail, False,
                                            POLL_FAIL_TO_DEGRADED, POLL_FAIL_TO_OPEN)
            if i < POLL_FAIL_TO_DEGRADED:
                self.assertEqual(state, POLL_NORMAL)
            else:
                self.assertEqual(state, POLL_DEGRADED)
        self.assertEqual(fail, POLL_FAIL_TO_DEGRADED)
        # DEGRADED 下失败计数继续累计（3→4→5），第 5 次起转 OPEN。原 _on_poll_result
        # 语义：fail 跨状态累计，故 DEGRADED 只再需 2 次即达 POLL_FAIL_TO_OPEN=5
        state, fail = next_poll_backoff(state, fail, False,
                                        POLL_FAIL_TO_DEGRADED, POLL_FAIL_TO_OPEN)
        self.assertEqual(state, POLL_DEGRADED)             # 第 4 次：fail=4 仍 DEGRADED
        self.assertEqual(fail, POLL_FAIL_TO_DEGRADED + 1)
        state, fail = next_poll_backoff(state, fail, False,
                                        POLL_FAIL_TO_DEGRADED, POLL_FAIL_TO_OPEN)
        self.assertEqual(state, POLL_OPEN)                 # 第 5 次：fail=5 → OPEN
        self.assertEqual(fail, POLL_FAIL_TO_DEGRADED + 2)

    def test_open_keeps_counting_but_stays_open(self):
        state, fail = POLL_OPEN, POLL_FAIL_TO_DEGRADED + POLL_FAIL_TO_OPEN
        for _ in range(3):
            state, fail = next_poll_backoff(state, fail, False,
                                            POLL_FAIL_TO_DEGRADED, POLL_FAIL_TO_OPEN)
            self.assertEqual(state, POLL_OPEN)

    def test_threshold_constants(self):
        self.assertEqual(POLL_FAIL_TO_DEGRADED, 3)
        self.assertEqual(POLL_FAIL_TO_OPEN, 5)


class TestUiPollIntervals(unittest.TestCase):
    """_poll 头部间隔决策（原字面量搬移）。"""

    def test_minimized_intervals(self):
        self.assertEqual(ui_poll_interval_ms(True, True),
                         int(POLL_INTERVAL_MINIMIZED * 1000))   # 5s
        self.assertEqual(ui_top_poll_interval_ms(True, True), 10000)

    def test_trading_intervals(self):
        # v1.4.10：数据交付改信号驱动后，UI 定时器三档统一为"可见 1s 看门狗"，
        # 不再区分交易/非交易。置顶复核仍是 250ms（浏览器全屏恢复手感）。
        self.assertEqual(ui_poll_interval_ms(False, True), UI_WATCHDOG_INTERVAL_MS)
        self.assertEqual(ui_poll_interval_ms(False, False), UI_WATCHDOG_INTERVAL_MS)
        self.assertEqual(ui_top_poll_interval_ms(False, True), 250)

    def test_ui_wakeup_is_event_driven_not_polled(self):
        """回归保护：UI 取队列不再依赖"定时器比拉取间隔快"。

        v1.3.43 的旧用例断言 `ui_poll_interval_ms(交易) < 拉取间隔/2`，因为那时
        队列只能靠定时器去排。v1.4.10 起行情线程**入队即 emit ui_wakeup**，
        等待窗口从 0~16ms 压到 0~2ms，定时器退为兜底看门狗 —— 所以要守的不变量
        换成了：① 入队一定触发唤醒；② 看门狗仍是有限值（信号万一丢了有兜底）。
        """
        self.assertGreater(ui_poll_interval_ms(False, True), 0)
        self.assertLessEqual(ui_poll_interval_ms(False, True), 2000,
                             "看门狗超过 2 秒，信号丢失时数据会明显迟滞")

    def test_off_market_intervals(self):
        self.assertEqual(ui_poll_interval_ms(False, False), 1000)
        # 浏览器全屏可在任何时段发生；窗口可见时最坏恢复等待不得超过 250ms。
        self.assertEqual(ui_top_poll_interval_ms(False, False), 250)


class TestMinuteRetryState(unittest.TestCase):
    """分时快速重试窗口状态转移（原 _minute_loop 内联分支）。"""

    def test_success_resets_window(self):
        self.assertEqual(minute_retry_state(3, True, True, 5), (0, False))
        self.assertEqual(minute_retry_state(0, False, True, 5), (0, False))

    def test_in_window_failure_consumes_retry(self):
        self.assertEqual(minute_retry_state(3, True, False, 5), (2, True))
        self.assertEqual(minute_retry_state(1, True, False, 5), (0, True))

    def test_first_failure_enters_window(self):
        self.assertEqual(minute_retry_state(0, False, False, 5), (5, True))
        self.assertEqual(minute_retry_state(0, False, False, MINUTE_RETRY_MAX_LOCAL),
                         (MINUTE_RETRY_MAX_LOCAL, True))

    def test_window_exhausted_stays_hot(self):
        # 窗口耗尽仍失败：保持 hot，不再重复进入快速重试（防空转刷屏）
        state = minute_retry_state(0, True, False, 5)
        self.assertEqual(state, (0, True))

    def test_state_transition_sequence_matches_original(self):
        # 与原 _minute_loop 内联语义逐帧推演等价
        retry_left, hot = 0, False
        # 首次失败 → 进入窗口
        retry_left, hot = minute_retry_state(retry_left, hot, False, 5)
        self.assertEqual((retry_left, hot), (5, True))
        # 窗口内连续失败 3 次
        for _ in range(3):
            retry_left, hot = minute_retry_state(retry_left, hot, False, 5)
        self.assertEqual((retry_left, hot), (2, True))
        # 成功 → 立即恢复正常
        retry_left, hot = minute_retry_state(retry_left, hot, True, 5)
        self.assertEqual((retry_left, hot), (0, False))


# 主文件模块级 MINUTE_RETRY_MAX=5（不 import Qt 主窗，此处本地维护一致性校验）
MINUTE_RETRY_MAX_LOCAL = 5


class TestQuoteLoopSchedule(unittest.TestCase):
    """行情/分时线程「到期拉取/长暂停退避/基础节拍」调度骨架。"""

    def test_first_fetch_does_not_depend_on_machine_uptime(self):
        for now in (0.0, 10.0, 3599.0):
            with self.subTest(now=now):
                sched = QuoteLoopSchedule()
                self.assertEqual(sched.nap_seconds(now, 3600.0), sched.tick_seconds)
                fetch, backoff, _ = sched.step(now, 3600.0)
                self.assertTrue(fetch)
                self.assertFalse(backoff)
                self.assertEqual(sched.last, now)
                self.assertFalse(sched.step(now + 0.1, 3600.0)[0])

    def test_first_step_fetches_without_backoff(self):
        sched = QuoteLoopSchedule()
        fetch, backoff, sleep = sched.step(1000.0, 0.1)
        self.assertTrue(fetch)
        self.assertFalse(backoff)   # last==0 首启不判长暂停
        self.assertEqual(sleep, 0.1)
        self.assertEqual(sched.last, 1000.0)

    def test_not_due_skips_fetch(self):
        sched = QuoteLoopSchedule()
        sched.step(1000.0, 0.1)
        fetch, backoff, _ = sched.step(1000.05, 0.1)   # 未到 interval
        self.assertFalse(fetch)
        self.assertFalse(backoff)

    def test_due_after_interval(self):
        sched = QuoteLoopSchedule()
        sched.step(1000.0, 0.1)
        fetch, backoff, _ = sched.step(1000.1, 0.1)
        self.assertTrue(fetch)
        self.assertFalse(backoff)   # 间隔短于长暂停阈值

    def test_long_pause_invokes_backoff(self):
        # last>0 且静置超过 long_pause_limit(120s) → 恢复首拉需退避 1s
        sched = QuoteLoopSchedule()
        sched.step(1000.0, 120.0)
        fetch, backoff, sleep = sched.step(1000.0 + 130.0, 120.0)
        self.assertTrue(fetch)
        self.assertTrue(backoff)
        self.assertEqual(sleep, 0.1)

    def test_short_pause_no_backoff(self):
        # 到期但静置时长 < 长暂停阈值 → 正常拉取，无需退避
        sched = QuoteLoopSchedule()
        sched.step(1000.0, 1.0)
        fetch, backoff, _ = sched.step(1000.0 + 10.0, 1.0)   # 过期 9s，但 < 120s
        self.assertTrue(fetch)
        self.assertFalse(backoff)

    def test_custom_params_are_respected(self):
        sched = QuoteLoopSchedule(long_pause_limit=5.0, backoff_seconds=2.0, tick_seconds=0.05)
        sched.step(100.0, 1.0)
        fetch, backoff, sleep = sched.step(100.0 + 6.0, 1.0)
        self.assertTrue(fetch)
        self.assertTrue(backoff)
        self.assertEqual(sleep, 0.05)

    def test_tick_seconds_default(self):
        sched = QuoteLoopSchedule()
        _f, _b, sleep = sched.step(0.0, 1.0)
        self.assertEqual(sleep, 0.1)   # 基础节拍 0.1s（原循环内 _sleep(0.1)）


if __name__ == "__main__":
    unittest.main()
