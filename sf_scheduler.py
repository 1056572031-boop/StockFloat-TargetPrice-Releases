# -*- coding: utf-8 -*-
"""sf_scheduler —— 上帝类 FloatWindow 拆分第一步（v1.2.39）。

背景：stock_float_window.py 的 FloatWindow 近 2000 行，事件/线程/轮询/动画/
状态/UI 高度耦合，属"上帝类"倾向，改一处易牵扯别处。按三方向分版迁移：

 ① 动画状态机（价格滚动/闪烁/窗口动画）  → 目标模块 sf_anim.py
 ② 行情循环与轮询调度（熔断状态机/间隔决策/行情线程循环） → 本模块 sf_scheduler.py
 ③ 运行时调度（App Nap / 置顶 / 省电策略） → 目标模块 sf_runtime.py

v1.2.39 完成第一步：把"轮询间隔决策"纯逻辑沉入本模块（含 熔断状态→间隔 映射
唯一来源），主窗 _poll_interval 仅保留薄委托；后续版本按同模式逐步搬移
_quote_loop / _minute_loop / 熔断状态机，期间保持单测全绿。
"""

from sf_constants import (POLL_DEGRADED, POLL_INTERVAL_DEGRADED, POLL_INTERVAL_NORMAL,
                          POLL_INTERVAL_OPEN, POLL_OPEN, POLL_NORMAL)

# 熔断状态 → 交易时段轮询间隔（秒）映射。
# v1.2.39：原散落在 FloatWindow._poll_interval 内的字面 dict 唯一来源移至此处，
# 主窗只读调用，避免"改间隔忘改映射"。
_POLL_INTERVAL_BY_STATE = {
    POLL_NORMAL: POLL_INTERVAL_NORMAL,
    POLL_DEGRADED: POLL_INTERVAL_DEGRADED,
    POLL_OPEN: POLL_INTERVAL_OPEN,
}


def compute_poll_interval(poll_state):
    """由当前熔断状态返回交易时段轮询间隔（秒）；未知状态兜底 NORMAL。

    与拆分前 FloatWindow._poll_interval 的行为保持一致：
    POLL_NORMAL→0.1s、POLL_DEGRADED→1s、POLL_OPEN→5s（数值见 sf_constants）。
    """
    return _POLL_INTERVAL_BY_STATE.get(poll_state, POLL_INTERVAL_NORMAL)


def instant_poll_interval():
    """poll_backoff 功能关闭时的固定高频回退值（0.1s），
    与拆分前 `return 0.1` 的分支语义完全一致。"""
    return POLL_INTERVAL_NORMAL
