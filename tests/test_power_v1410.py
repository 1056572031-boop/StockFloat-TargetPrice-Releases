# -*- coding: utf-8 -*-
"""v1.4.10 功耗优化回归用例。

守的是四件事，全部按**可观测行为**断言（AGENTS.md 第 6 节新用例约定）：

  P1  常驻线程按需休眠：睡到"下一次真正该决策"，而不是恒定 0.1s 空转；
      且**绝不比旧实现更早拉取**（拉取时刻逐次不变），边界一定醒得来。
  P2  数据交付信号驱动：入队即唤醒 UI，且跨线程 emit 不会把 _poll 拖到后台线程跑。
  P3  置顶复核幂等：状态本来就对时，一个 WindowServer 写调用都不发。
  P4  图标队列门控：空队列不再每拍排一遍。

"""
import datetime
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._base import SFCase, fixed_clock, wait_for, wait_until_false

from sf_constants import (LOOP_TICK_CAP, POLL_INTERVAL_MINIMIZED, POLL_NORMAL,
                          UI_WATCHDOG_INTERVAL_MS)
from sf_quote_loop import QuoteLoopSchedule, ui_poll_interval_ms
import sf_quote

import stock_float_window as m

# 必须自己持有 QApplication 引用：`QApplication.instance() or QApplication([])`
# 若把结果丢给一个立刻销毁的局部变量，PyQt 会在包装器回收时连 C++ 对象一起删掉，
# 下一个 QWidget 构造就直接 `QWidget: Must construct a QApplication before a
# QWidget` abort（不是异常，是进程级中止，排查成本极高）。
_APP = None


def ensure_app():
    global _APP
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    return _APP


# =====================================================================
# P1-A  nap_seconds：睡多久才算"按需"
# =====================================================================
class TestNapSeconds(unittest.TestCase):
    """QuoteLoopSchedule.nap_seconds —— 按需休眠的核心决策，纯函数。"""

    def test_in_market_unchanged_from_legacy_tick(self):
        """盘中 interval=0.1s：必须仍睡满一个节拍，不提前、不延后。

        这是"不改变轮询间隔"这条红线的直接守护点。若这里改成睡 0.061s，
        行情请求会从实测 7.1 次/秒涨到 10 次/秒 —— 那是网络行为变化，不允许。
        """
        s = QuoteLoopSchedule()
        s.step(1000.0, 0.1)                     # 触发一次拉取，last=1000.0
        self.assertAlmostEqual(s.nap_seconds(1000.0, 0.1, 0.0), 0.1, places=9)

    def test_never_wakes_earlier_than_due_except_boundary(self):
        """未到期时不得早于旧节拍醒来（边界除外）—— 防"按需"变成"更密"。"""
        s = QuoteLoopSchedule()
        s.step(1000.0, 1.0)
        # 只过了 0.05s，距到期还有 0.95s：旧实现会睡 0.1，新实现睡 0.95（更省）
        nap = s.nap_seconds(1000.05, 1.0, 0.0)
        self.assertGreater(nap, 0.1)
        self.assertAlmostEqual(nap, 0.95, places=6)

    def test_degraded_one_sleep_per_interval(self):
        """熔断降频到 1s：一次睡到点，取代原来 10 次 0.1s 空转。"""
        s = QuoteLoopSchedule()
        s.step(1000.0, 1.0)
        self.assertAlmostEqual(s.nap_seconds(1000.0, 1.0, 0.0), 1.0, places=6)

    def test_off_market_capped(self):
        """盘后 3600s 间隔：单次睡眠受 LOOP_TICK_CAP 兜底上限约束。"""
        s = QuoteLoopSchedule()
        s.step(1000.0, 3600.0)
        self.assertEqual(s.nap_seconds(1000.0, 3600.0, 0.0), LOOP_TICK_CAP)

    def test_session_boundary_overrides_long_sleep(self):
        """时段边界比"距下次到期"更近时，必须按边界醒。

        否则盘后一睡 5 秒封顶没问题，但**开盘前**那一刻绝不能睡过头。
        """
        s = QuoteLoopSchedule()
        s.step(1000.0, 3600.0)
        self.assertEqual(s.nap_seconds(1000.0, 3600.0, 0.5), 0.5)

    def test_boundary_shorter_than_tick_still_wins(self):
        """边界比节拍还近（开盘前 10ms）：允许早于节拍醒，重新决策一次。"""
        s = QuoteLoopSchedule()
        s.step(1000.0, 0.1)
        self.assertEqual(s.nap_seconds(1000.0, 0.1, 0.01), 0.01)

    def test_zero_boundary_is_treated_as_unknown(self):
        """边界算不出来（返回 0）时不参与决策，退回纯节拍 —— 安全兜底。"""
        s = QuoteLoopSchedule()
        s.step(1000.0, 3600.0)
        self.assertEqual(s.nap_seconds(1000.0, 3600.0, 0.0), LOOP_TICK_CAP)


