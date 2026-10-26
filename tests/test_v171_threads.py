# -*- coding: utf-8 -*-
"""实时股价 v1.2.72 回归测试：线程与定时器管理（第 4 轮走查 Task #5）。

覆盖四项修复：
A. _force_fetch 不再无上限建线程 —— 改为置标志，由常驻线程在节拍内消费；
   常驻线程已死时仍退回建线程兜底，刷新能力不丢。
B. 强制拉取标志"消费即失效"，不会重复触发多次请求（sched.mark 防连拉）。
C. 行情/分时的到期判定走单调时钟 time.monotonic()，墙钟 NTP 回拨不再静默停拉。
D. _minute_fail 成功后复位 —— 与 _fetch_quote 对称，新故障段首条日志不会被吞。
E. 窗口不可见（缩到 Dock / 隐藏）期间不启动闪烁定时器，避免恢复后补跑一批动画。

时钟说明（重要）：A~C 用假时钟驱动常驻循环。假时钟打在哪个函数上，就验证
生产代码**实际读的是哪个时钟** —— 只喂单调时钟、墙钟恒定，若生产误用
time.time() 则 `now-last` 恒为 0、间隔判定永不成立、一次都不拉取。
"""
import contextlib
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_quote
import stock_float_window as m
from PyQt6.QtWidgets import QApplication


class _AliveThreadStub:
    """仅提供 is_alive() 的线程替身（用于模拟常驻线程仍存活）。"""

    def __init__(self, alive=True):
        self._alive = alive

    def is_alive(self):
        return self._alive


class _WinBase(unittest.TestCase):
    """公共夹具：真窗口与循环逻辑，但构造期不创建无关后台 worker。"""

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
                         "窗口构造必须登记两条常驻循环")
        controller, quote_target, minute_target = self._runtime_starts[0]
        self.assertIs(controller, self.win._quote_runtime)  # discipline-ok(R5): 接线断言必须取窗口持有的运行时控制器
        self.assertEqual(quote_target.__name__, "_quote_loop_wrapper")
        self.assertEqual(minute_target.__name__, "_minute_loop_wrapper")
        self.assertEqual(controller.threads(), (None, None),
                         "本文件直接驱动循环，不应真的创建 worker")
        self.win.running = False
        self.win._wake.set()
        self.win.timer.stop()
        self.win.top_timer.stop()
        self.win._anim_timer.stop()
        self.win._flash_timer.stop()
        try:
            self.win._flash_drop_timer.stop()
        except Exception:
            pass
        self.win.show()

    def tearDown(self):
        self.win._exit_persisted = True  # discipline-ok(R5): §6.11 规定关窗前必须阻止真实配置落盘
        self.win.close()

    # ---- 驱动器 ----
    def _drive_quote_loop(self, mono_seq, wall_seq, interval, stop_after,
                          force_at=None):
        """用假时钟驱动 _quote_loop，返回每次拉取时刻的列表。

        mono_seq / wall_seq 分别是喂给 time.monotonic / time.time 的时间序列
        （用尽后停在末值）。force_at 为 None 时不干预标志（走真实 _take_force）；
        为整数 n 时让 _take_force 在第 n 次调用返回 True（模拟外部强制拉取）。
        """
        mono = iter(mono_seq)
        wall = iter(wall_seq)
        cur = [mono_seq[0]]
        wcur = [wall_seq[0]]
        fetches = []
        slept = [0]

        def fake_mono():
            try:
                cur[0] = next(mono)
            except StopIteration:
                pass
            return cur[0]

        def fake_wall():
            try:
                wcur[0] = next(wall)
            except StopIteration:
                pass
            return wcur[0]

        def fake_fetch():
            fetches.append(cur[0])

        def fake_sleep(sec):
            slept[0] += 1
            if slept[0] >= stop_after:
                self.win.running = False
                return False
            return True

        calls = [0]

        def fake_take(name):
            calls[0] += 1
            if force_at is not None and calls[0] == force_at:
                return True
            return False

        self.win.running = True
        patches = [
            patch.object(m.time, "monotonic", side_effect=fake_mono),
            patch.object(m.time, "time", side_effect=fake_wall),
            patch.object(self.win, "_poll_interval", return_value=interval),
            patch.object(self.win, "_fetch_quote", side_effect=fake_fetch),
            patch.object(self.win, "_sleep", side_effect=fake_sleep),
        ]
        if force_at is not None:
            patches.append(patch.object(self.win, "_take_force",
                                        side_effect=fake_take))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            self.win._quote_loop()
        return fetches


