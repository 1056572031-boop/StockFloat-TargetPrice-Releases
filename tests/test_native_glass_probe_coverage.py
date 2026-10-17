"""Coverage of native_glass_probe orchestration without native UI side effects."""
from __future__ import annotations

import io
import json
import os
import sys
import types
from contextlib import ExitStack
from pathlib import Path
from typing import ClassVar
from unittest import mock

from tests._base import SFCase


class _Image:
    def toImage(self):
        return self

    def scaled(self, *_args):
        return self

    def save(self, _path):
        return True

    def isNull(self):
        return False


class _Timer:
    def stop(self):
        pass

    def start(self):
        pass


class _Window:
    def __init__(self):
        self.quote = {"price": "1"}
        self._disp_price = 1
        self.top_timer = _Timer()
        self._bg_effect = True
        self._inverted = False
        self._glass_window = types.SimpleNamespace(
            windowNumber=lambda: 22, level=lambda: 20, isVisible=lambda: True,
            orderFront_=lambda _obj: setattr(self, "_inverted", True),
        )
        self._hidden = False
        self._height = 224
        self._x = 400

    def findChildren(self, _kind):
        return []

    def paintEvent(self, _event):
        return None

    def show(self):
        self._hidden = False
        paint = getattr(self, "paintEvent", None)
        if paint is not None:
            paint(None)

    def hide(self):
        self._hidden = True

    def move(self, *_args):
        self._x += 50


    def isVisible(self):
        return not self._hidden

    def grab(self):
        return _Image()

    def width(self):
        return 184

    def height(self):
        return self._height

    def y(self):
        return 180

    def x(self):
        return self._x

    def pos(self):
        return (self._x, 180)


    def mapToGlobal(self, point):
        return point

    def mapFromGlobal(self, point):
        return point

    def glass_diagnostics(self):
        return {"surface": {"backend": "liquid"}}


    def minimize_to_dock(self):
        self._hidden = True
        self._glass_window.isVisible = lambda: False

    def _restore_window(self):
        self._hidden = False
        self._glass_window.isVisible = lambda: True

    def _sync_dot_timer(self):
        pass

    def _persist_on_exit(self):
        pass

    def _shutdown_threads(self):
        pass

    def close(self):
        pass

    def click(self, *_args):
        self._height = 100 if self._height == 224 else 224


class _App:
    def setQuitOnLastWindowClosed(self, _value):
        pass

    def processEvents(self):
        pass


class _Host:
    def __init__(self):
        self.stdin = self
        self.stdout = self
        self.stderr = self
        self._read = iter(("ready", "entered", "left"))

    def write(self, _value):
        pass

    def flush(self):
        pass

    def readline(self):
        return next(self._read)

    def communicate(self, timeout=None):
        return ("", "")

    def terminate(self):
        pass


class _Capture:
    instances: ClassVar[list] = []

    def __init__(self, out):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=False)
        self.phases = []
        self.active = None
        self.template_path = None
        self.__class__.instances.append(self)

    def wait_ready(self, pump):
        pump()

    def begin(self, name):
        if self.active is not None:
            raise RuntimeError("overlap")
        self.active = name

    def end(self):
        if self.active is None:
            raise RuntimeError("not active")
        self.phases.append(self.active)
        self.active = None

    def finish(self, template):
        self.template_path = Path(template)
        return {"passed": True, "phases": self.phases}

    def stop(self):
        self.active = None


