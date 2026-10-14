"""Exit resource convergence and target-calendar branches on explicit hosts.

No FloatWindow is allocated. Real event-blocked workers prove shutdown wakes
and joins its threads; errors in individual cleanup steps must not abort later
cleanup, persistence or release of the glass surface.
"""
from __future__ import annotations

import datetime
import pathlib
import sys
import threading
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from _base import SFCase
except ImportError:  # pragma: no cover
    from tests._base import SFCase

import stock_float_window as window

shutdown = window.FloatWindow._shutdown_threads  # discipline-ok(R5): unified exit lifecycle entry
persist_exit = window.FloatWindow._persist_on_exit  # discipline-ok(R5): exit persistence boundary
arm_clear = window.FloatWindow._arm_clear_timer  # discipline-ok(R5): calendar-driven timer entry
sync_panel = window.FloatWindow._sync_target_panel  # discipline-ok(R5): visible target state synchronization
WINDOW_X, WINDOW_Y, WINDOW_HEIGHT = 100, 200, 224
MIN_TIMER_MS = 1
BJ = datetime.timezone(datetime.timedelta(hours=8))


def host(**state):
    return types.SimpleNamespace(**state)


class DestroyedEditor:
    @property
    def _wake(self):
        raise RuntimeError("editor C++ object deleted")


class ShutdownFailures(SFCase):
    def setUp(self):
        super().setUp()
        self.pool = mock.Mock()
        self.track_patch(mock.patch.object(window, "_NET_POOL", self.pool))
        self.disconnect = mock.Mock()
        self.app = host(applicationStateChanged=host(disconnect=self.disconnect))
        self.track_patch(mock.patch.object(window.QApplication, "instance", return_value=self.app))

    def target(self, **changes):
        state = dict(running=True, _threads_stopped=False,
                     _check_updates_after_resume=lambda: None,
                     _update_retry_timer=host(stop=mock.Mock()),
                     _update_periodic_timer=host(stop=mock.Mock()),
                     _update_cancel=threading.Event(), _wake=threading.Event(),
                     _resched=threading.Event(), _editor=None,
                     _quote_thread=None, _minute_thread=None)
        state.update(changes)
        return host(**state)

    def test_real_event_blocked_workers_wake_and_join_before_shutdown_returns(self):
        target = self.target()
        observed = []
        started = [threading.Event(), threading.Event()]
        workers = []
        for index, name in enumerate(("_wake", "_resched")):
            event = vars(target)[name]

            def work(event=event, ready=started[index]):
                ready.set()
                event.wait()
                observed.append(target.running)

            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            workers.append(worker)
        try:
            self.assertTrue(all(event.wait(2) for event in started))
            vars(target).update(_quote_thread=workers[0], _minute_thread=workers[1])
            shutdown(target)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(observed, [False, False])
            self.assertTrue(vars(target)["_update_cancel"].is_set())
            self.pool.close_all.assert_called_once_with()
        finally:
            for name in ("_wake", "_resched"):
                vars(target)[name].set()
            for worker in workers:
                worker.join(timeout=2)

    def test_disconnect_error_does_not_skip_cancel_wakeup_or_pool_cleanup(self):
        self.disconnect.side_effect = RuntimeError("signal deleted")
        target = self.target()
        shutdown(target)
        self.assertFalse(target.running)
        self.assertTrue(vars(target)["_wake"].is_set())
        self.assertTrue(vars(target)["_update_cancel"].is_set())
        self.pool.close_all.assert_called_once_with()

    def test_failed_update_timer_and_cancel_do_not_block_other_cleanup(self):
        target = self.target(_update_retry_timer=host(stop=mock.Mock(side_effect=RuntimeError("timer gone"))),
                             _update_cancel=host(set=mock.Mock(side_effect=RuntimeError("cancel gone"))))
        shutdown(target)
        vars(target)["_update_periodic_timer"].stop.assert_called_once_with()
        self.assertTrue(vars(target)["_resched"].is_set())
        self.pool.close_all.assert_called_once_with()

    def test_deleted_editor_is_cleared_without_losing_other_threads(self):
        target = self.target(_editor=DestroyedEditor())
        shutdown(target)
        self.assertIsNone(vars(target)["_editor"])
        self.assertTrue(vars(target)["_wake"].is_set())
        self.pool.close_all.assert_called_once_with()

    def test_one_wakeup_failure_does_not_skip_other_wakeup(self):
        target = self.target(_wake=host(set=mock.Mock(side_effect=RuntimeError("event lost"))))
        shutdown(target)
        self.assertTrue(vars(target)["_resched"].is_set())
        self.pool.close_all.assert_called_once_with()

    def test_join_failure_does_not_skip_other_join_or_pool(self):
        quote = host(is_alive=lambda: True, join=mock.Mock(side_effect=RuntimeError("join failed")))
        minute = host(is_alive=lambda: True, join=mock.Mock())
        target = self.target(_quote_thread=quote, _minute_thread=minute)
        shutdown(target)
        minute.join.assert_called_once_with(timeout=1.5)
        self.pool.close_all.assert_called_once_with()

    def test_failed_search_status_read_still_releases_pool(self):
        search = host(isRunning=mock.Mock(side_effect=RuntimeError("QThread deleted")))
        target = self.target(_editor=host(_wake=threading.Event(), _search_thread=search))
        shutdown(target)
        self.pool.close_all.assert_called_once_with()
        self.assertFalse(target.running)

    def test_repeated_shutdown_performs_no_duplicate_cleanup(self):
        target = self.target()
        shutdown(target)
        shutdown(target)
        self.disconnect.assert_called_once()
        self.pool.close_all.assert_called_once_with()
        vars(target)["_update_retry_timer"].stop.assert_called_once_with()


