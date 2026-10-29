# -*- coding: utf-8 -*-
"""收盘后打开、睡眠唤醒后打开，看到的行情必须是最新的，不能干等一小时。

用户要求（2026-10-29）：无论什么时候打开程序，看到的都必须是最新行情。除了
test_closed_market_valid_quote 管的「别拿旧分时点覆盖真实收盘价」，还有两条路会让人看到旧数据：

1. 收盘后轮询间隔是 3600 秒，开机自启时 Wi-Fi 还没连上、第一次拉取失败，
   下一次要一小时以后，期间界面一直是上次的旧价。现在失败后按 5/10/20/40/60 秒重试。
2. 行情循环用 time.monotonic 判到期，macOS 上它在睡眠期间不前进（本机实测
   mach_absolute_time）。盘中睡下、收盘后醒来，循环以为才过几秒，同样要再等一小时。
   现在用「墙钟 - 单调钟」的突增探测刚醒，立即补拉。
"""
import contextlib
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_quote
import stock_float_window as m
from _base import SFCase
from sf_constants import (OFF_MARKET_RETRY_BASE_SEC, OFF_MARKET_RETRY_CAP_SEC,
                          POLL_INTERVAL_OFF_MARKET, WAKE_GAP_SEC)
from sf_quote_loop import WakeDetector, off_market_retry_interval

BASE_MONO = 100000.0
BASE_WALL = 1_800_000_000.0
NAP = 2.0          # 假 _nap 的返回值：每轮循环虚拟时间前进 2 秒
BACKOFF = 1.0      # 循环内「长暂停/唤醒后首拉退避」的睡眠长度，与 NAP 区分开


def _wire(host, **members):
    for key, value in members.items():
        setattr(host, key, value)


def _peek(host, name):
    return getattr(host, name)


def _run(host, name, *args):
    """按名字调用宿主的内部方法（循环入口没有公开入口，集中在这一处）。"""
    return getattr(host, name)(*args)


class OffMarketRetryInterval(SFCase):
    def test_success_keeps_the_hourly_cadence(self):
        self.assertEqual(off_market_retry_interval(POLL_INTERVAL_OFF_MARKET, 0),
                         POLL_INTERVAL_OFF_MARKET)

    def test_failures_retry_5_10_20_40_then_cap_at_60(self):
        got = [off_market_retry_interval(POLL_INTERVAL_OFF_MARKET, n) for n in range(1, 8)]
        self.assertEqual(got, [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0])

    def test_never_longer_than_the_base_it_was_given(self):
        # 调用方传入比封顶更短的 base（例如以后改小常规间隔）时不能被拉长。
        self.assertEqual(off_market_retry_interval(30.0, 6), 30.0)

    def test_huge_streak_stays_at_cap_without_overflow(self):
        self.assertEqual(off_market_retry_interval(POLL_INTERVAL_OFF_MARKET, 10 ** 6),
                         OFF_MARKET_RETRY_CAP_SEC)

    def test_unreadable_streak_falls_back_to_base_not_to_high_frequency(self):
        for bad in (None, "x", object(), -3):
            self.assertEqual(off_market_retry_interval(POLL_INTERVAL_OFF_MARKET, bad),
                             POLL_INTERVAL_OFF_MARKET, repr(bad))

    def test_constants_are_ordered_sanely(self):
        self.assertLess(OFF_MARKET_RETRY_BASE_SEC, OFF_MARKET_RETRY_CAP_SEC)
        self.assertLess(OFF_MARKET_RETRY_CAP_SEC, POLL_INTERVAL_OFF_MARKET)


