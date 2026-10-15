"""Native cross-window material, pixel and popup lifecycle checks."""

from __future__ import annotations

import gc
import statistics
import subprocess
from typing import Any


class CaptureScene:
    """Isolate color sampling from other GUI probes, then restore production levels."""

    def __init__(self, main, popup, backdrop):
        from sf_glass import content_ns_window
        windows: list[Any] = [content_ns_window(backdrop), getattr(main, "_glass_window", None),
                              content_ns_window(main), content_ns_window(popup)]
        if any(window is None for window in windows):
            raise RuntimeError("capture scene is missing a native window")
        self.windows = list(windows)
        self.saved = [(window, int(window.level()), bool(window.hidesOnDeactivate()))
                      for window in self.windows]
        self.timer = main.top_timer
        self.timer_active = self.timer.isActive()

    def activate(self):
        from sf_glass import TOOL_WINDOW_LEVEL
        self.timer.stop()
        for window, offset in zip(self.windows, (10, 11, 11, 12)):
            window.setHidesOnDeactivate_(False)
            window.setLevel_(TOOL_WINDOW_LEVEL + offset)
            window.orderFrontRegardless()

    def close(self):
        if not self.saved:
            return
        try:
            for window, level, hidden in self.saved:
                window.setLevel_(level)
                window.setHidesOnDeactivate_(hidden)
            if self.timer_active:
                self.timer.start()
        finally:
            self.saved = []


def sample_surface(image, x, y, width, height, scale):
    """Sample the empty bottom padding, away from controls and glass rims."""
    if image.isNull() or scale <= 0:
        raise ValueError("invalid compositor image or scale")
    values: list[list[int]] = [[], [], []]
    for dx in range(-4, 5):
        for dy in range(3):
            px = round((x + width / 2 + dx) * scale)
            py = round((y + height - 10 + dy) * scale)
            if not (0 <= px < image.width() and 0 <= py < image.height()):
                raise ValueError("surface sample is outside the screenshot")
            color = image.pixelColor(px, py)
            for channel, value in zip(values, color.getRgb()[:3]):
                channel.append(value)
    return tuple(int(statistics.median(channel)) for channel in values)


