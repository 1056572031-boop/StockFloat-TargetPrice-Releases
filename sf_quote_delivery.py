"""Keep received price extremes independently of discardable display frames."""
import math
import threading
from sf_quote import quote_is_current


class TargetTickBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}
        self._sequence = 0

    def add(self, code, quote):
        if not quote_is_current(quote):
            return
        try:
            price = float(quote["price"])
            if not math.isfinite(price) or price <= 0:
                return
        except (TypeError, ValueError, KeyError):
            return
        with self._lock:
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

    def take(self, code):
        """取出当前股票的本轮低高价，并丢弃其他股票的滞后缓冲。

        主窗一次只消费当前股票；切股时会主动清空缓冲，因此这里不是多标的消息队列。
        """
        with self._lock:
            pending = self._pending.pop(code, None)
            self._pending.clear()
        if pending is None:
            return []
        low, high = pending
        return [item[2] for item in sorted({low[0]: low, high[0]: high}.values())]

    def clear(self):
        with self._lock:
            self._pending.clear()
