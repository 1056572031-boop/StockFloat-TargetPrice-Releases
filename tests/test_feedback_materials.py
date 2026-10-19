"""Feedback shares main-card surface pixels and safe popup lifecycle."""
import tempfile
from unittest.mock import Mock, patch

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QShowEvent
from PyQt6.QtWidgets import QApplication

import sf_glass
from sf_feedback_ui import FeedbackDialog
from tests.test_feedback_dialog import _FeedbackCase
from tests.test_feedback_pipeline import snapshot
from tests.test_popup_materials import SurfaceHost, render


class FeedbackMaterialTests(_FeedbackCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.owner = self.track_widget(SurfaceHost())
        self.dialog = self.track_widget(FeedbackDialog(
            self.owner, snapshot=snapshot(), storage_dir=temporary.name,
            endpoint="https://synthetic.invalid/v1/feedback"))

    def test_surface_matches_main_at_both_scales_and_all_backends(self):
        self.owner.resize(self.dialog.size())
        for effective, backend, mode in ((True, "liquid", "translucent"),
                                         (True, "legacy", "translucent"),
                                         (False, "none", "translucent"), (False, "none", "solid")):
            for appearance in ("light", "dark"):
                for scale in (1, 2):
                    for widget in (self.owner, self.dialog):
                        vars(widget).update(_bg_effect=effective, _glass_backend=backend, _bg_mode=mode)
                    with patch.object(sf_glass, "detect_system_appearance", return_value=appearance):
                        actual = render(self.dialog, sf_glass.paint_popup_surface, scale)
                        expected = render(self.owner, sf_glass.paint_popup_surface, scale)
                    self.assertEqual(actual, expected)
                    self.assertEqual(actual.pixelColor(0, 0).alpha(), 0)
        self.assertTrue(self.dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(self.dialog.isSizeGripEnabled())
        self.assertTrue(self.dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertIs(vars(self.dialog)["_surface_owner_ref"](), self.owner)
        self.assertIn("QPlainTextEdit{background:rgba", self.dialog.styleSheet())
        with patch("sf_feedback_ui.paint_popup_surface", wraps=sf_glass.paint_popup_surface) as paint:
            self.dialog.show()
            QApplication.processEvents()
            self.dialog.repaint()
            self.assertTrue(any(call.args[0] is self.dialog for call in paint.call_args_list))

    def test_hide_reopen_detaches_and_reinjects_without_losing_draft(self):
        self.dialog.description.setPlainText("synthetic retained draft")
        with patch.object(self.dialog, "_inject_glass") as inject:
            self.dialog.show()
            QApplication.processEvents()
            inject.assert_called_once()
            self.dialog.hide()
            self.assertTrue(vars(self.dialog)["_glass_pending"])
            self.dialog.show()
            QApplication.processEvents()
            self.assertEqual(inject.call_count, 2)
            # A redundant show event must not schedule another native layer.
            self.dialog.showEvent(QShowEvent())
            QApplication.processEvents()
            self.assertEqual(inject.call_count, 2)
        self.assertEqual(self.dialog.description.toPlainText(), "synthetic retained draft")

    def event(self, kind, point, button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton):
        return QMouseEvent(kind, QPointF(point), QPointF(self.dialog.mapToGlobal(point)), button, buttons,
                           Qt.KeyboardModifier.NoModifier)

    def test_title_drag_fallback_and_release_do_not_intercept_editor(self):
        self.dialog.show()
        QApplication.processEvents()
        start = self.dialog.pos()
        press = self.event(QEvent.Type.MouseButtonPress, QPoint(25, 25))
        for handle in (None, Mock(startSystemMove=Mock(return_value=False))):
            with patch.object(self.dialog, "windowHandle", return_value=handle):
                self.dialog.mousePressEvent(press)
            self.assertIsNotNone(vars(self.dialog)["_drag_origin"])
        self.dialog.mouseMoveEvent(self.event(QEvent.Type.MouseMove, QPoint(35, 30)))
        self.assertEqual(self.dialog.pos(), start + QPoint(10, 5))
        self.dialog.mouseReleaseEvent(self.event(QEvent.Type.MouseButtonRelease, QPoint(35, 30)))
        self.assertIsNone(vars(self.dialog)["_drag_origin"])
        self.dialog.mouseMoveEvent(self.event(QEvent.Type.MouseMove, QPoint(40, 35), buttons=Qt.MouseButton.NoButton))
        self.dialog.mousePressEvent(self.event(QEvent.Type.MouseButtonPress, QPoint(25, 150)))
        self.assertIsNone(vars(self.dialog)["_drag_origin"])
        with patch.object(self.dialog, "windowHandle", return_value=Mock(startSystemMove=Mock(return_value=True))):
            self.dialog.mousePressEvent(press)
        self.assertIsNone(vars(self.dialog)["_drag_origin"])
