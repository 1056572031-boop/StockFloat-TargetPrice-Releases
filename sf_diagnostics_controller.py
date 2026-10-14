"""诊断包后台收口控制器。

诊断快照的字段、脱敏和附件选择仍由主窗现有导出逻辑负责；本模块只拥有 ZIP
完成后的后台线程、临时文件清理和完成结果通知。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any

from sf_logging import log_line, log_swallow


class DiagnosticsExportController:
    def __init__(self, publish: Callable[..., str]):
        self._publish = publish
        self._exporting = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def exporting(self) -> bool:
        with self._lock:
            return self._exporting

    @exporting.setter
    def exporting(self, value: bool) -> None:
        with self._lock:
            self._exporting = bool(value)

    @property
    def thread(self) -> threading.Thread | None:
        with self._lock:
            return self._thread

    @thread.setter
    def thread(self, value: threading.Thread | None) -> None:
        with self._lock:
            self._thread = value

    def start(
        self,
        archive: Any,
        temporary: str,
        requested: str,
        on_ready: Callable[[tuple[Any, Any]], None],
    ) -> threading.Thread | None:
        if self.exporting:
            return None
        self.exporting = True
        worker = threading.Thread(
            target=self._run,
            args=(archive, temporary, requested, on_ready),
            name="diagnostics-export",
            daemon=True,
        )
        self.thread = worker
        worker.start()
        return worker

    def finish_sync(
        self,
        archive: Any,
        temporary: str,
        requested: str,
        on_ready: Callable[[tuple[Any, Any]], None],
    ) -> None:
        """兼容旧测试/调用方，沿用同一收口实现但不另起线程。"""
        self.exporting = True
        self._run(archive, temporary, requested, on_ready)

    def _run(
        self,
        archive: Any,
        temporary: str,
        requested: str,
        on_ready: Callable[[tuple[Any, Any]], None],
    ) -> None:
        result: tuple[Any, Any] = (None, None)
        try:
            archive.finish()
            result = (self._publish(temporary, requested), None)
        except Exception as exc:
            log_line("diagnostics export error: %r" % (exc,), "ERROR")
            result = (None, type(exc).__name__)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                temporary = ""
            except OSError as exc:
                log_line("diagnostics temporary cleanup error: %r" % (exc,), "WARNING")
            self.exporting = False
            self.thread = None
        try:
            on_ready(result)
        except RuntimeError as exc:
            log_swallow("sf_diagnostics_controller.completed_after_close", exc)

    def diagnostics(self) -> dict[str, Any]:
        return {"exporting": self.exporting, "thread_alive": bool(
            self.thread is not None and self.thread.is_alive())}
