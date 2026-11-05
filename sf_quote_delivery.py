"""Keep received price extremes independently of discardable display frames."""
import itertools
import json
import math
import sqlite3
import threading
import weakref
from typing import TYPE_CHECKING
from sf_quote import QuoteData, quote_is_current
from sf_utils import normalize_code

if TYPE_CHECKING:
    from sf_config import TickPublisherToken


_MEMORY_SPILL_MAX = 8192
UNKNOWN_REVISION_MAX_AGE_SEC = 15.0


class TargetTickBuffer:
    def __init__(self, persistent_path=None, *, persistent_max_entries=None,
                 persistent_max_bytes=None, async_recovery=False, on_recovery=None):
        self._lock = threading.Lock()
        self._on_recovery = on_recovery
        self._async_recovery = async_recovery
        self._recovery_running = False
        self._recovery_error: Exception | None = None
        self._recovery_thread: threading.Thread | None = None
        self._persistent_path = persistent_path
        self._persistent_max_entries = persistent_max_entries
        self._persistent_max_bytes = persistent_max_bytes
        self._persistent_objects: weakref.WeakValueDictionary[int, QuoteData] = weakref.WeakValueDictionary()
        self._unsaved_ticks: list[tuple[str, QuoteData]] = []
        self._publisher_tokens: dict[str, "TickPublisherToken"] = {}
        self._pending = {}
        self._ordered = {}
        self._overflow_first = {}
        self._pinned = {}
        self._paged_codes = set()
        self.blocked_stocks = set()
        self._unknown_first_seen: dict[int, float] = {}
        self._sequence = 0
        self._taken = {}

    @property
    def persistent(self):
        return bool(self._persistent_path)

    @property
    def persistent_path(self):
        """磁盘队列文件路径（只读）。诊断导出只对它做 stat，从不打开。"""
        return self._persistent_path

    def add(self, code, quote):
        try:
            price = float(quote["price"])
            if not math.isfinite(price) or price <= 0:
                return
        except (TypeError, ValueError, KeyError):
            return
        with self._lock:
            prior = self._taken.pop(id(quote), None)
            if prior is not None and prior[2] is quote:
                tick = prior
            else:
                if not quote_is_current(quote):
                    return
                self._sequence += 1
                tick = (self._sequence, price, quote)
            old = self._pending.get(code)
            if old is None:
                self._pending[code] = (tick, tick)
            else:
                low, high = old
                self._pending[code] = (tick if price < low[1] else low,
                                       tick if price > high[1] else high)
            # Only the current and a few late previous requests can be in flight.
            while len(self._pending) > 4:
                del self._pending[next(iter(self._pending))]
            low, high = self._pending[code]
            return (low[2], high[2])

    def _sync_publishers_locked(self):
        """Keep one cross-process marker per stock whose ticks are only in memory.

        A volatile tick is invisible to every other process until it reaches the
        SQLite queue, yet the automatic target expiry cleanup in another process
        would happily delete the target that tick still belongs to.  The marker
        exists exactly while this process holds such a tick, so the declarative
        reconciliation below also heals a missed call site on the next mutation.
        """
        import sf_config
        wanted = {normalize_code(item_code) for item_code, _ in self._unsaved_ticks}
        for code in sorted(wanted - set(self._publisher_tokens)):
            token = sf_config.target_tick_publisher_open(code)
            if token is not None:
                self._publisher_tokens[code] = token
        for code in sorted(set(self._publisher_tokens) - wanted):
            try:
                self._publisher_tokens[code].close()
            except OSError as exc:
                # The tick itself is already durable. Keep the token so the next
                # reconciliation retries the removal; its owner is this live
                # process, so the entry stays conservative until then.
                from sf_logging import log_swallow
                log_swallow("sf_quote_delivery.publisher_release", exc)
                continue
            del self._publisher_tokens[code]

    def diagnostics(self):
        """Read-only snapshot for the diagnostic package; never blocks and never touches disk.

        It answers "why did my reminder not fire": how many ticks exist only in memory,
        for which stocks, and which cross-process markers this buffer is holding.
        """
        if not self._persistent_path:
            return {"persistent": False}
        if not self._lock.acquire(blocking=False):
            return {"persistent": True, "busy": True}
        try:
            counts: dict = {}
            for item_code, _quote in self._unsaved_ticks:
                key = normalize_code(item_code)
                counts[key] = counts.get(key, 0) + 1
            body = {
                "persistent": True,
                "unsaved": len(self._unsaved_ticks),
                "unsaved_codes": dict(sorted(counts.items(), key=lambda kv: -kv[1])[:20]),
                "held_markers": sorted(self._publisher_tokens)[:20],
                "recovery_error": self._recovery_error is not None,
            }
            if len(counts) > 20:
                body["truncated"] = len(counts) - 20
            return body
        finally:
            self._lock.release()

    def adopt_publisher(self, code, token):
        """Take over a caller's marker when this buffer holds volatile ticks for the
        stock but could not register one of its own.

        Returns True when the buffer now owns ``token``; the caller must then not
        close it.  A False result leaves ownership with the caller.
        """
        code = normalize_code(code)
        with self._lock:
            holds = any(normalize_code(item_code) == code for item_code, _ in self._unsaved_ticks)
            if holds and code not in self._publisher_tokens:
                self._publisher_tokens[code] = token
                return True
        return False

    def add_ordered(self, code, quote):
        """Retain received ticks in order while the UI is waiting to poll."""
        try:
            price = float(quote["price"])
            if not math.isfinite(price) or price <= 0:
                return
        except (TypeError, ValueError, KeyError):
            return
        if self._persistent_path:
            if not isinstance(quote, QuoteData):
                if not quote_is_current(quote):
                    return
                original_quote = quote
                quote = QuoteData(quote)
                quote._plain_quote = True
                quote._plain_original = original_quote
            with self._lock:
                existing = getattr(quote, "_target_store_record", None)
                if (existing is not None and existing[0] == self._persistent_path and
                        existing[1] == code and self._persistent_objects.get(existing[2]) is quote):
                    return (quote,)
            if not quote_is_current(quote):
                return
            from sf_target_tick_store import CapacityError, RetainedCapacityError, TargetTickStore
            with self._lock:
                if self._unsaved_ticks:
                    if len(self._unsaved_ticks) >= 4096:
                        raise CapacityError("target tick volatile queue full")
                    self._unsaved_ticks.append((code, quote))
                    self._sync_publishers_locked()
                    return (quote,)
                try:
                    with TargetTickStore(self._persistent_path, max_entries=self._persistent_max_entries,
                                         max_bytes=self._persistent_max_bytes) as store:
                        tick_id = store.save(code, quote)
                except CapacityError as exc:
                    self._unsaved_ticks.append((code, quote))
                    self._sync_publishers_locked()
                    raise RetainedCapacityError(str(exc)) from exc
                except Exception as exc:
                    from sf_logging import log_line
                    log_line("target tick persistence failed: %r" % (exc,), "WARNING")
                    self._unsaved_ticks.append((code, quote))
                    self._sync_publishers_locked()
                    return (quote,)
                quote._target_store_record = (self._persistent_path, code, tick_id)
                self._persistent_objects[tick_id] = quote
            return (quote,)
        with self._lock:
            prior = self._taken.pop(id(quote), None)
            if prior is not None and prior[2] is quote:
                record = prior
            else:
                if not quote_is_current(quote):
                    return
                self._sequence += 1
                record = (self._sequence, price, quote)
            records = self._ordered.setdefault(code, [])
            if len(records) >= 4096:
                compacted: list = []
                segment: list = []
                previous_revision = object()
                for item in records:
                    revision = getattr(item[2], "target_revision", None)
                    if segment and revision != previous_revision:
                        if segment[-1] is not compacted[-1]:
                            compacted.append(segment[-1])
                        segment = []
                    if not segment:
                        low = high = item[1]
                        compacted.append(item)
                    elif item[1] < low or item[1] > high:
                        low = min(low, item[1])
                        high = max(high, item[1])
                        compacted.append(item)
                    segment = [item]
                    previous_revision = revision
                if segment and segment[-1] is not compacted[-1]:
                    compacted.append(segment[-1])
                records[:] = compacted
            if len(records) >= 4096:
                revision = getattr(record[2], "target_revision", None)
                try:
                    if not (isinstance(revision, tuple) and len(revision) == 3 and
                            isinstance(revision[2], tuple) and revision[2] and
                            all(getattr(item[2], "target_revision", None) == revision for item in records)):
                        raise ValueError("unavailable target structure")
                    targets = [json.loads(value) for value in revision[2]]
                    conditions = [(item["direction"], float(item["target"])) for item in targets]
                    if any(direction not in ("above", "below") or not math.isfinite(target) or target <= 0
                           for direction, target in conditions):
                        raise ValueError("invalid target structure")
                    remaining = set(range(len(conditions)))
                    essential = []
                    for item in records:
                        hits = {index for index in remaining
                                if (item[1] >= conditions[index][1] if conditions[index][0] == "above"
                                    else item[1] <= conditions[index][1])}
                        if hits:
                            essential.append(item)
                            remaining.difference_update(hits)
                    if records and (not essential or essential[-1] is not records[-1]):
                        essential.append(records[-1])
                    records[:] = essential
                except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    from sf_logging import log_swallow
                    log_swallow("sf_quote_delivery.add_ordered.target_structure", exc)
            if len(records) >= 4096:
                if not self._pinned.get(code) and len(records) < _MEMORY_SPILL_MAX:
                    records.append(record)
                    pending = self._pending.get(code, ())
                    pinned = self._pinned.get(code, ())
                    return tuple(item[2] for item in pending) + tuple(item[2] for item in pinned) + tuple(item[2] for item in records)
                from sf_logging import log_line
                log_line("target tick queue full for %s; preserving overflow extremes" % code, "WARNING")
                self._overflow_first.setdefault(code, record)
                old = self._pending.get(code)
                if old is None:
                    self._pending[code] = (record, record)
                else:
                    low, high = old
                    self._pending[code] = (record if price < low[1] else low,
                                           record if price > high[1] else high)
                pending = self._pending[code]
                pinned = self._pinned.get(code, ())
                return ((self._overflow_first[code][2],) +
                        tuple(item[2] for item in pending) +
                        tuple(item[2] for item in pinned) +
                        tuple(item[2] for item in records))
            records.append(record)
            while len(self._ordered) > 4:
                evicted = next(iter(self._ordered))
                del self._ordered[evicted]
                self._paged_codes.discard(evicted)
                self._overflow_first.pop(evicted, None)
                self._pending.pop(evicted, None)
            pending = self._pending.get(code, ())
            pinned = self._pinned.get(code, ())
            return tuple(item[2] for item in pending) + tuple(item[2] for item in pinned) + tuple(item[2] for item in records)

    def retain_unsaved(self, code, quote):
        """Retain a rejected tick temporarily until durable capacity is available."""
        from sf_target_tick_store import CapacityError
        with self._lock:
            record = getattr(quote, "_target_store_record", None)
            if (record is not None and record[0] == self._persistent_path and
                    record[1] == code and self._persistent_objects.get(record[2]) is quote):
                return
            if any(getattr(item, "_plain_original", None) is quote and
                   getattr(item, "_target_store_record", (None, None))[1] == code
                   for item in self._persistent_objects.values()):
                return
            if any(item_code == code and (item is quote or
                   getattr(item, "_plain_original", None) is quote)
                   for item_code, item in self._unsaved_ticks):
                return
            if not isinstance(quote, QuoteData):
                original_quote = quote
                quote = QuoteData(quote)
                quote._plain_quote = True
                quote._plain_original = original_quote
            if len(self._unsaved_ticks) >= 4096:
                raise CapacityError("target tick volatile queue full")
            self._unsaved_ticks.append((code, quote))
            self._sync_publishers_locked()
            return True

    def pin(self, code, quote):
        """Keep a failed tick separately from later compressible extrema."""
        if self._persistent_path:
            with self._lock:
                item = getattr(quote, "_target_store_record", None)
            if item is not None and item[0] == self._persistent_path and item[1] == code:
                return tuple(self.take(code))
        try:
            price = float(quote["price"])
        except (TypeError, ValueError, KeyError):
            return ()
        with self._lock:
            record = self._taken.pop(id(quote), None)
            if record is None or record[2] is not quote:
                self._sequence += 1
                record = (self._sequence, price, quote)
            records = self._pinned.setdefault(code, [])
            if not any(item[0] == record[0] for item in records):
                if len(records) >= 128:
                    from sf_logging import log_line
                    log_line("target retry queue full for %s" % code, "WARNING")
                    pending = self._pending.get(code, ())
                    ordered = self._ordered.get(code, ())
                    first = self._overflow_first.get(code)
                    return ((first[2],) if first is not None else ()) + (
                            tuple(item[2] for item in pending) +
                            tuple(item[2] for item in ordered) +
                            tuple(item[2] for item in records))
                records.append(record)
            pending = self._pending.get(code, ())
            ordered = self._ordered.get(code, ())
            first = self._overflow_first.get(code)
            return ((first[2],) if first is not None else ()) + (
                    tuple(item[2] for item in pending) +
                    tuple(item[2] for item in ordered) +
                    tuple(item[2] for item in records))

    def _recover_async_batch(self):
        from sf_target_tick_store import TargetTickStore
        saved_count = 0
        try:
            import sf_config
            with sf_config.target_tick_transaction(blocking=False):
                with self._lock:
                    try:
                        with TargetTickStore(self._persistent_path, max_entries=self._persistent_max_entries,
                                             max_bytes=self._persistent_max_bytes) as store:
                            for _ in range(min(len(self._unsaved_ticks), 64)):
                                code, quote = self._unsaved_ticks[0]
                                tick_id = store.save(code, quote)
                                quote._target_store_record = (self._persistent_path, code, tick_id)
                                self._persistent_objects[tick_id] = quote
                                self._unsaved_ticks.pop(0)
                                saved_count += 1
                    finally:
                        self._sync_publishers_locked()
        except Exception as exc:
            with self._lock:
                self._recovery_error = exc
            from sf_logging import log_line
            log_line("target tick async recovery deferred: %r" % (exc,), "WARNING")
        finally:
            with self._lock:
                self._recovery_running = False
            if saved_count and callable(self._on_recovery):
                try:
                    self._on_recovery()
                except Exception as exc:
                    from sf_logging import log_line
                    log_line("target tick recovery wake failed: %r" % (exc,), "WARNING")

    def take(self, code, *, global_order=False):
        if self._persistent_path and not self._async_recovery:
            import sf_config
            with sf_config.target_tick_transaction(blocking=False):
                return self._take(code, global_order=global_order)
        return self._take(code, global_order=global_order)

    def _take(self, code, *, global_order=False):
        """取出当前股票的本轮低高价，并丢弃其他股票的滞后缓冲。

        主窗一次只消费当前股票；切股时会主动清空缓冲，因此这里不是多标的消息队列。
        """
        if self._persistent_path:
            from sf_target_tick_store import CapacityError, TargetTickStore
            if not self._lock.acquire(blocking=False):
                raise BlockingIOError("target tick producer is busy")
            try:
                if self._unsaved_ticks and self._async_recovery:
                    with TargetTickStore(self._persistent_path) as store:
                        if global_order:
                            existing, self.blocked_stocks = store.read_actionable_prefix(limit=4096)
                        else:
                            existing = store.read(code, limit=4096)
                    if global_order:
                        existing = list(itertools.takewhile(
                            lambda item: normalize_code(item.code) == normalize_code(code), existing))
                    if existing:
                        result = []
                        for item in existing:
                            quote = self._persistent_objects.get(item.id, item.quote)
                            quote._target_store_record = (self._persistent_path, item.code, item.id)
                            self._persistent_objects[item.id] = quote
                            result.append(quote)
                        return result
                    if self._recovery_error is not None:
                        error = self._recovery_error
                        self._recovery_error = None
                        raise error
                    if not self._recovery_running:
                        self._recovery_running = True
                        try:
                            worker = threading.Thread(target=self._recover_async_batch, daemon=True,
                                                      name="target-tick-recovery")
                            self._recovery_thread = worker
                            worker.start()
                        except Exception:
                            self._recovery_running = False
                            raise
                    raise BlockingIOError("target ticks awaiting durable recovery")
                if self._unsaved_ticks:
                    with TargetTickStore(self._persistent_path, max_entries=self._persistent_max_entries,
                                         max_bytes=self._persistent_max_bytes) as store:
                        for _ in range(min(len(self._unsaved_ticks), 64)):
                            pending_code, pending_quote = self._unsaved_ticks[0]
                            try:
                                tick_id = store.save(pending_code, pending_quote)
                            except CapacityError:
                                break
                            except (OSError, sqlite3.Error) as exc:
                                from sf_logging import log_line
                                log_line("target tick retry persistence failed: %r" % (exc,), "WARNING")
                                break
                            pending_quote._target_store_record = (self._persistent_path, pending_code, tick_id)
                            self._persistent_objects[tick_id] = pending_quote
                            self._unsaved_ticks.pop(0)
                self._sync_publishers_locked()
            finally:
                self._lock.release()
            with TargetTickStore(self._persistent_path) as store:
                if global_order:
                    saved, self.blocked_stocks = store.read_actionable_prefix(limit=4096)
                else:
                    saved = store.read(code, limit=4096)
            if global_order:
                saved = list(itertools.takewhile(
                    lambda item: normalize_code(item.code) == normalize_code(code), saved))
            with self._lock:
                result = []
                for item in saved:
                    quote = self._persistent_objects.get(item.id, item.quote)
                    quote._target_store_record = (self._persistent_path, item.code, item.id)
                    self._persistent_objects[item.id] = quote
                    result.append(quote)
            return result
        with self._lock:
            pending = self._pending.pop(code, ())
            first = self._overflow_first.pop(code, None)
            ordered = self._ordered.pop(code, ())
            pinned = self._pinned.pop(code, ())
            self._pending = {key: value for key, value in self._pending.items()
                             if key in self._overflow_first and key in self._ordered}
            self._overflow_first = {key: value for key, value in self._overflow_first.items()
                                    if key in self._ordered}
            self._pinned.clear()
            records = {item[0]: item for item in pending}
            if first is not None:
                records[first[0]] = first
            records.update({item[0]: item for item in ordered})
            records.update({item[0]: item for item in pinned})
            all_records = sorted(records.values())
            if code in self._paged_codes and len(all_records) > 4096:
                priority = {item[0]: item for item in pending}
                if first is not None:
                    priority[first[0]] = first
                priority_records = sorted(priority.values())
                remaining = [item for item in all_records if item[0] not in priority]
                page_size = max(0, 4096 - len(priority_records))
                self._ordered[code] = remaining[page_size:]
                all_records = priority_records + remaining[:page_size]
            else:
                self._paged_codes.discard(code)
            self._taken = {id(item[2]): item for item in all_records}
        return [item[2] for item in all_records]

    def oldest_persistent(self):
        """Return the oldest durable record without removing it."""
        if not self._persistent_path:
            return None
        from sf_target_tick_store import TargetTickStore
        if not self._lock.acquire(blocking=False):
            raise BlockingIOError("target tick producer is busy")
        try:
            with TargetTickStore(self._persistent_path) as store:
                records, self.blocked_stocks = store.read_actionable_prefix(limit=1)
            if not records:
                return None
            item = records[0]
            quote = self._persistent_objects.get(item.id, item.quote)
            quote._target_store_record = (self._persistent_path, item.code, item.id)
            self._persistent_objects[item.id] = quote
            return item.code, quote
        finally:
            self._lock.release()

    def expire_unknown_revisions(self, now, max_age=UNKNOWN_REVISION_MAX_AGE_SEC):
        """限期保留修订号未知的行，超时后确认丢弃，返回丢弃条数。

        复核 M1：这类行（接收那一刻配置锁超时，取不到修订号）没有任何事后补号的路径，却会
        被 read_actionable_prefix 整只股票挡住。保留一小段时间给锁恢复留余地，超时后留痕丢弃：
        丢的只是这一笔行情对目标价的判定，行情照常显示，提醒由后续行情继续判。
        `now` 用单调时钟；首次见到的时刻只存内存，重启后重新计时，所以一行最多多留一个限期。"""
        if not self._persistent_path:
            return 0
        from sf_target_tick_store import TargetTickStore
        if not self._lock.acquire(blocking=False):
            raise BlockingIOError("target tick producer is busy")
        try:
            with TargetTickStore(self._persistent_path) as store:
                rows = store.unknown_revision_rows()
                alive = {tick_id for tick_id, _ in rows}
                # 已经不在队列里的（被别处确认了）不再占着记录。
                self._unknown_first_seen = {tick_id: seen for tick_id, seen
                                            in self._unknown_first_seen.items() if tick_id in alive}
                expired = []
                for tick_id, code in rows:
                    first = self._unknown_first_seen.setdefault(tick_id, now)
                    if now - first >= max_age:
                        expired.append((tick_id, code))
                dropped = 0
                for tick_id, code in expired:
                    dropped += store.acknowledge(code, [tick_id])
                    self._unknown_first_seen.pop(tick_id, None)
                    self._persistent_objects.pop(tick_id, None)
        finally:
            self._lock.release()
        if dropped:
            # 独立复核 R2：配置锁被持续占住时行情会持续以空修订号落盘，每满限期就丢一批，
            # 逐批写 ERROR 一分钟能写几百条，挤掉 7 天日志轮转。首条必记，其后每 10 批一条
            # （附累计批数）；丢弃本身一条不少，只是日志限流。
            from sf_logging import log_line_throttled
            log_line_throttled("target.unknown_revision_discarded",
                               "target tick revision unknown for over %.0fs; discarded %d tick(s) for %s"
                               % (max_age, dropped, ",".join(sorted({code for _, code in expired}))),
                               "ERROR", 10)
        return dropped

    def unsaved_count(self):
        """Return volatile tick count, or None while the buffer is busy."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return len(self._unsaved_ticks)
        finally:
            self._lock.release()

    def wait_recovery(self, timeout):
        """Wait for an asynchronous recovery worker without holding the buffer lock."""
        worker = self._recovery_thread
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()

    def has_pending(self, code):
        """Return whether durable or volatile ticks remain for this stock.

        The registry check is what makes this answer cross-process: another
        instance can hold a tick only in its own memory, and a volatile tick is
        invisible to this process' SQLite queue.
        """
        if self._persistent_path:
            import sf_config
            from sf_target_tick_store import TargetTickStore
            with self._lock:
                wanted = normalize_code(code)
                if any(normalize_code(item_code) == wanted for item_code, _ in self._unsaved_ticks):
                    return True
            if sf_config.target_tick_publisher_active(code):
                return True
            with TargetTickStore(self._persistent_path) as store:
                return store.has_equivalent_code(code)
        with self._lock:
            return bool(self._pending.get(code) or self._ordered.get(code) or
                        self._overflow_first.get(code) or self._pinned.get(code))

    def acknowledge(self, quote):
        """Acknowledge a tick only after its business processing succeeds."""
        if not self._persistent_path:
            return
        item = getattr(quote, "_target_store_record", None)
        if item is None or item[0] != self._persistent_path:
            return
        from sf_target_tick_store import TargetTickStore
        with TargetTickStore(self._persistent_path) as store:
            store.acknowledge(item[1], [item[2]])
        with self._lock:
            self._persistent_objects.pop(item[2], None)
        del quote._target_store_record

    def park(self, code):
        """Remove the current code's validated ticks without losing their order."""
        with self._lock:
            pending = self._pending.pop(code, ())
            first = self._overflow_first.pop(code, None)
            ordered = self._ordered.pop(code, ())
            self._paged_codes.discard(code)
            pinned = self._pinned.pop(code, ())
            self._pending = {key: value for key, value in self._pending.items()
                             if key in self._overflow_first and key in self._ordered}
            self._overflow_first = {key: value for key, value in self._overflow_first.items()
                                    if key in self._ordered}
            self._pinned.clear()
            self._taken.clear()
            records = {item[0]: item for item in pending}
            if first is not None:
                records[first[0]] = first
            records.update({item[0]: item for item in ordered})
            records.update({item[0]: item for item in pinned})
            return tuple(sorted(records.values()))

    def restore_ordered(self, code, records):
        """Restore parked ticks ahead of arrivals while retaining original sequence."""
        if not records:
            return True
        with self._lock:
            existing = self._ordered.get(code, ())
            existing_pending = self._pending.get(code, ())
            existing_first = self._overflow_first.get(code)
            merged = {item[0]: item for item in existing}
            merged.update({item[0]: item for item in records})
            ordered = sorted(merged.values())
            if ordered:
                self._sequence = max(self._sequence, ordered[-1][0])
            overflow_candidates = list(existing_pending)
            if existing_first is not None:
                overflow_candidates.append(existing_first)
            if len(ordered) > _MEMORY_SPILL_MAX:
                from sf_logging import log_line
                log_line("target tick gap while restoring %s" % code, "ERROR")
                overflow_candidates.extend(ordered[_MEMORY_SPILL_MAX:])
            if len(ordered) > 4096 and not overflow_candidates:
                revision = getattr(ordered[-1][2], "target_revision", None)
                try:
                    if (isinstance(revision, tuple) and len(revision) == 3 and
                            isinstance(revision[2], tuple) and revision[2] and
                            all(getattr(item[2], "target_revision", None) == revision for item in ordered)):
                        conditions = []
                        for value in revision[2]:
                            target = json.loads(value)
                            direction = target["direction"]
                            price = float(target["target"])
                            if direction not in ("above", "below") or not math.isfinite(price) or price <= 0:
                                raise ValueError("invalid target structure")
                            conditions.append((direction, price))
                        remaining = set(range(len(conditions)))
                        for item in ordered:
                            hits = {index for index in remaining
                                    if (item[1] >= conditions[index][1] if conditions[index][0] == "above"
                                        else item[1] <= conditions[index][1])}
                            if hits:
                                overflow_candidates.append(item)
                                remaining.difference_update(hits)
                                if not remaining:
                                    break
                except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    from sf_logging import log_swallow
                    log_swallow("sf_quote_delivery.restore_ordered.target_structure", exc)
            if len(ordered) > 4096:
                from sf_logging import log_line
                log_line("target tick queue full while restoring %s" % code, "WARNING")
                self._paged_codes.add(code)
            if overflow_candidates:
                by_id = {item[0]: item for item in overflow_candidates}
                candidates = sorted(by_id.values())
                self._overflow_first[code] = existing_first or candidates[0]
                self._pending[code] = (min(candidates, key=lambda item: (item[1], item[0])),
                                       max(candidates, key=lambda item: (item[1], -item[0])))
            self._ordered[code] = ordered[:_MEMORY_SPILL_MAX]
            return len(ordered) <= _MEMORY_SPILL_MAX

    def restore(self, code, records):
        """Restore previously validated ticks after a stock switch."""
        with self._lock:
            if not records:
                return
            self._sequence = max(self._sequence, max(item[0] for item in records))
            existing = self._pending.get(code, ())
            pinned = list(self._pinned.get(code, ()))
            seen = {item[0] for item in pinned}
            overflow = False
            for record in records:
                if record[0] in seen:
                    continue
                if len(pinned) >= 128:
                    overflow = True
                    continue
                pinned.append(record)
                seen.add(record[0])
            if overflow:
                from sf_logging import log_line
                log_line("target retry queue full while restoring %s" % code, "WARNING")
            self._pinned[code] = pinned
            combined = tuple(existing)
            if combined:
                low = min(combined, key=lambda item: (item[1], item[0]))
                high = max(combined, key=lambda item: (item[1], -item[0]))
                self._pending[code] = (low, high)

    def clear(self):
        with self._lock:
            self._pending.clear()
            self._ordered.clear()
            self._overflow_first.clear()
            self._pinned.clear()
            self._paged_codes.clear()
            self._taken.clear()
