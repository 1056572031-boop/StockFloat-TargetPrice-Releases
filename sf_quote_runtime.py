"""行情/分时后台线程的生命周期控制器。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class QuoteRuntimeController:
    """拥有两条常驻循环的线程句柄，不改变循环函数本身。"""

    def __init__(self):
        self.quote_thread: threading.Thread | None = None
        self.minute_thread: threading.Thread | None = None

    def start(self, quote_target: Callable[[], Any], minute_target: Callable[[], Any]) -> None:
        self.quote_thread = threading.Thread(
            target=quote_target, daemon=True, name="quote-loop")
        self.minute_thread = threading.Thread(
            target=minute_target, daemon=True, name="minute-loop")
        self.quote_thread.start()
        self.minute_thread.start()

    def replace_quote(self, thread: threading.Thread) -> None:
        self.quote_thread = thread

    def replace_minute(self, thread: threading.Thread) -> None:
        self.minute_thread = thread

    def threads(self) -> tuple[threading.Thread | None, threading.Thread | None]:
        return self.quote_thread, self.minute_thread

    def diagnostics(self) -> dict[str, bool]:
        return {
            "quote_alive": bool(self.quote_thread and self.quote_thread.is_alive()),
            "minute_alive": bool(self.minute_thread and self.minute_thread.is_alive()),
        }
