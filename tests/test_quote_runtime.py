import threading
from unittest import main

from sf_quote_runtime import QuoteRuntimeController

try:
    from _base import SFCase, wait_for
except ImportError:
    from tests._base import SFCase, wait_for


class TestQuoteRuntime(SFCase):
    def test_start_owns_both_worker_threads(self):
        calls = []
        runtime = QuoteRuntimeController()
        runtime.start(lambda: calls.append("quote"), lambda: calls.append("minute"))
        self.assertTrue(wait_for(lambda: len(calls) == 2))
        self.assertEqual(runtime.diagnostics(), {"quote_alive": False, "minute_alive": False})

    def test_thread_slots_can_be_replaced_after_recovery(self):
        runtime = QuoteRuntimeController()
        quote = threading.Thread(target=lambda: None)
        minute = threading.Thread(target=lambda: None)
        runtime.replace_quote(quote)
        runtime.replace_minute(minute)
        self.assertEqual(runtime.threads(), (quote, minute))


if __name__ == "__main__":
    main()