# =====================================================================
# P1-B  时段边界计算
# =====================================================================
class TestSessionBoundary(unittest.TestCase):
    """seconds_to_session_change —— 按需休眠的正确性前提。"""

    TZ = datetime.timezone(datetime.timedelta(hours=8))

    def at(self, *args):
        return datetime.datetime(*args, tzinfo=self.TZ)

    def test_before_open_wakes_at_open(self):
        self.assertAlmostEqual(
            sf_quote.seconds_to_session_change(self.at(2026, 9, 22, 9, 29, 50)),
            10.0, places=3)

    def test_1130_still_trading_until_1131(self):
        """_is_trading_time 按分钟判定，11:30 整仍算盘中，翻转点在 11:31:00。"""
        self.assertTrue(sf_quote._is_trading_time(self.at(2026, 9, 22, 11, 30)))
        self.assertAlmostEqual(
            sf_quote.seconds_to_session_change(self.at(2026, 9, 22, 11, 30, 30)),
            30.0, places=3)

    def test_lunch_break_wakes_at_1300(self):
        self.assertAlmostEqual(
            sf_quote.seconds_to_session_change(self.at(2026, 9, 22, 11, 31)),
            (13 * 60 - (11 * 60 + 31)) * 60.0, places=3)

    def test_close_boundary_at_1501(self):
        self.assertTrue(sf_quote._is_trading_time(self.at(2026, 9, 22, 15, 0)))
        self.assertAlmostEqual(
            sf_quote.seconds_to_session_change(self.at(2026, 9, 22, 15, 0, 30)),
            30.0, places=3)

    def test_off_market_falls_back_to_midnight(self):
        self.assertAlmostEqual(
            sf_quote.seconds_to_session_change(self.at(2026, 9, 22, 16, 30)),
            7.5 * 3600.0, places=3)

    def test_weekend_no_intraday_flip_offered(self):
        """周六：盘中四个翻转点都不该出现（周末全天休市），只剩跨天。"""
        self.assertAlmostEqual(
            sf_quote.seconds_to_session_change(self.at(2026, 9, 26, 12, 0)),
            12 * 3600.0, places=3)

    def test_holiday_same_as_weekend(self):
        """法定节假日（2026-10-01）：同样只认跨天。"""
        with mock.patch.object(sf_quote, "is_holiday", return_value=True):
            got = sf_quote.seconds_to_session_change(self.at(2026, 10, 1, 10, 0))
        self.assertAlmostEqual(got, 14 * 3600.0, places=3)

    def test_never_negative_and_safe_on_garbage(self):
        self.assertGreaterEqual(
            sf_quote.seconds_to_session_change(self.at(2026, 9, 22, 10, 30)), 0.0)
        with mock.patch.object(sf_quote, "_beijing_now", side_effect=RuntimeError):
            self.assertEqual(sf_quote.seconds_to_session_change(None), 0.0,
                             "算不出来必须返回 0，让调用方退回固定节拍")


# =====================================================================
# 共用窗口夹具
# =====================================================================
class _WinCase(SFCase):
    """造一个"不碰网络、不写配置、不起图标线程"的可信窗口替身。

    与存量用例（test_smoke 等）同口径的 patch 清单 —— 这些 patch 是"让窗口能
    在离屏环境里被安全构造"的最小集合，不是被测对象。
    """

    def make_win(self):
        ensure_app()
        import sf_quote as _q
        self.track_patch(mock.patch.object(m, "load_config",
                                           return_value=("sh600519", 100, 100, 224, "glass")))
        self.track_patch(mock.patch.object(m, "save_config", lambda *a, **k: True))
        self.track_patch(mock.patch.object(_q, "http_get", lambda *a, **k: ""))
        self.track_patch(mock.patch.object(m, "start_icon_worker", lambda: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_force_top", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_inject_glass", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_setup_reopen_handler",
                                           lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "raise_", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "activateWindow",
                                           lambda self: None))
        # 常驻循环整体停掉：否则后台线程会在断言期间自己改 _poll_state /
        # 自己 set _resched，"谁唤醒了线程"这类断言必然被污染。
        self.track_patch(mock.patch.object(m.FloatWindow, "_quote_loop_wrapper",
                                           lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_minute_loop_wrapper",
                                           lambda self: None))
        win = self.track_widget(m.FloatWindow())
        # 先停掉常驻循环与定时器：用例里不该有真实线程在后台改状态
        win.running = False
        win._wake.set()
        win._resched.set()
        for t in (win.timer, win.top_timer, win._anim_timer, win._flash_timer,
                  win._flash_drop_timer, win._rainbow_timer, win._session_timer):
            t.stop()
        return win