class WakeDetectorLogic(SFCase):
    def test_first_call_only_records_the_baseline(self):
        self.assertFalse(WakeDetector().check(BASE_WALL, BASE_MONO))

    def test_steady_clocks_never_report_a_wake(self):
        det = WakeDetector()
        det.check(BASE_WALL, BASE_MONO)
        for step in range(1, 200):
            self.assertFalse(det.check(BASE_WALL + step * 5, BASE_MONO + step * 5))

    def test_sleep_gap_is_reported_once(self):
        det = WakeDetector()
        det.check(BASE_WALL, BASE_MONO)
        # 睡了 3 小时：墙钟走了 10805 秒，单调钟只走了 5 秒
        self.assertTrue(det.check(BASE_WALL + 10805, BASE_MONO + 5))
        self.assertFalse(det.check(BASE_WALL + 10810, BASE_MONO + 10),
                         "同一次醒来只报一次，不能每一拍都补拉")

    def test_gap_below_threshold_is_ignored(self):
        det = WakeDetector()
        det.check(BASE_WALL, BASE_MONO)
        self.assertFalse(det.check(BASE_WALL + 5 + WAKE_GAP_SEC - 1, BASE_MONO + 5))

    def test_clock_stepped_back_rebaselines_without_reporting(self):
        det = WakeDetector()
        det.check(BASE_WALL, BASE_MONO)
        self.assertFalse(det.check(BASE_WALL - 3600, BASE_MONO + 5), "校时回拨不是醒来")
        self.assertFalse(det.check(BASE_WALL - 3595, BASE_MONO + 10))

    def test_slow_drift_accumulating_over_many_checks_is_not_a_wake(self):
        det = WakeDetector()
        det.check(BASE_WALL, BASE_MONO)
        for step in range(1, 500):
            # 每拍墙钟比单调钟多走 1 秒：累计 499 秒，但单拍增量始终低于阈值
            self.assertFalse(det.check(BASE_WALL + step * 6, BASE_MONO + step * 5))

    def test_custom_threshold_is_respected(self):
        det = WakeDetector(gap_sec=2.0)
        det.check(BASE_WALL, BASE_MONO)
        self.assertTrue(det.check(BASE_WALL + 10, BASE_MONO + 5))


class VirtualClock:
    """单调钟只在循环睡觉时前进；墙钟 = 单调钟 + 一个可以突然加大的偏移（模拟睡眠）。"""

    def __init__(self):
        self.elapsed = 0.0
        self.jump = 0.0

    def mono(self):
        return BASE_MONO + self.elapsed

    def wall(self):
        return BASE_WALL + self.elapsed + self.jump


