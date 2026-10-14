import queue
from unittest import main

from sf_window_message_router import WindowMessageRouter

try:
    from _base import SFCase
except ImportError:
    from tests._base import SFCase


class TestWindowMessageRouter(SFCase):
    def test_coalesces_quotes_but_keeps_intermediate_evaluations(self):
        q = queue.Queue()
        q.put(("quote", ("sh600519", {"price": 1})))
        q.put(("quote", ("sh600519", {"price": 2})))
        q.put(("quote", ("sh600519", {"price": 3})))

        batch = WindowMessageRouter.drain(q, "sh600519")

        self.assertTrue(batch.processed)
        self.assertEqual(batch.quote_evaluations, [{"price": 1}, {"price": 2}])
        self.assertEqual(batch.latest_quote, {"price": 3})

    def test_successful_minute_message_clears_prior_stale_marker(self):
        q = queue.Queue()
        q.put(("minute_stale", "sh600519"))
        q.put(("minute", ("sh600519", [(1, 2)])))

        batch = WindowMessageRouter.drain(q, "sh600519")

        self.assertEqual(batch.latest_minute, [(1, 2)])
        self.assertIsNone(batch.minute_stale_after)

    def test_progress_keeps_only_latest_item(self):
        q = queue.Queue()
        q.put(("update_progress", (10, 1, 10)))
        q.put(("update_progress", (80, 8, 10)))

        batch = WindowMessageRouter.drain(q, "sh600519")

        self.assertEqual(batch.update_progresses, [(80, 8, 10)])

    def test_wrong_code_is_discarded_only_for_code_bound_messages(self):
        q = queue.Queue()
        q.put(("quote", ("sh000001", {"price": 1})))
        q.put(("minute", ("sh000001", [(1, 2)])))
        q.put(("net", False))

        batch = WindowMessageRouter.drain(q, "sh600519")

        self.assertIsNone(batch.latest_quote)
        self.assertIsNone(batch.latest_minute)
        self.assertFalse(batch.latest_net)
        self.assertTrue(batch.has_latest_net)

    def test_non_coalescing_mode_preserves_order(self):
        q = queue.Queue()
        q.put(("quote", ("sh600519", {"price": 1})))
        q.put(("minute_stale", "sh600519"))
        q.put(("update_progress", (20, 2, 10)))

        batch = WindowMessageRouter.drain(q, "sh600519", coalesce=False)

        self.assertEqual(batch.messages, [
            ("quote", ("sh600519", {"price": 1})),
            ("minute_stale", "sh600519"),
            ("update_progress", (20, 2, 10)),
        ])


if __name__ == "__main__":
    main()
