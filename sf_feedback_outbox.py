"""Crash-safe local outbox for diagnostic feedback packages.

The store has no transport implementation.  A future explicit user action may
read a pending row and submit the same archive with the same envelope.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sf_feedback import (
    FeedbackProtocolError,
    make_envelope,
    validate_archive,
    validate_receipt,
)

STATUSES = ("created", "sealed", "locally_verified", "submitting", "response_unknown",
            "receipt_persisted", "cleanup_pending", "cleaned")
OUTBOX_SCHEMA_VERSION = 2
_TRANSITIONS = {
    "created": {"sealed"}, "sealed": {"locally_verified"},
    "locally_verified": {"submitting"}, "submitting": {"response_unknown", "receipt_persisted"},
    "response_unknown": {"submitting", "receipt_persisted"},
    "receipt_persisted": {"cleanup_pending"}, "cleanup_pending": {"receipt_persisted", "cleaned"},
    "cleaned": set(),
}


class OutboxConflict(FeedbackProtocolError):
    """The same idempotency key was reused for different archive bytes."""


class OutboxBusy(FeedbackProtocolError):
    """Another live worker owns the requested outbox operation."""


def read_diagnostics(database_path: os.PathLike[str] | str,
                     objects_dir: os.PathLike[str] | str) -> dict[str, Any]:
    """Read a bounded outbox summary without creating, migrating, or writing the store.

    Diagnostic export runs while the app may still be writing feedback state.  It must
    use SQLite's read-only URI mode: constructing FeedbackOutbox would run migrations,
    begin a write transaction, and update directory mtimes even when the user has never
    opened the feedback history page.
    """
    database = Path(database_path)
    result: dict[str, Any] = {
        "present": database.is_file(), "recent": 0, "by_status": {},
        "last_errors": [], "awaiting_cleanup": 0, "archive_missing": 0,
        "orphan_objects": None,
    }
    if not result["present"]:
        return result
    resolved = database.resolve()
    # WAL 库没有 -wal/-shm 时，连 mode=ro 的只读打开也会把这两个文件建出来并改目录修改时间
    # （实测，会触发整档回归的"用户数据被改动"红线）。没有 WAL 说明主文件已含全部已提交内容，
    # 用 immutable=1 打开不产生任何副作用；有 WAL（写入进行中或上次异常退出）就必须读它，退回 mode=ro。
    # 极小窗口里恰有写入者起来时，immutable 读可能报错，由调用方的 _snapshot 点名记入 export_issues。
    quiet = not any(Path(str(resolved) + suffix).exists() for suffix in ("-wal", "-shm"))
    uri = resolved.as_uri() + ("?mode=ro&immutable=1" if quiet else "?mode=ro")
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT status, last_error, archive_path FROM outbox "
            "ORDER BY updated_at DESC LIMIT 100").fetchall()
        result["recent"] = len(rows)
        referenced: set[str] = set()
        for row in rows:
            status = row["status"]
            result["by_status"][status] = result["by_status"].get(status, 0) + 1
            archive = Path(row["archive_path"])
            referenced.add(archive.name)
            if row["last_error"] and len(result["last_errors"]) < 5:
                result["last_errors"].append({"status": status, "error": row["last_error"]})
            if status in ("receipt_persisted", "cleanup_pending"):
                result["awaiting_cleanup"] += 1
                if not archive.exists():
                    result["archive_missing"] += 1
        objects = Path(objects_dir)
        if objects.is_dir():
            result["orphan_objects"] = sum(1 for item in objects.glob("*.zip")
                                           if item.name not in referenced)
        return result
    finally:
        connection.close()


def _utc_clock(value: datetime | None = None) -> datetime:
    clock = value or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise FeedbackProtocolError("outbox lease time requires timezone")
    return clock.astimezone(timezone.utc)


def _utc_stamp(value: datetime | None = None) -> str:
    return _utc_clock(value).isoformat()


def _utc_clock_from_stamp(stamp: str) -> datetime:
    return _utc_clock(datetime.fromisoformat(stamp.replace("Z", "+00:00")))


class FeedbackOutbox:
    def __init__(self, database_path: os.PathLike[str] | str, objects_dir: os.PathLike[str] | str):
        self.database_path = Path(database_path)
        self.objects_dir = Path(objects_dir)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS outbox (
                outbox_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                archive_path TEXT NOT NULL,
                archive_sha256 TEXT NOT NULL,
                archive_size INTEGER NOT NULL,
                app_version TEXT NOT NULL,
                consent_policy_version TEXT NOT NULL,
                status TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                receipt_json TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(outbox)")}
            for name, definition in (
                ("lease_token", "TEXT"),
                ("lease_kind", "TEXT"),
                ("lease_until", "TEXT"),
                ("lease_fence", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    db.execute("ALTER TABLE outbox ADD COLUMN %s %s" % (name, definition))
            db.execute("PRAGMA user_version=%d" % OUTBOX_SCHEMA_VERSION)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _connect(self):
        db = sqlite3.connect(self.database_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @contextmanager
    def _db(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _copy_archive(self, source: Path, outbox_id: str) -> Path:
        target = self.objects_dir / (outbox_id + ".zip")
        fd, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=self.objects_dir)
        try:
            with os.fdopen(fd, "wb") as destination, source.open("rb") as origin:
                shutil.copyfileobj(origin, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, target)
            descriptor = os.open(self.objects_dir, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return target
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError as exc:
                del exc

    def enqueue(self, archive_path: os.PathLike[str] | str, *, app_version: str,
                consent_policy_version: str, idempotency_key: str | None = None) -> dict[str, Any]:
        source = Path(archive_path)
        manifest = validate_archive(source)
        outbox_id = uuid.uuid4().hex
        envelope = make_envelope(archive_path=source, app_version=app_version,
                                 outbox_id=outbox_id,
                                 consent_policy_version=consent_policy_version,
                                 idempotency_key=idempotency_key)
        now = datetime.now(timezone.utc).isoformat()
        target: Path | None = None
        try:
            with self._db() as db:
                existing = db.execute("SELECT * FROM outbox WHERE idempotency_key = ?",
                                      (envelope["idempotency_key"],)).fetchone()
                if existing:
                    if existing["archive_sha256"] != envelope["archive_sha256"]:
                        raise OutboxConflict("idempotency key is bound to a different archive")
                    return self.get(existing["outbox_id"])
                target = self._copy_archive(source, outbox_id)
                from sf_feedback import sha256_file
                validate_archive(target)
                if sha256_file(target) != envelope["archive_sha256"]:
                    raise FeedbackProtocolError("保存后的反馈包与封存内容不一致")
                db.execute("""INSERT INTO outbox (
                    outbox_id, idempotency_key, archive_path, archive_sha256, archive_size,
                    app_version, consent_policy_version, status, envelope_json, receipt_json,
                    last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)""",
                           (outbox_id, envelope["idempotency_key"], str(target),
                            envelope["archive_sha256"], envelope["size_bytes"], app_version,
                            consent_policy_version, "locally_verified", json.dumps(envelope, sort_keys=True),
                            now, now))
        except sqlite3.IntegrityError:
            if target is not None:
                target.unlink(missing_ok=True)
            existing = self.find_by_idempotency_key(envelope["idempotency_key"])
            if existing is None:
                raise
            if existing["archive_sha256"] != envelope["archive_sha256"]:
                raise OutboxConflict("idempotency key is bound to a different archive")
            return existing
        except Exception:
            if target is not None:
                target.unlink(missing_ok=True)
            raise
        row = self.get(outbox_id)
        row["manifest"] = manifest
        return row

    def get(self, outbox_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()
        if row is None:
            raise KeyError(outbox_id)
        result = dict(row)
        result["envelope"] = json.loads(result.pop("envelope_json"))
        if result.get("receipt_json"):
            result["receipt"] = json.loads(result.pop("receipt_json"))
        else:
            result.pop("receipt_json", None)
        return result

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT outbox_id FROM outbox WHERE idempotency_key=?", (key,)).fetchone()
        return self.get(row[0]) if row is not None else None

    def claim(self, outbox_id: str, *, kind: str, statuses: tuple[str, ...],
              target_status: str | None = None, lease_seconds: int = 120,
              now: datetime | None = None, token: str | None = None) -> dict[str, Any]:
        """Atomically reserve one row for capture, submission, or cleanup work.

        A claimant receives a monotonically increasing fence.  Future worker
        completion paths must present both token and fence so a late worker
        cannot overwrite a row recovered by a newer process.
        """
        if (not kind or not statuses or lease_seconds <= 0 or any(item not in STATUSES for item in statuses)
                or (target_status is not None and target_status not in STATUSES)):
            raise FeedbackProtocolError("invalid outbox lease request")
        if target_status is not None and any(target_status not in _TRANSITIONS[item] for item in statuses):
            raise FeedbackProtocolError("invalid outbox lease transition")
        clock = _utc_clock(now)
        issued = clock.isoformat()
        expiry = datetime.fromtimestamp(clock.timestamp() + lease_seconds, timezone.utc).isoformat()
        lease_token = token or uuid.uuid4().hex
        placeholders = ",".join("?" for _ in statuses)
        with self._db() as db:
            if target_status is None:
                cursor = db.execute(
                    "UPDATE outbox SET lease_token=?, lease_kind=?, lease_until=?, lease_fence=lease_fence+1, "
                    "updated_at=? WHERE outbox_id=? AND status IN (%s) AND (lease_until IS NULL OR datetime(lease_until) <= datetime(?))"
                    % placeholders,
                    (lease_token, kind, expiry, issued, outbox_id, *statuses, issued),
                )
            else:
                cursor = db.execute(
                    "UPDATE outbox SET status=?, lease_token=?, lease_kind=?, lease_until=?, lease_fence=lease_fence+1, "
                    "updated_at=? WHERE outbox_id=? AND status IN (%s) AND (lease_until IS NULL OR datetime(lease_until) <= datetime(?))"
                    % placeholders,
                    (target_status, lease_token, kind, expiry, issued, outbox_id, *statuses, issued),
                )
            if cursor.rowcount != 1:
                row = db.execute("SELECT outbox_id FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
                if row is None:
                    raise KeyError(outbox_id)
                raise OutboxBusy("outbox row is owned or in an incompatible state")
            row = db.execute("SELECT * FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
        assert row is not None
        result = dict(row)
        result["envelope"] = json.loads(result.pop("envelope_json"))
        if result.get("receipt_json"):
            result["receipt"] = json.loads(result.pop("receipt_json"))
        else:
            result.pop("receipt_json", None)
        return result

    def finish_claim(self, outbox_id: str, *, token: str, fence: int, status: str,
                     error: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        """Advance a leased row only when the exact claimant still owns it."""
        if status not in STATUSES or not token or type(fence) is not int:
            raise FeedbackProtocolError("invalid outbox lease completion")
        current = self.get(outbox_id)
        if current.get("lease_token") != token or current.get("lease_fence") != fence:
            raise OutboxBusy("outbox lease was lost")
        if status not in _TRANSITIONS[current["status"]]:
            raise FeedbackProtocolError("invalid outbox lease transition")
        stamp = _utc_stamp(now)
        with self._db() as db:
            cursor = db.execute(
                "UPDATE outbox SET status=?, last_error=?, lease_token=NULL, lease_kind=NULL, lease_until=NULL, "
                "updated_at=? WHERE outbox_id=? AND lease_token=? AND lease_fence=? AND "
                "(lease_until IS NOT NULL AND datetime(lease_until) > datetime(?))",
                (status, error, stamp, outbox_id, token, fence, stamp),
            )
            if cursor.rowcount != 1:
                row = db.execute("SELECT outbox_id FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
                if row is None:
                    raise KeyError(outbox_id)
                raise OutboxBusy("outbox lease was lost")
        return self.get(outbox_id)

    def recover_expired_leases(self, *, now: datetime | None = None) -> int:
        """Release expired reservations without guessing whether remote receipt exists."""
        stamp = _utc_stamp(now)
        with self._db() as db:
            cursor = db.execute(
                "UPDATE outbox SET lease_token=NULL, lease_kind=NULL, lease_until=NULL, updated_at=? "
                "WHERE lease_until IS NOT NULL AND datetime(lease_until) <= datetime(?)",
                (stamp, stamp),
            )
            return cursor.rowcount

    def _refusal(self, db, outbox_id: str, stamp: str, status: str, *, expected_status: str | None = None):
        """一条 UPDATE 没改动任何行时说清是谁拦的。

        「租约还在别人手里」与「状态已经被改走」是两件事：前者必须让调用方知道有并发工人，
        后者才是状态机拒绝。都写成同一句话会让人去查错方向。
        """
        row = db.execute("SELECT status, lease_token, lease_until FROM outbox WHERE outbox_id = ?",
                         (outbox_id,)).fetchone()
        if row is None:
            raise KeyError(outbox_id)
        if expected_status is not None and row["status"] != expected_status:
            raise FeedbackProtocolError("invalid outbox transition: %s -> %s" % (row["status"], status))
        if row["lease_token"]:
            raise OutboxBusy("outbox lease requires explicit recovery")
        raise FeedbackProtocolError("invalid outbox transition: %s -> %s" % (row["status"], status))

    def transition(self, outbox_id: str, status: str, *, error: str | None = None,
                   now: datetime | None = None) -> dict[str, Any]:
        if status not in STATUSES:
            raise FeedbackProtocolError("unknown outbox status")
        stamp = _utc_stamp(now)
        current = self.get(outbox_id)
        if status not in _TRANSITIONS[current["status"]]:
            raise FeedbackProtocolError(f"invalid outbox transition: {current['status']} -> {status}")
        with self._db() as db:
            # 租约条件写进同一条 UPDATE：先读再写会留下窗口，别人正好在这两步之间接手。
            cursor = db.execute(
                "UPDATE outbox SET status = ?, last_error = ?, updated_at = ? WHERE outbox_id = ? AND status = ?"
                " AND lease_token IS NULL",
                (status, error, stamp, outbox_id, current["status"]))
            if cursor.rowcount != 1:
                self._refusal(db, outbox_id, stamp, status, expected_status=current["status"])
        return self.get(outbox_id)

    def persist_claimed_receipt(self, outbox_id: str, receipt: Mapping[str, Any], *,
                                token: str, fence: int, now: datetime | None = None) -> dict[str, Any]:
        """Persist a validated receipt only for the worker that owns the submission."""
        current = self.get(outbox_id)
        if current["status"] not in ("submitting", "response_unknown"):
            raise FeedbackProtocolError("receipt cannot be persisted from " + current["status"])
        checked = validate_receipt(receipt, current["envelope"])
        stamp = _utc_stamp(now)
        with self._db() as db:
            cursor = db.execute(
                "UPDATE outbox SET status='receipt_persisted', receipt_json=?, lease_token=NULL, lease_kind=NULL, "
                "lease_until=NULL, updated_at=? WHERE outbox_id=? AND lease_token=? AND lease_fence=? "
                "AND lease_until IS NOT NULL AND datetime(lease_until) > datetime(?)",
                (json.dumps(checked, sort_keys=True), stamp, outbox_id, token, fence, stamp),
            )
            if cursor.rowcount != 1:
                raise OutboxBusy("outbox lease was lost")
        return self.get(outbox_id)

    def persist_receipt(self, outbox_id: str, receipt: Mapping[str, Any],
                        *, now: datetime | None = None) -> dict[str, Any]:
        stamp = _utc_stamp(now)
        current = self.get(outbox_id)
        if current["status"] not in ("submitting", "response_unknown"):
            raise FeedbackProtocolError("receipt cannot be persisted from " + current["status"])
        checked = validate_receipt(receipt, current["envelope"])
        if current.get("lease_token") is not None:
            raise OutboxBusy("outbox lease requires a fenced completion")
        with self._db() as db:
            cursor = db.execute(
                "UPDATE outbox SET status = 'receipt_persisted', receipt_json = ?, updated_at = ?"
                " WHERE outbox_id = ? AND status = ? AND lease_token IS NULL",
                (json.dumps(checked, sort_keys=True), stamp, outbox_id, current["status"]))
            if cursor.rowcount != 1:
                self._refusal(db, outbox_id, stamp, "receipt_persisted", expected_status=current["status"])
        return self.get(outbox_id)

    def history(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return safe record metadata without opening diagnostic archives."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise FeedbackProtocolError("invalid outbox history limit")
        with self._db() as db:
            rows = db.execute(
                "SELECT outbox_id, app_version, status, receipt_json, last_error, created_at, updated_at, "
                "archive_size, archive_sha256 FROM outbox ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            receipt = json.loads(row["receipt_json"]) if row["receipt_json"] else None
            result.append({
                "outbox_id": row["outbox_id"], "app_version": row["app_version"], "status": row["status"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "case_id": receipt.get("case_id") if isinstance(receipt, dict) else None,
                "received_at": receipt.get("received_at") if isinstance(receipt, dict) else None,
                "last_error": row["last_error"], "archive_size": row["archive_size"],
                "archive_sha256_short": row["archive_sha256"][:12],
            })
        return result

    def cleanup_local(self, outbox_id: str) -> dict[str, Any]:
        """Delete only a locally received object, preserving receipt metadata."""
        claimed = self.claim(outbox_id, kind="cleanup", statuses=("receipt_persisted",),
                             target_status="cleanup_pending", lease_seconds=60)
        token, fence = claimed["lease_token"], claimed["lease_fence"]
        path = Path(claimed["archive_path"])
        try:
            path.unlink(missing_ok=True)
            descriptor = os.open(self.objects_dir, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return self.finish_claim(outbox_id, token=token, fence=fence, status="cleaned",
                                     now=datetime.now(timezone.utc))
        except Exception as exc:
            now = datetime.now(timezone.utc).isoformat()
            with self._db() as db:
                db.execute(
                    "UPDATE outbox SET last_error=?, lease_token=NULL, lease_kind=NULL, lease_until=NULL, updated_at=? "
                    "WHERE outbox_id=? AND lease_token=? AND lease_fence=?",
                    (type(exc).__name__, now, outbox_id, token, fence),
                )
            raise

    def pending(self) -> list[dict[str, Any]]:
        with self._db() as db:
            ids = [row[0] for row in db.execute(
                "SELECT outbox_id FROM outbox WHERE status != 'cleaned' ORDER BY created_at").fetchall()]
        return [self.get(item) for item in ids]

    def recover_interrupted(self, *, now: datetime | None = None) -> int:
        """Recover expired submissions and interrupted local-cleanup work.

        A receipt has already been persisted before cleanup begins, so recovery
        never resubmits it.  If the local object is gone, cleanup crossed its
        destructive step before the process stopped and can safely be recorded
        as complete; otherwise the receipt is returned to the retryable state.
        """
        stamp = _utc_stamp(now)
        recovered = 0
        with self._db() as db:
            cursor = db.execute(
                "UPDATE outbox SET status='response_unknown', last_error='process_interrupted', "
                "lease_token=NULL, lease_kind=NULL, lease_until=NULL, updated_at=? "
                "WHERE status='submitting' AND (lease_until IS NULL OR datetime(lease_until) <= datetime(?))",
                (stamp, stamp),
            )
            recovered += cursor.rowcount
            pending = db.execute(
                "SELECT outbox_id, archive_path FROM outbox WHERE status='cleanup_pending' "
                "AND (lease_until IS NULL OR datetime(lease_until) <= datetime(?))", (stamp,)
            ).fetchall()
            for row in pending:
                status = "cleaned" if not Path(row["archive_path"]).exists() else "receipt_persisted"
                cursor = db.execute(
                    "UPDATE outbox SET status=?, last_error='cleanup_interrupted', lease_token=NULL, "
                    "lease_kind=NULL, lease_until=NULL, updated_at=? WHERE outbox_id=? "
                    "AND status='cleanup_pending' AND (lease_until IS NULL OR datetime(lease_until) <= datetime(?))",
                    (status, stamp, row["outbox_id"], stamp),
                )
                recovered += cursor.rowcount
        return recovered

    def reconcile_orphans(self) -> list[str]:
        """Return unreferenced object files; never silently deletes them."""
        with self._db() as db:
            referenced = {Path(row[0]).name for row in db.execute("SELECT archive_path FROM outbox")}
        return sorted(str(path) for path in self.objects_dir.glob("*.zip") if path.name not in referenced)
