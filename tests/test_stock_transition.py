"""Pixel and input-path checks for the stock-change content transition."""

import math
from unittest.mock import Mock, patch

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QWidget

from sf_editor import EditorWindow
from sf_stock_transition import StockTransition
from sf_ui_render import RenderMixin
from stock_float_window import FloatWindow
from tests._base import SFCase


class ContentWindow(RenderMixin, QWidget):
    """Only replace data I/O; use the real switch entry and content renderer."""

    apply_stock = FloatWindow.apply_stock

    def __init__(self):
        super().__init__()
        self.code = "sh600519"
        self.color = QColor("red")
        self.resize(240, 180)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._partial_price = None
        self._stock_transition = StockTransition(self)

    def _apply_stock_now(self, code):
        self.code = code
        self.color = QColor("blue")
        return True

    def _paint_stock_content(self, painter, width):
        painter.fillRect(width // 3, self.height() // 3, 8, 48, self.color)

    def paintEvent(self, event):
        RenderMixin._paint_event_inner(self, event)  # discipline-ok(R5): exercise production paint routing

    def _glass_mask_flush(self):
        pass

    def _paint_background(self, painter, width):
        pass

    def _paint_buttons(self, painter, width):
        painter.fillRect(width - 20, 12, 5, 5, QColor("white"))


class StockTransitionTests(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()

    def setUp(self):
        super().setUp()
        self.win = self.track_widget(ContentWindow())
        # Private access is required to scrub animation time deterministically,
        # rather than depend on wall-clock timing or a loaded GUI event loop.
        self.transition = self.win._stock_transition  # discipline-ok(R5): controlled animation clock
        self.addCleanup(self.transition.stop)

    def image(self):
        return self.win._stock_content_image()  # discipline-ok(R5): transparent stock-content output

    def test_blur_spreads_pixels_then_returns_to_exact_new_content(self):
        for scale in (1, 2):
            with self.subTest(scale=scale):
                old = QImage(120 * scale, 100 * scale, QImage.Format.Format_ARGB32_Premultiplied)
                old.setDevicePixelRatio(scale)
                old.fill(Qt.GlobalColor.transparent)
                painter = QPainter(old)
                painter.fillRect(50, 20, 8, 50, QColor("red"))
                painter.end()
                new = old.copy()
                self.transition.start(old)
                self.assertEqual(self.transition.frame(new), old)
                self.transition.animation.setCurrentTime(self.transition.DURATION_MS // 2)
                blurred = self.transition.frame(new)
                self.assertGreater(blurred.pixelColor(46 * scale, 40 * scale).alpha(), 0)
                self.assertLess(blurred.pixelColor(54 * scale, 40 * scale).alpha(), 255)
                self.assertEqual(self.transition.item.pixmap().width(), new.width() // scale)
                self.transition.animation.setCurrentTime(self.transition.DURATION_MS)
                self.assertFalse(self.transition.active)
                self.assertEqual(self.transition.frame(new), new)
                self.assertTrue(self.transition.item.pixmap().isNull())

    def test_transition_lasts_eight_hundred_ms_and_releases_all_frame_buffers(self):
        self.transition.start(self.image())
        self.assertEqual(self.transition.animation.duration(), 800)
        self.transition.animation.setCurrentTime(799)
        self.assertTrue(self.transition.active)
        self.transition.animation.setCurrentTime(800)
        self.assertFalse(self.transition.active)
        self.assertTrue(self.transition.old_content.isNull())
        self.assertTrue(self.transition.item.pixmap().isNull())
        self.assertFalse(self.transition.old_blurs)
        self.assertFalse(self.transition.new_blurs)

    def test_peak_blur_is_stronger_than_previous_twelve_pixel_setting(self):
        image = self.image()
        self.transition.start(image)
        self.transition.animation.setCurrentTime(self.transition.DURATION_MS // 2)
        stronger = self.transition.frame(image)
        with patch.object(self.transition, "MAX_BLUR", 12.0):
            previous = self.transition.frame(image)
        scale = image.devicePixelRatio()
        center = QPoint(round((self.win.width() / 3 + 4) * scale),
                        round((self.win.height() / 3 + 24) * scale))
        self.assertLess(stronger.pixelColor(center).alpha(), previous.pixelColor(center).alpha())

    def test_kernel_interpolation_is_continuous_across_integer_radius(self):
        image = self.image()
        self.transition.start(image)
        crossing = math.asin((12.0 / self.transition.MAX_BLUR) ** (1.0 / 3.0)) / math.pi
        self.transition.advance(crossing - 0.00001)
        before = self.transition.frame(image)
        self.transition.advance(crossing + 0.00001)
        after = self.transition.frame(image)
        max_change = max(abs(before.pixelColor(x, y).alpha() - after.pixelColor(x, y).alpha())
                         for y in range(before.height()) for x in range(before.width()))
        self.assertLessEqual(max_change, 1, "Crossing a blur kernel must not cause a visible step")

    def test_progressive_blur_has_one_peak_and_no_plateau(self):
        radii = [self.transition.profile(index / 800.0)[1] for index in range(801)]
        self.assertAlmostEqual(radii[0], 0.0)
        self.assertAlmostEqual(radii[-1], 0.0)
        self.assertAlmostEqual(radii[400], self.transition.MAX_BLUR)
        self.assertTrue(all(left < right for left, right in zip(radii[:400], radii[1:401])))
        self.assertTrue(all(left > right for left, right in zip(radii[400:-1], radii[401:])))
        for before, after in zip(radii, reversed(radii)):
            self.assertAlmostEqual(before, after)

    def test_blur_and_content_easing_join_rest_without_acceleration_jumps(self):
        step = 0.00001
        profile = self.transition.profile
        for endpoint, direction in ((0.0, 1), (1.0, -1)):
            center = profile(endpoint)[1]
            first = profile(endpoint + direction * step)[1]
            second = profile(endpoint + direction * step * 2)[1]
            self.assertLess(abs((first - center) / step), 0.001)
            self.assertLess(abs((second - 2 * first + center) / step ** 2), 0.1)
        for endpoint, direction in ((self.transition.BLEND_START, 1),
                                    (self.transition.BLEND_END, -1)):
            center = profile(endpoint)[0]
            first = profile(endpoint + direction * step)[0]
            second = profile(endpoint + direction * step * 2)[0]
            self.assertLess(abs((first - center) / step), 0.001)
            self.assertLess(abs((second - 2 * first + center) / step ** 2), 0.1)

    def test_blur_spline_preserves_brightness_and_has_continuous_velocity_and_acceleration(self):
        weights = self.transition.blur_weights
        for index in range(241):
            samples = weights(index / 10.0)
            self.assertAlmostEqual(sum(samples.values()), 1.0)
            self.assertTrue(all(0 <= kernel <= self.transition.MAX_BLUR and weight >= 0
                                for kernel, weight in samples.items()))
        step = 0.0001
        for radius in range(3, 24, 3):
            samples = [weights(radius + offset * step) for offset in (-2, -1, 0, 1, 2)]
            for kernel in range(0, 25, 3):
                a, b, c, d, e = (sample.get(kernel, 0.0) for sample in samples)
                self.assertLess(abs((c - b) / step - (d - c) / step), 0.001)
                self.assertLess(abs((c - 2 * b + a) / step ** 2
                                    - (e - 2 * d + c) / step ** 2), 0.001)

    def test_source_and_blur_layers_are_reused_until_content_changes(self):
        image = self.image()
        self.transition.start(image)
        provider = Mock(return_value=image)
        self.transition.advance(0.1)
        self.transition.frame_from(provider, "first quote")
        provider.assert_not_called()
        self.transition.advance(0.6)
        with patch.object(self.transition.scene, "render", wraps=self.transition.scene.render) as render:
            first = self.transition.frame_from(provider, "first quote")
            renders = render.call_count
            second = self.transition.frame_from(provider, "first quote")
            self.assertGreater(renders, 0)
            self.assertEqual(render.call_count, renders)
            self.assertEqual(first, second)
            provider.assert_called_once()
            changed = image.copy()
            changed.fill(QColor("green"))
            provider.return_value = changed
            latest = self.transition.frame_from(provider, "second quote")
            self.assertEqual(provider.call_count, 2)
            self.assertGreater(render.call_count, renders)
            self.assertNotEqual(latest, first)

    def test_click_and_return_apply_immediately_and_animate(self):
        self.win.show()
        for use_return in (False, True):
            with self.subTest(use_return=use_return):
                self.win.code = "sh600519"
                self.transition.stop()
                editor = EditorWindow(self.win.code, self.win.apply_stock, QPoint(0, 0), self.win.width())
                editor.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
                try:
                    editor.show()
                    editor.entry.setText("000001")
                    with patch("sf_universe.match_confident", return_value=[("sz000001", "bank")]):
                        if use_return:
                            QTest.keyClick(editor.entry, Qt.Key.Key_Return)
                        else:
                            QTest.mouseClick(editor.ok, Qt.MouseButton.LeftButton)
                    self.assertEqual(self.win.code, "sz000001")
                    self.assertTrue(self.transition.active)
                    self.assertFalse(editor.isVisible())
                    self.transition.animation.setCurrentTime(self.transition.DURATION_MS)
                    self.assertEqual(self.transition.frame(self.image()), self.image())
                finally:
                    editor.close()
                    editor.deleteLater()

    def test_rapid_switch_restarts_from_visible_frame_not_stale_stock(self):
        self.win.show()
        self.win.apply_stock("sz000001")
        self.transition.animation.setCurrentTime(self.transition.DURATION_MS // 2)
        visible = self.transition.frame(self.image())
        self.win.apply_stock("sh600183")
        self.assertEqual(self.win.code, "sh600183")
        self.assertEqual(self.transition.frame(self.image()), visible)
        self.transition.animation.setCurrentTime(self.transition.DURATION_MS)
        self.assertFalse(self.transition.active)

    def test_network_state_change_invalidates_transition_content(self):
        self.win.show()
        self.win.apply_stock("sz000001")
        self.transition.animation.pause()
        self.transition.advance(0.6)
        # Observe cache regeneration through the renderer; the fixture replaces business drawing.
        with patch.object(self.win, "_stock_content_image", wraps=self.win._stock_content_image) as provider:  # discipline-ok(R5): must observe transition cache provider
            self.win.grab()
            before = provider.call_count
            with patch.object(self.win, "_net_ok", False, create=True):
                self.win.grab()
            self.assertGreater(provider.call_count, before)

    def test_hidden_window_skips_animation(self):
        self.win.apply_stock("sz000001")
        self.assertFalse(self.transition.active)

    def test_same_stock_does_not_start_animation(self):
        self.win.show()
        self.win.apply_stock(self.win.code)
        self.assertFalse(self.transition.active)

    def test_capture_failure_does_not_block_switch(self):
        self.win.show()
        with patch.object(self.win, "_stock_content_image", side_effect=RuntimeError("capture failed")), \
                patch("stock_float_window.log_swallow") as log:
            self.assertTrue(self.win.apply_stock("sz000001"))
        self.assertEqual(self.win.code, "sz000001")
        self.assertFalse(self.transition.active)
        log.assert_called_once()

    def test_blur_failure_falls_back_to_visible_new_content(self):
        self.win.show()
        self.win.apply_stock("sz000001")
        with patch.object(self.transition, "frame", side_effect=RuntimeError("blur failed")), \
                patch("sf_ui_render.log_swallow") as log:
            image = self.win.grab().toImage()
        scale = image.devicePixelRatio()
        self.assertEqual(image.pixelColor(round(self.win.width() / 3 * scale),
                                         round(self.win.height() / 3 * scale)), QColor("blue"))
        self.assertFalse(self.transition.active)
        log.assert_called_once()

    def test_controls_stay_sharp_and_corners_transparent_at_peak_blur(self):
        self.win.show()
        self.win.apply_stock("sz000001")
        self.transition.animation.setCurrentTime(self.transition.DURATION_MS // 2)
        image = self.win.grab().toImage()
        scale = image.devicePixelRatio()
        self.assertEqual(image.pixelColor(round((self.win.width() - 18) * scale),
                                         round(14 * scale)), QColor("white"))
        for x, y in ((0, 0), (image.width() - 1, 0),
                     (0, image.height() - 1), (image.width() - 1, image.height() - 1)):
            self.assertEqual(image.pixelColor(x, y).alpha(), 0)
