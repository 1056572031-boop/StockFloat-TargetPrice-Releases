"""Deterministic observer tests; never launch or modify the installed application."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import plistlib
import tempfile
from pathlib import Path
from unittest import mock

from tests._base import SFCase
from tools import resource_soak as soak

START = dt.datetime(2030, 1, 15, 3, tzinfo=soak.TZ)
EXECUTABLE = "/Applications/Example.app/Contents/MacOS/Example"


def row(pid=10, parent=1, cpu=1, started="session-a", executable=EXECUTABLE):
    return {"pid": pid, "ppid": parent, "cpu_seconds": cpu, "rss_kib": 10240,
            "started": started, "executable": executable, "ps_cpu_percent": 0.5}


def sample(epoch, processes=None, power="ac"):
    processes = [row()] if processes is None else processes
    return {"epoch": epoch, "app_roots": [10] if processes else [],
            "app_processes": processes, "rss_kib": 10240 if processes else None,
            "power_source": power, "thermal_state": "nominal", "errors": []}


class TestResourceSoak(SFCase):
    def test_cpu_time_formats(self):
        for text, expected in (("00:01.50", 1.5), ("01:02:03.00", 3723),
                               ("2-01:02:03.00", 176523)):
            with self.subTest(text=text):
                self.assertEqual(soak.cpu_seconds(text), expected)

    def test_process_parser_keeps_paths_with_spaces(self):
        text = "10 1 2.0 10240 00:02.50 Tue Jan 15 03:00:00 2030 /Applications/Example App/bin\n"
        result = soak.parse_processes(text)
        self.assertEqual(result[0]["executable"], "/Applications/Example App/bin")
        self.assertEqual(result[0]["cpu_seconds"], 2.5)

    def test_malformed_process_output_is_not_app_missing(self):
        with self.assertRaises(ValueError):
            soak.parse_processes("10 1 0")

    def test_only_exact_app_and_descendants_are_measured(self):
        rows = [row(), row(11, 10, executable="child"), row(12, 11, executable="grandchild"),
                row(13, executable=EXECUTABLE + "-test")]
        roots, processes = soak.process_tree(rows, EXECUTABLE)
        self.assertEqual(roots, [10])
        self.assertEqual([p["pid"] for p in processes], [10, 11, 12])

    def test_ac_only_without_battery_percentage(self):
        self.assertEqual(soak.power_source("Now drawing from 'AC Power'\n80%"), "ac")
        self.assertEqual(soak.power_source("Now drawing from 'Battery Power'\n79%"), "battery_excluded")
        self.assertEqual(soak.power_source(""), "unknown")

    def test_interval_cpu_is_single_core_percentage(self):
        result = soak.interval_metrics(sample(100, [row(cpu=1)]), sample(160, [row(cpu=2.2)]), 60)
        self.assertAlmostEqual(result["app_cpu_percent"], 2.0)

    def test_sleep_gap_restart_new_child_and_battery_have_no_cpu_claim(self):
        for current in (sample(300), sample(160, [row(started="session-b")]),
                        sample(160, [row(), row(11, 10)]), sample(160, power="battery_excluded"),
                        sample(99), sample(160, [row(cpu=0)])):
            with self.subTest(current=current):
                self.assertIsNone(soak.interval_metrics(sample(100), current, 60)["app_cpu_percent"])

    def test_missing_app_and_non_ac_samples_never_become_valid(self):
        samples = [sample(100, []), sample(160, power="battery_excluded")]
        result = soak.summarize(samples)
        self.assertEqual(result["valid_ac_app_samples"], 0)
        self.assertIsNone(result["cpu_mean_percent"])
        self.assertIsNone(result["rss_max_mib"])

    def test_summary_uses_only_valid_app_values(self):
        first, last = sample(100), sample(160)
        first["app_cpu_percent"], last["app_cpu_percent"] = 1.0, 3.0
        last["rss_kib"] = 20480
        result = soak.summarize([first, last])
        self.assertEqual(result["cpu_mean_percent"], 2.0)
        self.assertEqual(result["rss_first_to_last_mib"], 10.0)

    def test_timezone_is_mandatory(self):
        with self.assertRaises(ValueError):
            soak.aware_time("2030-01-15T03:00:00")
        self.assertEqual(soak.aware_time("2030-01-14T19:00:00+00:00"), START)

    def test_agent_has_no_run_at_load_keep_alive_or_wake_assertion(self):
        agent = soak.launch_agent("python", "observer.py", Path("app"), Path("logs"),
                                  START, START + dt.timedelta(hours=3), 60, "example")
        self.assertNotIn("RunAtLoad", agent)
        self.assertNotIn("KeepAlive", agent)
        self.assertEqual(agent["StartCalendarInterval"], {"Month": 1, "Day": 15, "Hour": 3, "Minute": 0})
        self.assertIn(START.isoformat(), agent["ProgramArguments"])

    def test_prepare_only_writes_schedule_in_temporary_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = soak.main(["prepare", "--start", START.isoformat(),
                                "--end", (START + dt.timedelta(hours=3)).isoformat(),
                                "--output", str(root / "out"), "--agent-dir", str(root / "agents")])
            self.assertEqual(rc, 0)
            files = list((root / "agents").glob("*.plist"))
            self.assertEqual(len(files), 1)
            self.assertNotIn("RunAtLoad", plistlib.loads(files[0].read_bytes()))
            self.assertFalse((root / "out/samples.jsonl").exists())

    def test_future_window_does_not_collect(self):
        reader, launcher = mock.Mock(), mock.Mock()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            soak.observe(Path("app"), Path(tmp), START, START + dt.timedelta(hours=3),
                         60, clock=lambda: START.timestamp() - 1, reader=reader, launcher=launcher)
        reader.assert_not_called()
        launcher.assert_not_called()

    def test_expired_window_does_not_collect_next_year(self):
        reader, launcher = mock.Mock(), mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            soak.observe(Path("app"), Path(tmp), START, START + dt.timedelta(hours=3),
                         60, clock=lambda: START.timestamp() + 86400, reader=reader, launcher=launcher)
            status = json.loads((Path(tmp) / "status.json").read_text())
            self.assertEqual(status["status"], "missed_window")
        reader.assert_not_called()
        launcher.assert_not_called()

    def test_running_application_is_not_launched_again(self):
        runner = mock.Mock()
        result = soak.ensure_app_running(Path("app"), runner=runner, detector=lambda: [10])
        self.assertEqual(result["action"], "already_running")
        runner.assert_not_called()

    def test_launch_is_verified_without_forcing_a_new_instance(self):
        runner = mock.Mock()
        result = soak.ensure_app_running(Path("app"), runner=runner,
                                         detector=mock.Mock(side_effect=[[], [10]]), clock=lambda: 0)
        runner.assert_called_once_with(["/usr/bin/open", "-a", "app"])
        self.assertEqual(result, {"action": "launched", "verified_pids": [10]})

    def test_successful_open_without_a_process_times_out(self):
        runner = mock.Mock()
        with self.assertRaises(TimeoutError):
            soak.ensure_app_running(Path("app"), timeout=0, runner=runner,
                                    detector=list, clock=lambda: 0)
        runner.assert_called_once()

    def test_agent_can_opt_in_to_application_launch(self):
        agent = soak.launch_agent("python", "observer.py", Path("app"), Path("logs"),
                                  START, START + dt.timedelta(hours=3), 60, "example",
                                  launch_if_missing=True)
        self.assertIn("--launch-if-missing", agent["ProgramArguments"])

    def test_launch_failure_is_reported_before_any_samples(self):
        reader = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaises(TimeoutError):
                soak.observe(Path("app"), out, START, START + dt.timedelta(seconds=60),
                             60, clock=lambda: START.timestamp(), reader=reader,
                             launcher=mock.Mock(side_effect=TimeoutError))
            status = json.loads((out / "status.json").read_text())
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["error"], "TimeoutError")
            self.assertFalse((out / "samples.jsonl").exists())
        reader.assert_not_called()

    def test_verified_launch_precedes_sampling_and_is_preserved(self):
        clock = [START.timestamp()]
        events = []

        def launcher(app, *, timeout):
            events.append("launch")
            return {"action": "launched", "verified_pids": [10]}

        def reader(app):
            events.append("sample")
            return sample(clock[0])

        def sleep(seconds):
            clock[0] += seconds

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            soak.observe(Path("app"), out, START, START + dt.timedelta(seconds=60),
                         60, clock=lambda: clock[0], sleep=sleep, reader=reader, launcher=launcher)
            status = json.loads((out / "status.json").read_text())
            self.assertEqual(events, ["launch", "sample"])
            self.assertEqual(status["app_launch"]["action"], "launched")

    def test_observer_stops_at_deadline_and_flushes_summary(self):
        clock = [START.timestamp()]

        def sleep(seconds):
            clock[0] += seconds

        def reader(app):
            return sample(clock[0], [row(cpu=(clock[0] - START.timestamp()) / 100)])

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            soak.observe(Path("app"), out, START, START + dt.timedelta(seconds=120),
                         60, clock=lambda: clock[0], sleep=sleep, reader=reader)
            status = json.loads((out / "status.json").read_text())
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["samples"], 2)
            self.assertAlmostEqual(status["cpu_mean_percent"], 1.0)
            self.assertFalse((out / ".running").exists())
            self.assertEqual(len((out / "samples.jsonl").read_text().splitlines()), 2)

    def test_collection_failure_is_persisted_and_lock_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaises(RuntimeError):
                soak.observe(Path("app"), out, START, START + dt.timedelta(seconds=60),
                             60, clock=lambda: START.timestamp(), reader=mock.Mock(side_effect=RuntimeError))
            status = json.loads((out / "status.json").read_text())
            self.assertEqual(status["status"], "failed")
            self.assertFalse((out / ".running").exists())

    def test_missing_application_does_not_become_a_pass(self):
        clock = [START.timestamp()]

        def sleep(seconds):
            clock[0] += seconds

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            soak.observe(Path("app"), out, START, START + dt.timedelta(seconds=60),
                         60, clock=lambda: clock[0], sleep=sleep,
                         reader=lambda app: sample(clock[0], []))
            status = json.loads((out / "status.json").read_text())
            self.assertEqual(status["status"], "no_valid_app_data")
            self.assertEqual(status["valid_ac_app_samples"], 0)

    def test_second_run_preserves_original_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            original = '{"original":true}\n'
            (out / "samples.jsonl").write_text(original)
            (out / "status.json").write_text(original)
            with self.assertRaises(FileExistsError):
                soak.observe(Path("app"), out, START, START + dt.timedelta(seconds=60),
                             60, clock=lambda: START.timestamp())
            self.assertEqual((out / "samples.jsonl").read_text(), original)
            self.assertEqual((out / "status.json").read_text(), original)

    def test_invalid_cli_arguments_fail_without_writing(self):
        for argv in (["run"], ["probe", "--interval", "nan"], ["probe", "--unexpected"],
                     ["prepare", "--start", "2030-01-15T03:00:00"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    soak.main(argv)
                self.assertEqual(caught.exception.code, 2)
