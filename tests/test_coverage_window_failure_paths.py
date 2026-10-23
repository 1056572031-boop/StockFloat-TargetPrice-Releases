"""Exit resource convergence and target-calendar branches on explicit hosts.

No FloatWindow is allocated. Real event-blocked workers prove shutdown wakes
and joins its threads; errors in individual cleanup steps must not abort later
cleanup, persistence or release of the glass surface.
"""
from __future__ import annotations

import datetime
import pathlib
import plistlib
import sys
import tempfile
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
nap = window.FloatWindow._nap  # discipline-ok(R5): quote-loop sleep fallback boundary
prefetch = window.FloatWindow._prefetch_calendar  # discipline-ok(R5): startup calendar orchestration
adopt = window.FloatWindow._adopt_targets_for  # discipline-ok(R5): stock-switch target adoption
minimize = window.FloatWindow.minimize_to_dock
flash_tick = window.FloatWindow._flash_tick  # discipline-ok(R5): animation timer delivery boundary
running_bundle_id = window._running_bundle_id  # discipline-ok(R5): installed identity read boundary


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
                           _stock_transition=host(stop=mock.Mock()),
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


class WindowOrchestrationFallbacks(SFCase):
    """Exercise real handlers without allocating a production window or workers."""

    def test_optional_dock_cleanup_failures_do_not_leave_the_main_window_visible(self):
        for failing in ("panel", "bubble", "glass", "flash"):
            with self.subTest(failing=failing), mock.patch.object(window, "show_dock") as show:
                actions = {name: mock.Mock() for name in ("panel", "bubble", "glass", "flash")}
                actions[failing].side_effect = RuntimeError("native object deleted")
                pending = host(stop=mock.Mock())
                target = host(
                    _close_editor_safely=mock.Mock(), _close_target_panel=actions["panel"],
                    _hide_target_bubble=actions["bubble"], _hide_dock_timer=pending,
                    _hover_btn="bell", _press_btn="min", _press_btn_actioned=True,
                    _runtime=host(enter_dock=mock.Mock()), hide=mock.Mock(),
                    _resched_loops=mock.Mock(), _glass_window=host(orderOut_=actions["glass"]),
                    top_timer=host(stop=mock.Mock()), _sync_dot_timer=mock.Mock(),
                    _update_nap_policy=mock.Mock(), _flash_drop_timer=host(stop=actions["flash"]),
                    timer=host(interval=lambda: 50, setInterval=mock.Mock()),
                    _sync_face_guard=mock.Mock(), _dock_motion=mock.Mock(return_value=None))
                minimize(target)
                target.hide.assert_called_once_with()
                vars(target)["_runtime"].enter_dock.assert_called_once_with()
                vars(target)["_resched_loops"].assert_called_once_with()
                pending.stop.assert_called_once_with()
                self.assertIsNone(vars(target)["_hide_dock_timer"])
                self.assertIsNone(vars(target)["_hover_btn"])
                self.assertIsNone(vars(target)["_press_btn"])
                self.assertFalse(vars(target)["_press_btn_actioned"])
                for action in actions.values():
                    action.assert_called_once()
                target.top_timer.stop.assert_called_once_with()
                vars(target)["_sync_dot_timer"].assert_called_once_with()
                vars(target)["_update_nap_policy"].assert_called_once_with(False)
                target.timer.setInterval.assert_called_once_with(int(window.POLL_INTERVAL_MINIMIZED * 1000))
                show.assert_called_once_with()
                vars(target)["_sync_face_guard"].assert_called_once_with("minimize", stop_wait=False)

    def test_trading_nap_short_circuits_boundary_and_schedule_calculations(self):
        schedule = host(last=10.0, tick_seconds=0.25, nap_seconds=mock.Mock())
        with mock.patch.object(window, "seconds_to_session_change") as boundary:
            self.assertEqual(nap(None, schedule, 10.0, 0.1), 0.25)
        boundary.assert_not_called()
        schedule.nap_seconds.assert_not_called()

    def test_boundary_failure_still_delegates_to_the_sleep_scheduler(self):
        schedule = host(last=10.0, tick_seconds=0.25, nap_seconds=mock.Mock(return_value=0.25))
        with mock.patch.object(window, "seconds_to_session_change", side_effect=RuntimeError("calendar unavailable")):
            self.assertEqual(nap(None, schedule, 11.0, 3600.0), 0.25)
        schedule.nap_seconds.assert_called_once_with(11.0, 3600.0, 0.0)

    def test_schedule_failure_uses_configured_tick_or_legacy_default(self):
        for tick in (None, 0.25):
            with self.subTest(tick=tick):
                schedule = host(last=None, nap_seconds=mock.Mock(side_effect=RuntimeError("bad schedule")))
                if tick is not None:
                    schedule.tick_seconds = tick
                with mock.patch.object(window, "seconds_to_session_change", return_value=2.0):
                    self.assertEqual(nap(None, schedule, 11.0, 3600.0), tick or 0.1)
                schedule.nap_seconds.assert_called_once_with(11.0, 3600.0, 2.0)

    def test_calendar_prefetch_filters_builtin_year_and_handles_december_rollover(self):
        for year, month, months in (
                (2026, 11, []), (2026, 12, ["2027-01"]),
                (2027, 12, ["2027-12", "2028-01"])):
            with self.subTest(year=year, month=month):
                now = datetime.datetime(year, month, 15, tzinfo=BJ)
                status = {ym: "loading" for ym in months}
                with mock.patch.object(window, "_beijing_now", return_value=now), \
                     mock.patch.object(window, "prefetch_months_async") as warm, \
                     mock.patch.object(window, "calendar_sync_status", return_value=status) as sync:
                    self.assertEqual(prefetch(None), status)
                warm.assert_called_once_with(months)
                sync.assert_called_once_with(months)

    def test_calendar_prefetch_or_status_failure_returns_empty_status_and_logs(self):
        for failed in ("prefetch_months_async", "calendar_sync_status"):
            with self.subTest(failed=failed), \
                 mock.patch.object(window, "_beijing_now", return_value=datetime.datetime(2027, 1, 8, tzinfo=BJ)), \
                 mock.patch.object(window, "prefetch_months_async"), \
                 mock.patch.object(window, "calendar_sync_status"), \
                 mock.patch.object(window, failed, side_effect=RuntimeError("calendar offline")), \
                 mock.patch.object(window, "log_line") as log:
                self.assertEqual(prefetch(None), {})
                self.assertIn("calendar offline", log.call_args.args[0])

    def test_failed_target_adoption_clears_the_previous_stocks_alerts(self):
        for failed in ("load_alerts", "_persist_alerts", "_arm_clear_timer"):
            with self.subTest(failed=failed):
                target = host(_targets_enabled=lambda: True, _alerts=[{"target": 99}],
                              _persist_alerts=mock.Mock(return_value=True),
                              _after_save_config=mock.Mock(), _arm_clear_timer=mock.Mock())
                with mock.patch.object(window, "load_alerts", return_value=[{"target": 10}]) as load, \
                     mock.patch.object(window, "expired", return_value=failed == "_persist_alerts"), \
                     mock.patch.object(window, "log_line") as log:
                    collaborator = load if failed == "load_alerts" else vars(target)[failed]
                    collaborator.side_effect = RuntimeError("adoption unavailable")
                    adopt(target, "sz000001")
                self.assertEqual(vars(target)["_alerts"], [])
                self.assertEqual(log.call_args.args[1], "WARNING")
                self.assertIn("sz000001", log.call_args.args[0])
                if failed == "load_alerts":
                    vars(target)["_persist_alerts"].assert_not_called()
                    vars(target)["_arm_clear_timer"].assert_not_called()

    def test_failed_expired_target_save_keeps_only_live_targets_and_reports_failure(self):
        live, old = {"target": 10}, {"target": 20}
        target = host(_targets_enabled=lambda: True, _alerts=[{"target": 99}],
                      _persist_alerts=mock.Mock(return_value=False),
                      _after_save_config=mock.Mock(), _arm_clear_timer=mock.Mock())
        with mock.patch.object(window, "load_alerts", return_value=[live, old]), \
             mock.patch.object(window, "expired", side_effect=lambda alert, now: alert is old):
            adopt(target, "sz000001")
        self.assertEqual(vars(target)["_alerts"], [live])
        vars(target)["_persist_alerts"].assert_called_once_with([live])
        vars(target)["_after_save_config"].assert_called_once_with(False)
        vars(target)["_arm_clear_timer"].assert_called_once_with()

    def test_flash_expiry_still_stops_timer_when_repaint_fails_and_logs(self):
        target = host(_anim_driver=host(flash_tick=mock.Mock(return_value=(False, False))),
                      _flash_timer=host(stop=mock.Mock()),
                      _repaint_now=mock.Mock(side_effect=RuntimeError("paint unavailable")))
        with mock.patch.object(window.time, "monotonic", return_value=42.0), \
             mock.patch.object(window, "log_line") as log:
            flash_tick(target)
        vars(target)["_anim_driver"].flash_tick.assert_called_once_with(42.0)
        vars(target)["_flash_timer"].stop.assert_called_once_with()
        self.assertIn("paint unavailable", log.call_args.args[0])