# =====================================================================
# P1-C  唤醒通道：状态一变，长睡的线程必须立刻醒
# =====================================================================
class TestRescheduleWake(_WinCase):

    def test_force_fetch_wakes_loops(self):
        """按需休眠之后，"立即刷新"不能再等一个节拍 —— 必须靠事件叫醒。"""
        win = self.make_win()
        win.running = True
        win._resched.clear()
        win._force_fetch()
        self.assertTrue(win._resched.is_set(),
                        "_force_fetch 必须叫醒常驻线程，否则点击刷新会静默延迟")

    def test_breaker_state_change_wakes_loops(self):
        """熔断档位切换 = 轮询间隔变了，必须立刻重算睡眠长度。"""
        win = self.make_win()
        win.running = True
        with mock.patch.object(m, "feature_enabled", return_value=True):
            win._resched.clear()
            win._on_poll_result(False)          # 第 1 次失败：NORMAL 内不切档
            self.assertFalse(win._resched.is_set(),
                             "未跨档不应叫醒 —— 否则又退化成每拍都动")
            win._poll_fail = 2
            win._on_poll_result(False)          # 累计 3 次 → DEGRADED，跨档
            self.assertTrue(win._resched.is_set())

    def test_minute_interval_change_wakes_loops(self):
        """展开/收起图表改的是分时拉取间隔，同样要叫醒分时线程。"""
        win = self.make_win()
        win.running = True
        win._resched.clear()
        win._set_minute_interval(7.0)
        self.assertTrue(win._resched.is_set())
        self.assertEqual(win._minute_interval, 7.0)

    def test_sleep_returns_false_once_quit_latched(self):
        """退出闩锁已置位时，_sleep 必须立刻返回 False（不得再睡）。"""
        win = self.make_win()
        win.running = True
        win._wake.set()
        t0 = time.monotonic()
        self.assertFalse(win._sleep(30.0))
        self.assertLess(time.monotonic() - t0, 0.5,
                        "退出信号已置位却还去等 30 秒 —— join 必然超时")

    def test_sleep_woken_by_resched_and_keeps_running(self):
        """被 _resched 叫醒后要清标志、继续跑（区别于退出的"一次性"）。"""
        win = self.make_win()
        win.running = True
        win._wake.clear()
        win._resched.clear()
        out = {}

        def sleeper():
            out["ret"] = win._sleep(10.0)
            out["cleared"] = not win._resched.is_set()

        th = threading.Thread(target=sleeper, daemon=True)
        th.start()
        time.sleep(0.05)
        win._resched.set()
        th.join(timeout=2.0)
        self.assertFalse(th.is_alive(), "_sleep 没有被 _resched 叫醒")
        self.assertTrue(out.get("ret"), "重新决策后线程应继续运行（running 仍为真）")
        self.assertTrue(out.get("cleared"), "消费完必须清标志，否则会一直空醒")

    def test_shutdown_threads_wakes_blocked_loops(self):
        """_shutdown_threads 必须同时 set _wake 与 _resched。

        只 set _wake 的话：_sleep 等的是 _resched，长睡中的线程不会被叫醒，
        join(1.5) 必然落空，退出时线程带着未完成的请求被进程强杀。
        """
        win = self.make_win()
        win.running = True
        win._resched.clear()
        with mock.patch.object(m, "_NET_POOL", mock.Mock()):
            win._shutdown_threads()
        self.assertTrue(win._wake.is_set())
        self.assertTrue(win._resched.is_set(),
                        "退出时必须叫醒等 _resched 的线程")


# =====================================================================
# P2  数据交付：入队即唤醒，且绝不跨线程跑 _poll
# =====================================================================
class TestUiWakeup(_WinCase):

    def test_q_put_emits_wakeup(self):
        win = self.make_win()
        win.running = True
        hits = []
        win.ui_wakeup.connect(lambda: hits.append(1))
        win._q_put(("net", True))
        self.assertEqual(len(hits), 1, "入队必须唤醒一次 UI")

    def test_no_emit_after_quit(self):
        """退出中不再 emit：队列里残留的数据由收尾路径消化。"""
        win = self.make_win()
        win.running = False
        hits = []
        win.ui_wakeup.connect(lambda: hits.append(1))
        win._q_put(("net", True))
        self.assertEqual(hits, [])

    def test_watchdog_interval_is_bounded(self):
        """看门狗仍是有限值 —— 信号万一丢失，积压最迟一个周期被消化。"""
        self.assertEqual(ui_poll_interval_ms(False, True), UI_WATCHDOG_INTERVAL_MS)
        self.assertEqual(ui_poll_interval_ms(True, True),
                         int(POLL_INTERVAL_MINIMIZED * 1000))

    def test_cross_thread_emit_does_not_run_poll_on_worker(self):
        """最关键的一条：后台线程 emit，_poll 必须在**主线程**执行。

        连接若写成 AutoConnection，Qt 会比较"发信号的线程"与接收者线程；从
        Python 原生线程直接 emit 时它拿不到 current thread 的 Qt 归属，会被判成
        同线程而走 DirectConnection —— _poll 就在后台线程上跑，等于跨线程操作
        QWidget（画花甚至崩溃）。这里用真实线程验证连接类型确实是排队的。
        """
        ensure_app()
        calls = []
        main = threading.current_thread()
        original = m.FloatWindow._poll

        def recorder(self):
            calls.append(threading.current_thread())
            return original(self)

        # 必须在构造之前换掉：__init__ 里 connect 抓的是当时的**绑定方法**，
        # 之后再改类属性拦不到已经连上的槽。
        self.track_patch(mock.patch.object(m.FloatWindow, "_poll", recorder))
        # 用 make_win 而不是裸构造：夹具会停掉两条常驻循环，否则后台线程在
        # tearDown 关窗时仍在跑，离屏插件下直接 SIGSEGV。
        win = self.make_win()
        win.running = True
        worker = threading.Thread(target=lambda: win._q_put(("net", True)),
                                  daemon=True)
        worker.start()
        worker.join(timeout=2.0)
        # 主线程没转事件循环，排队连接就不该投递 —— 这一步证明它不是直连
        self.assertEqual(calls, [],
                         "后台线程 emit 却已执行 _poll —— 连接不是 QueuedConnection")
        m.QApplication.processEvents()
        self.assertEqual(len(calls), 1, "主线程事件循环未取到唤醒事件")
        self.assertIs(calls[0], main, "_poll 必须在主线程执行")


# =====================================================================
# P3  置顶复核：状态没变就不要惊动 WindowServer
# =====================================================================
class _FakeRect(object):
    def __init__(self, v):
        self.v = v

    def __eq__(self, other):
        return isinstance(other, _FakeRect) and other.v == self.v

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.v)


