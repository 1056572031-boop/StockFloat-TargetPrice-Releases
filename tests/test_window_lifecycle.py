from unittest import main

from sf_window_lifecycle import WindowLifecycleController

try:
    from _base import SFCase
except ImportError:
    from tests._base import SFCase


class TestWindowLifecycle(SFCase):
    def test_quit_and_thread_stop_are_idempotent(self):
        lifecycle = WindowLifecycleController()
        self.assertTrue(lifecycle.begin_quit())
        self.assertFalse(lifecycle.begin_quit())
        self.assertTrue(lifecycle.mark_threads_stopped())
        self.assertFalse(lifecycle.mark_threads_stopped())

    def test_running_can_be_stopped_without_marking_threads_done(self):
        lifecycle = WindowLifecycleController()
        lifecycle.stop_running()
        self.assertFalse(lifecycle.running)
        self.assertFalse(lifecycle.threads_stopped)


if __name__ == "__main__":
    main()