class RunningBundleIdentityFailures(SFCase):
    def test_installed_identity_is_read_from_real_plist_not_source_constants(self):
        with tempfile.TemporaryDirectory() as directory:
            contents = pathlib.Path(directory) / "Fixture.app" / "Contents"
            executable = contents / "MacOS" / "Fixture"
            executable.parent.mkdir(parents=True)
            plist = contents / "Info.plist"
            with mock.patch.object(window.sys, "executable", str(executable)):
                for bundle_id in ("com.fixture.installed", "com.fixture.legacy"):
                    with self.subTest(bundle_id=bundle_id):
                        plist.write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id}))
                        self.assertEqual(running_bundle_id(), bundle_id)
                plist.write_bytes(plistlib.dumps({"CFBundleName": "Fixture"}))
                self.assertEqual(running_bundle_id(), "")
                plist.unlink()
                self.assertEqual(running_bundle_id(), "")

    def test_corrupt_installed_plist_returns_empty_identity_with_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            contents = pathlib.Path(directory)
            executable = contents / "MacOS" / "Fixture"
            executable.parent.mkdir()
            (contents / "Info.plist").write_bytes(b"not a plist")
            with mock.patch.object(window.sys, "executable", str(executable)), \
                 mock.patch.object(window, "log_line") as log:
                self.assertEqual(running_bundle_id(), "")
            self.assertEqual(log.call_args.args[1], "WARNING")
            self.assertIn("bundle id unreadable", log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