class _FakeNSWindow(object):
    """NSWindow 替身：只实现 _top_state_ok / _force_top 用到的接口。"""

    def __init__(self, level=25, visible=True, active=True, behavior=1 << 8,
                 frame=None, fail=False):
        self._level = level
        self._visible = visible
        self._active = active
        self._behavior = behavior
        self._frame = frame or _FakeRect("f")
        self._fail = fail
        self._hides = False
        self._ignores_mouse = False
        self.writes = []

    def _check(self):
        if self._fail:
            raise RuntimeError("objc boom")

    def level(self):
        self._check()
        return self._level

    def isVisible(self):
        self._check()
        return self._visible

    def isOnActiveSpace(self):
        self._check()
        return self._active

    def collectionBehavior(self):
        self._check()
        return self._behavior

    def frame(self):
        self._check()
        return self._frame

    def setLevel_(self, v):
        self.writes.append(("setLevel_", v))

    def setCollectionBehavior_(self, v):
        self.writes.append(("setCollectionBehavior_", v))

    def setHidesOnDeactivate_(self, v):
        self._hides = v
        self.writes.append(("setHidesOnDeactivate_", v))

    def hidesOnDeactivate(self):
        self._check()
        return self._hides

    def ignoresMouseEvents(self):
        self._check()
        return self._ignores_mouse

    def setIgnoresMouseEvents_(self, v):
        self._ignores_mouse = v
        self.writes.append(("setIgnoresMouseEvents_", v))

    def setBecomesKeyOnlyIfNeeded_(self, v):
        self.writes.append(("setBecomesKeyOnlyIfNeeded_", v))

    def orderFrontRegardless(self):
        self.writes.append(("orderFrontRegardless", None))

    def orderFront_(self, _):
        self.writes.append(("orderFront_", None))

    def orderWindow_relativeTo_(self, mode, number):
        # v1.4.35：置顶路径末尾把"背景层在内容窗之下"显式交给 WindowServer，
        # 替身必须记下这一笔，否则断言"发了什么调用"会退化成断言"没抛异常"。
        self.writes.append(("orderWindow_relativeTo_", (mode, number)))

    def windowNumber(self):
        # 真 NSWindow 一定有号；替身缺这个方法会让 v1.4.35 的相对摆层在测试里
        # 走 except 分支（SWALLOWED 一条），把"没断言"伪装成"测过了"。
        return 1


