# -*- coding: utf-8 -*-
"""sf_scheduler —— 轮询调度的纯逻辑（v1.2.39 从主窗拆出）。

只负责把行情状态映射为轮询间隔；行情线程、界面刷新和动画分别由调用方及其专用模块处理。
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
