"""Short-lived content blur; native glass and window geometry stay untouched."""

import math

from PyQt6.QtCore import QObject, QRectF, Qt, QVariantAnimation
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene


def smooth_step(value):
    """Quintic easing with zero velocity and acceleration at both ends."""
    value = max(0.0, min(1.0, value))
    return value * value * value * (value * (value * 6.0 - 15.0) + 10.0)


class StockTransition(QObject):
    DURATION_MS = 800
    MAX_BLUR = 24.0
    BLUR_STEP = 3
    BLEND_START = 0.30
    BLEND_END = 0.70

    def __init__(self, owner):
        super().__init__(owner)
        self.progress = 1.0
        self.old_content = QImage()
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(self.DURATION_MS)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.valueChanged.connect(self.advance)
        self.animation.finished.connect(self.stop)
        self.scene = QGraphicsScene(self)
        self.item = QGraphicsPixmapItem()
        self.scene.addItem(self.item)
        self.blur = QGraphicsBlurEffect()
        self.blur.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
        self.item.setGraphicsEffect(self.blur)
        self.owner = owner
        self.new_content = QImage()
        self.content_key = None
        self.old_blurs = {}
        self.new_blurs = {}

    @property
    def active(self):
        return not self.old_content.isNull()

    def start(self, content):
        self.animation.stop()
        self.old_content = content
        self.new_content = QImage()
        self.content_key = None
        self.old_blurs.clear()
        self.new_blurs.clear()
        self.progress = 0.0
        self.animation.start()

    def advance(self, value):
        self.progress = float(value)
        self.owner.update()

    def stop(self):
        self.animation.stop()
        self.progress = 1.0
        self.old_content = QImage()
        self.new_content = QImage()
        self.content_key = None
        self.old_blurs.clear()
        self.new_blurs.clear()
        self.item.setPixmap(QPixmap())
        self.owner.update()

    def frame_from(self, provider, key):
        if self.progress <= self.BLEND_START and self.active:
            return self.frame(self.old_content)
        if key != self.content_key or self.new_content.isNull():
            content = provider()
            self.content_key = key
        else:
            content = self.new_content
        return self.frame(content)

    def blurred_layer(self, source, cache, radius):
        if radius not in cache:
            scale = source.devicePixelRatio()
            working = source.scaled(round(source.width() / scale), round(source.height() / scale),
                                    Qt.AspectRatioMode.IgnoreAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
            working.setDevicePixelRatio(1.0)
            self.item.setPixmap(QPixmap.fromImage(working))
            self.blur.setBlurRadius(radius)
            blurred = QImage(working.size(), working.format())
            blurred.fill(Qt.GlobalColor.transparent)
            painter = QPainter(blurred)
            rect = QRectF(0, 0, working.width(), working.height())
            try:
                self.scene.render(painter, rect, rect)
            finally:
                painter.end()
            cache[radius] = blurred
        return cache[radius]

    def profile(self, progress):
        progress = max(0.0, min(1.0, progress))
        blend = smooth_step((progress - self.BLEND_START) / (self.BLEND_END - self.BLEND_START))
        # A single C2 envelope: no peak hold, and no abrupt acceleration at rest.
        radius = self.MAX_BLUR * math.sin(math.pi * progress) ** 3
        return blend, radius

    def blur_weights(self, radius):
        lower = math.floor(radius / self.BLUR_STEP) * self.BLUR_STEP
        fraction = (radius - lower) / self.BLUR_STEP
        weights = ((1.0 - fraction) ** 3 / 6.0,
                   (3.0 * fraction ** 3 - 6.0 * fraction ** 2 + 4.0) / 6.0,
                   (-3.0 * fraction ** 3 + 3.0 * fraction ** 2 + 3.0 * fraction + 1.0) / 6.0,
                   fraction ** 3 / 6.0)
        samples: dict[float, float] = {}
        # Nonnegative cubic B-spline weights keep brightness constant and make
        # kernel changes C2 continuous without stopping at each cached radius.
        for offset, weight in zip((-1, 0, 1, 2), weights):
            kernel = max(0, min(self.MAX_BLUR, lower + offset * self.BLUR_STEP))
            samples[kernel] = samples.get(kernel, 0.0) + weight
        return samples

    def frame(self, content):
        if not self.active:
            return content
        blend, radius = self.profile(self.progress)
        if self.new_content != content:
            self.new_content = content
            self.new_blurs.clear()
        result = QImage(content.size(), QImage.Format.Format_ARGB32_Premultiplied)
        result.setDevicePixelRatio(content.devicePixelRatio())
        result.fill(Qt.GlobalColor.transparent)
        blur_weight = smooth_step(radius / self.BLUR_STEP)
        samples = self.blur_weights(radius)
        painter = QPainter(result)
        try:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            # Blur is linear: cache each source layer, then crossfade the results.
            # Work in logical pixels and interpolate a small reusable blur pyramid.
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            for source, cache, opacity in ((self.old_content, self.old_blurs, 1.0 - blend),
                                            (content, self.new_blurs, blend)):
                if opacity <= 0:
                    continue
                if blur_weight < 1.0:
                    painter.setOpacity(opacity * (1.0 - blur_weight))
                    painter.drawImage(0, 0, source)
                if blur_weight > 0:
                    for kernel, weight in samples.items():
                        if weight > 0:
                            painter.setOpacity(opacity * weight * blur_weight)
                            painter.drawImage(0, 0, self.blurred_layer(source, cache, kernel))
        finally:
            painter.end()
        return result