# =====================================================================
# A. _force_fetch 不再无上限建线程
# =====================================================================
class TestForceFetchNoThreadStorm(_WinBase):
    """切股 / 立即刷新 / Dock 恢复都走 _force_fetch，不得每次新建线程。"""

    def test_alive_threads_means_flag_only(self):
        """常驻线程存活：只置标志，不建线程（连续点击不会堆积并发请求）。"""
        self.win._quote_thread = _AliveThreadStub(True)
        self.win._minute_thread = _AliveThreadStub(True)
        self.win._force_quote = False
        self.win._force_minute = False
        with patch("threading.Thread") as th:
            self.win._force_fetch()
            self.assertEqual(th.call_count, 0,
                             "常驻线程存活时不应新建线程（标志已足够）")
        self.assertTrue(self.win._force_quote, "应置位行情强制拉取标志")
        self.assertTrue(self.win._force_minute, "应置位分时强制拉取标志")

    def test_stopped_window_never_restarts_threads(self):
        """退出后到达的刷新回调必须直接丢弃，不能重建后台线程。"""
        self.win.running = False
        self.win._threads_stopped = True
        self.win._force_quote = False
        self.win._force_minute = False
        with patch("threading.Thread") as th:
            self.win._force_fetch()
        self.assertEqual(th.call_count, 0)
        self.assertFalse(self.win._force_quote)
        self.assertFalse(self.win._force_minute)

    def test_dead_thread_falls_back_to_spawn(self):
        """常驻线程已死：重建并登记常驻循环，刷新能力不因线程死亡而丢失。"""
        self.win._quote_thread = _AliveThreadStub(False)
        self.win._minute_thread = _AliveThreadStub(False)
        with patch("threading.Thread") as th:
            quote_new = th.return_value
            minute_new = th.return_value
            quote_new.is_alive.return_value = True
            minute_new.is_alive.return_value = True
            self.win._force_fetch()
            self.assertEqual(th.call_count, 2,
                             "常驻线程已死时应重建行情 + 分时两条常驻循环")
            self.assertEqual(th.return_value.start.call_count, 2)

    def test_take_force_consumes_once(self):
        """标志消费即失效：不会因一次触发连拉两次。"""
        self.win._force_quote = False
        self.assertFalse(self.win._take_force("_force_quote"))
        self.win._force_quote = True
        self.assertTrue(self.win._take_force("_force_quote"))
        self.assertFalse(self.win._take_force("_force_quote"),
                         "标志清除后不得再次生效")


# =====================================================================
# B. 强制拉取确实驱动常驻循环，且只多拉一次
# =====================================================================
class TestForceFlagDrivesLoop(_WinBase):
    """未到期时置位强制标志 → 本节拍立即拉取，且下一拍不重复拉。"""

    def test_baseline_no_fetch_when_not_due(self):
        """基线：间隔 3600s、无强制标志 → 仅首拍（last=0）拉取一次。

        时钟基准取 100000s：sched.last 初值为 0，只有 now 本身大于间隔时首拍
        才判"到期"（否则会被误判为未到期，掩盖强制拉取的作用）。
        """
        fetches = self._drive_quote_loop(
            mono_seq=[100000.0, 100000.05, 100000.10, 100000.15, 100000.20],
            wall_seq=[0.0] * 5, interval=3600.0, stop_after=5)
        self.assertEqual(len(fetches), 1, "未到期不应反复拉取")

    def test_force_flag_forces_one_extra_fetch(self):
        """第 2 拍强制置位 → 恰好多拉一次（第 3 拍起恢复，不重复拉）。"""
        fetches = self._drive_quote_loop(
            mono_seq=[100000.0, 100000.05, 100000.10, 100000.15, 100000.20],
            wall_seq=[0.0] * 5, interval=3600.0, stop_after=5, force_at=2)
        self.assertEqual(len(fetches), 2,
                         "强制标志应恰好带来一次额外拉取（多拉说明 sched.mark 失效）")
        self.assertEqual(fetches[1], 100000.05, "强制拉取应发生在本节拍")

    def test_force_uses_quote_flag_name(self):
        """行情循环消费的是 _force_quote（与分时标志互不干扰）。"""
        seen = []
        real_take = self.win._take_force
        self.win._take_force = lambda name: (seen.append(name), real_take(name))[1]
        self._drive_quote_loop(
            mono_seq=[100000.0, 100000.05], wall_seq=[0.0] * 2,
            interval=3600.0, stop_after=2)
        self.assertEqual(set(seen), {"_force_quote"})


