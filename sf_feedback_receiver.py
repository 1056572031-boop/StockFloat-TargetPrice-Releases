"""Local-only diagnostic feedback receiver, kept for protocol verification.

This is not the product's upload path: the app uploads through
``sf_feedback_transport`` to the hosted receiver.  This module is started only by
tests and tools, never by the app itself.

The wire protocol is intentionally private and small: a length-prefixed JSON
envelope followed by a length-prefixed ZIP body, returned as one length-
prefixed JSON receipt.  Binding a Unix socket keeps it off the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import socketserver
import sqlite3
import struct
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sf_feedback import (
    MAX_ARCHIVE_BYTES,
    SCHEMA_VERSION,
    FeedbackProtocolError,
    validate_archive,
)

MAX_ENVELOPE_BYTES = 64 * 1024
_HEADER = struct.Struct("!Q")


class ReceiverError(FeedbackProtocolError):
    """Raised for a rejected local submission."""


def _send_frame(stream, value: bytes) -> None:
    stream.sendall(_HEADER.pack(len(value)) + value)


def _read_exact(stream, size: int) -> bytes:
    if size < 0 or size > max(MAX_ENVELOPE_BYTES, MAX_ARCHIVE_BYTES):
        raise ReceiverError("frame exceeds the local receiver limit")
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.recv(min(1024 * 1024, remaining))
        if not chunk:
            raise ReceiverError("connection ended before the frame was complete")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(stream) -> bytes:
    raw_size = _read_exact(stream, _HEADER.size)
    size = _HEADER.unpack(raw_size)[0]
    return _read_exact(stream, size)


def submit(socket_path: os.PathLike[str] | str, envelope: dict[str, Any], archive_path: os.PathLike[str] | str,
           *, timeout: float = 5.0, read_response: bool = True) -> dict[str, Any] | None:
    """Submit one archive to a local receiver; no retry or network fallback is performed."""
    envelope_bytes = json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")).encode("utf-8")
    archive = Path(archive_path).read_bytes()
    if len(envelope_bytes) > MAX_ENVELOPE_BYTES or len(archive) > MAX_ARCHIVE_BYTES:
        raise ReceiverError("submission exceeds the local receiver limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(os.fspath(socket_path))
        _send_frame(client, envelope_bytes)
        _send_frame(client, archive)
        if not read_response:
            return None
        response = json.loads(_read_frame(client).decode("utf-8"))
        if response.get("ok") is not True:
            raise ReceiverError(response.get("error") or "local receiver rejected submission")
        return response["receipt"]


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        receiver: FeedbackReceiver = self.server.receiver  # type: ignore[attr-defined]
        try:
            envelope = json.loads(_read_frame(self.request).decode("utf-8"))
            archive = _read_frame(self.request)
            receipt = receiver.accept(envelope, archive)
            try:
                _send_frame(self.request, json.dumps({"ok": True, "receipt": receipt},
                                                      sort_keys=True).encode("utf-8"))
            except BrokenPipeError:
                return
        except (FeedbackProtocolError, OSError, ValueError, sqlite3.Error) as exc:
            try:
                _send_frame(self.request, json.dumps({"ok": False, "error": str(exc)},
                                                      sort_keys=True).encode("utf-8"))
            except BrokenPipeError:
                return


class _Server(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, path: str, receiver: FeedbackReceiver):
        self.receiver = receiver
        super().__init__(path, _Handler)


class FeedbackReceiver:
    """Persist validated local submissions and immutable receipts."""

    def __init__(self, socket_path: os.PathLike[str] | str,
                 database_path: os.PathLike[str] | str,
                 objects_dir: os.PathLike[str] | str,
                 *, fault_after_object: bool = False):
        self.socket_path = Path(socket_path)
        self.database_path = Path(database_path)
        self.objects_dir = Path(objects_dir)
        self.fault_after_object = fault_after_object
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                archive_sha256 TEXT NOT NULL,
                archive_size INTEGER NOT NULL,
                object_path TEXT NOT NULL,
                received_at TEXT NOT NULL,
                storage_status TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                receipt_json TEXT NOT NULL
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

    def start(self) -> None:
        if self._server is not None:
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self._server = _Server(os.fspath(self.socket_path), self)
        os.chmod(self.socket_path, 0o600)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="feedback-receiver", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.socket_path.unlink(missing_ok=True)

    def accept(self, envelope: dict[str, Any], archive: bytes) -> dict[str, Any]:
        if not isinstance(envelope, dict) or envelope.get("schema_version") != SCHEMA_VERSION:
            raise ReceiverError("unsupported envelope")
        key = envelope.get("idempotency_key")
        declared_hash = envelope.get("archive_sha256")
        declared_size = envelope.get("size_bytes")
        if not isinstance(key, str) or not key or not isinstance(declared_hash, str) \
                or not isinstance(declared_size, int):
            raise ReceiverError("envelope is incomplete")
        actual_hash = hashlib.sha256(archive).hexdigest()
        if len(archive) != declared_size or actual_hash != declared_hash:
            raise ReceiverError("received archive does not match envelope")
        with tempfile.NamedTemporaryFile(prefix="incoming-", suffix=".zip", dir=self.objects_dir,
                                         delete=False) as stream:
            stream.write(archive)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        try:
            validate_archive(temporary)
            with self._db() as db:
                existing = db.execute("SELECT * FROM cases WHERE idempotency_key = ?", (key,)).fetchone()
                if existing:
                    if existing["archive_sha256"] != actual_hash:
                        raise ReceiverError("idempotency key is bound to a different archive")
                    return json.loads(existing["receipt_json"])
                object_path = self.objects_dir / (actual_hash + ".zip")
                os.replace(temporary, object_path)
                if self.fault_after_object:
                    raise ReceiverError("injected failure after object write")
                received_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                receipt = {
                    "case_id": "case-" + secrets.token_hex(12),
                    "archive_sha256": actual_hash,
                    "size_bytes": len(archive),
                    "received_at": received_at,
                    "storage_status": "stored",
                    "validation_status": "validated",
                    "retention_until": (datetime.now(timezone.utc) + timedelta(days=30))
                    .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                }
                try:
                    db.execute("""INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                               (receipt["case_id"], key, actual_hash, len(archive), str(object_path), received_at,
                                receipt["storage_status"], receipt["validation_status"],
                                json.dumps(receipt, sort_keys=True)))
                except sqlite3.IntegrityError:
                    concurrent = db.execute("SELECT * FROM cases WHERE idempotency_key = ?", (key,)).fetchone()
                    if concurrent is None or concurrent["archive_sha256"] != actual_hash:
                        raise ReceiverError("idempotency key was concurrently bound to a different archive")
                    return json.loads(concurrent["receipt_json"])
                return receipt
        finally:
            temporary.unlink(missing_ok=True)

    def reconcile(self) -> dict[str, list[str]]:
        with self._db() as db:
            rows = db.execute("SELECT object_path FROM cases").fetchall()
        referenced = {Path(row[0]).name for row in rows}
        objects = {path.name for path in self.objects_dir.glob("*.zip")}
        orphaned = sorted(objects - referenced)
        missing = sorted(name for name in referenced if not (self.objects_dir / name).exists())
        if missing:
            with self._db() as db:
                for name in missing:
                    row = db.execute("SELECT case_id, receipt_json FROM cases WHERE object_path = ?",
                                     (str(self.objects_dir / name),)).fetchone()
                    if row is None:
                        continue
                    receipt = json.loads(row["receipt_json"])
                    receipt["storage_status"] = "storage_missing"
                    receipt["validation_status"] = "storage_fault"
                    db.execute("UPDATE cases SET storage_status = ?, validation_status = ?, receipt_json = ? "
                               "WHERE case_id = ?",
                               ("storage_missing", "storage_fault", json.dumps(receipt, sort_keys=True),
                                row["case_id"]))
        return {"orphaned_objects": orphaned, "missing_objects": missing}

    def case_for(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM cases WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        return dict(row) if row is not None else None


def run_receiver(socket_path: os.PathLike[str] | str, database_path: os.PathLike[str] | str,
                 objects_dir: os.PathLike[str] | str) -> None:
    receiver = FeedbackReceiver(socket_path, database_path, objects_dir)
    receiver.start()
    try:
        threading.Event().wait()
    finally:
        receiver.stop()
