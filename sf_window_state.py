"""主窗控制器使用的可变状态容器。

这些对象只承载 Python 值，不创建 Qt 对象、不启动线程，也不执行业务逻辑。把值
状态从 QWidget 上收口后，后续控制器可以通过窄接口交换状态，而不是互相读写一
大片 `FloatWindow._...` 字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LifecycleState:
    running: bool = True
    quitting: bool = False
    threads_stopped: bool = False


@dataclass
class QuoteState:
    quote: Any = None
    minute_points: list[Any] = field(default_factory=list)
    minute_day: str = ""
    minute_stale: bool = False
    minute_last_ok_at: float = 0.0
    net_ok: bool = True
    quote_fail: int = 0
    last_ok_time: Any = None
    real_price: float = 0.0
    price_is_fallback: bool = False
    pct_unknown: bool = False
    disp_price: Any = None
    disp_pct: Any = None
    line_ver: int = 0
    chart_changed: bool = False
    need_redraw: bool = False
    width_dirty: bool = True


@dataclass
class UpdateFlowState:
    checking: bool = False
    downloading: bool = False
    prompted_version: Any = None
    pending: Any = None
    cancel: Any = None
    prompt_when_ready: bool = False
    ready_archive: Any = None
    ready_version: Any = None
    progress_pending: Any = None
    manual_wait: bool = False
    last_check_at: float = 0.0
    last_result: Any = None
    stale_resets: int = 0
    check_aborted: bool = False
    failure_count: int = 0
    transition_pending: bool = False


@dataclass
class TargetState:
    alerts: list[Any] = field(default_factory=list)


@dataclass
class FaceGuardValueState:
    preference: bool = False
    paused: bool = False
    next_retry: float = 0.0
    logged_state: str = ""
    stalled: bool = False
    screen_locked: bool = False
    screen_probe_at: float = 0.0
    last_fresh: float = 0.0
    fed_ts: float = 0.0
    last_stop: dict[str, Any] = field(default_factory=lambda: {"reason": "", "at": 0.0})
