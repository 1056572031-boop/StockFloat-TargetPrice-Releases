"""Durable, explicitly acknowledged target-price tick queue.

The caller owns the database path and must close the store. Limits apply to the
whole queue; byte limits count UTF-8 encoded stock codes and serialized payloads,
not SQLite's file size. Supported values are JSON primitives, lists, tuples and
string-keyed dictionaries. Unsupported values fail instead of being coerced.
"""
import heapq
import json
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from sf_quote import QuoteData


class CapacityError(Exception):
    """The new tick would exceed a configured queue limit."""



class RetainedCapacityError(CapacityError):
    """The durable queue is full, but the tick remains in volatile memory."""


class DisplayQueuedCapacityError(CapacityError):
    """The target tick was lost, but its quote entered the display queue."""


@dataclass(frozen=True)
class StoredTick:
    id: int
    code: str
    quote: QuoteData


def _encode(value):
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite numbers cannot be stored")
        return value
    if type(value) in (list, tuple):
        return {"type": "tuple" if type(value) is tuple else "list",
                "items": [_encode(item) for item in value]}
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError("dictionary keys must be strings")
        return {"type": "dict", "items": [[key, _encode(item)] for key, item in value.items()]}
    raise TypeError(f"unsupported tick value: {type(value).__name__}")


def _decode(value):
    if not isinstance(value, dict):
        return value
    kind = value["type"]
    items = value["items"]
    if kind == "dict":
        return {key: _decode(item) for key, item in items}
    if kind == "tuple":
        return tuple(_decode(item) for item in items)
    if kind == "list":
        return [_decode(item) for item in items]
    raise ValueError(f"unknown tick value type: {kind!r}")


def _timestamp(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError("quote timestamps must be datetime or None")
    return value.isoformat()


def _positive_limit(value, name, *, allow_zero=False):
    if value is not None and (type(value) is not int or value < (0 if allow_zero else 1)):
        requirement = "nonnegative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a {requirement} integer")


