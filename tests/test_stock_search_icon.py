# -*- coding: utf-8 -*-
"""The stock-replacement entry remains a hollow magnifier in both render paths."""
from types import SimpleNamespace
import unittest

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QImage, QPainter

import sf_constants as C
import sf_icons as I
import sf_ui_render as R
from tests._base import SFCase


draw_entry = R.RenderMixin._draw_edit_icon  # discipline-ok(R5): isolate the production paint path without window/network side effects


class TestStockSearchIcon(SFCase):
    @classmethod
    def setUpClass(cls):
        cls._app = cls.ensure_qapp()

    def _paint(self, fallback=False, disabled=False, brush=False):
        ratio = C.BTN_ICON_DEVICE_RATIO
        px = int(round(C.BTN_ICON_PT * ratio))
        image = QImage(px, px, QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if brush:
            painter.setBrush(QColor("white"))
        host = SimpleNamespace(_edit_pm=None if fallback else I.button_pixmaps()["edit"])
        try:
            draw_entry(host, painter, QRectF(0, 0, C.BTN_ICON_PT, C.BTN_ICON_PT), disabled)
        finally:
            painter.end()
        return image

    def _assert_magnifier(self, image):
        # Points are fractions of the resource box, independent of device ratio.
        def alpha(x, y):
            return image.pixelColor(int(image.width() * x), int(image.height() * y)).alpha()

        self.assertEqual(alpha(0.44, 0.44), 0, "The lens must remain hollow")
        self.assertGreater(alpha(0.44, 0.25), 20, "The lens rim must be visible")
        self.assertGreater(alpha(0.75, 0.75), 20, "The handle must extend down and right")
        self.assertEqual(alpha(0.25, 0.75), 0, "The old pencil diagonal must be absent")
        ink = [(x, y) for y in range(image.height()) for x in range(image.width())
               if image.pixelColor(x, y).alpha() > 20]
        span = max(x for x, _ in ink) - min(x for x, _ in ink) + 1
        self.assertLessEqual(span, image.width() * 0.75,
                             "The entry must retain the neighboring icons' visual scale")

    def test_regular_entry_has_a_lens_and_right_down_handle(self):
        self._assert_magnifier(self._paint())

    def test_fallback_has_the_same_shape_even_with_a_previous_fill_brush(self):
        self._assert_magnifier(self._paint(fallback=True, brush=True))

    def test_disabled_svg_keeps_the_shape_and_reduces_opacity(self):
        normal = self._paint()
        disabled = self._paint(disabled=True)
        self._assert_magnifier(disabled)
        normal_alpha = sum(normal.pixelColor(x, y).alpha()
                           for y in range(normal.height()) for x in range(normal.width()))
        disabled_alpha = sum(disabled.pixelColor(x, y).alpha()
                             for y in range(disabled.height()) for x in range(disabled.width()))
        self.assertAlmostEqual(disabled_alpha / normal_alpha, 0.3, delta=0.02)

    def test_disabled_fallback_keeps_a_hollow_lens(self):
        self._assert_magnifier(self._paint(fallback=True, disabled=True, brush=True))

    def test_normal_entry_uses_the_same_foreground_as_close(self):
        def brightest(image):
            return max((image.pixelColor(x, y)
                        for y in range(image.height()) for x in range(image.width())),
                       key=lambda color: color.alpha())

        search = brightest(self._paint())
        close = brightest(I.button_images()["close"])
        self.assertEqual(search.getRgb()[:3], close.getRgb()[:3])


if __name__ == "__main__":
    unittest.main()
