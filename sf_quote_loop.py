# -*- coding: utf-8 -*-
"""sf_quote_loop —— 上帝类 FloatWindow 拆分第二步②（v1.2.40）：行情循环与轮询调度。

背景：上帝类拆分规划方向②「行情循环与轮询调度（熔断状态机/间隔决策/行情线程循环）」
（见 FloatWindow 类 docstring 与 sf_scheduler.py 模块 docstring）。

v1.2.39 已把「熔断状态 → 交易时段轮询间隔」映射下沉到 sf_scheduler.compute_poll_interval；
v1.2.40 继续下沉剩余调度决策（纯逻辑，无 Qt/网络副作用）：

  - next_poll_backoff        熔断状态机一步转移（原 _on_poll_result / _switch_poll_state）
  - ui_poll_interval_ms      行情 UI 定时器间隔决策（原 _poll 头部字面量）
  - ui_top_poll_interval_ms  置顶定时器间隔决策（原 _poll 头部字面量）
  - minute_retry_state       分时快速重试窗口状态转移（原 _minute_loop 内联分支）
  - QuoteLoopSchedule        行情线程"上次拉取/到期判定/长暂停首拉退避"调度骨架
                              （原 _quote_loop / _minute_loop 内联 last 逻辑）

主窗保留 QTimer、线程循环入口与副作用调用（fetch/sleep/日志），仅作薄委托；
行为与拆分前逐位一致。_minute_loop 中「间隔选择 + 常量字面量」因存在源码级
防回归测试，仍内联在主窗，本模块只收敛纯状态转移。
"""

from sf_constants import (LOOP_TICK_CAP, POLL_DEGRADED, POLL_INTERVAL_MINIMIZED,
                          POLL_NORMAL, POLL_OPEN, UI_WATCHDOG_INTERVAL_MS)


def next_poll_backoff(state, fail, ok, fail_to_degraded, fail_to_open):
    """熔断状态机一步转移（纯函数）：由一次轮询结果 ok 计算 (new_state, new_fail)。

    与原 FloatWindow._on_poll_result + _switch_poll_state 行为逐位一致：
    - 成功：若曾失败或状态非 NORMAL，fail 清零并回退 NORMAL；否则保持原状不变。
    - 失败：fail+1；NORMAL 下连续达 fail_to_degraded 次 → DEGRADED；
            DEGRADED 下连续达 fail_to_open 次 → OPEN。
    状态切换的落值与记日志由调用方完成（_switch_poll_state 的日志职责内联主窗，
    避免纯函数依赖 log_line）。"""
    if ok:
        if fail > 0 or state != POLL_NORMAL:
            return POLL_NORMAL, 0
        return state, fail
    fail += 1
    if state == POLL_NORMAL and fail >= fail_to_degraded:
        return POLL_DEGRADED, fail
    if state == POLL_DEGRADED and fail >= fail_to_open:
        return POLL_OPEN, fail
    return state, fail


def ui_poll_interval_ms(minimized, trading):
    """UI 兜底看门狗间隔（毫秒）。

    历史：v1.2.x 缩到 Dock 2s、非交易 1000ms、交易 50ms；v1.3.43 把交易档
    压到 16ms —— 因为它是"数据进了队列、UI 还没取走"的等待窗口。

    v1.4.10 之后这个前提不成立了：行情/分时线程**入队即发跨线程信号**唤醒
    `_poll`，等待窗口从 0~16ms 压到 0~2ms，定时器不再承担"及时发现数据"的
    职责，只留兜底 —— ① 某次信号万一没送达，最迟 1 秒把积压全部消化；
    ② 按秒复核交易时段与 App Nap 状态。所以三档统一成：可见 1000ms、
    缩到 Dock 2000ms（Dock 态数据本来就 2s 才来一条，取队列无需更勤）。

    trading 参数保留：调用点仍按交易/非交易传值，便于将来分档，且不改签名。
    """
    del trading
    if minimized:
        return int(POLL_INTERVAL_MINIMIZED * 1000)
    return int(UI_WATCHDOG_INTERVAL_MS)


def ui_top_poll_interval_ms(minimized, trading):
    """置顶定时器间隔决策（毫秒）。

    缩到 Dock 后 10s 一次即可；只要窗口可见，无论是否交易都每 250ms 复核。
    浏览器系统全屏会切换到新的 Space，旧版非交易时段最长等 10s 才发现，表现为
    悬浮窗过几秒才出现。250ms 把最坏等待压到四分之一秒；这只调用窗口层级/Space
    复位，不发行情请求，也不改变轮询频率。
    """
    del trading
    return 10000 if minimized else 250


