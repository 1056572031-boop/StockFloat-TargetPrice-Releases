# -*- coding: utf-8 -*-
"""分时「数据暂缺 · 重试中」→ 恢复正常曲线这段时间的三条判据。

起因（2026-10-12 盘中实测，本机直连两个真实源站）：
  · 健康态 fetch_minute 全链 p50 46ms / p90 65ms / max 194ms（冷启动含 TLS 握手 114ms）；
  · 源站卡死不响应时**单次** http_get = 17.24s（4s 超时 ×3 次 + 2 次 0.5s 退避
    + 连接池失败后再走 urllib 一次 4s），而分时是主源→备用源**串行**
    ⇒ 一次分时尝试最长 34.3s；
  · 而 `_fetch_minute` 把 `_minute_lock` 攥在这 34.3s 上，主线程
    `_maybe_cache_first_screen`（每 20 秒一次）对同一把锁做**阻塞** acquire
    ⇒ 实测主线程被卡 **33.78s**：Qt 事件循环停在 `_apply_quote` 里，
    不重绘、不响应点击，**同一批队列里已经到手的分时数据也贴不上屏**。
    所以"重试中"可以在数据早就回来之后继续挂着。

三处对应三条判据，四处种错全部验过"改坏必红"（M1 合锁 / M2 超时调回默认 /
M2b 重试调回 2 / M3 重试间隔写回固定值）：
  GuardIsNotTheDataLock      —— 把两把锁合回去 → 红
  MinuteBudgetIsBounded      —— 预算调回全局默认 → 红（推导出的上限翻四倍）
  RetryWindowNeverSlower     —— 重试间隔写回固定 2.5s → 展开态那一档红
"""
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_quote
from sf_constants import HTTP_RETRIES, HTTP_TIMEOUT

try:
    from _base import SFCase, wait_for           # noqa: E402
except ImportError:                              # 以包形式被 discover 导入时
    from tests._base import SFCase, wait_for     # noqa: E402

import stock_float_window as m

# QApplication 必须被模块级引用持有：只调 SFCase.ensure_qapp() 丢掉返回值，PyQt 会在
# 包装器回收时连 C++ 侧一起销毁，下一个 QWidget 构造直接 abort()（无 traceback、退出码
# 134，看起来像用例把进程搞崩了）。
_APP = None


def _ensure_app():
    global _APP
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    return _APP


class _Internals(tuple):
    """本文件要直接驱动的主窗内部成员，一次性取好。

    为什么必须看内部状态（AGENTS §6 第 5 条要求把理由写在这里）：三条判据守的全是
    **没有对外输出**的调度事实 —— "哪把锁被谁攥住""失败期这一拍究竟睡了多久"
    "一次网络请求的墙钟预算是多少"。分时拉取在成功之前不产生任何可观测的界面变化，
    从公开接口（抓帧像素、配置文件、通知参数）无从判断这些量。
    """

    __slots__ = ()

    def __new__(cls, win):
        return super().__new__(cls, (
            win._minute_busy,               # discipline-ok(R5): 本轮新增的守卫，无公开读取口
            win._minute_lock,               # discipline-ok(R5): 数据锁，同上
            win._fetch_minute,              # discipline-ok(R5): 被测对象本身
            win._maybe_cache_first_screen,  # discipline-ok(R5): 被测对象本身（主线程那一手）
            win._seed_first_screen,         # discipline-ok(R5): 被测对象本身（冷启动那一手）
            win._minute_loop,               # discipline-ok(R5): 被测对象本身（调度循环）
        ))

    busy = property(lambda s: s[0])
    data_lock = property(lambda s: s[1])
    fetch_minute = property(lambda s: s[2])
    maybe_cache = property(lambda s: s[3])
    seed_first_screen = property(lambda s: s[4])
    minute_loop = property(lambda s: s[5])


def _set_base_interval(win, seconds):
    """把分时常规周期设成指定值（等价于用户展开/收起图表后的状态）。"""
    win._minute_interval = float(seconds)   # discipline-ok(R5): 常规周期没有 setter，只有这个字段


def _seeded(win):
    return win._seeded_from_cache           # discipline-ok(R5): 预填结果没有公开读取口


def _hold_until_told(lock, held, release, hold_sec=10.0):
    """后台线程抢住锁直到测试发话，用来证明"主线程不该等这把锁"。"""
    if not lock.acquire(blocking=False):
        return
    try:
        held.set()
        release.wait(hold_sec)
    finally:
        lock.release()