class TargetTickStore:
    """SQLite queue. IDs are monotonically increasing across acknowledgements."""

    def __init__(self, path, *, max_entries=None, max_bytes=None):
        _positive_limit(max_entries, "max_entries", allow_zero=True)
        _positive_limit(max_bytes, "max_bytes", allow_zero=True)
        path = os.fspath(path)
        if not path or path == ":memory:":
            raise ValueError("a persistent database path is required")
        if os.path.islink(path):
            raise ValueError("target tick database path is a symlink")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            fd = -1
        else:
            os.close(fd)
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._db = sqlite3.connect(path, isolation_level=None, timeout=0.05)
        try:
            self._db.execute("CREATE TABLE IF NOT EXISTS target_ticks ("
                             "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, "
                             "payload TEXT NOT NULL, size INTEGER NOT NULL)")
            self._db.execute("CREATE INDEX IF NOT EXISTS target_ticks_code_id "
                             "ON target_ticks(code, id)")
        except BaseException:
            self._db.close()
            raise

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def save(self, code, quote):
        if type(code) is not str or not code:
            raise ValueError("code must be a nonempty string")
        if not isinstance(quote, QuoteData):
            raise TypeError("quote must be QuoteData")
        payload = {"values": _encode(dict(quote)),
                   "source_time": _timestamp(quote.source_time),
                   "received_at": _timestamp(quote.received_at),
                   "plain_quote": bool(getattr(quote, "_plain_quote", False)),
                   "has_revision": hasattr(quote, "target_revision")}
        if payload["has_revision"]:
            payload["revision"] = _encode(quote.target_revision)
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        size = len(code.encode("utf-8")) + len(text.encode("utf-8"))
        self._db.execute("BEGIN IMMEDIATE")
        try:
            count, used = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM target_ticks").fetchone()
            if ((self.max_entries is not None and count + 1 > self.max_entries) or
                    (self.max_bytes is not None and used + size > self.max_bytes)):
                raise CapacityError("target tick queue capacity exceeded")
            cursor = self._db.execute("INSERT INTO target_ticks(code, payload, size) VALUES (?, ?, ?)",
                                      (code, text, size))
            self._db.execute("COMMIT")
            return cursor.lastrowid
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def read(self, code, *, after_id=0, limit=100):
        """Read a page for one stock in insertion order without acknowledging it."""
        _positive_limit(limit, "limit")
        _positive_limit(after_id, "after_id", allow_zero=True)
        rows = self._db.execute("SELECT id, code, payload FROM target_ticks "
                                "WHERE code = ? AND id > ? ORDER BY id LIMIT ?",
                                (code, after_id, limit)).fetchall()
        return self._decode_rows(rows)

    def unknown_revision_rows(self, *, limit=256):
        """列出接收时没取到修订号的行 [(id, code)]，不解码整张表。

        先用 LIKE 在 SQL 里粗筛（保存时 revision 为 None 会写成 "revision":null），
        再逐行解码确认，免得行情文本里恰好出现同样的字样而误判。"""
        _positive_limit(limit, "limit")
        rows = self._db.execute(
            "SELECT id, code, payload FROM target_ticks WHERE payload LIKE ? ORDER BY id LIMIT ?",
            ('%"revision":null%', limit)).fetchall()
        found = []
        for tick_id, code, text in rows:
            payload = json.loads(text)
            if payload.get("has_revision") and payload.get("revision") is None:
                found.append((tick_id, code))
        return found

    def has_equivalent_code(self, code):
        """Check pending stocks without loading or decoding tick payloads."""
        from sf_utils import normalize_code
        wanted = normalize_code(code)
        rows = self._db.execute("SELECT DISTINCT code FROM target_ticks").fetchall()
        return any(normalize_code(row[0]) == wanted for row in rows)

    def read_global(self, *, after_id=0, limit=100):
        """Read across stocks in insertion order without acknowledging records."""
        _positive_limit(limit, "limit")
        _positive_limit(after_id, "after_id", allow_zero=True)
        rows = self._db.execute("SELECT id, code, payload FROM target_ticks "
                                "WHERE id > ? ORDER BY id LIMIT ?",
                                (after_id, limit)).fetchall()
        return self._decode_rows(rows)

    def read_actionable_prefix(self, *, limit=100):
        """Read the oldest actionable stock prefix, retaining unknown revisions."""
        from sf_utils import normalize_code
        _positive_limit(limit, "limit")
        blocked: set[str] = set()
        result: list[StoredTick] = []
        selected = None
        own_transaction = not self._db.in_transaction
        if own_transaction:
            self._db.execute("BEGIN")
        try:
            cursor = self._db.execute("SELECT id, code, payload FROM target_ticks ORDER BY id")
            for row in cursor:
                stock = normalize_code(row[1])
                item = self._decode_rows((row,))[0]
                if hasattr(item.quote, "target_revision") and item.quote.target_revision is None:
                    break
                if selected is None:
                    selected = stock
                if stock != selected:
                    return result, blocked
                result.append(item)
                if len(result) >= limit:
                    return result, blocked
            else:
                return result, blocked
            result = []
            selected = None
            streams: list[tuple[int, str]] = []
            code = None
            while True:
                if code is None:
                    row = self._db.execute(
                        "SELECT code FROM target_ticks INDEXED BY target_ticks_code_id "
                        "ORDER BY code LIMIT 1").fetchone()
                else:
                    row = self._db.execute(
                        "SELECT code FROM target_ticks INDEXED BY target_ticks_code_id "
                        "WHERE code > ? ORDER BY code LIMIT 1", (code,)).fetchone()
                if row is None:
                    break
                code = row[0]
                first = self._db.execute(
                    "SELECT id FROM target_ticks WHERE code = ? ORDER BY id LIMIT 1", (code,)).fetchone()
                heapq.heappush(streams, (first[0], code))
            while streams:
                tick_id, code = heapq.heappop(streams)
                stock = normalize_code(code)
                if stock in blocked:
                    continue
                row = self._db.execute("SELECT id, code, payload FROM target_ticks WHERE id = ?",
                                       (tick_id,)).fetchone()
                item = self._decode_rows((row,))[0]
                if hasattr(item.quote, "target_revision") and item.quote.target_revision is None:
                    blocked.add(stock)
                    if stock == selected:
                        break
                    continue
                if selected is None:
                    selected = stock
                if stock != selected:
                    break
                result.append(item)
                if len(result) >= limit:
                    break
                next_row = self._db.execute(
                    "SELECT id FROM target_ticks WHERE code = ? AND id > ? ORDER BY id LIMIT 1",
                    (code, tick_id)).fetchone()
                if next_row is not None:
                    heapq.heappush(streams, (next_row[0], code))
            return result, blocked
        finally:
            if own_transaction:
                self._db.execute("ROLLBACK")

    @staticmethod
    def _decode_rows(rows):
        result = []
        for tick_id, stock, text in rows:
            payload = json.loads(text)
            quote = QuoteData(_decode(payload["values"]))
            quote.source_time = (datetime.fromisoformat(payload["source_time"])
                                 if payload["source_time"] is not None else None)
            quote.received_at = (datetime.fromisoformat(payload["received_at"])
                                 if payload["received_at"] is not None else None)
            if payload.get("plain_quote", False):
                quote._plain_quote = True
            if payload["has_revision"]:
                quote.target_revision = _decode(payload["revision"])
            result.append(StoredTick(tick_id, stock, quote))
        return result

    def acknowledge(self, code, ids):
        """Atomically delete only the supplied IDs belonging to the given stock."""
        ids = tuple(ids)
        if any(type(tick_id) is not int or tick_id <= 0 for tick_id in ids):
            raise ValueError("tick IDs must be positive integers")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            count = 0
            for tick_id in ids:
                count += self._db.execute("DELETE FROM target_ticks WHERE code = ? AND id = ?",
                                          (code, tick_id)).rowcount
            self._db.execute("COMMIT")
            return count
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