class NativeProbeOrchestration(SFCase):
    def make_modules(self):
        qtcore = types.ModuleType("PyQt6.QtCore")
        qtcore.QCoreApplication = types.SimpleNamespace(sendEvent=lambda *_args: None)
        qtcore.QEvent = types.SimpleNamespace(Type=types.SimpleNamespace(MouseMove=1))
        qtcore.QPoint = lambda x, y: (x, y)
        qtcore.QPointF = lambda *args: args
        qtcore.Qt = types.SimpleNamespace(
            MouseButton=types.SimpleNamespace(LeftButton=1, NoButton=0),
            KeyboardModifier=types.SimpleNamespace(NoModifier=0),
        )
        qtcore.QTimer = _Timer
        qtgui = types.ModuleType("PyQt6.QtGui")
        qtgui.QColor = lambda *_args: object()
        qtgui.QImage = lambda *_args: _Image()
        qtgui.QMouseEvent = lambda *args: args
        qtgui.QPainter = object
        qt_test = types.ModuleType("PyQt6.QtTest")
        qt_test.QTest = types.SimpleNamespace(
            mousePress=lambda *_args, **_kwargs: None,
            mouseRelease=lambda *_args, **_kwargs: None,
            mouseClick=lambda win, *_args, **_kwargs: win.click(),
        )
        qtwidgets = types.ModuleType("PyQt6.QtWidgets")
        qtwidgets.QApplication = lambda _args: _App()
        qtwidgets.QWidget = object
        pyqt = types.ModuleType("PyQt6")
        pyqt.__path__ = []
        product = types.SimpleNamespace(
            FloatWindow=_Window,
            _beijing_now=lambda: types.SimpleNamespace(strftime=lambda _fmt: "2026-01-01"),
            load_bundled_fonts=lambda: None,
            load_api_urls=lambda: None,
            show_dock=lambda: None,
        )
        glass = types.SimpleNamespace(
            active_space_id=lambda: 2,
            content_ns_window=lambda _win: types.SimpleNamespace(
                windowNumber=lambda: 11, collectionBehavior=lambda: 0,
            ),
            glass_main_real_state=lambda *_args: {"glass_on_top": False, "desync_px": 0},
        )
        quartz = types.SimpleNamespace(
            kCGWindowListOptionIncludingWindow=1,
            kCGWindowListOptionOnScreenOnly=2,
            kCGNullWindowID=0,
            CGWindowListCopyWindowInfo=lambda *_args: [{"kCGWindowNumber": 11,
                "kCGWindowBounds": {"X": 1, "Y": 2, "Width": 184, "Height": 224}}],
        )
        modules = {
            "PyQt6": pyqt, "PyQt6.QtCore": qtcore, "PyQt6.QtGui": qtgui,
            "PyQt6.QtTest": qt_test, "PyQt6.QtWidgets": qtwidgets,
            "stock_float_window": product, "sf_glass": glass,
            "sf_ui_render": types.SimpleNamespace(CHART_TOP=40, collapsed_height=lambda: 100),
            "Quartz": quartz, "continuous_frames": types.SimpleNamespace(ContinuousCapture=_Capture),
        }
        return modules

    def test_continuous_fullscreen_exercise_orchestrates_capture_and_negative_control(self):
        import tools.native_glass_probe as probe

        modules = self.make_modules()
        argv = ["native_glass_probe.py", "--continuous-video", "--continuous-control", "hide",
                "--fullscreen", "--exercise", "--seconds", "0", "--out", 
                str(self._temp_out())]
        host = _Host()
        run_result = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        private = self._temp_out() / "private"
        private.mkdir()
        clock = iter(i * 0.2 for i in range(10000))
        with mock.patch.dict(os.environ), mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.dict(sys.modules, modules), mock.patch.object(probe.sys, "argv", argv), \
                mock.patch.object(probe.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(probe.time, "sleep"), \
                mock.patch.object(probe.subprocess, "Popen", return_value=host), \
                mock.patch.object(probe.subprocess, "run", return_value=run_result), \
                mock.patch.object(probe.select, "select", return_value=([host.stdout], [], [])), \
                mock.patch.object(probe, "tempfile", types.SimpleNamespace(mkdtemp=lambda **_k: str(private))):
            result = probe.main()

        self.assertEqual(result, 1, "替身没有真实Space切换和WindowServer轨迹，不能报告实机通过")
        capture = _Capture.instances[-1]
        self.assertIn("desktop_return", capture.phases)
        self.assertIn("drag", capture.phases)
        self.assertEqual(capture.template_path.name, "continuous-template.png")

    def isolated_probe(self, flags, *, timeout=False, missing_rows=False):
        """Run the real CLI orchestration, replacing only OS/Qt/hardware boundaries."""
        import tools.native_glass_probe as probe

        stack = self.enterContext(ExitStack())
        modules = self.make_modules()
        out = self._temp_out()
        private = self._temp_out()
        host = _Host()
        processes = []
        windows = []
        reads = []
        clock = [1000.0]

        class Window(_Window):
            def __init__(self):
                super().__init__()
                self.closed = False
                windows.append(self)

            def close(self):
                self.closed = True

        modules["stock_float_window"].FloatWindow = Window
        # No compositor image exists in this simulation: the real text judge must reject it.
        modules["PyQt6.QtGui"].QImage = lambda *_args: types.SimpleNamespace(isNull=lambda: True)

        def rows(option, number):
            reads.append((option, number))
            if option == 2 or (missing_rows and len(reads) == 1):
                return []
            return [{"kCGWindowNumber": 11, "kCGWindowBounds": {
                "X": 1, "Y": 2, "Width": 184, "Height": 224}}]

        modules["Quartz"].CGWindowListCopyWindowInfo = rows
        health = {"checks": {name: {"status": status} for name, status in (
            ("visibility", "passed"), ("space", "unknown"),
            ("glass", "failed"), ("window_level", "passed"))}}
        modules["sf_startup_health"] = types.SimpleNamespace(capture=mock.Mock(return_value=health))
        # These imports are patched too: the tool's network guard must not resolve live modules.
        modules["sf_quote"] = types.SimpleNamespace(http_get=mock.Mock(side_effect=AssertionError("network")))
        modules["sf_network"] = types.SimpleNamespace(http_get=mock.Mock(side_effect=AssertionError("network")))

        def popen(command, **_kwargs):
            if "--fullscreen-host" in command:
                return host
            self.assertEqual(command[0], "/usr/sbin/screencapture")
            process = mock.Mock()
            process.returncode = None
            polls = [0]

            def poll():
                polls[0] += 1
                if not timeout and polls[0] > 2:
                    process.returncode = 1
                return process.returncode

            def kill():
                process.returncode = -9

            process.poll.side_effect = poll
            process.kill.side_effect = kill
            processes.append(process)
            return process

        def monotonic():
            clock[0] += 0.001
            return clock[0]

        def sleep(seconds):
            clock[0] += seconds

        stack.enter_context(mock.patch.dict(os.environ))
        stack.enter_context(mock.patch.object(sys, "path", list(sys.path)))
        stack.enter_context(mock.patch.dict(sys.modules, modules))
        stack.enter_context(mock.patch.object(probe.sys, "argv", ["native_glass_probe.py", *flags,
                            "--seconds", "0", "--out", str(out)]))
        stack.enter_context(mock.patch.object(probe.time, "monotonic", side_effect=monotonic))
        stack.enter_context(mock.patch.object(probe.time, "sleep", side_effect=sleep))
        stack.enter_context(mock.patch.object(probe.subprocess, "Popen", side_effect=popen))
        stack.enter_context(mock.patch.object(probe.subprocess, "run", return_value=types.SimpleNamespace(
            returncode=1, stdout="", stderr="unavailable")))
        stack.enter_context(mock.patch.object(probe.select, "select", return_value=([host.stdout], [], [])))
        stack.enter_context(mock.patch.object(probe, "tempfile", types.SimpleNamespace(
            mkdtemp=lambda **_kwargs: str(private))))
        output = stack.enter_context(mock.patch("sys.stdout", new_callable=io.StringIO))
        return types.SimpleNamespace(probe=probe, out=out, modules=modules, processes=processes,
                                     windows=windows, output=output, health=health)

    def test_cli_rejects_incompatible_modes_before_system_access(self):
        import tools.native_glass_probe as probe

        cases = ((["--quality-sequence"], "--quality-sequence requires"),
                 (["--continuous-video"], "--continuous-video requires"),
                 (["--quality-sequence", "--continuous-video", "--fullscreen", "--exercise"],
                  "cannot use --quality-sequence"),
                 (["--continuous-control", "hide"], "--continuous-control requires"),
                 ([], "--out is required"), (["--not-a-mode"], "unrecognized arguments"))
        for flags, message in cases:
            with self.subTest(flags=flags), mock.patch.object(sys, "argv", ["probe", *flags]), \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as errors, \
                    mock.patch.object(probe.tempfile, "mkdtemp") as mkdir:
                with self.assertRaises(SystemExit) as raised:
                    probe.main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(message, errors.getvalue())
                mkdir.assert_not_called()

    def test_quality_sequence_keeps_missing_capture_and_space_evidence_failed(self):
        env = self.isolated_probe(["--quality-sequence", "--fullscreen", "--exercise",
                                   "--startup-health"], missing_rows=True)
        self.assertEqual(env.probe.main(), 1)
        result = json.loads((env.out / "result.json").read_text())
        self.assertEqual(json.loads(env.output.getvalue()), result)
        self.assertFalse(result["passed"])
        self.assertEqual(set(result["presentation_sequences"]),
                         {"startup", "fullscreen", "desktop_return", "after_drag"})
        startup = result["presentation_sequences"]["startup"]
        self.assertIsNone(startup[0]["pair"])
        self.assertFalse(startup[0]["content_visible"])
        self.assertEqual(len(startup), 6)
        self.assertEqual(startup[1]["text_match"]["reason"], "missing image")
        checks = {item["name"]: item for item in result["checks"]}
        for phase in result["presentation_sequences"]:
            self.assertFalse(checks[phase + "_sampled_compositor_sequence"]["passed"])
            self.assertTrue(checks[phase + "_sampled_compositor_sequence"]["detail"])
        self.assertFalse(checks["entered_distinct_fullscreen_space"]["passed"])
        self.assertFalse(checks["rapid_drag_windowserver_trajectory"]["passed"])
        self.assertTrue(checks["actual_startup_paint_geometry_and_price"]["passed"])
        self.assertFalse(checks["startup_health_space"]["passed"])
        self.assertFalse(checks["startup_health_glass"]["passed"])
        self.assertEqual(result["startup_health"], env.health)
        self.assertEqual(len(env.processes), 23)
        self.assertTrue(all(win.closed for win in env.windows))

    def test_sample_capture_timeout_is_killed_reaped_and_rejected(self):
        env = self.isolated_probe(["--quality-sequence", "--fullscreen", "--exercise"], timeout=True)
        self.assertEqual(env.probe.main(), 1)
        result = json.loads((env.out / "result.json").read_text())
        for process in env.processes:
            process.kill.assert_called_once_with()
            process.wait.assert_called_once_with()
        for samples in result["presentation_sequences"].values():
            self.assertTrue(all(sample["capture_seconds"] >= 3 for sample in samples))
            self.assertTrue(all(not sample["content_visible"] for sample in samples))
        self.assertFalse(result["passed"])

    def test_continuous_template_save_failure_stops_capture_and_closes_window(self):
        env = self.isolated_probe(["--continuous-video", "--fullscreen", "--exercise"])
        with (mock.patch.object(_Image, "save", return_value=False),
              mock.patch.object(_Capture, "stop", autospec=True) as stop,
              self.assertRaisesRegex(RuntimeError, "price template could not be saved")):
            env.probe.main()
        stop.assert_called_once()
        self.assertTrue(all(win.closed for win in env.windows))
        self.assertFalse((env.out / "result.json").exists())

    def test_missing_sequence_import_closes_window_without_success_report(self):
        env = self.isolated_probe(["--quality-sequence", "--fullscreen", "--exercise"])
        with (mock.patch.dict(sys.modules, {"visual_sequence_guard": None}),
              self.assertRaises(ModuleNotFoundError)):
            env.probe.main()
        self.assertTrue(all(win.closed for win in env.windows))
        self.assertFalse((env.out / "result.json").exists())

    def test_capture_launch_failure_closes_window_without_success_report(self):
        env = self.isolated_probe(["--quality-sequence", "--fullscreen", "--exercise"])
        with (mock.patch.object(env.probe.subprocess, "Popen", side_effect=OSError("capture denied")),
              self.assertRaisesRegex(OSError, "capture denied")):
            env.probe.main()
        self.assertTrue(all(win.closed for win in env.windows))
        self.assertFalse((env.out / "result.json").exists())

    def _temp_out(self):
        import tempfile
        path = Path(tempfile.mkdtemp(prefix="native-probe-test-", dir="/tmp"))
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        return path