class _Host(SFCase):
    """公共夹具：真 FloatWindow，但后台循环换成 no-op、落盘与网络全部挡住。"""

    def setUp(self):
        super().setUp()
        _ensure_app()
        self.track_patch(patch.object(m, "load_config", return_value=(
            "sh600183", 100, 100, 200, "translucent")))
        # save_config 必须返回 True：否则每条用例结尾刷一行 "exit: config save failed"
        self.track_patch(patch.object(m, "save_config", lambda *a, **k: True))
        self.track_patch(patch.object(sf_quote, "http_get", lambda *a, **k: ""))
        self.track_patch(patch.object(m, "start_icon_worker", lambda: None))
        self.track_patch(patch.object(m.FloatWindow, "_force_top", lambda self: None))
        self.track_patch(patch.object(m.FloatWindow, "_setup_reopen_handler",
                                      lambda self: None))
        # 常驻循环必须在构造前就换成 no-op：只置 running=False 挡不住它自己改
        # _poll_state / set _resched，会把"谁唤醒了这一拍"污染成随机失败。
        self.track_patch(patch.object(m.FloatWindow, "_quote_loop_wrapper",
                                      lambda self: None))
        self.track_patch(patch.object(m.FloatWindow, "_minute_loop_wrapper",
                                      lambda self: None))
        # 首屏快照真的会写盘，这里换成记录器：判据只看"有没有被锁挡住"
        self.saved = []
        self.track_patch(patch.object(m, "save_last_quote",
                                      lambda *a, **k: self.saved.append(time.monotonic())))
        self.win = m.FloatWindow()
        self.win.running = False
        self.I = _Internals(self.win)
        for name in ("timer", "top_timer", "anim_timer", "flash_timer", "_dot_timer"):
            t = getattr(self.win, name, None)
            if t is not None:
                t.stop()

    def tearDown(self):
        # §6.11：关窗必须发生在 patch 还活着的时候，否则 closeEvent 会写用户真配置
        try:
            self.win._exit_persisted = True    # discipline-ok(R5): §6.11 规定的关窗前动作
            self.win.close()
        except Exception:
            pass
        super().tearDown()


class GuardIsNotTheDataLock(_Host):
    """F1：分时线程攥的是防重入守卫，不是主线程要用的数据锁。"""

    def _assert_ui_path_skips_the_guard(self, call):
        """通用形状：后台攥住在飞守卫不放，主线程那一手必须照样立刻返回。

        判据不用"睡一小觉再看"那种魔法超时 —— 后台攥到测试发话为止，所以
        **只要主线程这次调用返回了**，比较"它返回时守卫是否仍未释放"就是确定的。
        """
        held, release = threading.Event(), threading.Event()
        th = threading.Thread(target=lambda: _hold_until_told(self.I.busy, held, release),
                              daemon=True)
        th.start()
        self.assertTrue(held.wait(2.0), "后台线程没能在 2s 内抢到守卫，夹具失效")
        try:
            call()
            still_held = not release.is_set()
        finally:
            release.set()
            th.join(timeout=5)
        self.assertTrue(still_held,
                        "主线程那一手一直等到分时请求结束才返回 —— 它又去抢在飞守卫了")

    def test_ui_snapshot_path_never_waits_for_the_inflight_fetch(self):
        self._assert_ui_path_skips_the_guard(self.I.maybe_cache)
        self.assertTrue(wait_for(lambda: len(self.saved) == 1), "后台快照没有完成")
        self.assertEqual(len(self.saved), 1, "快照本身仍要写出去，别把功能一起挡没了")

    def test_seed_first_screen_path_also_bypasses_the_guard(self):
        """冷启动预填同样不许等在飞请求上（它也读 minute_points）。"""
        snap = {"code": "600183", "quote": {"price": 122.11, "pct": -1.64},
                "minute": [["1107", 121.63, 123.47]], "day": "2026-10-12"}
        # sf_constants 在测试隔离态把预填整个关掉（SF_SEED_FIRST_SCREEN=0），
        # 不设回 1 的话这条用例测的是"函数直接 return"，守卫那半句会假绿。
        with patch.dict(os.environ, {"SF_SEED_FIRST_SCREEN": "1"}), \
             patch.object(m, "load_last_quote", return_value=snap):
            self._assert_ui_path_skips_the_guard(self.I.seed_first_screen)
        self.assertTrue(_seeded(self.win), "预填本身仍要生效")
        self.assertEqual(len(self.win.minute_points), 1, "分时曲线要预填进来")

    def test_reentrancy_guard_is_still_enforced(self):
        """拆锁不许把"上一请求还在飞就跳过"这条语义一起拆掉。"""
        got = self.I.busy.acquire(blocking=False)
        self.assertTrue(got)
        try:
            self.assertIsNone(self.I.fetch_minute(),
                              "守卫被攥住时必须返回 None（防重入），不能排队等")
        finally:
            self.I.busy.release()

    def test_guard_is_released_even_when_the_fetch_raises(self):
        """网络抛异常也要还守卫，否则一次故障就把分时线程永久锁死。"""
        with patch.object(m, "fetch_minute", side_effect=OSError("boom")), \
             patch.object(m, "_is_trading_time", return_value=True):
            self.assertFalse(self.I.fetch_minute())
        self.assertTrue(self.I.busy.acquire(blocking=False),
                        "一次失败之后守卫没释放，此后所有分时拉取都会静默跳过")
        self.I.busy.release()

    def test_the_two_locks_are_not_the_same_object(self):
        """最硬的一条：两把锁必须是两把（合并回一把时当场红）。"""
        self.assertIsNot(self.I.busy, self.I.data_lock)


