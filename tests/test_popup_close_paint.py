"""Popup close feedback must be antialiased and device-pixel sharp."""

from PyQt6.QtCore import QPoint, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPen
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QWidget

from sf_editor import MainCloseButton
from sf_icons import _ICON_SVG, svg_pixmap
from tests._base import SFCase


def render_button(button, scale):
    image = QImage(button.width() * scale, button.height() * scale,
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(scale)
    image.fill(0)
    painter = QPainter(image)
    try:
        button.render(painter, QPoint(), flags=QWidget.RenderFlag.DrawChildren)
    finally:
        painter.end()
    return image


def reference(size, scale, state, antialias=True):
    image = QImage(size * scale, size * scale, QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(scale)
    image.fill(0)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, antialias)
        if state in ("hover", "pressed"):
            painter.setBrush(QColor(255, 255, 255, 84 if state == "pressed" else 58))
            painter.setPen(QPen(QColor(255, 255, 255, 132 if state == "pressed" else 108), 1))
            painter.drawRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1), 6, 6)
        if state == "disabled":
            painter.setOpacity(0.3)
        icon = svg_pixmap(_ICON_SVG["close"], 12 * scale)  # discipline-ok(R5): Compare the actual icon at destination pixels.
        painter.drawImage(QRectF((size - 12) / 2, (size - 12) / 2, 12, 12), icon)
    finally:
        painter.end()
    return image


class PopupClosePaintingTests(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()

    def test_all_states_match_antialiased_reference_at_each_scale_and_size(self):
        button = self.track_widget(MainCloseButton())
        for size in (20, 26):
            button.setFixedSize(size, size)
            for scale in (1, 2):
                for state in ("idle", "hover", "pressed", "disabled"):
                    with self.subTest(size=size, scale=scale, state=state):
                        button.setEnabled(state != "disabled")
                        button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, state != "idle")
                        button.setDown(state == "pressed")
                        actual = render_button(button, scale)
                        self.assertEqual(actual, reference(size, scale, state))
                        self.assertEqual(actual.pixelColor(0, 0).alpha(), 0)

    def test_jagged_hover_is_not_accepted_as_the_reference(self):
        for scale in (1, 2):
            self.assertNotEqual(reference(20, scale, "hover"),
                                reference(20, scale, "hover", antialias=False))

    def test_hover_state_changes_the_picture_and_click_still_works(self):
        """换图与点击用显式状态驱动（与本文件的矩阵那条同口径），不受别人留什么状态影响。"""
        host = self.track_widget(QWidget())
        host.resize(100, 80)
        button = MainCloseButton(host)
        button.move(20, 20)
        host.show()
        self.app.processEvents()
        clicked = []
        button.clicked.connect(lambda: clicked.append(True))
        button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
        hovered = render_button(button, 2)
        button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
        self.assertNotEqual(hovered, render_button(button, 2))
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.assertEqual(clicked, [True])

    def test_real_cursor_moves_are_tracked_when_the_platform_delivers_enter(self):
        """真鼠标移动要跟着换状态，但只有平台确实投递 Enter 时才断言。

        `underMouse()` 是全应用一份的游标归属状态：随机顺序档里，前面别的用例留下的
        窗口激活状态会让真实 mouseMove 收不到 Enter（2026-10-21 实测 seed=1464 红在
        这里，而单跑与默认顺序都绿）。收不到就带理由跳过 —— 既不伪装成通过，
        也不让「别的用例恰好留了什么」决定这条红不红。
        """
        host = self.track_widget(QWidget())
        host.resize(100, 80)
        button = MainCloseButton(host)
        button.move(20, 20)
        host.show()
        self.app.processEvents()
        host.activateWindow()
        self.app.processEvents()
        clicked = []
        button.clicked.connect(lambda: clicked.append(True))
        QTest.mouseMove(button, button.rect().center())
        self.app.processEvents()
        if not button.underMouse():
            self.skipTest("该平台未把鼠标进入事件投递给子控件（离屏/无窗口服务环境）")
        hovered = render_button(button, 2)
        QTest.mouseMove(host, QPoint(80, 60))
        self.app.processEvents()
        self.assertFalse(button.underMouse())
        self.assertNotEqual(hovered, render_button(button, 2))
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.assertEqual(clicked, [True])
