"""Received crossings survive frame coalescing, queue overflow and stale data."""
import datetime
import queue
from types import SimpleNamespace
from unittest import mock

import sf_quote
from sf_quote_delivery import TargetTickBuffer
import stock_float_window as window
import targets
from tests._base import SFCase
from tests.test_window_poll_message_routing import _Host

NOW = datetime.datetime(2026, 10, 14, 10, 0,
                        tzinfo=datetime.timezone(datetime.timedelta(hours=8)))


class QuoteDeliveryTests(SFCase):
    def test_received_crossing_is_not_lost_when_only_final_price_is_drawn(self):
        host = _Host()
        host.running = True
        host.q = queue.Queue(maxsize=2)
        state = [{"direction": "above", "target": 100, "session_date": "2026-10-14",
                  "state": "pending", "last_side": None}]
        fired = []

        def evaluate(quote):
            nonlocal state
            state, events = targets.evaluate_tick(state, float(quote["price"]), now=NOW)
            fired.extend(events)

        members = {"_minimized": False, "_target_ticks": TargetTickBuffer(),
                   "_evaluate_received_quote": evaluate, "_is_trading_time": lambda: True,
                   "_icons_pending": lambda: False, "_wake_ui": lambda: None}
        for key, value in members.items():
            setattr(host, key, value)
        for price in (99, 101, 99, 98, 99):
            window.FloatWindow._q_put(host, ("quote", (host.code, {"price": str(price)})))  # discipline-ok(R5): actual producer entry with deliberately overflowing display queue
        window.FloatWindow._poll(host)  # discipline-ok(R5): real queue consumer is the behavior under test
        self.assertEqual(len(fired), 1)
        self.assertEqual(host.args_of("_apply_quote"), [({"price": "99"},)])

    def test_both_directions_and_original_extreme_order_are_preserved(self):
        ticks = TargetTickBuffer()
        for price in (100, 110, 90, 100):
            ticks.add("sh600519", {"price": str(price)})
        self.assertEqual([q["price"] for q in ticks.take("sh600519")], ["110", "90"])
        self.assertEqual(ticks.take("sh600519"), [])

    def test_old_stock_and_bad_values_do_not_pollute_new_stock(self):
        ticks = TargetTickBuffer()
        for price in ("nan", "inf", "bad", 0, -1):
            ticks.add("A", {"price": price})
        ticks.add("A", {"price": "5"})
        self.assertEqual(ticks.take("B"), [])
        self.assertEqual(ticks.take("A"), [])

    def test_timestamp_does_not_change_dictionary_contract(self):
        quote = sf_quote.QuoteData({"price": "101"}, "20261014100000")
        self.assertEqual(set(quote), {"price"})
        self.assertTrue(sf_quote.quote_is_current(quote, NOW))
        self.assertFalse(sf_quote.quote_is_current(quote, NOW + datetime.timedelta(days=1)))
        self.assertFalse(sf_quote.quote_is_current(sf_quote.QuoteData({"price": "101"}), NOW))

    def test_stale_and_future_quotes_are_not_trusted(self):
        for stamp in ("20251014100000", "20261014100101", "20261014095000", "bad"):
            self.assertFalse(sf_quote.quote_is_current(sf_quote.QuoteData({}, stamp), NOW), stamp)

    def test_weekend_and_preopen_use_last_trading_day(self):
        quote = sf_quote.QuoteData({}, "20261016150000")
        weekend = NOW.replace(day=18, hour=12)
        self.assertTrue(sf_quote.quote_is_current(quote, weekend))
        self.assertTrue(sf_quote.quote_is_current(quote, NOW.replace(day=19, hour=8)))

    def test_current_backup_can_recover_stale_primary(self):
        stale = sf_quote.QuoteData({"price": "1"}, "20251014100000")
        fresh = sf_quote.QuoteData({"price": "2"}, "20261014100000")
        with mock.patch.object(sf_quote, "_beijing_now", return_value=NOW), \
                mock.patch.object(sf_quote, "fetch_quote_fallback", return_value=fresh):
            self.assertIs(sf_quote.ensure_current_quote("sh600519", stale), fresh)
        with mock.patch.object(sf_quote, "_beijing_now", return_value=NOW), \
                mock.patch.object(sf_quote, "fetch_quote_fallback", return_value=stale):
            with self.assertRaises(sf_quote.QuoteDataError):
                sf_quote.ensure_current_quote("sh600519", stale)

    def test_ui_business_entry_rejects_wrong_stock_and_nontrading(self):
        seen = []
        host = SimpleNamespace(code="sh600519", _targets_enabled=lambda: True,
                               _is_trading_time=lambda: True, _evaluate_targets=seen.append)
        entry = window.FloatWindow._evaluate_received_quote  # discipline-ok(R5): validate the business entry without constructing a native window
        entry(host, {"code": "000001", "price": "101"})
        entry(host, {"code": "600519", "price": "101"})
        self.assertEqual(seen, [101])
        host._is_trading_time = lambda: False  # discipline-ok(R5): inject the nontrading decision at the same business boundary
        entry(host, {"code": "600519", "price": "102"})
        self.assertEqual(seen, [101])
