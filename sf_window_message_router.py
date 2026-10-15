"""主窗后台消息的无状态消费与合批。

`FloatWindow` 仍然负责决定何时轮询、如何应用一批消息以及何时重绘；本模块只
负责从有界队列中取出消息，并保留原有的两条语义：

* 实时行情展示只应用最后一条；
* 被合并掉的行情仍逐笔返回给目标价判断，不能因为 UI 合批而漏掉越线。

这样先把隐含消息总线从主窗拿出来，不改变线程、信号或消息格式，后续控制器可以
逐个接管消息类型。
"""

from __future__ import annotations

import queue
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PollBatch:
    """一次 UI 消息消费的结果。"""

    processed: bool = False
    latest_quote: Any = None
    quote_evaluations: list[Any] = field(default_factory=list)
    latest_minute: Any = None
    minute_stale_after: Any = None
    latest_net: Any = None
    has_latest_net: bool = False
    update_checks: list[Any] = field(default_factory=list)
    update_check_cancels: list[Any] = field(default_factory=list)
    update_prompt_answers: list[Any] = field(default_factory=list)
    update_progresses: list[Any] = field(default_factory=list)
    update_downloads: list[Any] = field(default_factory=list)
    messages: list[tuple[str, Any]] = field(default_factory=list)
    error: Exception | None = None


class WindowMessageRouter:
    """消费主窗队列，但不直接触碰 QWidget 或业务对象。"""

    @staticmethod
    def _code_payload(data: Any, code: str) -> tuple[Any, Any]:
        if isinstance(data, tuple) and len(data) == 2:
            return data
        return code, data

    @classmethod
    def drain(cls, messages: queue.Queue, code: str, *, coalesce: bool = True) -> PollBatch:
        """从队列中消费一批消息。

        `coalesce=False` 用于缩到 Dock 的低频路径，保持原顺序逐条交给宿主；默认
        模式对应正常交易时段的合批逻辑。
        """
        batch = PollBatch()
        try:
            while True:
                kind, data = messages.get_nowait()
                batch.processed = True
                if not coalesce:
                    if kind in ("quote", "minute"):
                        data_code, payload = cls._code_payload(data, code)
                        if data_code != code:
                            continue
                        data = (data_code, payload)
                    batch.messages.append((kind, data))
                    continue

                if kind == "quote":
                    data_code, quote = cls._code_payload(data, code)
                    if data_code != code:
                        continue
                    # FloatWindow 原实现会在新行情到来时逐笔判断上一条，最后一条
                    # 留给 _apply_quote。保留这个顺序，目标价越线不能被合批吞掉。
                    if batch.latest_quote is not None:
                        batch.quote_evaluations.append(batch.latest_quote)
                    batch.latest_quote = quote
                elif kind == "minute":
                    data_code, points = cls._code_payload(data, code)
                    if data_code != code:
                        continue
                    batch.latest_minute = points
                    batch.minute_stale_after = None
                elif kind == "minute_stale":
                    if data == code:
                        batch.minute_stale_after = data
                elif kind == "net":
                    batch.latest_net = data
                    batch.has_latest_net = True
                elif kind == "update_check":
                    batch.update_checks.append(data)
                elif kind == "update_check_cancel":
                    batch.update_check_cancels.append(data)
                elif kind == "update_prompt_answer":
                    batch.update_prompt_answers.append(data)
                elif kind == "update_progress":
                    # 进度只保留本批最后一条，避免落后进度覆盖最新进度。
                    batch.update_progresses[:] = [data]
                elif kind == "update_download":
                    batch.update_downloads.append(data)
        except queue.Empty:
            return batch
        except Exception as exc:
            # 旧 _poll() 对坏消息的契约是：留痕，但不阻断这一拍的后续收尾。
            batch.error = exc
        return batch