def verify_fullscreen_popups(main, out, check, wait):
    import Quartz
    from PyQt6.QtCore import QCoreApplication, QEvent
    from PyQt6.QtGui import QImage
    from sf_glass import active_space_id, content_ns_window

    fullscreen_space = active_space_id()

    for name, open_popup, attr in (("editor", main.open_editor, "_editor"),
                                   ("targets", main._open_target_panel, "_target_panel")):
        open_popup()
        popup = getattr(main, attr)
        ready = popup is not None and wait(lambda popup=popup: bool(getattr(popup, "_bg_effect", False)))
        check("fullscreen_" + name + "_surface_ready", ready)
        if not ready:
            if popup is not None:
                popup.close()
            continue
        try:
            check("fullscreen_" + name + "_stays_in_same_space",
                  fullscreen_space is not None and wait(lambda: active_space_id() == fullscreen_space))
            window = content_ns_window(popup)
            if window is None:
                check("fullscreen_" + name + "_window", False)
                continue
            number = int(window.windowNumber())

            def onscreen():
                entries = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, 0)
                return any(int(row["kCGWindowNumber"]) == number for row in entries)

            check("fullscreen_" + name + "_onscreen", wait(onscreen))
            check("fullscreen_" + name + "_same_material", getattr(popup, "_glass_backend", None) ==
                  getattr(main, "_glass_backend", None))
            info = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, number)
            bounds = info[0]["kCGWindowBounds"]
            path = out / ("fullscreen-" + name + ".png")
            capture = subprocess.run(["/usr/sbin/screencapture", "-x", "-o",
                f"-R{int(bounds['X'])},{int(bounds['Y'])},{int(bounds['Width'])},{int(bounds['Height'])}",
                str(path)], capture_output=True, text=True, check=False)
            image = QImage(str(path))
            check("fullscreen_" + name + "_capture", capture.returncode == 0 and not image.isNull())
            if not image.isNull():
                sample = sample_surface(image, 0, 0, popup.width(), popup.height(), image.width() / popup.width())
                check("fullscreen_" + name + "_dark_material_visible", max(sample) < 100,
                      {"rgb": sample, "screenshot": str(path)})
        finally:
            popup.close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def verify_popups(main, app, out, check, wait, observe):
    import AppKit
    import objc
    from PyQt6.QtCore import QCoreApplication, QEvent, Qt
    from PyQt6.QtGui import QColor, QImage, QPainter
    from PyQt6.QtWidgets import QWidget

    from sf_glass import content_ns_window

    class Backdrop(QWidget):
        color = QColor(245, 245, 245)

        def paintEvent(self, event):
            painter = QPainter(self)
            try:
                painter.fillRect(self.rect(), self.color)
            finally:
                painter.end()

    backdrop = Backdrop()
    backdrop.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
    baseline = None
    scene = None
    try:
        # Production entrances are mutually exclusive. Exercise each separately.
        for name, open_popup, attr in (("editor", main.open_editor, "_editor"),
                                       ("targets", main._open_target_panel, "_target_panel")):
            open_popup()
            popup = getattr(main, attr)
            if popup is None:
                check(name + "_created", False)
                continue
            check(name + "_surface_ready", wait(lambda popup=popup: bool(getattr(popup, "_bg_effect", False))))
            window = content_ns_window(popup)
            surface = getattr(popup, "_popup_surface", None)
            check(name + "_same_backend", getattr(popup, "_glass_backend", None) ==
                  getattr(main, "_glass_backend", None))
            embedded = (window is not None and surface is not None and surface.view is not None
                        and surface.view.window() == window
                        and getattr(popup, "_glass_window", None) is None)
            check(name + "_single_native_window", embedded)
            if not embedded or surface is None:
                popup.close()
                continue
            content = window.contentView()
            siblings = list(content.superview().subviews())
            check(name + "_material_below_qt", siblings.index(surface.view) < siblings.index(content))
            original = popup.pos()
            popup.move(original.x() + 12, original.y() + 16)
            observe(0.05)
            check(name + "_moves_as_one_window", surface.view.window() == content_ns_window(popup))
            popup.move(original)

            group = main.frameGeometry().united(popup.frameGeometry()).adjusted(-24, -24, 24, 24)
            backdrop.setGeometry(group)
            backdrop.show()
            native = content_ns_window(backdrop)
            native.setHasShadow_(False)
            native.setLevel_(content_ns_window(main).level() - 1)
            native.setCollectionBehavior_(content_ns_window(main).collectionBehavior())
            native.orderFront_(None)
            scene = CaptureScene(main, popup, backdrop)
            scene.activate()
            for color_name, rgb in (("light", (245, 245, 245)), ("dark", (42, 42, 42)),
                                    ("warm", (184, 142, 114)), ("cool", (118, 162, 190))):
                backdrop.color = QColor(*rgb)
                backdrop.update()
                observe(0.35)
                path = out / ("popup-" + name + "-" + color_name + ".png")
                capture = subprocess.run(["/usr/sbin/screencapture", "-x", "-o",
                    f"-R{group.x()},{group.y()},{group.width()},{group.height()}", str(path)],
                    capture_output=True, text=True, check=False)
                image = QImage(str(path))
                valid = capture.returncode == 0 and not image.isNull()
                check(name + "_" + color_name + "_capture", valid, capture.stderr or None)
                if not valid:
                    continue
                scale = image.width() / group.width()
                reference = sample_surface(image, main.x() - group.x(), main.y() - group.y(),
                                           main.width(), main.height(), scale)
                actual = sample_surface(image, popup.x() - group.x(), popup.y() - group.y(),
                                        popup.width(), popup.height(), scale)
                difference = max(abs(a - b) for a, b in zip(reference, actual))
                check(name + "_" + color_name + "_material_pixels", difference <= 8,
                      {"main_rgb": reference, "popup_rgb": actual, "max_difference": difference,
                       "tolerance": 8, "screenshot": str(path)})
                check(name + "_" + color_name + "_material_is_visible",
                      all(max(abs(a - b) for a, b in zip(sample, rgb)) > 8
                          for sample in (reference, actual)))
                # A blank/incorrect-region screenshot must not satisfy parity.
                corner = image.pixelColor(round(4 * scale), round(4 * scale)).getRgb()[:3]
                check(name + "_" + color_name + "_backdrop_visible",
                      max(abs(a - b) for a, b in zip(corner, rgb)) <= 3, {"actual": corner, "expected": rgb})
                if color_name == "light":
                    crop = image.copy(round((popup.x() - group.x()) * scale),
                                      round((popup.y() - group.y()) * scale),
                                      round(popup.width() * scale), round(popup.height() * scale))
                    bright = sum(crop.pixelColor(x, y).lightness() > 180
                                 for x in range(crop.width()) for y in range(crop.height()))
                    check(name + "_content_visible", 80 < bright < crop.width() * crop.height() // 4,
                          {"bright_pixels": bright})

            saved_levels = [(native_window, level) for native_window, level, hidden in scene.saved]
            scene.close()
            scene = None
            check(name + "_capture_restores_production_levels",
                  all(int(native_window.level()) == level for native_window, level in saved_levels),
                  {"sampling_uses_temporary_levels": True})
            backdrop.hide()
            popup.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            for _ in range(5):
                old_view = getattr(popup, "_glass_view", None)
                popup.hide()
                check(name + "_hide_detaches_material", old_view is not None and old_view.superview() is None)
                popup.show()
                check(name + "_reshow_recreates_material", wait(lambda popup=popup: bool(getattr(popup, "_bg_effect", False))))
            popup.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            popup.close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            check(name + "_destroy_clears_owner_reference", getattr(main, attr) is None)
            del popup, surface, content, window, siblings
            gc.collect()
            if baseline is None:
                baseline = len(list(AppKit.NSApp.windows()))

            # Drain real autorelease pools as well as Python references. A mock
            # cannot reproduce the historical objc_release crash on close.
            retained_views = []
            for _ in range(20):
                with objc.autorelease_pool():
                    open_popup()
                    popup = getattr(main, attr)
                    ready = popup is not None and wait(lambda popup=popup: bool(getattr(popup, "_bg_effect", False)))
                    if not ready:
                        check(name + "_stress_surface_ready", False)
                        break
                    view = getattr(popup, "_glass_view", None)
                    retained_views.append(view)
                    popup.close()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    app.processEvents()
                    check(name + "_stress_clears_owner_reference", getattr(main, attr) is None)
                    del popup
                gc.collect()
            check(name + "_stress_views_detached", len(retained_views) == 20 and
                  all(view is not None and view.superview() is None for view in retained_views))
            retained_views.clear()
            gc.collect()
            count = len(list(AppKit.NSApp.windows()))
            check(name + "_stress_no_window_growth", count <= baseline + 1,
                  {"baseline": baseline, "after": count})
    finally:
        if scene is not None:
            scene.close()
        backdrop.close()
