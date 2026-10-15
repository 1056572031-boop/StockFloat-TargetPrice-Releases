"""Isolated, reproducible CPU benchmarks and product-module inventory.

No installed app, camera, network, real configuration, or release action is used.
Timing is descriptive evidence, never a machine-dependent test threshold.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def inventory():
    result = []
    for path in sorted(ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        result.append({
            "module": path.stem,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "lines": len(path.read_text(encoding="utf-8").splitlines()),
            "functions": sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                             for node in ast.walk(tree)),
        })
    return result


def timed(call, iterations, rounds):
    for _ in range(min(iterations, 10)):
        call()
    samples = []
    for _ in range(rounds):
        started = time.perf_counter_ns()
        for _ in range(iterations):
            call()
        samples.append((time.perf_counter_ns() - started) / iterations / 1000)
    return {"median_us": statistics.median(samples), "rounds_us": samples,
            "iterations_per_round": iterations}


def measure(iterations, rounds, image_dir=None):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix="sf_perf_") as tmp, contextlib.ExitStack() as stack:
        # These must be set before any product import, including sf_constants.
        for name, filename in (("SF_CONFIG_PATH", "config.json"),
                               ("SF_LAST_QUOTE_PATH", "quote.json"),
                               ("SF_LOG_PATH", "debug.log"),
                               ("SF_SINGLE_SOCK", "instance.sock"),
                               ("SF_UNIVERSE_PATH", "securities.json")):
            os.environ[name] = str(Path(tmp) / filename)
        os.environ["SF_UNIVERSE_DISABLE"] = "1"
        from unittest.mock import patch
        from PyQt6.QtCore import QRectF, QTimer, QT_VERSION_STR
        from PyQt6.QtGui import QImage, QPainter
        from PyQt6.QtWidgets import QApplication
        import stock_float_window as m
        import sf_quote
        import sf_ui_render as render
        import sf_universe as universe

        app = QApplication.instance() or QApplication([])
        if not isinstance(app, QApplication):
            raise RuntimeError("A QApplication is required for the render benchmark")
        app.setQuitOnLastWindowClosed(False)
        m.load_bundled_fonts()
        stack.enter_context(patch.object(m, "load_config", return_value=(
            "sh600519", 100, 100, 300, "glass")))
        stack.enter_context(patch.object(m, "save_config", return_value=True))
        stack.enter_context(patch.object(m, "load_api_urls"))
        stack.enter_context(patch.object(m, "start_icon_worker"))
        stack.enter_context(patch.object(sf_quote, "http_get", side_effect=AssertionError(
            "Performance audit must never request the network")))
        for name in ("_force_top", "_inject_glass", "_setup_reopen_handler", "raise_",
                     "activateWindow", "_quote_loop_wrapper", "_minute_loop_wrapper",
                     "_sync_face_guard", "_show_previous_update_result", "_check_updates_background", "_prefetch_calendar",
                     "_sweep_temp_copies", "_prune_update_staging"):
            stack.enter_context(patch.object(m.FloatWindow, name))
        window = m.FloatWindow()
        window.running = False
        for timer in window.findChildren(QTimer):
            timer.stop()
        # Only the temporary configuration may be touched by closeEvent.
        stack.callback(window.close)
        window.quote = {"name": "Benchmark", "code": "600519", "price": "20.25",
                        "pre_close": "20.00", "change": "0.25", "pct": "1.25",
                        "high": "20.40", "low": "19.80", "volume": "1000",
                        "limit_up": "22.00", "limit_down": "18.00"}
        window._disp_price, window._disp_pct = 20.25, 1.25
        window._chart_expanded = True
        minutes = list(range(570, 691)) + list(range(781, 901))
        window.minute_points = [("%02d%02d" % divmod(minute, 60),
                                 20 + math.sin(index / 13) * 0.3,
                                 20 + math.sin(index / 31) * 0.1)
                                for index, minute in enumerate(minutes)]
        window._line_ver += 1
        # A fixed animation phase makes exact pixel comparisons reproducible.
        stack.enter_context(patch.object(render.time, "monotonic", return_value=1000.0))
        image = QImage(600, 840, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(2)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(16, 108, 268, 146)
        metrics = window._chart_metrics(window.minute_points, rect)
        window._bg_effect = True
        window._glass_backend = "legacy"
        stack.enter_context(patch.object(window, "_glass_dim", return_value=140))
        stack.enter_context(patch.object(window, "_glass_surface_rim", return_value=(171, 190, 219, 16)))
        bench = {}
        bench["chart_fill_241"] = timed(lambda: window._draw_chart_fill(painter, rect, metrics),
                                         iterations, rounds)
        bench["chart_241"] = timed(lambda: window._draw_chart(painter, rect), iterations, rounds)
        bench["background_legacy"] = timed(lambda: window._paint_background(painter, 300),
                                             iterations, rounds)
        bench["bottom_layout"] = timed(lambda: window._paint_bottom(painter, 300), iterations, rounds)
        heights = iter([90, 146, 210, 300] * (iterations * rounds + 10))

        def resize_chart():
            window._draw_chart(painter, QRectF(16, 108, 268, next(heights)))

        bench["chart_resizing"] = timed(resize_chart, iterations, rounds)
        painter.end()
        fingerprints = {}
        for backend in ("legacy", "liquid", "translucent"):
            window._glass_backend = backend
            window._bg_effect = backend != "translucent"
            for w, h in ((184, 155), (300, 300), (380, 420)):
                window.setFixedSize(w, h)
                for ratio in (1, 2):
                    image = QImage(w * ratio, h * ratio, QImage.Format.Format_ARGB32_Premultiplied)
                    image.setDevicePixelRatio(ratio)
                    image.fill(0)
                    painter = QPainter(image)
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                    for name in ("_paint_background", "_paint_header", "_paint_price",
                                 "_paint_chart", "_paint_bottom"):
                        getattr(window, name)(painter, w)
                    painter.end()
                    key = "%s-%dx%d-%dx" % (backend, w, h, ratio)
                    fingerprints[key] = hashlib.sha256(image.constBits().asstring(image.sizeInBytes())).hexdigest()
                    if image_dir:
                        image_dir.mkdir(parents=True, exist_ok=True)
                        if not image.save(str(image_dir / (key + ".png"))):
                            raise OSError("Could not save benchmark screenshot")

        rows = [("sh%06d" % (600000 + i), "bank%04d" % i) for i in range(6000)]
        stack.enter_context(patch.object(universe, "_DISABLED", False))
        for name in ("_rows", "_by_code", "_by_name"):
            stack.enter_context(patch.object(universe, name, getattr(universe, name)))
        universe._index(rows)
        bench["search_ambiguous_6000"] = timed(lambda: universe.match_confident("bank"), iterations, rounds)
        bench["search_fuzzy_6000_limit8"] = timed(lambda: universe.match_fuzzy("bank", 8), iterations, rounds)
        return {"schema": 1, "python": sys.version, "qt": QT_VERSION_STR,
                "benchmarks": bench, "pixels": fingerprints, "modules": inventory(),
                "limits": ["Synthetic 241-point data; not a network-latency measurement",
                           "Offscreen CPU rendering; not WindowServer, GPU, energy, or camera evidence",
                           "Warm microbenchmarks; not cold-start or long-duration leak evidence"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=300)
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--images", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.iterations <= 0 or args.rounds <= 0:
        parser.error("iterations and rounds must be positive")
    if args.child:
        result = measure(args.iterations, args.rounds, args.images)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        # Qt offscreen teardown is not part of the timed workload.
        os._exit(0)
    command = [sys.executable, str(Path(__file__).resolve()), "--child", "--iterations",
               str(args.iterations), "--rounds", str(args.rounds)]
    if args.images:
        command += ["--images", str(args.images)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=180, check=True)
    result = json.loads(completed.stdout)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["benchmarks"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