class MinuteBudgetIsBounded(_Host):
    """F2：分时这一路的网络预算单独收紧，并且一次尝试的墙钟上限可推导。"""

    def _record(self, fn, *a):
        seen = []

        def fake_get(url, gbk=True, timeout=None, retries=None, **kw):
            seen.append({"url": url, "timeout": timeout, "retries": retries})
            return "{}"

        with patch.object(sf_quote, "http_get", side_effect=fake_get):
            try:
                fn(*a)
            except Exception:
                pass          # 空 JSON 走不通解析不是本判据关心的事
        return seen

    def test_both_minute_sources_pass_a_bounded_budget(self):
        primary = sf_quote._fetch_minute_primary  # discipline-ok(R5): 主源没有公开入口能单独量它的入参
        for seen, name in ((self._record(primary, "sh600183"), "主源"),
                           (self._record(sf_quote.fetch_minute_fallback, "sh600183"), "备源")):
            self.assertEqual(len(seen), 1, "%s 应该只发一次 http_get" % name)
            self.assertIsNotNone(seen[0]["timeout"], "%s 没显式给超时，吃的是全局默认" % name)
            self.assertIsNotNone(seen[0]["retries"], "%s 没显式给重试次数" % name)
            self.assertLessEqual(seen[0]["timeout"], 2.5,
                                 "%s 超时 %ss 太宽：健康态实测最坏 0.194s"
                                 % (name, seen[0]["timeout"]))
            self.assertEqual(seen[0]["retries"], 0,
                             "%s 重试次数应为 0（http_get 自带一次 urllib 兜底）" % name)

    def test_derived_worst_case_attempt_stays_bounded(self):
        """一次分时尝试的墙钟上限 = 2 个源 × (pooled 若干次 + 退避 + urllib 兜底一次)。

        这条才是本轮真正要守的东西：34.3s → 8s。把常量调回全局默认（4s/2 次）这里
        立刻翻四倍判红，而不是等到用户盯着一动不动的分时图才发现。
        """
        per_source = (sf_quote.MINUTE_HTTP_TIMEOUT * (sf_quote.MINUTE_HTTP_RETRIES + 1)
                      + 0.5 * sf_quote.MINUTE_HTTP_RETRIES   # 每次重试前退避 0.5s
                      + sf_quote.MINUTE_HTTP_TIMEOUT)        # 连接池失败后的 urllib 一次
        worst = per_source * 2                                # 主源 → 备用源 串行
        self.assertLessEqual(worst, 10.0,
                             "一次分时尝试最长 %.1fs，用户就要多等这么久才看得见恢复" % worst)

    def test_minute_budget_is_tighter_than_the_global_default(self):
        """分时比行情/搜索更容易"串行叠加"，所以它的预算必须比全局默认更紧。"""
        self.assertLess(sf_quote.MINUTE_HTTP_TIMEOUT, HTTP_TIMEOUT)
        self.assertLess(sf_quote.MINUTE_HTTP_RETRIES, HTTP_RETRIES)


