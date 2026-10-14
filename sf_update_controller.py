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
        self._lock = threading.Lock()

    def _start(self, target: Callable[[], None], name: str) -> None:
        with self._lock:
            if self._stopped:
                return
            thread = threading.Thread(target=target, daemon=True, name=name)
            self._threads.add(thread)
            thread.start()

    def _forget(self, thread: threading.Thread) -> None:
        with self._lock:
            self._threads.discard(thread)

    def check(self, manual: bool) -> None:
        """异步检查更新，结果沿用主窗的 `update_check` 消息。"""

        def worker() -> None:
            thread = threading.current_thread()
            try:
                import sf_update

                update = sf_update.check_for_update(self._version())
                self._enqueue(("update_check", (manual, update, None)))
            except Exception as exc:
                self._enqueue(("update_check", (manual, None, exc)))
            finally:
                self._forget(thread)

        self._start(worker, "update-check")

    def download(self, update: Any, cancel_event: threading.Event) -> None:
        """异步下载更新，进度和终态沿用主窗现有消息格式。"""

        def worker() -> None:
            thread = threading.current_thread()
            last_pct = {"value": -1}

            def report(downloaded: int, total: int) -> None:
                pct = 0 if not total else downloaded * 100 // total
                if pct == last_pct["value"] and pct < 100:
                    return
                last_pct["value"] = pct
                self._enqueue(("update_progress", (pct, downloaded, total)))

            try:
                import sf_update

                archive = sf_update.download_update(
                    update,
                    cancel_event=cancel_event,
                    progress_callback=report,
                )
                self._enqueue(("update_download", (update, archive, None)))
            except Exception as exc:
                self._enqueue(("update_download", (update, None, exc)))
            finally:
                self._forget(thread)

        self._start(worker, "update-download")

    def stop(self) -> None:
        """阻止新任务；下载取消由调用方持有的 Event 完成。"""
        with self._lock:
            self._stopped = True

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {"stopped": self._stopped, "workers": len(self._threads)}
