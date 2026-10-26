"""Crash-safe runtime-session ledger used before UI startup.

This module is intentionally standard-library only.  It records observable
startup milestones without deciding whether a missing clean exit was a crash,
a forced termination, or a power event.

Storage is one SQLite row per session (``session_events``), written inside
``BEGIN IMMEDIATE``: two processes starting at the same time can no longer
overwrite each other's unfinished session, which a single-slot JSON file did.
Reading is strictly side-effect free — the diagnostic export asks this ledger
about the last session while the app is running, and an open that created
``-journal`` files would change the user's data directory (the exact failure
that once made a full regression reject its own run).  So readers use
``mode=ro`` on a rollback-journal database and never create the file or its
parent directory.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
LEGACY_JSON_SCHEMA_VERSION = 1
LEDGER_FILENAME = "run-session.sqlite3"
PHASE_LAUNCHING = "launching"
PHASE_FRAME_READY = "frame_ready"
PHASE_HEALTHY = "healthy"
PHASE_CLEAN_EXIT = "clean_exit"
PHASE_UPDATE_HANDOFF = "update_handoff"
PHASES = frozenset({
    PHASE_LAUNCHING,
    PHASE_FRAME_READY,
    PHASE_HEALTHY,
    PHASE_CLEAN_EXIT,
    PHASE_UPDATE_HANDOFF,
})
_TERMINAL_PHASES = frozenset({PHASE_CLEAN_EXIT, PHASE_UPDATE_HANDOFF})
_PHASE_ORDER = {PHASE_LAUNCHING: 0, PHASE_FRAME_READY: 1, PHASE_HEALTHY: 2}
PROMPT_NONE = "none"
PROMPT_DEFERRED = "deferred"
PROMPT_DISMISSED = "dismissed"
PROMPT_OPENED_FEEDBACK = "opened_feedback"
PROMPT_STATES = frozenset({PROMPT_NONE, PROMPT_DEFERRED, PROMPT_DISMISSED, PROMPT_OPENED_FEEDBACK})

_COLUMNS = ("session_id", "started_at", "app_version", "bundle_id", "pid", "phase",
            "prompt_state", "prompt_count", "update_handoff_id", "expected_update_version",
            "ended_at")
_SELECT = "SELECT %s FROM session_events ORDER BY rowid DESC" % ", ".join(_COLUMNS)
_META_TABLE = "ledger_meta"
_LEGACY_FILENAME = "run-session.json"


class SessionLedgerError(ValueError):
    """The on-disk session ledger is malformed or cannot advance safely."""


class MalformedLedger(SessionLedgerError):
    """A persisted ledger row no longer satisfies the storage contract."""


@dataclass(frozen=True)
class SessionAssessment:
    session_id: str
    category: str
    should_prompt: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def session_path_for_log(log_path: os.PathLike[str] | str) -> Path:
    return Path(log_path).resolve().parent / LEDGER_FILENAME


def _valid_session(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value) != set(_COLUMNS):
        return False
    return (
        isinstance(value["session_id"], str) and bool(value["session_id"])
        and isinstance(value["started_at"], str)
        and isinstance(value["app_version"], str)
        and isinstance(value["bundle_id"], str)
        and type(value["pid"]) is int and value["pid"] > 0
        and value["phase"] in PHASES
        and value["prompt_state"] in PROMPT_STATES
        and type(value["prompt_count"]) is int and value["prompt_count"] >= 0
        and (value["update_handoff_id"] is None or isinstance(value["update_handoff_id"], str))
        and (value["expected_update_version"] is None or isinstance(value["expected_update_version"], str))
        and (value["ended_at"] is None or isinstance(value["ended_at"], str))
    )


def _row(record: dict[str, Any]) -> dict[str, Any]:
    value = dict(record)
    value["pid"] = int(value["pid"])
    value["prompt_count"] = int(value["prompt_count"])
    return value


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc).lower() or "busy" in str(exc).lower()


def _rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every session, newest first, or reject a malformed ledger."""
    rows = [_row(dict(zip(_COLUMNS, record))) for record in connection.execute(_SELECT).fetchall()]
    if not all(_valid_session(item) for item in rows):
        raise MalformedLedger("session ledger contains malformed rows")
    return rows


