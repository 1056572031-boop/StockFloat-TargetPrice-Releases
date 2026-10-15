"""Isolated real-window lifecycle and resource measurements for glass backends."""

from __future__ import annotations

import argparse
import json
import os
import select
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


def fullscreen_host():
    import AppKit
    from Foundation import NSDate, NSRunLoop

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

    class TransitionDelegate(AppKit.NSObject):
        def windowDidEnterFullScreen_(self, notification):
            print("entered", flush=True)

        def windowDidExitFullScreen_(self, notification):
            print("left", flush=True)

    delegate = TransitionDelegate.alloc().init()
    style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskResizable | AppKit.NSWindowStyleMaskClosable
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((200, 200), (640, 420)), style, AppKit.NSBackingStoreBuffered, False)
    window.setReleasedWhenClosed_(False)
    window.setDelegate_(delegate)
    window.setTitle_("Native Glass Fullscreen Test Host")
    window.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorFullScreenPrimary)
    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    print("ready", flush=True)
    end = time.monotonic() + 45
    while time.monotonic() < end:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))
        if select.select([sys.stdin], [], [], 0)[0]:
            command = sys.stdin.readline().strip()
            if command == "quit" or not command:
                break
            if command in ("enter", "leave"):
                window.toggleFullScreen_(None)
    window.orderOut_(None)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--exercise", action="store_true")
    parser.add_argument("--fullscreen", action="store_true")
    parser.add_argument("--corners", action="store_true")
    parser.add_argument("--popups", action="store_true")
    parser.add_argument("--fullscreen-host", action="store_true")
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.fullscreen_host:
        return fullscreen_host()
    if args.out is None:
        parser.error("--out is required for a native-window probe")
    args.out.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="sf_ng_", dir="/tmp"))
    for name, filename in {"SF_CONFIG_PATH": "config.json", "SF_LOG_PATH": "debug.log",
                           "SF_LAST_QUOTE_PATH": "last_quote.json", "SF_UNIVERSE_PATH": "securities.json",
                           "SF_SINGLE_SOCK": "single.sock"}.items():
        os.environ[name] = str(temporary / filename)
    os.environ["QT_QPA_PLATFORM"] = "cocoa"
    os.environ["SF_SEED_FIRST_SCREEN"] = "1"
    os.environ["SF_UNIVERSE_DISABLE"] = "1"
    sys.path.insert(0, str(ROOT))

    import Quartz
    from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt, QTimer
    from PyQt6.QtGui import QColor, QImage, QMouseEvent, QPainter
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QWidget

    import stock_float_window as product
    from sf_glass import active_space_id, content_ns_window, glass_main_real_state
    from sf_ui_render import CHART_TOP, collapsed_height

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    product.load_bundled_fonts()
    if args.snapshot:
        snapshot = json.loads(args.snapshot.read_text())
    else:
        snapshot = {"code": "sh600183", "day": product._beijing_now().strftime("%Y-%m-%d"),
                    "quote": {"name": "\u751f\u76ca\u79d1\u6280", "code": "600183", "price": "119.63",
                              "pct": "-2.37", "change": "-2.91", "pre_close": "122.54", "high": "126.66",
                              "low": "119.00", "volume": "455539"},
                    "minute": [["0930", 122.2, 122.2], ["1030", 124.1, 122.8],
                               ["1330", 121.0, 122.8], ["1500", 119.63, 122.63]]}
    (temporary / "last_quote.json").write_text(json.dumps(snapshot))
    (temporary / "config.json").write_text(json.dumps({"code": snapshot["code"], "x": 400,
        "y": 180, "h": 224, "bg": "glass", "face_guard": False, "alerts": []}))

    class ProbeWindow(product.FloatWindow):
        _prefer_liquid_glass = not args.legacy

        def _quote_loop_wrapper(self):
            return None

        def _minute_loop_wrapper(self):
            return None

        def _setup_reopen_handler(self):
            return None

        def _apply_icons(self):
            return None

        def _check_updates_background(self, manual=False):
            return None

        def _check_updates_after_resume(self, state):
            return None

        def _sweep_temp_copies(self):
            return None

        def _prune_update_staging(self):
            return None

        def _is_trading_time(self, now=None):
            return True

    result: dict[str, Any] = {"backend_requested": "legacy" if args.legacy else "liquid", "checks": [],
              "limits": ["Network and camera disabled; simulated Qt input, not HID injection.",
                         "WindowServer CPU is shared system activity; GPU energy is not measured."]}

    def check(name, value, detail=None):
        result["checks"].append({"name": name, "passed": bool(value), "detail": detail})

    def wait(predicate, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if predicate():
                return True
            time.sleep(0.008)
        return False

    def observe(seconds):
        end = time.monotonic() + seconds
        wait(lambda: time.monotonic() >= end, seconds + 0.1)

    def state():
        main_window = content_ns_window(win)
        background = getattr(win, "_glass_window", None)
        if main_window is None or background is None:
            return None
        return glass_main_real_state(int(main_window.windowNumber()), int(background.windowNumber()))

    def aligned():
        pair = state()
        return pair is not None and not pair["glass_on_top"] and pair["desync_px"] <= 1

    def glass_window() -> Any:
        background = getattr(win, "_glass_window", None)
        if background is None:
            raise RuntimeError("Native background window disappeared")
        return background

    def reject_network(*_args, **_kwargs):
        raise RuntimeError("Native UI probe must never make live network requests")

    win: Any = None
    # Qt's stubs incorrectly model these static input helpers as instance methods.
    input_api: Any = QTest
    with (patch.object(product, "load_api_urls", lambda: None), patch.object(product, "show_dock", lambda: None),
          patch("sf_quote.http_get", reject_network), patch("sf_network.http_get", reject_network)):
        try:
            win = ProbeWindow()
            for timer in win.findChildren(QTimer):
                timer.stop()
            win.top_timer.start()
            win.show()
            check("native_surface_ready", wait(lambda: bool(getattr(win, "_bg_effect", False))))
            check("surface_backend", win.glass_diagnostics()["surface"]["backend"] == result["backend_requested"])
            check("initial_alignment", wait(aligned))
            if args.popups:
                from popup_material_probe import verify_popups
                verify_popups(win, app, args.out, check, wait, observe)
            if args.fullscreen:
                normal_space = active_space_id()
                if normal_space is None:
                    raise RuntimeError("Active Space id is unavailable; fullscreen verification is not valid")
                host = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--fullscreen-host"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    assert host.stdin is not None and host.stdout is not None
                    ready = host.stdout.readline().strip()
                    check("fullscreen_host_ready", ready == "ready")

                    def acknowledged(expected):
                        if host.stdout is not None and select.select([host.stdout], [], [], 0)[0]:
                            return host.stdout.readline().strip() == expected
                        return False

                    host.stdin.write("enter\n")
                    host.stdin.flush()
                    check("fullscreen_transition_completed", wait(lambda: acknowledged("entered"), 10))
                    check("entered_distinct_fullscreen_space", wait(lambda:
                        (space := active_space_id()) is not None and space != normal_space, 10))
                    check("glass_and_content_visible_in_fullscreen", wait(aligned, 10))
                    if args.popups:
                        from popup_material_probe import verify_fullscreen_popups
                        verify_fullscreen_popups(win, args.out, check, wait)
                    host.stdin.write("leave\n")
                    host.stdin.flush()
                    check("exit_fullscreen_transition_completed", wait(lambda: acknowledged("left"), 10))
                    check("returned_to_original_space", wait(lambda: active_space_id() == normal_space, 10))
                    check("desktop_alignment_after_fullscreen", wait(aligned, 10))
                finally:
                    if host.stdin is not None:
                        host.stdin.write("quit\n")
                        host.stdin.flush()
                    try:
                        host.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        host.terminate()
                        host.communicate(timeout=5)
            if args.exercise:
                origin = win.pos()
                input_api.mousePress(win, Qt.MouseButton.LeftButton, pos=QPoint(20, 5))
                local = QPoint(68, 29)
                move = QMouseEvent(QEvent.Type.MouseMove, QPointF(local), QPointF(win.mapToGlobal(local)),
                                   Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                QCoreApplication.sendEvent(win, move)
                input_api.mouseRelease(win, Qt.MouseButton.LeftButton, pos=local)
                check("default_qt_drag_moves_window", wait(lambda: win.pos() != origin))
                check("drag_alignment", wait(aligned))
                original_height = win.height()
                input_api.mouseClick(win, Qt.MouseButton.LeftButton,
                                 pos=QPoint(win.width() // 2, (CHART_TOP + original_height - 46) // 2))
                check("chart_collapses", wait(lambda: win.height() == collapsed_height()))
                input_api.mouseClick(win, Qt.MouseButton.LeftButton,
                                 pos=QPoint(win.width() // 2, CHART_TOP + 4))
                check("chart_expands", wait(lambda: win.height() == original_height))
                check("chart_resize_alignment", wait(aligned))
                win.minimize_to_dock()
                check("minimize_hides_both", wait(lambda: not win.isVisible()
                    and not glass_window().isVisible()))
                win._restore_window()
                check("restore_shows_content_above_glass", wait(aligned))
                background = glass_window()
                background.orderFront_(None)
                check("inverted_order_observed", wait(lambda: bool(state() and state()["glass_on_top"])))
                check("watchdog_recovers_order", wait(aligned))
            win._sync_dot_timer()
            observe(1)
            cpu_start, wall_start = time.process_time(), time.monotonic()
            rss, ws_cpu = [], []
            ws_pid = subprocess.run(["/usr/bin/pgrep", "-x", "WindowServer"], capture_output=True, text=True, check=False).stdout.strip().splitlines()
            sample_at = wall_start
            while time.monotonic() - wall_start < args.seconds:
                app.processEvents()
                if time.monotonic() >= sample_at:
                    raw = subprocess.run(["/bin/ps", "-p", str(os.getpid()), "-o", "rss="], capture_output=True, text=True, check=False).stdout.strip()
                    if raw:
                        rss.append(int(raw) / 1024)
                    if ws_pid:
                        raw = subprocess.run(["/bin/ps", "-p", ws_pid[0], "-o", "%cpu="], capture_output=True, text=True, check=False).stdout.strip()
                        if raw:
                            ws_cpu.append(float(raw))
                    sample_at = time.monotonic() + 0.5
                time.sleep(0.008)
            wall = time.monotonic() - wall_start
            result["resources"] = {"seconds": round(wall, 3),
                "process_cpu_percent": round(100 * (time.process_time() - cpu_start) / wall, 3),
                "rss_median_mib": round(statistics.median(rss), 2) if rss else None,
                "rss_growth_mib": round(rss[-1] - rss[0], 2) if rss else None,
                "windowserver_cpu_median": statistics.median(ws_cpu) if ws_cpu else None}
            result["surface"] = win.glass_diagnostics()["surface"]
            main_window = content_ns_window(win)
            numbers = (int(main_window.windowNumber()), int(glass_window().windowNumber()))
            window_info = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, numbers[0])
            bounds = window_info[0]["kCGWindowBounds"]
            backdrop = None
            if args.corners:
                class WhiteBackdrop(QWidget):
                    def paintEvent(self, event):
                        painter = QPainter(self)
                        try:
                            painter.fillRect(self.rect(), QColor(245, 245, 245))
                        finally:
                            painter.end()

                backdrop = WhiteBackdrop()
                backdrop.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
                backdrop.setGeometry(int(bounds["X"]) - 40, int(bounds["Y"]) - 40,
                                     int(bounds["Width"]) + 80, int(bounds["Height"]) + 80)
                backdrop.show()
                native = content_ns_window(backdrop)
                native.setHasShadow_(False)
                native.setHidesOnDeactivate_(False)
                native.setCollectionBehavior_(main_window.collectionBehavior())
                native.setLevel_(int(glass_window().level()) - 1)
                native.orderFront_(None)
                observe(0.25)
            capture = subprocess.run(["/usr/sbin/screencapture", "-x", "-o",
                f"-R{int(bounds['X'])},{int(bounds['Y'])},{int(bounds['Width'])},{int(bounds['Height'])}",
                str(args.out / "window.png")], capture_output=True, text=True, check=False)
            check("real_compositor_capture", capture.returncode == 0 and not QImage(str(args.out / "window.png")).isNull())
            if args.corners:
                from sf_constants import GLASS_INSET, R
                from verify_card_corners import native_corner_problems
                image = QImage(str(args.out / "window.png"))
                backend = result["surface"]["backend"]
                problems = native_corner_problems(image, backend, image.width() / win.width(), R,
                                                  GLASS_INSET if backend == "legacy" else 0)
                check("four_native_corners_have_no_second_outline", not problems, problems)
            if backdrop is not None:
                backdrop.close()
            win._persist_on_exit()
            win._shutdown_threads()
            win.close()
            def gone():
                rows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
                return not any(int(row["kCGWindowNumber"]) in numbers for row in rows)
            check("both_windows_removed_after_close", wait(gone))
            check("window_threads_stopped", all(not thread.is_alive() for value in vars(win).values()
                if isinstance((thread := value), threading.Thread)))
        finally:
            if win is not None:
                win._persist_on_exit()
                win._shutdown_threads()
                win.close()
    result["passed"] = all(item["passed"] for item in result["checks"])
    (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