def minute_retry_state(retry_left, hot, result, retry_max):
    """分时快速重试窗口一步转移（纯函数）：由一次分时拉取结果返回 (retry_left, hot)。

    与原 _minute_loop 内联分支逐位一致（v1.2.30）：
    - result 为真：成功立即恢复正常周期（retry_left=0, hot=False）；
    - retry_left>0：窗口内继续失败，消耗一次快速重试（-1）；
    - 未在窗口且非 hot：首次失败进入快速重试窗口（retry_left=retry_max, hot=True）；
    - 窗口耗尽仍失败：保持 hot 回落常规周期，不重复进入（防空转刷屏）。
    注：result 为 None（防重入，上一次请求未完成）时调用方不应调用本函数、
    应直接 continue 保持 interval/重试状态。"""
    if result:
        return 0, False
    if retry_left > 0:
        return retry_left - 1, hot
    if not hot:
        return retry_max, True
    return retry_left, hot


class QuoteLoopSchedule:
    """行情/分时线程轮询调度骨架（纯逻辑，原 _quote_loop/_minute_loop 内联 last 决策）。

    维护"上次实际拉取时刻"并在每步决策：是否到期应拉取、休眠唤醒/长时间暂停后
    是否需首拉退避。原实现以局部变量 last + 字面量（120s 长暂停 / 1.0s 退避 /
    0.1s 节拍）分别在两个循环内联，现将调度判定收敛于此，循环体仅保留副作用
    调用与簿记，行为不变。
    """

    __slots__ = ("last", "long_pause_limit", "backoff_seconds", "tick_seconds")

    def __init__(self, long_pause_limit=120.0, backoff_seconds=1.0, tick_seconds=0.1):
        # v1.2.71：改用单调时钟（time.monotonic）而非 Unix 时间戳。墙钟会被 NTP
        # 校时/手动改表拨回，一旦回拨则 `now - last` 为负 → 到期判定永远不成立 →
        # 行情与分时**静默停拉**，直到时钟追回原值（故障期间界面静止、无报错）。
        self.last = 0.0                   # 上次实际拉取时刻（单调时钟秒）；0 表示从未拉取
        self.long_pause_limit = long_pause_limit   # 超过此静置时长判定"长时间暂停"（秒）
        self.backoff_seconds = backoff_seconds     # 长暂停后的首拉退避时长（秒）
        self.tick_seconds = tick_seconds           # 循环基础休眠节拍（秒）

    def step(self, now, interval):
        """本步决策（纯函数）：返回 (fetch, backoff, sleep)。

        原 `if now - last >= interval: if last > 0 and now - last > 120: sleep(1.0)
        last = now; fetch` 判定搬移：
        - fetch=False：未到期，只做基础节拍休眠（sleep=tick_seconds）；
        - fetch=True：到期应拉取。若 last>0 且静置超过 long_pause_limit（休眠唤醒 /
          长时间暂停后恢复，应先在循环体退避 1s 再拉取，避免请求洪峰），则
          backoff=True；随后记 last=now。
        * 与拆分前一致：last==0（首次启动）不做长暂停判定（避免 Unix 时间戳跑到
          now-last 必然 >120 的旧陷阱白白退避 1s）。
        """
        if (now - self.last) < interval:
            return False, False, self.tick_seconds
        backoff = self.last > 0 and (now - self.last) > self.long_pause_limit
        self.last = now
        return True, backoff, self.tick_seconds

    def mark(self, now):
        """记录"刚刚拉取过一次"（v1.2.71 外部强制拉取用）。

        强制拉取不走 step()，若不记 last，下一节拍仍会判定"已到期"再拉一次，
        等于一次强制触发两次请求。置 last=now 使下一次常规轮询从现在起重新计时。
        """
        self.last = now

    def nap_seconds(self, now, interval, boundary_seconds=0.0, cap=LOOP_TICK_CAP):
        """本步该睡多久（v1.4.10 功耗）。返回秒数。

        原实现恒睡 tick_seconds(0.1s)，只为"到点能及时发现"。实测盘后两条常驻
        线程各 9.7 次/秒的唤醒里 99.7% 什么都没干 —— CPU 时间可以忽略，但唤醒
        本身是 macOS 的能耗大头。这里改成睡到"下一次真正可能需要决策"的时刻。

        刻意**不会比原实现更早醒来**（除时段边界外），因此拉取时刻逐次不变：
            nap = max(tick, min(距下次到期, 距时段边界, 上限))
        - max(tick, …)：到期不足 0.1s 时仍按原节拍睡。盘中 interval=0.1s 恒落在
          这一档，所以 10Hz 的行情节奏一秒都没变、也没变快（本函数在 fetch **之前**
          的 now 上计算，fetch 耗时仍像旧实现一样叠加到下一拍，实测 7.1 次/秒）。
        - min(…, boundary)：唯一允许早于 tick 醒来的理由 —— 09:30 开盘必须当场
          发现，不能等到下个节拍。boundary_seconds<=0 表示"算不出来/无边界"，
          此时不参与 min，退回纯节拍行为。
        - min(…, cap)：兜底上限，任何未登记的状态变化最迟 cap 秒后被重新评估。
        """
        due_in = (self.last + interval) - now
        nap = due_in if due_in > self.tick_seconds else self.tick_seconds
        if boundary_seconds and boundary_seconds > 0:
            nap = min(nap, boundary_seconds)
        if cap and cap > 0:
            nap = min(nap, cap)
        return nap if nap > 0 else self.tick_seconds
