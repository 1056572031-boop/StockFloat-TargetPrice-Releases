from types import SimpleNamespace
from unittest import mock

from sf_performance import PerformanceMonitor
from tests._base import SFCase


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class TestPerformanceDiagnostics(SFCase):
    def setUp(self):
        super().setUp()
        self.app = self.ensure_qapp()

    def monitor(self, **kwargs):
        monitor = PerformanceMonitor(**kwargs)
        self.addCleanup(monitor.stop)
        return monitor

    def test_sample_formula_capacity_and_peak_rss(self):
        clock = Clock()
        cpu = iter((10.0, 10.25, 10.75, 11.0))
        usage = lambda _kind: SimpleNamespace(ru_maxrss=123456)
        monitor = self.monitor(clock=clock, process_clock=lambda: next(cpu),
                                     resource_usage=usage, capacity=2)
        monitor.start()
        clock.advance(1.2)
        monitor.sample()
        self.assertAlmostEqual(monitor.export()["recent_samples"][0]["event_loop_lag_ms"], 200.0)
        self.assertAlmostEqual(monitor.export()["recent_samples"][0]["cpu_single_core_pct"], 0.25 / 1.2 * 100)
        clock.advance(1.0)
        monitor.sample()
        clock.advance(1.0)
        monitor.sample()
        payload = monitor.export()
        self.assertEqual(len(payload["recent_samples"]), 2)
        self.assertEqual(payload["recent_samples"][-1]["event_loop_lag_ms"], 0.0)
        self.assertEqual(payload["recent_samples"][-1]["cpu_single_core_pct"], 25.0)
        self.assertEqual(payload["recent_samples"][-1]["peak_rss_bytes"], 123456)
        self.assertEqual(payload["window"]["samples_discarded"], 1)
        self.assertAlmostEqual(payload["window"]["retained_from_monotonic"], 101.2)
        self.assertAlmostEqual(payload["window"]["retained_to_monotonic"], 103.2)
        self.assertAlmostEqual(payload["window"]["exported_at_monotonic"], 103.2)

    def test_paint_recording_is_bounded_and_survives_error(self):
        clock = Clock()
        monitor = self.monitor(clock=clock, capacity=1, paint_capacity=2)
        monitor.start()
        monitor.record_paint(4.0)
        clock.advance(0.5)
        monitor.record_paint(8.0)
        clock.advance(0.5)
        monitor.record_paint(12.0)
        payload = monitor.export()
        self.assertEqual(payload["paint"]["count"], 3)
        self.assertEqual(len(payload["paint"]["recent_samples"]), 2)
        self.assertEqual(payload["paint"]["max_ms"], 12.0)

    def test_sampling_failure_is_recorded_without_escaping(self):
        monitor = self.monitor(clock=lambda: 1.0,
                               resource_usage=mock.Mock(side_effect=RuntimeError("boom")))
        monitor.start()
        monitor.sample()
        self.assertTrue(any(item.startswith("sample:")
                            for item in monitor.export()["failures"]))

    def test_unavailable_shape_is_explicit(self):
        monitor = self.monitor()
        monitor.stop()
        # An object which was never started must not claim a healthy zero sample.
        self.assertFalse(self.monitor().export()["available"])

    def test_start_failure_is_unavailable_and_window_samples_are_independent(self):
        broken = self.monitor(process_clock=mock.Mock(side_effect=RuntimeError("no clock")))
        broken.start()
        self.assertFalse(broken.export()["available"])
        self.assertFalse(broken.timer.isActive())
        first, second = self.monitor(), self.monitor()
        first.start()
        second.start()
        first.record_paint(3.0)
        self.assertEqual(second.export()["paint"]["count"], 0)
        first.stop()
        self.assertFalse(first.timer.isActive())
        self.assertTrue(second.timer.isActive())

    def test_non_finite_samples_are_named_and_not_exported(self):
        monitor = self.monitor()
        monitor.start()
        monitor.record_paint(float("inf"))
        monitor.record_paint(1.0, at=float("nan"))
        self.assertEqual(monitor.export()["paint"]["count"], 0)
        self.assertEqual(len(monitor.export()["failures"]), 2)

    def test_start_is_idempotent_and_preserves_sampling_baseline(self):
        clock = Clock()
        cpu = Clock()
        monitor = self.monitor(clock=clock, process_clock=cpu,
                               resource_usage=lambda _: SimpleNamespace(ru_maxrss=123))
        monitor.start()
        clock.advance(2.0)
        cpu.advance(0.5)
        monitor.start()
        monitor.sample()
        payload = monitor.export()
        self.assertTrue(monitor.timer.isActive())
        self.assertEqual(payload["window"]["started_at_monotonic"], 100.0)
        self.assertEqual(payload["recent_samples"][0]["interval_sec"], 2.0)
        self.assertEqual(payload["recent_samples"][0]["cpu_single_core_pct"], 25.0)
        self.assertEqual(payload["failures"], [])

    def test_nonfinite_start_is_unavailable_and_can_be_retried(self):
        for source in ("clock", "process_clock"):
            for invalid in (float("nan"), float("inf")):
                with self.subTest(source=source, invalid=invalid):
                    clock, cpu = Clock(), Clock()
                    selected = clock if source == "clock" else cpu
                    selected.value = invalid
                    monitor = self.monitor(clock=clock, process_clock=cpu)
                    monitor.start()
                    payload = monitor.export()
                    self.assertFalse(payload["available"])
                    self.assertEqual(payload["failures"], ["start:ValueError"])
                    self.assertFalse(monitor.timer.isActive())
                    selected.value = 100.0
                    monitor.start()
                    self.assertTrue(monitor.export()["available"])
                    self.assertTrue(monitor.timer.isActive())

    def test_sample_without_baseline_waits_for_a_second_sample(self):
        clock, cpu = Clock(), Clock()
        usage = mock.Mock(return_value=SimpleNamespace(ru_maxrss=456))
        monitor = self.monitor(clock=clock, process_clock=cpu, resource_usage=usage)
        monitor.sample()
        usage.assert_not_called()
        self.assertFalse(monitor.export()["available"])
        clock.advance(1.0)
        cpu.advance(0.25)
        monitor.sample()
        usage.assert_called_once()
        # Starting makes the retained sample observable, without replacing it.
        monitor.start()
        samples = monitor.export()["recent_samples"]
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["interval_sec"], 1.0)
        self.assertEqual(samples[0]["cpu_single_core_pct"], 25.0)
        self.assertEqual(samples[0]["peak_rss_bytes"], 456)

    def test_nonfinite_sample_is_rejected_without_losing_previous_baseline(self):
        for source in ("clock", "process_clock"):
            for invalid in (float("nan"), float("inf")):
                with self.subTest(source=source, invalid=invalid):
                    clock, cpu = Clock(), Clock()
                    usage = mock.Mock(return_value=SimpleNamespace(ru_maxrss=789))
                    monitor = self.monitor(clock=clock, process_clock=cpu, resource_usage=usage)
                    monitor.start()
                    selected = clock if source == "clock" else cpu
                    selected.value = invalid
                    monitor.sample()
                    payload = monitor.export()
                    self.assertEqual(payload["recent_samples"], [])
                    self.assertEqual(payload["failures"], ["sample:ValueError"])
                    usage.assert_not_called()
                    clock.value, cpu.value = 102.0, 100.5
                    monitor.sample()
                    sample = monitor.export()["recent_samples"][0]
                    self.assertEqual(sample["interval_sec"], 2.0)
                    self.assertEqual(sample["cpu_single_core_pct"], 25.0)

    def test_stop_clock_failure_still_stops_timer_and_retry_sets_end(self):
        clock = mock.Mock(return_value=100.0)
        monitor = self.monitor(clock=clock, process_clock=Clock())
        monitor.start()
        clock.side_effect = RuntimeError("clock unavailable")
        monitor.stop()
        self.assertFalse(monitor.timer.isActive())
        clock.side_effect = None
        clock.return_value = 102.0
        payload = monitor.export()
        self.assertEqual(payload["failures"], ["stop:RuntimeError"])
        self.assertIsNone(payload["window"]["ended_at_monotonic"])
        monitor.stop()
        clock.return_value = 105.0
        monitor.stop()
        self.assertEqual(monitor.export()["window"]["ended_at_monotonic"], 102.0)

    def test_export_clock_failure_preserves_collected_evidence(self):
        clock = mock.Mock(return_value=100.0)
        monitor = self.monitor(clock=clock, process_clock=Clock(),
                               resource_usage=lambda _: SimpleNamespace(ru_maxrss=123))
        monitor.start()
        clock.return_value = 101.0
        monitor.sample()
        monitor.record_paint(4.0)
        before = monitor.export()
        clock.side_effect = RuntimeError("clock unavailable")
        after = monitor.export()
        self.assertTrue(after["available"])
        self.assertIsNone(after["window"]["exported_at_monotonic"])
        self.assertEqual(after["recent_samples"], before["recent_samples"])
        self.assertEqual(after["paint"], before["paint"])
        clock.side_effect = None

    def test_paint_recorder_failure_is_throttled_and_does_not_interrupt_paint(self):
        from sf_ui_render import RenderMixin

        recorder = mock.Mock(side_effect=RuntimeError("recorder unavailable"))
        paint = mock.Mock()
        # Minimal host avoids real window construction and its workers/persistence.
        host = SimpleNamespace(**{"_performance_monitor": SimpleNamespace(record_paint=recorder),
                                  "_paint_event_inner": paint, "_sf_first_frame_logged": True})
        with mock.patch("sf_ui_render.time.monotonic", return_value=100.0), \
                mock.patch("sf_logging._THROTTLE_STATE", {}), \
                mock.patch("sf_logging._record_swallow") as summary, \
                mock.patch("sf_logging.log_line") as logged:
            RenderMixin.paintEvent(host, None)
            self.assertEqual(logged.call_count, 1, "首个记录故障必须留下日志")
            for _ in range(9):
                RenderMixin.paintEvent(host, None)
            self.assertEqual(paint.call_count, 10, "诊断故障不得阻断后续绘制")
            self.assertEqual(recorder.call_count, 10)
            self.assertEqual(summary.call_count, 10)
            self.assertEqual(logged.call_count, 1, "重复故障必须限流，不能每帧刷日志")
            message = logged.call_args.args[0]
            self.assertIn("SWALLOWED sf_ui_render.performance_record", message)
            self.assertIn("recorder unavailable", message)

    def test_actual_paint_wrapper_records_even_when_paint_fails(self):
        from sf_ui_render import RenderMixin
        monitor = self.monitor()
        monitor.start()
        host = SimpleNamespace(
            _performance_monitor=monitor,  # discipline-ok(R5): 最小宿主装配，验证真实paintEvent的finally
            _paint_event_inner=mock.Mock(side_effect=RuntimeError("bad paint")),
            _paint_recovery_pending=True)
        with mock.patch("sf_ui_render.log_line_throttled"):
            RenderMixin.paintEvent(host, None)
        self.assertEqual(monitor.export()["paint"]["count"], 1)
        self.assertGreaterEqual(monitor.export()["paint"]["max_ms"], 0)

    def test_run_aggregates_cover_more_than_the_retained_window(self):
        clock, cpu = Clock(), Clock()
        rss = iter((100, 300, 200, 250))
        monitor = self.monitor(clock=clock, process_clock=cpu, capacity=1,
                               resource_usage=lambda _kind: SimpleNamespace(ru_maxrss=next(rss)))
        monitor.start()
        # (墙钟间隔, CPU 增量)：第二拍卡了 300ms，第三拍慢了 80ms，只有第一拍是 50% 的 CPU 峰。
        for wall, used in ((1.0, 0.5), (1.3, 0.13), (1.08, 0.108), (1.0, 0.1)):
            clock.advance(wall)
            cpu.advance(used)
            monitor.sample()
        payload = monitor.export()
        run = payload["run"]
        self.assertEqual(len(payload["recent_samples"]), 1, "窗口只留 1 拍，汇总必须不靠它")
        self.assertEqual(run["samples"], 4)
        self.assertAlmostEqual(run["wall_sec"], 4.38, places=6)
        self.assertAlmostEqual(run["cpu_mean_pct"], 0.838 / 4.38 * 100.0, places=6)
        self.assertAlmostEqual(run["cpu_max_pct"], 50.0, places=6)
        self.assertAlmostEqual(run["cpu_max_at_monotonic"], 101.0, places=6)
        self.assertAlmostEqual(run["event_loop_lag_max_ms"], 300.0, places=3)
        self.assertAlmostEqual(run["event_loop_lag_max_at_monotonic"], 102.3, places=6)
        self.assertEqual(run["event_loop_lag_over_warn"], 2, "300ms 与 80ms 都过 50ms 线")
        self.assertEqual(run["event_loop_lag_over_stall"], 1, "只有 300ms 过 200ms 线")
        self.assertEqual(run["peak_rss_bytes"], 300, "峰值要取整段最大，不是最后一拍")

    def test_paint_aggregates_count_slow_frames_and_keep_the_first_worst_moment(self):
        clock = Clock()
        monitor = self.monitor(clock=clock, paint_capacity=2)
        monitor.start()
        for duration in (5.0, 40.0, 33.0, 500.0, 12.0):
            monitor.record_paint(duration)
            clock.advance(1.0)
        monitor.record_paint(500.0)                 # 与最大值持平：不许把时刻挪走
        monitor.record_paint(float("inf"))          # 被拒绝的样本不进汇总
        paint = monitor.export()["paint"]
        self.assertEqual(paint["count"], 6)
        self.assertEqual(len(paint["recent_samples"]), 2, "窗口只留 2 条，汇总必须不靠它")
        self.assertEqual(paint["slow_count"], 4, "40、33（含边界）、500、500")
        self.assertEqual(paint["max_ms"], 500.0)
        self.assertEqual(paint["max_at_monotonic"], 103.0)
        self.assertEqual(paint["slow_ms"], 33.0)