class TestTopStateCheck(_WinCase):

    def _ok_pair(self, behavior):
        """构造一对"状态完全正确"的替身窗口（主窗 + 玻璃窗）。"""
        frame = _FakeRect("same")
        main = _FakeNSWindow(level=25, behavior=behavior, frame=frame)
        glass = _FakeNSWindow(level=25, behavior=behavior, frame=frame)
        return main, glass

    def test_steady_state_does_no_writes(self):
        """状态本来就对 → 一个写调用都不发。这是本项优化的全部意义。"""
        import sf_glass
        win = self.make_win()
        behavior = sf_glass.public_overlay_collection_behavior()
        main, glass = self._ok_pair(behavior)
        win._glass_window = glass
        self.assertTrue(win._top_state_ok(main, glass))
        self.assertEqual(main.writes, [])
        self.assertEqual(glass.writes, [])

    def test_wrong_level_needs_work(self):
        import sf_glass
        win = self.make_win()
        main, _ = self._ok_pair(sf_glass.public_overlay_collection_behavior())
        main._level = 0
        self.assertFalse(win._top_state_ok(main, None))

    def test_invisible_needs_work(self):
        import sf_glass
        win = self.make_win()
        main, _ = self._ok_pair(sf_glass.public_overlay_collection_behavior())
        main._visible = False
        self.assertFalse(win._top_state_ok(main, None))

    def test_glass_frame_drift_needs_work(self):
        """玻璃层与主窗 frame 不一致 → 必须重做，否则用户看到背景错位。"""
        import sf_glass
        win = self.make_win()
        behavior = sf_glass.public_overlay_collection_behavior()
        main, glass = self._ok_pair(behavior)
        glass._frame = _FakeRect("other")
        self.assertFalse(win._top_state_ok(main, glass))

    def test_open_tool_window_no_longer_forces_full_path(self):
        """v1.4.28：工具窗开着**不再**单独判"需要重做"。

        改前这条分支的意图是"借每 250ms 的完整路径把面板抬回主窗之上"，代价实测为：
        面板/编辑器一开着，每 250ms 就白跑一次完整置顶，单次 2.6~3.5ms、
        p95 5.8ms、偶发 10~12.7ms —— 正好落在用户拖动或打字的时候，表现为"偶尔黏一下"。
        抬层的真实触发点只有"主窗刚被 orderFront"，那在完整路径里已就地补做
        （见 test_force_top_raises_open_tool_windows）。
        """
        import sf_glass
        win = self.make_win()
        main, glass = self._ok_pair(sf_glass.public_overlay_collection_behavior())
        win._glass_window = glass
        panel = mock.Mock()
        panel.isVisible.return_value = True
        win._target_panel = panel
        self.assertTrue(win._top_state_ok(main, glass),
                        "面板开着仍判需要重做 → 每 250ms 一次主线程停顿又回来了")

    def test_force_top_raises_open_tool_windows(self):
        """真的重做一次置顶之后，必须把可见工具窗抬回主窗之上（P1 拆出去的那一半）。"""
        import sf_glass
        win = self.make_win()
        main, glass = self._ok_pair(sf_glass.public_overlay_collection_behavior())
        main._level = 0                      # 制造"状态不对"，逼完整路径执行
        win._glass_window = glass
        view = mock.Mock()
        view.window.return_value = main
        fake_objc = mock.Mock()
        fake_objc.objc_object.return_value = view
        self.track_patch(mock.patch.object(sf_glass, "_objc_deps",
                                           return_value=(fake_objc, mock.Mock())))
        # v1.4.33：取 NSWindow 统一走 content_ns_window()，它第一行就问平台
        # （offscreen 下 winId() 不是 NSView*，真去转换是不可捕获的段错误）。
        # 本用例要驱动"拿到窗口之后"的真实逻辑，所以显式声明"假装在 cocoa 上"。
        self.track_patch(mock.patch.object(sf_glass, "native_cocoa_active",
                                           lambda: True))
        self.track_patch(mock.patch.object(win, "winId", return_value=1))
        self.track_patch(mock.patch.object(win, "_sync_glass_frame", lambda *a, **k: None))
        self.track_patch(mock.patch.object(win, "_sync_open_tool_windows", lambda: None))
        raised = []
        self.track_patch(mock.patch.object(win, "_raise_open_tool_windows",
                                           lambda: raised.append(True)))
        sf_glass.GlassMixin._force_top(win)
        self.assertIn(("orderFrontRegardless", None), main.writes,
                      "完整路径没重做置顶，后面的抬层也就无从谈起")
        self.assertEqual(raised, [True], "主窗前置后没有把工具窗抬回去")

    def test_read_failure_is_conservative(self):
        """读属性抛异常一律判"需要重做"：宁可白干一次，不能漏置顶。"""
        win = self.make_win()
        bad = _FakeNSWindow(fail=True)
        self.assertFalse(win._top_state_ok(bad, None))

    def test_force_top_skips_full_path_when_ok(self):
        """端到端：体检通过时 _force_top 不得调用任何 setLevel_/orderFront。"""
        import sf_glass
        win = self.make_win()
        behavior = sf_glass.public_overlay_collection_behavior()
        main, glass = self._ok_pair(behavior)
        win._glass_window = glass
        view = mock.Mock()
        view.window.return_value = main
        fake_objc = mock.Mock()
        fake_objc.objc_object.return_value = view
        self.track_patch(mock.patch.object(sf_glass, "_objc_deps",
                                           return_value=(fake_objc, mock.Mock())))
        # v1.4.33：取 NSWindow 统一走 content_ns_window()，它第一行就问平台
        # （offscreen 下 winId() 不是 NSView*，真去转换是不可捕获的段错误）。
        # 本用例要驱动"拿到窗口之后"的真实逻辑，所以显式声明"假装在 cocoa 上"。
        self.track_patch(mock.patch.object(sf_glass, "native_cocoa_active",
                                           lambda: True))
        self.track_patch(mock.patch.object(win, "winId", return_value=1))
        # 夹具为了离屏能安全构造窗口，把 FloatWindow._force_top 换成了空实现；
        # 这里必须显式调用 GlassMixin 上的**真实**实现，否则"体检通过所以跳过"
        # 和"根本没执行"两种情况都表现为零写入，用例就失去意义了。
        sf_glass.GlassMixin._force_top(win)
        self.assertEqual(main.writes, [], "稳态下仍向 WindowServer 发了写指令")
        self.assertEqual(glass.writes, [])

    def test_force_top_redoes_when_level_wrong(self):
        """体检不过 → 完整路径照旧执行（行为不回退）。"""
        import sf_glass
        win = self.make_win()
        behavior = sf_glass.public_overlay_collection_behavior()
        main, glass = self._ok_pair(behavior)
        main._level = 0
        win._glass_window = glass
        view = mock.Mock()
        view.window.return_value = main
        fake_objc = mock.Mock()
        fake_objc.objc_object.return_value = view
        self.track_patch(mock.patch.object(sf_glass, "_objc_deps",
                                           return_value=(fake_objc, mock.Mock())))
        # v1.4.33：取 NSWindow 统一走 content_ns_window()，它第一行就问平台
        # （offscreen 下 winId() 不是 NSView*，真去转换是不可捕获的段错误）。
        # 本用例要驱动"拿到窗口之后"的真实逻辑，所以显式声明"假装在 cocoa 上"。
        self.track_patch(mock.patch.object(sf_glass, "native_cocoa_active",
                                           lambda: True))
        self.track_patch(mock.patch.object(win, "winId", return_value=1))
        self.track_patch(mock.patch.object(win, "_sync_glass_frame", lambda *a, **k: None))
        self.track_patch(mock.patch.object(win, "_sync_open_tool_windows", lambda: None))
        sf_glass.GlassMixin._force_top(win)
        self.assertIn(("setLevel_", 25), main.writes)
        self.assertIn(("orderFrontRegardless", None), main.writes)


# =====================================================================
# P4  图标队列门控
# =====================================================================
class TestIconGate(_WinCase):

    def test_empty_queue_after_dock_applied_is_skipped(self):
        """Dock 图标已落地且队列为空 → 本轮不必再排一遍空队列。"""
        win = self.make_win()
        win._dock_applied = True
        self.assertFalse(win._icons_pending())

    def test_pending_dock_still_checked(self):
        """Dock 图标还没落地：必须继续检查，否则图标可能永远不出现。"""
        win = self.make_win()
        win._dock_applied = False
        self.assertTrue(win._icons_pending())

    def test_non_empty_queue_is_processed(self):
        import sf_icons
        win = self.make_win()
        win._dock_applied = True
        sf_icons._ICON_QUEUE.put(("app_icon", None))
        try:
            self.assertTrue(win._icons_pending())
        finally:
            try:
                sf_icons._ICON_QUEUE.get_nowait()
            except Exception:
                pass