class ExitPersistenceFailures(SFCase):
    def setUp(self):
        super().setUp()
        self.save, self.swallowed, self.log = mock.Mock(return_value=True), mock.Mock(), mock.Mock()
        self.track_patch(mock.patch.object(window, "save_config", self.save))
        self.track_patch(mock.patch.object(window, "log_swallow", self.swallowed))
        self.track_patch(mock.patch.object(window, "log_line", self.log))
        self.events = []
        self.camera = mock.Mock(side_effect=lambda *a: self.events.append("camera"))
        self.runtime = host(shutdown=mock.Mock(side_effect=lambda: self.events.append("runtime")))
        self.surface = host(orderOut_=mock.Mock(side_effect=lambda sender: self.events.append("glass")),
                            close=mock.Mock(side_effect=AssertionError("native surfaces must not close")))
        self.timer = host(stop=mock.Mock(side_effect=lambda: self.events.append("timer")))
        self.child_timer = host(stop=mock.Mock())
        self.target = host(running=True, code="sh600519", x=lambda: WINDOW_X, y=lambda: WINDOW_Y,
                           height=lambda: WINDOW_HEIGHT, _exit_persisted=False, _bg_cfg="glass",
                           _bg_effect=True, _glass_window=self.surface, _runtime=self.runtime,
                           _close_update_flow_box=mock.Mock(), _stop_face_guard=self.camera,
                           timer=self.timer, findChildren=lambda cls: [self.child_timer])

    def assert_final(self):
        self.assertFalse(self.target.running)
        self.assertFalse(vars(self.target)["_bg_effect"])
        self.assertIsNone(vars(self.target)["_glass_window"])
        self.save.assert_called_once_with("sh600519", WINDOW_X, WINDOW_Y, WINDOW_HEIGHT, "glass")
        self.runtime.shutdown.assert_called_once_with()
        self.surface.close.assert_not_called()

    def test_update_dialog_cleanup_failure_does_not_abort_camera_persistence_or_glass(self):
        vars(self.target)["_close_update_flow_box"].side_effect = RuntimeError("native dialog deleted")
        persist_exit(self.target)
        self.camera.assert_called_once_with("exit")
        self.assert_final()
        self.assertIn("_persist_on_exit", self.swallowed.call_args.args[0])

    def test_camera_cleanup_failure_does_not_abort_other_exit_cleanup(self):
        self.camera.side_effect = RuntimeError("camera cleanup failed")
        persist_exit(self.target)
        self.assert_final()
        self.assertIn("_persist_on_exit", self.swallowed.call_args.args[0])

    def test_timer_failure_does_not_skip_dynamically_discovered_child_timers(self):
        self.timer.stop.side_effect = RuntimeError("timer already deleted")
        persist_exit(self.target)
        self.child_timer.stop.assert_called_once_with()
        self.assert_final()

    def test_child_timer_failure_does_not_skip_remaining_children(self):
        broken = host(stop=mock.Mock(side_effect=RuntimeError("child deleted")))
        self.target.findChildren = lambda cls: [broken, self.child_timer]
        persist_exit(self.target)
        self.child_timer.stop.assert_called_once_with()
        self.assert_final()

    def test_child_enumeration_failure_does_not_abort_camera_or_persistence(self):
        self.target.findChildren = mock.Mock(side_effect=RuntimeError("QWidget deleted"))
        persist_exit(self.target)
        self.assert_final()

    def test_save_failure_is_reported_and_runtime_and_glass_still_release(self):
        self.save.return_value = False
        persist_exit(self.target)
        self.assert_final()
        self.assertTrue(any("config save failed" in call.args[0] for call in self.log.call_args_list))

    def test_save_exception_is_reported_and_does_not_abort_glass_cleanup(self):
        self.save.side_effect = OSError("disk failed")
        persist_exit(self.target)
        self.assert_final()
        self.assertTrue(any("exit persist error" in call.args[0] for call in self.log.call_args_list))

    def test_glass_orderout_exception_still_resets_effect_and_reference(self):
        self.surface.orderOut_.side_effect = RuntimeError("surface already gone")
        persist_exit(self.target)
        self.assert_final()

    def test_idempotent_exit_does_not_save_or_release_twice(self):
        persist_exit(self.target)
        persist_exit(self.target)
        self.assert_final()
        self.camera.assert_called_once_with("exit")