class _VirtualClock:
    """假时钟：**循环自己要求睡多久，虚拟时间就前进多少**。

    与 tests/test_v130.py 那套"把时刻序列喂进去"的驱动法的区别（那套测不出间隔）：
    那里 fetch 之间的间隔等于夹具写死的 times 差值，把 MINUTE_RETRY_INTERVAL 改成
    任何值都不会改变结果 —— 判据恒真。这里时刻只由 `_sleep(请求量)` 推进，
    所以观察到的节奏是代码算出来的，改错间隔一定看得出来。

    起点取 1000.0 而不是 0.0：QuoteLoopSchedule.last 初值是 0，真机上
    time.monotonic() 从开机算起必然远大于 interval，第一拍就该拉取；从 0 起算会让
    第一拍"未到期"，测出来的节奏和实机不是一回事。
    """

    def __init__(self):
        self.t = 1000.0
        self.fetches = []
        self.stopped = False

    def monotonic(self):
        return self.t

    def sleep(self, sec):
        self.t += sec
        return not self.stopped


class RetryWindowNeverSlower(_Host):
    """F3：失败后的重试节奏只许比常规更快，不许更慢。"""

    def _drive(self, base_interval, results, max_fetches=12):
        clk = _VirtualClock()

        def fake_fetch():
            clk.fetches.append(clk.t)
            idx = len(clk.fetches) - 1
            if idx >= len(results):
                clk.stopped = True
                return False
            ok = results[idx]
            if len(clk.fetches) >= max_fetches:
                clk.stopped = True
            return ok

        _set_base_interval(self.win, base_interval)
        with patch.object(m.time, "monotonic", side_effect=lambda: clk.monotonic()), \
             patch.object(m, "seconds_to_session_change", return_value=0.0), \
             patch.object(self.win, "_is_trading_time", return_value=True), \
             patch.object(self.win, "_take_force", lambda name: False), \
             patch.object(self.win, "_fetch_minute", side_effect=fake_fetch), \
             patch.object(self.win, "_sleep", side_effect=lambda s: clk.sleep(s)):
            self.win.running = True
            try:
                self.I.minute_loop()
            finally:
                self.win.running = False
        return [round(b - a, 3) for a, b in zip(clk.fetches, clk.fetches[1:])]

    def test_expanded_state_failure_keeps_the_normal_cadence(self):
        """展开态常规 1s：失败后仍按 1s 拉，不许被"快速重试"拖到 2.5s。

        这一条就是本轮那处倒挂的判据：改回固定 2.5s 之后这里的节奏被放慢 2.5 倍，
        而展开态正是用户唯一看得见分时图、看得见"重试中"那一档。
        """
        deltas = self._drive(1.0, [False] * 8)
        self.assertGreaterEqual(len(deltas), 7, "拉取次数不够，夹具失效")
        self.assertTrue(all(abs(d - 1.0) < 1e-6 for d in deltas),
                        "展开态失败期的实际节奏=%s，常规周期是 1.0s" % deltas)

    def test_collapsed_state_failure_retries_faster_than_its_base(self):
        """收起态常规 30s：快速重试必须真的快。

        第一格是 10s 而不是 2.5s —— LOOP_TICK_CAP=5.0 让这一拍先按上一拍的 30s 决策
        睡了 5 秒、醒来仍未到期又睡 5 秒，第三次才发现自己进了重试窗口。收起态看不见
        分时图，这一格不值得为它放宽判据；稳态 2.5s 才是要守的东西。
        """
        deltas = self._drive(30.0, [False] * 8)
        self.assertTrue(all(abs(d - 2.5) < 1e-6 for d in deltas[1:5]),
                        "收起态重试窗口稳态应为 2.5s，实测=%s" % deltas)
        self.assertLess(min(deltas), 30.0, "失败期从没比 30s 常规更快过")

    def test_retry_interval_never_exceeds_the_base_interval(self):
        """通用不变量：进入重试窗口之后，任何一档常规周期下节奏都不许比常规更慢。

        取 deltas[1]（窗口已建立）而不是 deltas[0]（那一格被上一拍的决策定死）。
        旧实现在 base=1.0 与 base=2.0 两档都会红（2.5s > 常规）。
        """
        for base in (1.0, 2.0, 5.0, 30.0):
            deltas = self._drive(base, [False] * 6)
            self.assertLessEqual(deltas[1], base + 1e-6,
                                 "常规 %.1fs 时失败期出现 %.1fs，比常规还慢"
                                 % (base, deltas[1]))

    def test_success_returns_to_the_normal_cadence(self):
        """恢复即回常规周期，不留在高频档空转。"""
        deltas = self._drive(30.0, [False, False, True, True, True])
        self.assertLess(deltas[1], 30.0, "失败期应已进入快速重试")
        self.assertTrue([d for d in deltas[2:] if d > 20.0],
                        "成功之后没有回到 30s 常规节奏，实测=%s" % deltas)


if __name__ == "__main__":
    unittest.main()
