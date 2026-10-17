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
_TRANSITIONS = {
    "created": {"sealed"}, "sealed": {"locally_verified"},
    "locally_verified": {"submitting"}, "submitting": {"response_unknown", "receipt_persisted"},
    "response_unknown": {"submitting", "receipt_persisted"},
    "receipt_persisted": {"cleanup_pending"}, "cleanup_pending": {"cleaned"},
    "cleaned": set(),
}


class OutboxConflict(FeedbackProtocolError):
    """The same idempotency key was reused for different archive bytes."""


class FeedbackOutbox:
    def __init__(self, database_path: os.PathLike[str] | str, objects_dir: os.PathLike[str] | str):
        self.database_path = Path(database_path)
        self.objects_dir = Path(objects_dir)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
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

    def _connect(self):
        db = sqlite3.connect(self.database_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
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
        with self._db() as db:
            existing = db.execute("SELECT * FROM outbox WHERE idempotency_key = ?",
                                  (envelope["idempotency_key"],)).fetchone()
            if existing:
                if existing["archive_sha256"] != envelope["archive_sha256"]:
                    raise OutboxConflict("idempotency key is bound to a different archive")
                return self.get(existing["outbox_id"])
            target = self._copy_archive(source, outbox_id)
            try:
                db.execute("""INSERT INTO outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)""",
                            (outbox_id, envelope["idempotency_key"], str(target),
                             envelope["archive_sha256"], envelope["size_bytes"], app_version,
                             consent_policy_version, "locally_verified", json.dumps(envelope, sort_keys=True),
                             now, now))
            except Exception:
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

    def transition(self, outbox_id: str, status: str, *, error: str | None = None) -> dict[str, Any]:
        if status not in STATUSES:
            raise FeedbackProtocolError("unknown outbox status")
        current = self.get(outbox_id)
        if status not in _TRANSITIONS[current["status"]]:
            raise FeedbackProtocolError(f"invalid outbox transition: {current['status']} -> {status}")
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as db:
            db.execute("UPDATE outbox SET status = ?, last_error = ?, updated_at = ? WHERE outbox_id = ? AND status = ?",
                       (status, error, now, outbox_id, current["status"]))
        return self.get(outbox_id)

    def persist_receipt(self, outbox_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
        current = self.get(outbox_id)
        if current["status"] not in ("submitting", "response_unknown"):
            raise FeedbackProtocolError("receipt cannot be persisted from " + current["status"])
        checked = validate_receipt(receipt, current["envelope"])
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as db:
            db.execute("UPDATE outbox SET status = 'receipt_persisted', receipt_json = ?, updated_at = ? WHERE outbox_id = ?",
                       (json.dumps(checked, sort_keys=True), now, outbox_id))
        return self.get(outbox_id)

    def pending(self) -> list[dict[str, Any]]:
        with self._db() as db:
            ids = [row[0] for row in db.execute(
                "SELECT outbox_id FROM outbox WHERE status != 'cleaned' ORDER BY created_at").fetchall()]
        return [self.get(item) for item in ids]

    def reconcile_orphans(self) -> list[str]:
        """Return unreferenced object files; never silently deletes them."""
        with self._db() as db:
            referenced = {Path(row[0]).name for row in db.execute("SELECT archive_path FROM outbox")}
        return sorted(str(path) for path in self.objects_dir.glob("*.zip") if path.name not in referenced)