# =====================================================================
# C. 到期判定走单调时钟
# =====================================================================
class TestMonotonicClock(_WinBase):
    """行情循环的到期判定必须读单调时钟，墙钟回拨不得造成静默停拉。"""

    def test_reads_monotonic_not_wall_clock(self):
        """只喂单调时钟、墙钟恒定 0：若误用 time.time() 则一次都拉不到。"""
        fetches = self._drive_quote_loop(
            mono_seq=[1000.0, 1001.0, 1002.0, 1003.0, 1004.0],
            wall_seq=[0.0] * 5, interval=1.0, stop_after=5)
        self.assertEqual(len(fetches), 5,
                         "单调时钟下每拍都应到期拉取；为 0 说明判定时钟用错了")

    def test_wall_clock_rollback_does_not_stall(self):
        """墙钟被 NTP/改表回拨时，行情仍按单调时钟持续拉取（不静默停拉）。"""
        # 单调时钟健康递增；墙钟在第 3 拍回拨 300s
        fetches = self._drive_quote_loop(
            mono_seq=[1000.0, 1001.0, 1002.0, 1003.0, 1004.0],
            wall_seq=[1000.0, 1001.0, 700.0, 701.0, 702.0],
            interval=1.0, stop_after=5)
        self.assertEqual(len(fetches), 5,
                         "墙钟回拨后必须继续拉取（用墙钟则回拨点之后全部停拉）")


# =====================================================================
# D. _minute_fail 成功后复位
# =====================================================================
class TestMinuteFailResets(_WinBase):
    """分时连续失败计数在成功后归零，与 _fetch_quote 的语义对称。"""

    def setUp(self):
        super().setUp()
        # 夹具启动期间后台分时线程已跑过一轮（网络打桩必然失败），计数不为 0，
        # 必须先归零，否则断言读到的是夹具噪声而非本用例的行为。
        self.win._minute_fail = 0

    def test_fail_counter_resets_on_success(self):
        with patch.object(m, "fetch_minute", side_effect=RuntimeError("down")), \
             patch.object(m, "_is_trading_time", return_value=True):
            for _ in range(3):
                self.win._fetch_minute()
        self.assertEqual(self.win._minute_fail, 3)

        with patch.object(m, "fetch_minute", return_value=[("0930", "410.0")]), \
             patch.object(m, "_is_trading_time", return_value=True):
            self.assertTrue(self.win._fetch_minute())
        self.assertEqual(self.win._minute_fail, 0,
                         "成功后必须复位，否则新故障段永远凑不到 count==1、首条日志被吞")

    def test_new_failure_segment_reaches_one(self):
        """复位后再失败，计数从 1 起算（首条故障日志可正常记录）。"""
        self.win._minute_fail = 137        # 模拟历史故障段残留值
        with patch.object(m, "fetch_minute", return_value=[("0930", "410.0")]), \
             patch.object(m, "_is_trading_time", return_value=True):
            self.win._fetch_minute()
        with patch.object(m, "fetch_minute", side_effect=RuntimeError("down")), \
             patch.object(m, "_is_trading_time", return_value=True):
            self.win._fetch_minute()
        self.assertEqual(self.win._minute_fail, 1)


# =====================================================================
# D2. 迟到行情不得覆盖切换后的美股/港股
# =====================================================================
class TestQuoteReplyCodeGuard(_WinBase):
    """行情回包携带请求代码；切股后旧回包必须在 _poll 丢弃。"""

    def _clear_queue(self):
        import queue
        try:
            while True:
                self.win.q.get_nowait()
        except queue.Empty:
            pass

    def test_stale_us_quote_is_dropped(self):
        self._clear_queue()
        self.win.code = "usmsft"
        self.win.running = True
        applied = []
        self.win._apply_quote = lambda q: applied.append(q)
        self.win.q.put(("quote", ("usaapl", {"code": "AAPL", "price": "100.00"})))
        self.win._poll()
        self.assertEqual(applied, [], "旧美股回包不得短暂覆盖新股票")

    def test_current_us_quote_is_applied(self):
        self._clear_queue()
        self.win.code = "usmsft"
        self.win.running = True
        applied = []
        quote = {"code": "MSFT", "price": "100.00"}
        self.win._apply_quote = lambda q: applied.append(q)
        self.win.q.put(("quote", ("usmsft", quote)))
        self.win._poll()
        self.assertEqual(applied, [quote])


