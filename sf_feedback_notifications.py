"""Private, restart-safe macOS notifications for the local feedback operator."""
from __future__ import annotations

import json
import fcntl
import re
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

from sf_feedback import _atomic_replace

_CASE = re.compile(r"feedback-[a-f0-9]{32}")
_RUN = re.compile(r"[a-f0-9]{32}")


def _reject_links(*paths):
    if any(path.is_symlink() for path in paths):
        raise OSError("feedback notification path is a symbolic link")


def display_notification(message):
    script = 'on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run'
    subprocess.run(["/usr/bin/osascript", "-e", script, "Skyrocket 反馈", message],
                   check=True, capture_output=True, text=True, timeout=10)


def report_versions(directory):
    database = Path(directory).resolve() / "queue.sqlite3"
    _reject_links(database, database.with_name("queue.sqlite3-wal"),
                  database.with_name("queue.sqlite3-shm"))
    if not database.exists():
        return {}
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT case_id, substr(report,1,65537) FROM queue WHERE status='ready' LIMIT 1001").fetchall()
    versions = {case: json.loads(report)["analysis_run_id"] for case, report in rows}
    _validate_versions(versions)
    return versions


def _validate_versions(versions):
    if (not isinstance(versions, dict) or len(versions) > 1000
            or any(not isinstance(case, str) or not _CASE.fullmatch(case)
                   or not isinstance(run, str) or not _RUN.fullmatch(run)
                   for case, run in versions.items())):
        raise ValueError("invalid notification report identities")


# 失败标识的长度上限；代理拼出的组合标识必须不超过它，否则整次同步报 state_unavailable。
MAX_FAILURE_IDENTITY = 128


class FeedbackNotifications:
    def __init__(self, directory, *, sender=None):
        self.path = Path(directory).resolve() / "notifications.json"
        self.sender = sender or display_notification

    def _read(self):
        _reject_links(self.path)
        if not self.path.exists():
            return {"schema": 1, "reports": {}, "failure": ""}
        if self.path.stat().st_size > 256 * 1024:
            raise ValueError("notification state too large")
        state = json.loads(self.path.read_text())
        if (not isinstance(state, dict) or set(state) != {"schema", "reports", "failure"}
                or type(state["schema"]) is not int or state["schema"] != 1
                or not isinstance(state["failure"], str) or len(state["failure"]) > MAX_FAILURE_IDENTITY):
            raise ValueError("invalid notification state")
        _validate_versions(state["reports"])
        return state

    def _write(self, state):
        _reject_links(self.path)
        payload = json.dumps(state, sort_keys=True).encode()
        _atomic_replace(self.path, lambda stream: stream.write(payload))

    def baseline(self, versions):
        """Explicit migration only: don't announce already-reviewed test reports."""
        _validate_versions(versions)
        _reject_links(self.path)
        if self.path.exists():
            raise FileExistsError("notification state already exists")
        self._write({"schema": 1, "reports": versions, "failure": ""})

    def sync(self, versions, failure=""):
        try:
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            _reject_links(self.path, self.path.with_suffix(".lock"))
            with self.path.with_suffix(".lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {"submitted": [], "failed": ["notification_busy"]}
                return self._sync_locked(versions, failure)
        except OSError:
            return {"submitted": [], "failed": ["state_unavailable"]}

    def _sync_locked(self, versions, failure):
        result: dict[str, list[str]] = {"submitted": [], "failed": []}
        try:
            _validate_versions(versions)
            if not isinstance(failure, str) or len(failure) > MAX_FAILURE_IDENTITY:
                raise ValueError("invalid notification failure identity")
            state = self._read()
        except Exception:
            result["failed"].append("state_unavailable")
            return result
        events = []
        new = {case: run for case, run in versions.items() if state["reports"].get(case) != run}
        if new:
            events.append(("reports", "已生成 %d 份新的反馈分析报告。" % len(new),
                           {**state, "reports": dict(versions)}))
        # Build each update from the last persisted state, so a failed event
        # doesn't consume a report or prevent an independent failure alert.
        for event, message, update in events:
            try:
                self.sender(message)
                self._write(update)
                state = update
                result["submitted"].append(event)
            except Exception:
                result["failed"].append(event)
        if failure != state["failure"]:
            event = "failure" if failure else "recovery"
            if failure == "backend:authentication":
                message = "无法读取 GitHub 登录，请检查本机 GitHub 授权。"
            elif failure == "backend:notification_index":
                message = "反馈报告索引读取失败，请查看反馈工具运行记录。"
            else:
                message = ("反馈后台处理失败，将在后续检查中重试。" if failure else
                           "反馈后台已恢复正常。")
            try:
                self.sender(message)
                self._write({**state, "failure": failure})
                result["submitted"].append(event)
            except Exception:
                result["failed"].append(event)
        return result