def _snapshot(connection: sqlite3.Connection) -> dict[str, Any] | None:
    """Render rows as the {current, pending} shape the assessment already understands.

    ``pending`` is every unfinished session nobody has been asked about yet; that is
    what the single-slot JSON file used to lose.
    """
    rows = _rows(connection)
    if not rows:
        return None
    unasked = [row for row in rows[1:]
               if row["prompt_state"] == PROMPT_NONE and row["phase"] not in _TERMINAL_PHASES]
    return {"current": rows[0], "pending": unasked[-1] if unasked else None}


class RuntimeSessionLedger:
    """Persist every session and answer conservatively about the previous ones."""

    def __init__(self, path: os.PathLike[str] | str):
        self.path = Path(path)

    def _open_read(self) -> sqlite3.Connection | None:
        """只读打开：文件不在就是「这台机器还没有账本」，一个文件都不许产生。"""
        if not self.path.is_file():
            return None
        return sqlite3.connect("%s?mode=ro" % self.path.resolve().as_uri(), uri=True, timeout=5,
                               isolation_level=None)

    def _open_write(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = self._create(self.path)
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise SessionLedgerError("session ledger is busy") from exc
            connection = None
        if connection is not None:
            return connection
        return self._quarantine_and_recreate()

    def _quarantine_and_recreate(self) -> sqlite3.Connection:
        """Preserve a genuinely unreadable ledger; never move a busy database."""
        corrupt = self.path.with_name(self.path.name + ".corrupt-" + uuid.uuid4().hex)
        try:
            os.replace(self.path, corrupt)
        except OSError as exc:
            raise SessionLedgerError("session ledger is unreadable and cannot be set aside") from exc
        try:
            connection = self._create(self.path)
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise SessionLedgerError("session ledger is busy") from exc
            connection = None
        if connection is None:
            raise SessionLedgerError("session ledger cannot be created")
        return connection

    def _create(self, path: Path) -> sqlite3.Connection | None:
        fresh = not path.exists()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS session_events ("
                "session_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, app_version TEXT NOT NULL,"
                " bundle_id TEXT NOT NULL, pid INTEGER NOT NULL, phase TEXT NOT NULL,"
                " prompt_state TEXT NOT NULL, prompt_count INTEGER NOT NULL DEFAULT 0,"
                " update_handoff_id TEXT, expected_update_version TEXT, ended_at TEXT)"
            )
            schema_cursor = connection.execute("PRAGMA table_info(session_events)")
            try:
                columns = {row[1] for row in schema_cursor}
            finally:
                schema_cursor.close()
            if columns != set(_COLUMNS):
                raise SessionLedgerError("session ledger schema mismatch")
            connection.execute("CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            meta_cursor = connection.execute("INSERT OR REPLACE INTO ledger_meta(key, value) VALUES ('schema_version', ?)",
                                             (str(SCHEMA_VERSION),))
            meta_cursor.close()
        except (sqlite3.Error, SessionLedgerError) as exc:
            if connection is not None:
                connection.close()
            if isinstance(exc, sqlite3.OperationalError) and _is_busy(exc):
                raise
            return None
        if fresh:
            os.chmod(path, 0o600)
        return connection

    def _legacy_path(self) -> Path:
        return self.path.with_name(_LEGACY_FILENAME)

    def _legacy_records(self) -> list[dict[str, Any]]:
        legacy = self._legacy_path()
        if not legacy.is_file() or legacy == self.path:
            return []
        try:
            import json
            value = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return []
        if not isinstance(value, dict) or value.get("schema_version") != LEGACY_JSON_SCHEMA_VERSION:
            return []
        return [item for item in (value.get("current"), value.get("pending"))
                if isinstance(item, dict) and _valid_session(item)]

    def _migrate_legacy_json(self, connection: sqlite3.Connection) -> None:
        records = self._legacy_records()
        if not records:
            return
        for record in reversed(records):
            connection.execute(
                "INSERT OR IGNORE INTO session_events (%s) VALUES (%s)"
                % (", ".join(_COLUMNS), ", ".join("?" * len(_COLUMNS))),
                tuple(record[column] for column in _COLUMNS),
            )
        legacy = self._legacy_path()
        migrated = legacy.with_name(legacy.name + ".migrated-" + uuid.uuid4().hex)
        try:
            os.replace(legacy, migrated)
        except FileNotFoundError:
            # Another starter already preserved this same legacy source.  The
            # INSERT OR IGNORE above completed this transaction safely.
            return

    def _read(self) -> dict[str, Any] | None:
        connection = self._open_read()
        if connection is None:
            return None
        try:
            return _snapshot(connection)
        except sqlite3.OperationalError as exc:
            # 只有「这个库里还没有这张表」才算「还没有账本」。别的打不开一律照实抛出：
            # 把「问不出答案」写成「答案是空的」，正是这一族闸门最爱犯的谎。
            if "no such table" in str(exc):
                return None
            raise
        finally:
            connection.close()

    def _transact(self, work):
        """判断与写入住在同一个 ``BEGIN IMMEDIATE`` 里。

        先读后写如果跨两个事务，第二个进程能在中间改走账本，判出的结论就是过期状态 ——
        这一版换 SQLite 要治的正是这件事，所以不给任何入口留旁路。
        """
        connection: sqlite3.Connection | None = self._open_write()
        try:
            assert connection is not None
            connection.execute("BEGIN IMMEDIATE")
            result = work(connection)
            connection.execute("COMMIT")
        except MalformedLedger:
            assert connection is not None
            connection.execute("ROLLBACK")
            connection.close()
            connection = None
            return self._recover_malformed_ledger(work)
        except BaseException:
            assert connection is not None
            connection.execute("ROLLBACK")
            raise
        finally:
            if connection is not None:
                connection.close()
        return result

    def _recover_malformed_ledger(self, work):
        """Replace a ledger whose stored rows no longer satisfy the contract."""
        connection = self._quarantine_and_recreate()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = work(connection)
            connection.execute("COMMIT")
            return result
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def begin(self, *, app_version: str, bundle_id: str, pid: int | None = None,
              session_id: str | None = None, started_at: str | None = None) -> SessionAssessment | None:
        identity = session_id or uuid.uuid4().hex

        def work(connection: sqlite3.Connection):
            self._migrate_legacy_json(connection)
            assessment = self.assess_previous(_snapshot(connection))
            connection.execute(
                "INSERT OR REPLACE INTO session_events (%s) VALUES (%s)"
                % (", ".join(_COLUMNS), ", ".join("?" * len(_COLUMNS))),
                (identity, started_at or utc_now(), app_version, bundle_id,
                 os.getpid() if pid is None else int(pid), PHASE_LAUNCHING, PROMPT_NONE, 0, None, None, None),
            )
            return assessment

        return self._transact(work)

    @staticmethod
    def assess_previous(value: dict[str, Any] | None) -> SessionAssessment | None:
        if value is None:
            return None
        pending = value.get("pending")
        if isinstance(pending, dict) and pending["prompt_state"] == PROMPT_NONE:
            category = "post_frame_unfinished" if pending["phase"] == PHASE_FRAME_READY else "unfinished"
            return SessionAssessment(pending["session_id"], category, True)
        current = value.get("current")
        if not isinstance(current, dict):
            return None
        phase = current["phase"]
        if phase == PHASE_UPDATE_HANDOFF:
            return SessionAssessment(current["session_id"], "update_handoff", False)
        if phase == PHASE_LAUNCHING:
            return SessionAssessment(current["session_id"], "startup_incomplete", False)
        if phase == PHASE_FRAME_READY:
            return SessionAssessment(current["session_id"], "post_frame_unfinished",
                                     current["prompt_state"] == PROMPT_NONE)
        if phase == PHASE_HEALTHY:
            return SessionAssessment(current["session_id"], "unfinished",
                                     current["prompt_state"] == PROMPT_NONE)
        return SessionAssessment(current["session_id"], "clean_exit", False)

    def _advance(self, session_id: str, phase: str, *, ended_at: str | None = None,
                 update_handoff_id: str | None = None, expected_update_version: str | None = None) -> None:
        def work(connection: sqlite3.Connection) -> None:
            found = connection.execute("SELECT %s FROM session_events WHERE session_id = ?"
                                      % ", ".join(_COLUMNS), (session_id,)).fetchone()
            current = _row(dict(zip(_COLUMNS, found))) if found is not None else None
            if current is None or not _valid_session(current):
                raise SessionLedgerError("current session is unavailable")
            if (phase in _PHASE_ORDER and current["phase"] in _PHASE_ORDER
                    and _PHASE_ORDER[phase] < _PHASE_ORDER[current["phase"]]):
                raise SessionLedgerError("session phase cannot move backward")
            if current["phase"] in _TERMINAL_PHASES:
                raise SessionLedgerError("terminal session cannot advance")
            if phase == PHASE_UPDATE_HANDOFF and not (update_handoff_id and expected_update_version):
                raise SessionLedgerError("update handoff requires identity and version")
            # 读与写住在同一个 BEGIN IMMEDIATE 里，主键刚查出来就在，不再留一条走不到的二次判空。
            connection.execute(
                "UPDATE session_events SET phase = ?, ended_at = ?,"
                " update_handoff_id = COALESCE(?, update_handoff_id),"
                " expected_update_version = COALESCE(?, expected_update_version) WHERE session_id = ?",
                (phase, ended_at if phase in _TERMINAL_PHASES else None, update_handoff_id,
                 expected_update_version, session_id))

        self._transact(work)

    def mark_frame_ready(self, session_id: str) -> None:
        self._advance(session_id, PHASE_FRAME_READY)

    def mark_healthy(self, session_id: str) -> None:
        self._advance(session_id, PHASE_HEALTHY)

    def mark_clean_exit(self, session_id: str, *, ended_at: str | None = None) -> None:
        self._advance(session_id, PHASE_CLEAN_EXIT, ended_at=ended_at or utc_now())

    def mark_update_handoff(self, session_id: str, *, handoff_id: str, expected_version: str,
                            ended_at: str | None = None) -> None:
        self._advance(session_id, PHASE_UPDATE_HANDOFF, ended_at=ended_at or utc_now(),
                      update_handoff_id=handoff_id, expected_update_version=expected_version)

    def mark_prompt(self, session_id: str, state: str, *, prompted_at: str | None = None) -> None:
        del prompted_at
        if state not in PROMPT_STATES - {PROMPT_NONE}:
            raise SessionLedgerError("unknown prompt state")

        def work(connection: sqlite3.Connection) -> None:
            found = connection.execute("SELECT %s FROM session_events WHERE session_id = ?"
                                       % ", ".join(_COLUMNS), (session_id,)).fetchone()
            row = _row(dict(zip(_COLUMNS, found))) if found is not None else None
            if row is None or not _valid_session(row):
                raise SessionLedgerError("current session is unavailable")
            if row["prompt_state"] == PROMPT_NONE:
                connection.execute("UPDATE session_events SET prompt_state = ?, prompt_count = prompt_count + 1"
                                   " WHERE session_id = ?", (state, session_id))
            elif row["prompt_state"] == PROMPT_DEFERRED and state in (PROMPT_DISMISSED, PROMPT_OPENED_FEEDBACK):
                connection.execute("UPDATE session_events SET prompt_state = ? WHERE session_id = ?",
                                   (state, session_id))
            else:
                raise SessionLedgerError("session prompt is already resolved")

        self._transact(work)

    def current(self) -> dict[str, Any] | None:
        snapshot = self._read()
        return None if snapshot is None else snapshot.get("current")

    def pending(self) -> dict[str, Any] | None:
        """还没问过的那一次历史会话（诊断包读它，不该逼导出面去碰私有读函数）。"""
        snapshot = self._read()
        return None if snapshot is None else snapshot.get("pending")

    def unfinished(self) -> list[dict[str, Any]]:
        """每次「没正常结束且没问过」的会话，一条都不丢 —— 单槽账本丢的就是这些。"""
        connection = self._open_read()
        if connection is None:
            return []
        try:
            rows = _rows(connection)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
        finally:
            connection.close()
        return [row for row in rows if row["prompt_state"] == PROMPT_NONE and row["phase"] not in _TERMINAL_PHASES]