# =====================================================================
# E. 高速 UI 轮询性能
# =====================================================================
class TestPollPerformance(_WinBase):
    """交易时段避免重复宽度测量和零延迟轮询事件链。"""

    def test_idle_poll_skips_auto_width_when_content_is_clean(self):
        self.win.running = True
        self.win._width_dirty = False
        with patch.object(self.win, "_apply_icons", lambda: None), \
             patch.object(self.win, "_auto_width") as auto_width, \
             patch.object(m, "_is_trading_time", return_value=True):
            self.win._poll()
        auto_width.assert_not_called()

    def test_dirty_width_runs_once_after_poll(self):
        self.win.running = True
        self.win._width_dirty = True
        with patch.object(self.win, "_apply_icons", lambda: None), \
             patch.object(self.win, "_auto_width") as auto_width, \
             patch.object(m, "_is_trading_time", return_value=True):
            self.win._poll()
            self.win._poll()
        self.assertEqual(auto_width.call_count, 1)

    def test_valid_quote_marks_width_dirty(self):
        self.win._width_dirty = False
        self.win._width_content_key = None
        quote = {"code": "600519", "price": "100.00", "pct": "1.00",
                 "high": "101.00", "low": "99.00", "pre_close": "99.00"}
        with patch.object(m, "market_status", return_value=None):
            self.win._apply_quote(quote)
        self.assertTrue(self.win._width_dirty)

    def test_same_bottom_content_skips_repeated_width_measurement(self):
        quote = {"code": "600519", "price": "100.00", "pct": "1.00",
                 "high": "101.00", "low": "99.00", "pre_close": "99.00"}
        with patch.object(m, "market_status", return_value=None):
            self.win._apply_quote(quote.copy())
            self.win._width_dirty = False
            self.win._apply_quote(quote.copy())
        self.assertFalse(self.win._width_dirty,
                         "价格未影响底部显示文本时不应重复测量窗口宽度")

    def test_bottom_content_change_marks_width_dirty(self):
        first = {"code": "600519", "price": "100.00", "pct": "1.00",
                 "high": "101.00", "low": "99.00", "pre_close": "99.00"}
        second = first | {"high": "102.00"}
        with patch.object(m, "market_status", return_value=None):
            self.win._apply_quote(first)
            self.win._width_dirty = False
            self.win._apply_quote(second)
        self.assertTrue(self.win._width_dirty)

    def test_trading_poll_does_not_queue_zero_delay_follow_up(self):
        self.win.running = True
        with patch.object(self.win, "_apply_icons", lambda: None), \
             patch.object(self.win, "_apply_quote", lambda quote: None), \
             patch.object(self.win, "_auto_width", lambda: None), \
             patch.object(m, "_is_trading_time", return_value=True), \
             patch.object(m.QTimer, "singleShot") as single_shot:
            self.win.q.put(("quote", ("sh600519", {"price": "100.00"})))
            self.win._poll()
        single_shot.assert_not_called()


# =====================================================================
# F. 不可见期间不启动动画定时器
# =====================================================================
class TestAnimationsSuspended(_WinBase):
    """缩到 Dock / 隐藏期间，行情刷新不得把闪烁定时器重新拉起。"""

    def test_suspended_by_minimized(self):
        self.win._minimized = False
        self.assertFalse(self.win._animations_suspended())
        self.win._minimized = True
        self.assertTrue(self.win._animations_suspended())

    def test_suspended_when_invisible(self):
        self.win._minimized = False
        with patch.object(self.win, "isVisible", return_value=False):
            self.assertTrue(self.win._animations_suspended())
        with patch.object(self.win, "isVisible", return_value=True):
            self.assertFalse(self.win._animations_suspended())

    def test_drop_flash_timer_not_started(self):
        """不可见时 _start_drop_flash 直接返回，不启动定时器。"""
        with patch.object(self.win, "_animations_suspended", return_value=True):
            self.win._start_drop_flash()
        self.assertFalse(self.win._flash_drop_timer.isActive(),
                         "窗口不可见时不应启动闪烁定时器（恢复后会补跑一批动画）")


if __name__ == "__main__":
    unittest.main()