class LoopCase(SFCase):
    """带真窗口、假时钟、假拉取的循环级夹具。常驻线程不启动，循环由用例同步驱动。"""

    @classmethod
    def setUpClass(cls):
        cls.ensure_qapp()

    def setUp(self):
        super().setUp()
        blocked = lambda *a, **k: None  # noqa: E731
        for p in (
            patch.object(m, "load_config",
                         return_value=("sh600584", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: True),
            patch.object(sf_quote, "http_get", lambda *a, **k: ""),
            patch.object(m, "start_icon_worker", blocked),
            patch.object(m.FloatWindow, "_force_top", blocked),
            patch.object(m.FloatWindow, "_setup_reopen_handler", blocked),
            patch.object(m.QuoteRuntimeController, "start", blocked),
            patch("PyQt6.QtWidgets.QMenu.popup", blocked),
            patch.object(m, "_is_trading_time", return_value=False),
        ):
            self.track_patch(p)
        self.win = self.track_widget(m.FloatWindow())
        _wire(self.win, running=False)
        for timer in (self.win.timer, self.win.top_timer):
            timer.stop()
        self.clock = VirtualClock()
        self.fetch_times = []
        self.sleeps = []
        self.intervals = []
        self.lap = 0

    def drive(self, loop, fetch_name, fetch_impl, laps, jump_at=None, jump_sec=0.0, **patches):
        """同步跑 `loop` 共 laps 圈；第 jump_at 圈睡完后墙钟突然多走 jump_sec 秒。"""
        clock = self.clock

        def fake_sleep(sec):
            self.sleeps.append(sec)
            clock.elapsed += sec
            if sec == NAP:
                self.lap += 1
                if jump_at is not None and self.lap == jump_at:
                    clock.jump += jump_sec
                if self.lap >= laps:
                    _wire(self.win, running=False)
                    return False
            return True

        def fake_nap(_sched, _now, interval):
            self.intervals.append(interval)
            return NAP

        _wire(self.win, running=True)
        with contextlib.ExitStack() as stack:
            for p in (
                patch.object(m.time, "monotonic", side_effect=clock.mono),
                patch.object(m.time, "time", side_effect=clock.wall),
                patch.object(self.win, "_sleep", side_effect=fake_sleep),
                patch.object(self.win, "_nap", side_effect=fake_nap),
                patch.object(self.win, fetch_name, side_effect=fetch_impl),
            ):
                stack.enter_context(p)
            for name, value in patches.items():
                stack.enter_context(patch.object(self.win, name, return_value=value))
            _run(self.win, loop)
        return self.fetch_times

    # ---- 假拉取：按 outcomes 依次成功/失败，用完后一律成功 ----
    def quote_fetcher(self, outcomes=()):
        queue = list(outcomes)

        def fetch():
            self.fetch_times.append(self.clock.elapsed)
            if queue.pop(0) if queue else True:
                _wire(self.win, _quote_fail=0)
            else:
                _wire(self.win, _quote_fail=_peek(self.win, "_quote_fail") + 1)
        return fetch

    def minute_fetcher(self, outcomes=()):
        queue = list(outcomes)

        def fetch():
            self.fetch_times.append(self.clock.elapsed)
            if queue.pop(0) if queue else True:
                _wire(self.win, _minute_fail=0)
                return True
            _wire(self.win, _minute_fail=_peek(self.win, "_minute_fail") + 1)
            return False
        return fetch


class QuotePollIntervalAfterClose(LoopCase):
    def test_interval_table_follows_the_failure_streak(self):
        got = []
        for streak in range(0, 8):
            _wire(self.win, _quote_fail=streak)
            got.append(_run(self.win, "_poll_interval"))
        self.assertEqual(got, [3600.0, 5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0])

    def test_trading_hours_cadence_ignores_the_off_market_retry(self):
        _wire(self.win, _quote_fail=7)
        with patch.object(m, "_is_trading_time", return_value=True):
            self.assertEqual(_run(self.win, "_poll_interval"), m.POLL_INTERVAL_NORMAL)

    def test_failed_first_fetch_is_retried_in_seconds_not_after_an_hour(self):
        fetch_times = self.drive("_quote_loop", "_fetch_quote",
                                 self.quote_fetcher([False] * 20), laps=75)
        self.assertEqual(fetch_times, [0.0, 6.0, 16.0, 36.0, 76.0, 136.0],
                         "开机自启时网络没连上，应按 5/10/20/40/60 秒重试，而不是等一小时")

    def test_recovery_returns_to_the_hourly_cadence(self):
        fetch_times = self.drive("_quote_loop", "_fetch_quote",
                                 self.quote_fetcher([False, False, True]), laps=75)
        self.assertEqual(fetch_times, [0.0, 6.0, 16.0],
                         "成功一次后回到 60 分钟一次，不能继续高频打接口")

    def test_healthy_start_fetches_once_and_then_stays_quiet(self):
        fetch_times = self.drive("_quote_loop", "_fetch_quote", self.quote_fetcher(), laps=75)
        self.assertEqual(fetch_times, [0.0])


class MinuteLoopAfterClose(LoopCase):
    def test_failed_fetches_keep_retrying_after_the_quick_window_is_used_up(self):
        fetch_times = self.drive("_minute_loop", "_fetch_minute",
                                 self.minute_fetcher([False] * 200), laps=200)
        late = [t for t in fetch_times if t > 25.0]
        self.assertTrue(late, "快速重试用完后不能直接睡一小时：%r" % fetch_times)
        self.assertGreaterEqual(late[-1], 300.0)
        gaps = [b - a for a, b in zip(late, late[1:])]
        self.assertTrue(all(gap <= OFF_MARKET_RETRY_CAP_SEC + 2.0 for gap in gaps),
                        "封顶 60 秒（循环一圈 2 秒的粒度）：%r" % gaps)

    def test_success_returns_to_the_hourly_cadence(self):
        fetch_times = self.drive("_minute_loop", "_fetch_minute",
                                 self.minute_fetcher([False, True]), laps=100)
        self.assertEqual(fetch_times, [0.0, 4.0])


class WakeFromSleepRefetches(LoopCase):
    HOUR = 3600.0

    def _quote(self, **kw):
        return self.drive("_quote_loop", "_fetch_quote", self.quote_fetcher(), laps=40,
                          _poll_interval=self.HOUR, **kw)

    def _minute(self, **kw):
        return self.drive("_minute_loop", "_fetch_minute", self.minute_fetcher(), laps=40, **kw)

    def test_quote_loop_refetches_right_after_waking(self):
        fetch_times = self._quote(jump_at=10, jump_sec=3 * 3600.0)
        self.assertEqual(fetch_times, [0.0, 21.0], "醒来后应立即补拉（先退避 1 秒给 Wi-Fi 重连）")
        self.assertEqual(self.sleeps.count(BACKOFF), 1)

    def test_minute_loop_refetches_right_after_waking(self):
        fetch_times = self._minute(jump_at=10, jump_sec=3 * 3600.0)
        self.assertEqual(fetch_times, [0.0, 21.0])
        self.assertEqual(self.sleeps.count(BACKOFF), 1)

    def test_without_sleep_nothing_extra_is_fetched(self):
        self.assertEqual(self._quote(), [0.0])

    def test_a_small_clock_step_is_not_mistaken_for_waking(self):
        self.assertEqual(self._quote(jump_at=10, jump_sec=WAKE_GAP_SEC - 5.0), [0.0])

    def test_a_clock_stepped_back_is_not_mistaken_for_waking(self):
        self.assertEqual(self._quote(jump_at=10, jump_sec=-3600.0), [0.0])


if __name__ == "__main__":
    unittest.main()