# =====================================================================
# P5  钳制位置的写盘去重
# =====================================================================
class TestClampSaveDedup(_WinCase):

    def test_repeated_clamp_at_same_pos_writes_once(self):
        """屏幕几何抖动时不得反复写配置（一次写 = 读底稿+备份+原子替换）。"""
        win = self.make_win()
        win._clamped = False
        win._clamped_saved_pos = None
        writes = []
        self.track_patch(mock.patch.object(m, "save_config",
                                           side_effect=lambda *a, **k: writes.append(a) or True))
        from PyQt6.QtCore import QRect
        from PyQt6.QtGui import QGuiApplication
        scr = mock.Mock()
        # 可用区故意与窗口不相交 → 每次都判定"需要钳回并写盘"
        scr.availableGeometry.return_value = QRect(10000, 10000, 800, 600)
        # _clamp_to_screen 里 QGuiApplication 是函数内 import 的，模块级没有这个名字，
        # 所以要 patch 真身而不是 stock_float_window 上的别名。
        self.track_patch(mock.patch.object(QGuiApplication, "screenAt",
                                           return_value=scr))
        self.track_patch(mock.patch.object(QGuiApplication, "primaryScreen",
                                           return_value=scr))
        win._clamp_to_screen(force=True)
        first = len(writes)
        win._clamp_to_screen(force=True)
        win._clamp_to_screen(force=True)
        self.assertGreaterEqual(first, 1, "首次越界必须写盘")
        self.assertEqual(len(writes), first,
                         "同一坐标被重复写盘：%d 次（应为 1 次）" % len(writes))