class TargetCalendarBranchCases(SFCase):
    def setUp(self):
        super().setUp()
        self.instant = datetime.datetime(2030, 1, 8, 15, 0, 29, 999800, tzinfo=BJ)
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=self.instant))
        self.confirmed = mock.Mock(return_value=True)
        self.expired = mock.Mock(return_value=False)
        self.track_patch(mock.patch.object(window, "is_trading_day_confirmed", self.confirmed))
        self.track_patch(mock.patch.object(window, "expired", self.expired))

    def target(self, alerts, enabled=True):
        return host(_targets_enabled=lambda: enabled, _safe_alerts=lambda: alerts,
                    _clear_timer=host(stop=mock.Mock(), start=mock.Mock()), _clear_expired=mock.Mock())

    def test_submillisecond_future_deadline_is_clamped_to_positive_timer(self):
        target = self.target([{"session_date": "2030-01-08"}])
        arm_clear(target)
        timer = vars(target)["_clear_timer"]
        timer.stop.assert_called_once_with()
        timer.start.assert_called_once_with(MIN_TIMER_MS)

    def test_unconfirmed_calendar_keeps_alert_without_arming_or_clearing(self):
        self.confirmed.return_value = None
        target = self.target([{"session_date": "2030-01-08"}])
        arm_clear(target)
        vars(target)["_clear_timer"].start.assert_not_called()
        vars(target)["_clear_expired"].assert_not_called()

    def test_invalid_or_missing_dates_do_not_create_an_incorrect_timer(self):
        target = self.target([{}, {"session_date": "broken"}])
        arm_clear(target)
        vars(target)["_clear_timer"].start.assert_not_called()

    def test_past_expired_alert_requests_cleanup_instead_of_zero_interval(self):
        self.expired.return_value = True
        target = self.target([{"session_date": "2030-01-07"}])
        arm_clear(target)
        vars(target)["_clear_expired"].assert_called_once_with()
        vars(target)["_clear_timer"].start.assert_not_called()

    def test_disabled_or_empty_targets_never_arm_clear_timer(self):
        for target in (self.target([], enabled=True), self.target([{}], enabled=False)):
            arm_clear(target)
            vars(target)["_clear_timer"].start.assert_not_called()

    def test_visible_panel_receives_real_live_alert_state_and_hidden_panel_does_not(self):
        alerts = [{"target": 10, "state": "hit"}]
        shown = {"alerts": None}
        panel = host(isVisible=lambda: True, sync_alerts=lambda value: shown.update(alerts=value))
        target = host(_targets_enabled=lambda: True, _target_panel=panel, _safe_alerts=lambda: alerts)
        sync_panel(target)
        self.assertEqual(shown["alerts"], alerts)
        shown["alerts"] = None
        panel.isVisible = lambda: False
        sync_panel(target)
        self.assertIsNone(shown["alerts"])


if __name__ == "__main__":
    unittest.main()
