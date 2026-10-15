#!/usr/bin/env python3
"""Release gate for card-corner pixels, packaged geometry and native composition."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import subprocess
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def native_corner_problems(image, backend, scale, radius, inset=0):
    """Independent circular/superellipse oracle; never derive it from a product path."""
    if image.isNull() or scale <= 0 or radius <= 0 or backend not in ("liquid", "legacy"):
        return ["invalid native corner evidence"]
    background = image.pixelColor(0, 0)
    center = image.pixelColor(image.width() // 2, image.height() // 2)
    if max(abs(a - b) for a, b in zip(center.getRgb()[:3], background.getRgb()[:3])) < 30:
        return ["blank or insufficiently contrasted native capture"]
    power = 2 if backend == "liquid" else 4
    center_at = radius + inset
    overflow = [0, 0, 0, 0]
    samples = 0
    for y in range(math.ceil(center_at * scale)):
        for x in range(math.ceil(center_at * scale)):
            px, py = (x + 0.5) / scale, (y + 0.5) / scale
            distance = ((center_at - px) ** power + (center_at - py) ** power) ** (1 / power)
            # One logical pixel excludes antialiasing and the native material edge.
            if distance <= radius + 1:
                continue
            samples += 1
            for corner, (cx, cy) in enumerate(((x, y), (image.width() - 1 - x, y),
                                              (x, image.height() - 1 - y),
                                              (image.width() - 1 - x, image.height() - 1 - y))):
                if not (0 <= cx < image.width() and 0 <= cy < image.height()):
                    return ["native capture is smaller than its corner geometry"]
                color = image.pixelColor(cx, cy)
                if max(abs(a - b) for a, b in zip(color.getRgb()[:3], background.getRgb()[:3])) > 10:
                    overflow[corner] += 1
    if not samples:
        return ["no exterior corner pixels were examined"]
    return [f"native corners protrude outside the material: {overflow}"] if any(overflow) else []


def oracle_self_test_problems():
    """Every release must prove the oracle still rejects a second card and blank capture."""
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath

    problems = []
    for name, actual_radius, expected_failure in (("matched", 20, False),
                                                   ("second_card", 5, True),
                                                   ("blank", None, True)):
        image = QImage(120, 160, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(245, 245, 245))
        if actual_radius is not None:
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, image.width(), image.height()), actual_radius, actual_radius)
            painter = QPainter(image)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.fillPath(path, QColor(44, 44, 44))
            finally:
                painter.end()
        if bool(native_corner_problems(image, "liquid", 1, 20)) != expected_failure:
            problems.append(f"corner oracle self-test failed: {name}")
    return problems


def self_test():
    problems = oracle_self_test_problems()
    for problem in problems:
        print("FAIL:", problem)
    print("Card corner gate self-test:", "FAIL" if problems else "PASS")
    return int(bool(problems))


def render_problems():
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtWidgets import QApplication, QWidget
    from sf_constants import BG_LIQUID_GLASS_DIM, LIQUID_GLASS_INSET, R
    from sf_ui_render import RenderMixin

    app = QApplication.instance() or QApplication([])

    class Host(RenderMixin, QWidget):
        def __init__(self):
            QWidget.__init__(self)
            self.__dict__.update(_bg_effect=True, _glass_backend="liquid", _bg_path=None,
                                 _bg_path_wh=None, _bg_path_ins=None, _bg_path_ins_wh=None)

        def _glass_dim(self):
            return BG_LIQUID_GLASS_DIM

    host = Host()
    problems = []
    try:
        for width, height in ((184, 224), (184, 128), (260, 300)):
            host.resize(width, height)
            for scale in (1, 2):
                image = QImage(width * scale, height * scale, QImage.Format.Format_ARGB32_Premultiplied)
                image.setDevicePixelRatio(scale)
                image.fill(0)
                painter = QPainter(image)
                try:
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                    RenderMixin._paint_background(host, painter, width)
                finally:
                    painter.end()
                mismatches = 0
                for y in range(R * scale):
                    for x in range(R * scale):
                        px, py = (x + 0.5) / scale, (y + 0.5) / scale
                        distance = math.hypot(R - px, R - py)
                        if min(px, py) < LIQUID_GLASS_INSET + 1 / scale or abs(distance - (R - LIQUID_GLASS_INSET)) < 1 / scale:
                            continue
                        expected = BG_LIQUID_GLASS_DIM if distance < R - LIQUID_GLASS_INSET else 0
                        for cx, cy in ((x, y), (image.width() - 1 - x, y),
                                       (x, image.height() - 1 - y),
                                       (image.width() - 1 - x, image.height() - 1 - y)):
                            mismatches += abs(image.pixelColor(cx, cy).alpha() - expected) > 1
                if mismatches:
                    problems.append(f"{width}x{height} @{scale}x: {mismatches} mismatched corner pixels")
    finally:
        host.close()
    if app is None:
        return ["QApplication unavailable"]
    return problems


def code_shape(code):
    """Compare executable behavior without build paths or source line numbers."""
    return (code.co_code, code.co_exceptiontable, code.co_names, code.co_varnames,
            code.co_freevars, code.co_cellvars, code.co_argcount, code.co_posonlyargcount,
            code.co_kwonlyargcount, code.co_flags,
            tuple(code_shape(value) if isinstance(value, types.CodeType) else value
                  for value in code.co_consts))


def find_code(code, path):
    for name in path.split("."):
        matches = [value for value in code.co_consts
                   if isinstance(value, types.CodeType) and value.co_name == name]
        if len(matches) != 1:
            raise ValueError(f"missing or ambiguous packaged function: {path}")
        code = matches[0]
    return code


def bundle_problems(bundle):
    import renderer
    import sf_constants
    from sf_glass import GlassMixin, create_glass_surface
    from sf_ui_render import RenderMixin
    from verify_dmg_pyz import top_level_literal

    problems = []
    targets = (("renderer", "liquid_glass_dim_path", renderer.liquid_glass_dim_path),
               ("sf_ui_render", "RenderMixin._paint_background", RenderMixin._paint_background),
               ("sf_glass", "create_glass_surface", create_glass_surface),
               ("sf_glass", "GlassMixin._set_glass_mask", GlassMixin._set_glass_mask))
    for module, path, function in targets:
        code = bundle.code(module)
        try:
            if code is None or code_shape(find_code(code, path)) != code_shape(function.__code__):
                problems.append(f"packaged geometry differs from validated source: {module}::{path}")
        except ValueError as exc:
            problems.append(str(exc))
    constants = bundle.code("sf_constants")
    for name in ("APP_VERSION", "R", "GLASS_INSET", "LIQUID_GLASS_INSET", "BG_LIQUID_GLASS_DIM", "LIQUID_GLASS_OPACITY"):
        if constants is None or top_level_literal(constants, name) != getattr(sf_constants, name):
            problems.append(f"packaged corner/material constant differs: {name}")
    return problems


def native_problems(out):
    import AppKit

    problems = []
    backends = ["legacy"]
    if getattr(AppKit, "NSGlassEffectView", None) is not None:
        backends.append("liquid")
    else:
        print("Native Liquid Glass API unavailable; legacy is the required backend on this host")
    for backend in backends:
        command = [sys.executable, str(ROOT / "tools/native_glass_probe.py"), "--corners",
                   "--exercise", "--seconds", "1", "--out", str(out / backend)]
        if backend == "legacy":
            command.append("--legacy")
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=90)
        print(result.stdout)
        if result.returncode:
            print(result.stderr, file=sys.stderr)
            problems.append(f"native {backend} corner/lifecycle probe failed")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, help="also verify actual packaged geometry")
    parser.add_argument("--native", action="store_true", help="require real Cocoa corner captures and lifecycle checks")
    parser.add_argument("--self-test", action="store_true", help="test the oracle only, without claiming a release was checked")
    parser.add_argument("--out", type=Path, default=ROOT / "build/card-corners")
    args = parser.parse_args(argv)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if args.self_test:
        return self_test()
    if not args.app:
        print("Packaged geometry: NOT CHECKED (no --app)")
    if not args.native:
        print("Native compositor: NOT CHECKED (no --native)")
    problems = oracle_self_test_problems()
    problems.extend(render_problems())
    if args.app:
        from verify_dmg_pyz import Bundle
        bundle = Bundle(args.app)
        try:
            problems.extend(bundle_problems(bundle))
        finally:
            bundle.dispose()
    if args.native:
        problems.extend(native_problems(args.out))
    for problem in problems:
        print("FAIL:", problem)
    print("Card corner gate:", "FAIL" if problems else "PASS")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
