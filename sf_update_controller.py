"""更新后台任务控制器。

`sf_update` 负责清单、下载、校验和安装助手；本模块只负责把这些阻塞操作放到
独立线程，并把结果按主窗现有消息格式送回 UI。弹窗和更新状态仍由主线程持有，
因此这一步不会改变 Qt 窗口的线程归属。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class UpdateController:
    """拥有更新检查/下载后台线程，不直接依赖 QWidget。"""

    def __init__(self, enqueue: Callable[[tuple[str, Any]], None], version: Callable[[], str]):
        self._enqueue = enqueue
        self._version = version
        self._stopped = False
        self._threads: set[threading.Thread] = set()
        self._active_cancel: threading.Event | None = None
        self._active_update: Any = None
        self._active_token: Any = None
        self._pending_download: tuple[Any, threading.Event, Any] | None = None
        self._lock = threading.Lock()
        self._checks_started = 0
        self._checks_completed = 0
        self._checks_failed = 0
        self._downloads_started = 0
        self._downloads_completed = 0
        self._downloads_failed = 0

    def _start(self, target: Callable[[], None], name: str) -> bool:
        with self._lock:
            if self._stopped:
                return False
            thread = threading.Thread(target=target, daemon=True, name=name)
            self._threads.add(thread)
            try:
                thread.start()
            except Exception:
                self._threads.discard(thread)
                raise
            return True

    def _forget(self, thread: threading.Thread) -> None:
        with self._lock:
            self._threads.discard(thread)

    def check(self, manual: bool, token: Any = None) -> None:
        """异步检查更新，结果沿用主窗的 `update_check` 消息。"""
        with self._lock:
            self._checks_started += 1

        def worker() -> None:
            thread = threading.current_thread()
            try:
                import sf_update

                update = sf_update.check_for_update(self._version(), sequence_aware=True)
                with self._lock:
                    self._checks_completed += 1
                self._enqueue((
                    "update_check",
                    (manual, update, None, token) if token is not None else (manual, update, None),
                ))
            except Exception as exc:
                with self._lock:
                    self._checks_failed += 1
                self._enqueue((
                    "update_check",
                    (manual, None, exc, token) if token is not None else (manual, None, exc),
                ))
            finally:
                self._forget(thread)

        try:
            self._start(worker, "update-check")
        except Exception as exc:
            with self._lock:
                self._checks_failed += 1
            self._enqueue((
                "update_check",
                (manual, None, exc, token) if token is not None else (manual, None, exc),
            ))

    def download(self, update: Any, cancel_event: threading.Event, token: Any = None) -> bool:
        """异步下载更新；身份切换时先取消旧 worker，再串行启动最新任务。"""
        displaced = None
        queued = False
        with self._lock:
            if self._stopped:
                cancel_event.set()
                return False
            if self._active_cancel is not None:
                self._active_cancel.set()
                displaced = self._pending_download
                if displaced is not None:
                    displaced[1].set()
                self._pending_download = (update, cancel_event, token)
                queued = True
            else:
                self._active_cancel = cancel_event
                self._active_update = update
                self._active_token = token
                self._downloads_started += 1
        if displaced is not None:
            try:
                import sf_update
                error: Exception = sf_update.UpdateCancelled("下载已取消")
            except ImportError:
                error = RuntimeError("下载已取消；更新模块不可用")
            old_update, _, old_token = displaced
            payload = ((old_update, None, error, old_token) if old_token is not None
                       else (old_update, None, error))
            self._enqueue(("update_download", payload))
        if queued:
            return True
        self._start_download_worker(update, cancel_event, token)
        return True

    def _start_download_worker(self, update: Any, cancel_event: threading.Event,
                               token: Any = None) -> None:
        def worker() -> None:
            thread = threading.current_thread()
            last_pct = {"value": -1}

            def report(downloaded: int, total: int) -> None:
                pct = 0 if not total else downloaded * 100 // total
                if pct == last_pct["value"] and pct < 100:
                    return
                last_pct["value"] = pct
                message = (pct, downloaded, total, token) if token is not None else (pct, downloaded, total)
                self._enqueue(("update_progress", message))

            try:
                import sf_update

                if cancel_event.is_set():
                    raise sf_update.UpdateCancelled("下载已取消")
                archive = sf_update.download_update(
                    update,
                    cancel_event=cancel_event,
                    progress_callback=report,
                )
                self._enqueue((
                    "update_download",
                    (update, archive, None, token) if token is not None else (update, archive, None),
                ))
                with self._lock:
                    self._downloads_completed += 1
            except Exception as exc:
                with self._lock:
                    self._downloads_failed += 1
                self._enqueue((
                    "update_download",
                    (update, None, exc, token) if token is not None else (update, None, exc),
                ))
            finally:
                try:
                    self._finish_download(cancel_event)
                finally:
                    self._forget(thread)

        try:
            started = self._start(worker, "update-download")
        except Exception as exc:
            with self._lock:
                self._downloads_failed += 1
            try:
                self._enqueue((
                    "update_download",
                    (update, None, exc, token) if token is not None else (update, None, exc),
                ))
            finally:
                self._finish_download(cancel_event)
            return
        if not started:
            self._finish_download(cancel_event)

    def _finish_download(self, cancel_event: threading.Event) -> None:
        pending = None
        with self._lock:
            if self._active_cancel is cancel_event:
                self._active_cancel = None
                self._active_update = None
                self._active_token = None
                pending = self._pending_download
                self._pending_download = None
                if pending is not None and not self._stopped:
                    next_update, next_cancel, next_token = pending
                    self._active_cancel = next_cancel
                    self._active_update = next_update
                    self._active_token = next_token
                else:
                    pending = None
        if pending is not None:
            self._start_download_worker(next_update, next_cancel, next_token)


    def cancel_download(self, update: Any = None, *, token: Any = None) -> bool:
        """Cancel an exact task token, or match the legacy caller's update object."""
        with self._lock:
            pending = self._pending_download
            if pending is not None and ((token is not None and pending[2] is token) or
                                        (token is None and (update is None or pending[0] is update))):
                pending[1].set()
                if token is not None or update is not None:
                    # Keep the cancelled task queued: its worker emits the owned
                    # cancellation terminal before any later replacement starts.
                    return True
                self._pending_download = None
            if self._active_cancel is None:
                return pending is not None and token is None and update is None
            if token is not None:
                if self._active_token is not token:
                    return False
            elif update is not None and self._active_update is not update:
                return False
            self._active_cancel.set()
            return True

    def stop(self) -> None:
        """Prevent new work and cancel the controller's active and queued downloads."""
        with self._lock:
            self._stopped = True
            if self._active_cancel is not None:
                self._active_cancel.set()
            if self._pending_download is not None:
                self._pending_download[1].set()
                self._pending_download = None

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": "update_controller.v1",
                "stopped": self._stopped,
                "workers": len(self._threads),
                "checks_started": self._checks_started,
                "checks_completed": self._checks_completed,
                "checks_failed": self._checks_failed,
                "downloads_started": self._downloads_started,
                "downloads_completed": self._downloads_completed,
                "downloads_failed": self._downloads_failed,
                "pending_download": self._pending_download is not None,
            }