# =====================================================================
# v1.4.13：分时图末端呼吸灯的帧率，不许用"少画一帧"去换
# =====================================================================
class TestChartDotFrameRateKept(_WinCase):
    """锁死一条老板划的红线：不得靠降低可见动效的帧率来省电。

    来龙去脉（v1.4.12 曾犯、v1.4.13 回退）：那条改动认为"动画定时器 ≤16ms 内
    会重画价格条带，所以每条行情到达时的整窗重绘是纯重复"。对价格数字成立，
    对呼吸灯不成立 ——

      · `_draw_chart_dot` 的呼吸相位按**墙上时钟**算
        （`time.monotonic() % CHART_DOT_BREATH_PERIOD`），只有真正重画图表那一帧才更新；
      · 价格动画的局部脏区按设计**绝不侵入分时图区**（`price_update_rect` 底部截断到
        `CHART_TOP`），所以动画帧一帧都不会重画图表。

    省掉那次整窗重绘后，重绘次数从"每条行情一次"(≈10/秒) 掉到只剩分时数据到达那一次
    (≈1/秒)，呼吸灯变成约 1 秒一跳。下面三条用例把这三层耦合分别钉住。
    """

    def quote(self, price, high="1520.00", low="1480.00"):
        return {"name": "贵州茅台", "code": "600519", "price": price,
                "pct": "1.34", "change": "20.00", "pre_close": "1490.00",
                "high": high, "low": low, "volume": "1",
                "limit_up": "", "limit_down": ""}

    def prime(self, win, visible=True):
        """摆成"已显示 1510.00 / +1.34%"的稳定状态。

        `visible` 钉住 `_animations_suspended()`：离屏窗口 `isVisible()` 不可靠，
        不钉住的话用例会全部滑进"微小变动直赋"分支，断言看着过、其实什么都没测。
        """
        win._disp_price = 1510.00
        win._disp_pct = 1.34
        win._anim_from_price = 1510.00
        win._anim_to_price = 1510.00
        win._anim_from_pct = 1.34
        win._anim_to_pct = 1.34
        win.quote = self.quote("1510.00")
        win._need_redraw = False
        self.track_patch(mock.patch.object(win, "_animations_suspended",
                                           return_value=not visible))

    def fake_timer(self, win, active):
        timer = mock.Mock()
        timer.isActive.return_value = active
        win._anim_timer = timer
        return timer

    def test_roll_start_still_requests_full_repaint(self):
        """核心断言：动画定时器已在跑、日内高低也没变，仍必须请求整窗重绘。

        这条就是 v1.4.12 被砍掉的那一帧。砍掉的直接后果是呼吸灯掉到约 1 帧/秒。
        """
        win = self.make_win()
        self.prime(win)
        timer = self.fake_timer(win, active=True)
        win._apply_quote(self.quote("1510.50"))
        timer.start.assert_called_once_with()
        self.assertTrue(win._need_redraw,
                        "呼吸灯只随整窗帧更新，这里不请求重绘就会把它冻成约 1 秒一跳")

    def test_animation_partial_rect_never_covers_chart(self):
        """证明"动画帧救不了呼吸灯"：价格局部脏区被截断在分时图顶之上。

        若哪天有人把脏区往下扩到图表区，本用例就会红 —— 那也意味着动画期会
        反复覆盖图表顶部（v1.2.43 当初正是为此截断的）。
        """
        from sf_ui_render import CHART_TOP, price_update_rect
        x, y, w, h = price_update_rect(184)
        self.assertLessEqual(y + h, CHART_TOP + 1,
                             "价格动画脏区伸进了分时图区，呼吸灯的刷新路径就变了")

    def test_breathing_dot_only_changes_on_full_repaint(self):
        """像素级实证：呼吸灯确实按墙上时钟呼吸，所以它**需要**持续的整窗帧来驱动。

        只断言"整窗重绘会改变图表区墨量"这一半。局部帧那一半在这里测不了：
        `QWidget.render()` 总是按整窗设备矩形走，`_paint_event_inner` 拿到的
        `e.rect()` 永远不等于价格脏区，因此永远命中完整路径 —— 想走局部路由就得
        让 `QPainter(self)` 画到真实窗口上，离屏环境做不到。
        局部脏区不含图表区这件事，由上面的几何断言
        `test_animation_partial_rect_never_covers_chart` 钉住，两条合起来才完整。
        """
        import sf_ui_render as R
        from PyQt6.QtGui import QImage, QPainter
        win = self.make_win()
        win._expanded_h = None
        win.resize(win.width(), win._height_expanded())
        m.QApplication.processEvents()
        win.minute_points = [(9 * 60 + i, 20.0 + (i % 30) * 0.01) for i in range(120)]
        win.quote = self.quote("1510.00")
        win._disp_price = 1510.00
        win._disp_pct = 1.34
        win._partial_price = None
        win._chart_changed = True
        W, H = win.width(), win.height()
        if H - R.CHART_TOP < R.CHART_MIN_DRAW_H:
            self.skipTest("窗口太矮，分时图整幅不画，无法验证呼吸灯")

        def chart_ink():
            img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(0)
            p = QPainter(img)
            try:
                win.render(p)
            finally:
                p.end()
            total = 0
            for yy in range(R.CHART_TOP, H - 1, 3):
                for xx in range(1, W - 1, 3):
                    c = img.pixelColor(xx, yy)
                    total += c.alpha() + c.red() + c.green() + c.blue()
            return total

        # 呼吸相位取一整个周期的两端：光晕 alpha 与半径都到极值 → 图表区墨量必须变。
        # 变了，才说明"少一次整窗帧 = 少一次呼吸灯更新"这个因果成立。
        with mock.patch.object(R.time, "monotonic", return_value=0.0):
            a = chart_ink()
        with mock.patch.object(R.time, "monotonic",
                               return_value=R.CHART_DOT_BREATH_PERIOD / 2.0):
            b = chart_ink()
        self.assertNotEqual(a, b,
                            "整窗重绘都换不了呼吸灯，说明它没按墙上时钟呼吸，"
                            "本用例的因果前提不成立（需重新评估帧率结论）")

    def test_suspended_window_still_repaints(self):
        """缩到 Dock / 不可见：动画被挂起，走"直赋 + 整窗重绘"。"""
        win = self.make_win()
        self.prime(win, visible=False)
        timer = self.fake_timer(win, active=True)
        win._apply_quote(self.quote("1510.50"))
        timer.start.assert_not_called()
        timer.stop.assert_called_once_with()
        self.assertTrue(win._need_redraw)

    def test_dot_timer_runs_when_chart_visible(self):
        """v1.4.14：图表展开且有数据时，呼吸灯必须有 16ms 专属时钟在驱动。

        这条是 v1.4.14 帧率审计量出来的原缺陷 —— 呼吸灯此前没有自己的定时器，
        价格一停就不呼吸（实测 0.2 帧/秒）。
        """
        win = self.make_win()
        win._chart_expanded = True
        win._minute_stale = False
        win.minute_points = [(9 * 60 + i, 20.0 + i * 0.01) for i in range(60)]
        with mock.patch.object(win, "isVisible", return_value=True), \
             mock.patch.object(win, "_is_trading_time", return_value=True):
            win._sync_dot_timer()
            self.assertTrue(win._dot_timer.isActive(), "呼吸灯没有被武装，价格静止时就是冻住的")
            self.assertEqual(win._dot_timer.interval(), 16,
                             "间隔不是 16ms，60Hz 屏上就到不了满帧")

    def test_dot_timer_idle_off_market(self):
        """收盘后不呼吸：呼吸灯是"实时"暗示，没有实时数据就不该演活着。

        这不是省电夹带 —— 不加这道闸门，盘后 CPU 实测从 0.62% 单核涨到 25.72%（41 倍），
        换来的是一条 2 秒周期的正弦在无人观看、也没有新数据的时候满帧摆动，
        语义上与 v1.4.7"数据滞后时冻结呼吸灯、绝不冒充实时"自相矛盾。
        """
        win = self.make_win()
        win._chart_expanded = True
        win._minute_stale = False
        win.minute_points = [(9 * 60 + i, 20.0 + i * 0.01) for i in range(60)]
        with mock.patch.object(win, "isVisible", return_value=True), \
             mock.patch.object(win, "_is_trading_time", return_value=False):
            win._dot_timer.start()          # 先让它跑起来，验证停得掉
            win._sync_dot_timer()
            self.assertFalse(win._dot_timer.isActive(),
                             "非交易时段仍在满帧呼吸，既冒充实时又白烧 CPU")

    def test_dot_timer_stops_when_nothing_to_animate(self):
        """收起图表 / 无数据 / 滞后冻结 / 不可见 → 停表（这些时刻本来就没动画）。"""
        win = self.make_win()
        win.minute_points = [(9 * 60 + i, 20.0 + i * 0.01) for i in range(60)]
        win._chart_expanded = True
        cases = {
            "图表收起": {"_chart_expanded": False},
            "无分时数据": {"minute_points": []},
            "数据滞后冻结": {"_minute_stale": True},
        }
        for label, patch_attrs in cases.items():
            saved = {k: getattr(win, k) for k in patch_attrs}
            for k, v in patch_attrs.items():
                setattr(win, k, v)
            try:
                with mock.patch.object(win, "isVisible", return_value=True), \
                     mock.patch.object(win, "_is_trading_time", return_value=True):
                    win._sync_dot_timer()
                    self.assertFalse(win._dot_timer.isActive(),
                                     "%s 时呼吸灯仍在跑，白耗一整个 60 帧时钟" % label)
            finally:
                for k, v in saved.items():
                    setattr(win, k, v)
        with mock.patch.object(win, "isVisible", return_value=False), \
             mock.patch.object(win, "_is_trading_time", return_value=True):
            win._sync_dot_timer()
            self.assertFalse(win._dot_timer.isActive(), "窗口不可见时不该继续刷 60 帧")

    def test_other_animations_defer_to_dot_clock(self):
        """呼吸灯在跑时，其余 16ms 级动画不得各自再 request 一帧（否则双倍白画）。"""
        win = self.make_win()
        calls = []
        self.track_patch(mock.patch.object(win, "update",
                                           side_effect=lambda *a: calls.append(a)))
        win._dot_timer.start()
        try:
            win._repaint_now()
            self.assertEqual(calls, [], "主画钟已在跑，还额外请求整窗帧")
            win._repaint_now(0, 100, 184, 40)
            self.assertEqual(calls, [], "主画钟已在跑，还额外请求局部帧")
        finally:
            win._dot_timer.stop()
        win._repaint_now()
        self.assertEqual(calls, [tuple()])
        win._repaint_now(0, 100, 184, 40)
        self.assertEqual(calls, [tuple(), (0, 100, 184, 40)])

    # ---------------- v1.4.15 修复复核 F1 / F2 / F4 ----------------
    def seeded(self, win):
        """把窗口摆成"盘中 + 图表展开 + 有分时数据"，即呼吸灯主钟该跑的状态。"""
        win.running = True
        win._chart_expanded = True
        win._minute_stale = False
        win.minute_points = [(9 * 60 + i, 20.0 + i * 0.01) for i in range(60)]
        return win

    def test_poll_defers_full_repaint_to_dot_clock(self):
        """F1：主画钟在跑时，_poll 那次整窗重绘是重复排队，必须跳过。

        这是 v1.4.12 那个想法的正确版本 —— 不是无条件砍掉（那会把呼吸灯冻到
        0.6 帧/秒），而是只在"主钟 ≤16ms 内必然画一次整窗帧"时跳过。
        """
        win = self.seeded(self.make_win())
        calls = []
        self.track_patch(mock.patch.object(win, "update",
                                           side_effect=lambda *a: calls.append(a)))
        with mock.patch.object(win, "_is_trading_time", return_value=True), \
             mock.patch.object(win, "isVisible", return_value=True):
            win._sync_dot_timer()
            self.assertTrue(win._dot_timer.isActive(), "前置条件：主钟应在跑")
            win._need_redraw = True
            win._poll()
            self.assertFalse(win._need_redraw, "脏标记必须被消费，不能攒着")
            self.assertEqual(calls, [], "主钟在跑时 _poll 又排了一次整窗帧")

            win._dot_timer.stop()
            win._need_redraw = True
            with mock.patch.object(win, "_sync_dot_timer", lambda: None):
                win._poll()
            self.assertEqual(calls, [tuple()],
                             "主钟停了还不画 —— 新数据就上不屏（v1.4.13 护栏语义）")

    def test_hot_path_does_not_read_the_clock(self):
        """F2：16ms 热路径不得判"现在几点"，否则 2027 年起可能每秒读 60 次磁盘。

        `_is_trading_time → is_holiday → _peek_month`，未来月份缓存未命中时会
        `_load_month_cache` 读盘。时段闸门移到只在状态复核处求值的 `_dot_armed()`。
        """
        win = self.seeded(self.make_win())
        spy = mock.Mock(return_value=True)
        with mock.patch.object(win, "_is_trading_time", spy), \
             mock.patch.object(win, "isVisible", return_value=True):
            self.assertTrue(win._dot_should_run())
            spy.assert_not_called()
            self.assertTrue(win._dot_armed())
            spy.assert_called_once_with()

    def test_session_boundary_rearms_dot_clock(self):
        """F2 的配套：时段闸门挪出热路径后，收盘那一秒要有地方停表。"""
        win = self.seeded(self.make_win())
        with mock.patch.object(win, "isVisible", return_value=True), \
             mock.patch.object(win, "_is_trading_time", return_value=True):
            win._sync_dot_timer()
        self.assertTrue(win._dot_timer.isActive())
        with mock.patch.object(win, "isVisible", return_value=True), \
             mock.patch.object(win, "_is_trading_time", return_value=False), \
             mock.patch.object(win, "_arm_session_timer", lambda: None):
            win._on_session_boundary()
        self.assertFalse(win._dot_timer.isActive(),
                         "收盘后主钟还在跑：时段闸门已不在 16ms 路径上，"
                         "必须由边界复核停表")

    def test_dot_tick_paints_when_it_stops(self):
        """F4：主钟停表那一帧必须自己补画，否则动画帧也跳了、两边都不画。"""
        win = self.seeded(self.make_win())
        calls = []
        self.track_patch(mock.patch.object(win, "update",
                                           side_effect=lambda *a: calls.append(a)))
        win._dot_timer.start()
        with mock.patch.object(win, "isVisible", return_value=False):
            win._dot_tick()
        self.assertFalse(win._dot_timer.isActive(), "不可见时应停表")
        self.assertEqual(calls, [tuple()],
                         "停表时没补画 —— 同周期内动画帧已因'主钟会画'而跳过，会丢一帧")

    def test_tiny_move_branch_unchanged(self):
        """微小变动直赋分支：停掉动画，整窗重绘是唯一的上屏路径。"""
        win = self.make_win()
        self.prime(win)
        timer = self.fake_timer(win, active=True)
        win._apply_quote(self.quote("1510.001"))     # 变动 < ANIM_TINY_PRICE
        self.assertTrue(win._need_redraw)
        timer.stop.assert_called_once_with()
        self.assertEqual(win._disp_price, 1510.001)


if __name__ == "__main__":
    unittest.main(verbosity=2)
