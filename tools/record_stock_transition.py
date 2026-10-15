"""Record the real Cocoa UI with deterministic quotes and isolated user data.

Writes a native screen recording, a Qt-rendered storyboard, and input assertions.
Network, camera, updates and user configuration are not used.
"""

import argparse
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import time
from typing import Any
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="sf_transition_", dir="/tmp")
    for key, filename in {"SF_CONFIG_PATH": "config.json", "SF_LOG_PATH": "debug.log",
                          "SF_LAST_QUOTE_PATH": "last_quote.json", "SF_UNIVERSE_PATH": "securities.json",
                          "SF_SINGLE_SOCK": "single.sock"}.items():
        os.environ[key] = str(Path(temporary.name) / filename)
    os.environ["QT_QPA_PLATFORM"] = "cocoa"
    os.environ["SF_SEED_FIRST_SCREEN"] = "0"
    os.environ["SF_UNIVERSE_DISABLE"] = "1"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from PyQt6.QtCore import QPoint, Qt, QTimer
    from PyQt6.QtGui import QColor, QImage, QPainter
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QWidget
    import stock_float_window as product

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    product.load_bundled_fonts()

    class DemoWindow(product.FloatWindow):
        def paintEvent(self, event):
            transition = self._stock_transition
            active = transition.active
            started = time.perf_counter()
            super().paintEvent(event)
            if active:
                results["animation_frames"].append({"code": self.code,
                    "time": started, "progress": transition.progress,
                    "paint_ms": (time.perf_counter() - started) * 1000})

        def _quote_loop_wrapper(self):
            pass

        def _minute_loop_wrapper(self):
            pass

        def _setup_reopen_handler(self):
            pass

        def _check_updates_background(self, manual=False):
            pass

        def _check_updates_after_resume(self, state):
            pass

        def _force_fetch(self):
            # Arrive during the reveal, using the real quote application path.
            QTimer.singleShot(110, lambda: feed(self.code))

    def feed(code):
        data = {
            "sh600519": ("\u8d35\u5dde\u8305\u53f0", 1456.80, 1.36, 1437.25),
            "sz000001": ("\u5e73\u5b89\u94f6\u884c", 11.82, -1.01, 11.94),
            "sh600183": ("\u751f\u76ca\u79d1\u6280", 119.63, 2.37, 116.86),
        }
        name, price, pct, previous = data[code]
        win._apply_quote({"code": code[2:], "name": name, "price": "%.2f" % price, "pct": str(pct),
                          "change": "%.2f" % (price - previous), "pre_close": "%.2f" % previous,
                          "high": "%.2f" % (price * 1.012), "low": "%.2f" % (previous * 0.99),
                          "open": "%.2f" % previous, "volume": "455539", "amount": "1234567890"})
        win.minute_points = [("0930", previous, previous),
                             ("1000", previous * 1.006, previous * 1.002),
                             ("1100", price * 0.994, previous * 1.003),
                             ("1400", price * 1.005, previous * 1.004),
                             ("1500", price, (price + previous) / 2)]
        win._line_ver += 1
        win._chart_changed = True
        win._disp_price = price
        win._disp_pct = pct
        win._anim_timer.stop()
        win.update()

    background = QWidget()
    background.setWindowFlags(Qt.WindowType.FramelessWindowHint)
    background.setStyleSheet("background: #e4e8eb;")
    screen = app.primaryScreen()
    assert screen is not None
    area = screen.availableGeometry()
    origin = QPoint(area.x() + 80, area.y() + 80)
    background.setGeometry(origin.x(), origin.y(), 960, 540)
    background.show()
    results: dict[str, Any] = {"duration_seconds": 10, "source": "macOS native screen recording",
               "quotes": "fixed demo fixtures, not live market data", "checks": [],
               "animation_frames": []}
    # PyQt stubs incorrectly describe the static QTest helpers as bound methods.
    input_api: Any = QTest
    with (patch.object(product, "load_config", return_value=("sh600519", origin.x() + 100,
            origin.y() + 150, 280, "glass")),
          patch.object(product, "save_config", return_value=True),
          patch.object(product, "load_api_urls"), patch.object(product, "start_icon_worker"),
          patch.object(product, "show_dock"), patch("sf_quote.http_get", side_effect=RuntimeError("offline demo")),
          patch("sf_universe.match_confident", return_value=[("sz000001", "bank")])):
        win = DemoWindow()
        results["transition_duration_ms"] = win._stock_transition.DURATION_MS
        results["max_blur"] = win._stock_transition.MAX_BLUR
        results["transition_profile"] = [
            {"progress": index / 100.0,
             "blend": win._stock_transition.profile(index / 100.0)[0],
             "radius": win._stock_transition.profile(index / 100.0)[1]}
            for index in range(101)]
        for timer in win.findChildren(QTimer):
            timer.stop()
        win.top_timer.start()
        win.show()
        feed(win.code)

        def open_editor(code):
            win.open_editor()
            editor = win._editor
            assert editor is not None
            editor.entry.setText(code)
            editor.entry.setFocus()

        def confirm(use_return):
            editor = win._editor
            assert editor is not None
            if use_return:
                input_api.keyClick(editor.entry, Qt.Key.Key_Return)
            else:
                input_api.mouseClick(editor.ok, Qt.MouseButton.LeftButton)
            expected = "sh600183" if use_return else "sz000001"
            results["checks"].append({"input": "return" if use_return else "button",
                "code": win.code, "passed": win.code == expected and win._stock_transition.active})
            QTimer.singleShot(350, lambda: results["checks"].append({"input": expected + "_visible",
                "passed": win.isVisible() and win._stock_transition.active,
                "progress": win._stock_transition.progress}))

        schedule = [(0.9, lambda: open_editor("000001")), (2.0, lambda: confirm(False)),
                    (4.4, lambda: open_editor("600183")), (5.5, lambda: confirm(True))]
        storyboard = []
        # Warm up native glass and fonts before the separate compositor recorder.
        warmup = time.monotonic() + 0.8
        while time.monotonic() < warmup:
            app.processEvents()
            time.sleep(0.005)
        movie = args.out / "stock-switch-blur.mov"
        recorder = subprocess.Popen(["/usr/sbin/screencapture", "-x", "-v", "-V", "10",
            "-R%d,%d,960,540" % (origin.x(), origin.y()), str(movie)])
        start = time.monotonic()
        for index in range(660):
            deadline = start + index / 60
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.001)
            elapsed = time.monotonic() - start
            while schedule and elapsed >= schedule[0][0]:
                schedule.pop(0)[1]()
            app.processEvents()
            if index in (119, 126, 134, 144, 164, 200):
                storyboard.append(win.grab().toImage())
        returncode = recorder.wait(timeout=15)
        results["checks"].append({"input": "native_video", "passed": returncode == 0
                                 and movie.exists() and movie.stat().st_size > 0})
        summary = QImage(320 * len(storyboard), 360, QImage.Format.Format_RGB32)
        summary.fill(QColor("#e4e8eb"))
        painter = QPainter(summary)
        for index, image in enumerate(storyboard):
            painter.drawImage(index * 320 + 20, 20, image)
        painter.end()
        summary.save(str(args.out / "storyboard.png"))
        results["checks"].append({"input": "cleanup", "passed": not win._stock_transition.active})
        results["checks"].append({"input": "both_transitions_painted", "passed": all(
            sum(frame["code"] == code for frame in results["animation_frames"]) >= 30
            for code in ("sz000001", "sh600183"))})
        log = Path(os.environ["SF_LOG_PATH"])
        if log.exists():
            (args.out / "demo.log").write_bytes(log.read_bytes())
        win.close()
        background.close()
        app.processEvents()
    (args.out / "recording.json").write_text(json.dumps(results, indent=2))
    temporary.cleanup()
    return 0 if all(check["passed"] for check in results["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
