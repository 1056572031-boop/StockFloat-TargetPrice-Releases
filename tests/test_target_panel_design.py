from unittest.mock import patch

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QLabel

import sf_constants as C
import sf_ui_render
import target_panel as TP
from tests._base import SFCase


class ApprovedPanelDesign(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()
        sf_ui_render.load_bundled_fonts()

    def panel(self, saved=(), price=117.60, code="600183"):
        self.price = price
        with patch.object(TP, "load_alerts", return_value=list(saved)):
            panel = TP.TargetPanel(code, "生益科技", lambda alerts: True, lambda: True,
                                   current_price_fn=lambda: self.price)
        self.track_widget(panel)
        panel.show()
        self.app.processEvents()
        return panel

    def sample(self, direction=C.TARGET_DIR_ABOVE, state="pending", target=121.00):
        return {"direction": direction, "state": state, "target": target}

    def right_edge(self, widget, panel):
        return widget.mapTo(panel, QPoint(widget.width(), 0)).x()

    def test_input_groups_and_actions_share_right_edge(self):
        panel = self.panel([self.sample()])
        edge = panel.width() - TP.MARGIN_X
        for widget in (panel.input_up_group, panel.input_dn_group,
                       panel.btn_up, panel.btn_dn, panel.btn_apply,
                       *panel.findChildren(TP.DeleteIconButton)):
            self.assertEqual(self.right_edge(widget, panel), edge)
        self.assertEqual(panel.btn_clear.width(), panel.btn_apply.width())
        self.assertEqual(panel.btn_clear.height(), TP.BUTTON_H)
        self.assertEqual(panel.btn_apply.height(), TP.BUTTON_H)
        self.assertEqual(panel.edit_up.placeholderText(), "多少钱或 +5%")
        self.assertEqual(panel.edit_dn.placeholderText(), "多少钱或 -5%")
        self.assertEqual(panel.btn_up.width(), panel.btn_up.height())
        self.assertEqual(panel.btn_up.height(), panel.input_up_group.height())
        self.assertEqual(panel.input_dn_group.y() - panel.input_up_group.y() - panel.input_up_group.height(), TP.INPUT_ROW_GAP)
        for label in panel.findChildren(QLabel):
            if label.parent() is panel and label.text() in ("涨到", "跌到"):
                self.assertTrue(label.font().bold())

    def test_distance_text_is_white_and_hit_pill_is_black_regular(self):
        for direction in (C.TARGET_DIR_ABOVE, C.TARGET_DIR_BELOW):
            panel = self.panel([self.sample(direction, "hit")])
            detail = panel.findChild(QLabel, "targetDistanceLabel").parentWidget()
            marker = panel.findChild(QLabel, "targetHit")
            for label in detail.findChildren(QLabel):
                expected = QColor(C.TARGET_HIT_TEXT_HEX if label is marker else C.FG_BRIGHT_HEX)
                self.assertEqual(label.palette().color(QPalette.ColorRole.WindowText), expected)
                center = label.geometry().center().y()
                self.assertLessEqual(abs(center - marker.geometry().center().y()), 1)
            self.assertFalse(marker.font().bold())
            self.assertEqual(marker.palette().color(QPalette.ColorRole.Window), QColor(C.FG_DIM_HEX))
            for name in ("targetDirection", "targetPrice", "targetPct"):
                label = panel.findChild(QLabel, name)
                self.assertEqual(label.palette().color(QPalette.ColorRole.WindowText), QColor(C.FG_HINT_HEX))

    def test_live_distance_changes_without_losing_draft(self):
        panel = self.panel([self.sample()])
        panel.edit_dn.setText("110.25")
        delete = panel.findChildren(TP.DeleteIconButton)[0]
        delete.setFocus()
        delete.setDown(True)
        self.price = 120.0
        def distance_changed():
            self.app.processEvents()
            return panel.findChild(QLabel, "targetDistance").text() == "1.00"
        self.assertTrue(self.wait_for(distance_changed))
        self.assertEqual(panel.edit_dn.text(), "110.25")
        self.assertIs(panel.findChildren(TP.DeleteIconButton)[0], delete)
        self.assertTrue(delete.isDown())
        self.assertTrue(delete.hasFocus())
        self.assertEqual(panel.findChild(QLabel, "targetPct").text(), "+0.8%")

    def test_unavailable_price_and_long_values_fit_with_hit_visible(self):
        for price, target in ((None, 121.0), (117.60, 1e308), (4.62, 4.621)):
            panel = self.panel([self.sample(state="hit", target=target)], price=price,
                               code="510300" if target == 4.621 else "600183")
            marker = panel.findChild(QLabel, "targetHit")
            self.assertEqual(marker.text(), TP.HIT_MARKER_TEXT)
            for label in panel.findChildren(QLabel):
                point = label.mapTo(panel, QPoint(label.width(), label.height()))
                self.assertLessEqual(point.x(), panel.width() - TP.MARGIN_X)
                self.assertLessEqual(point.y(), panel.height() - TP.MARGIN_Y)
            if price is None:
                self.assertEqual(panel.findChild(QLabel, "targetDistance").text(), "--")
            if target == 4.621:
                self.assertEqual(panel.findChild(QLabel, "targetDistance").text(), "0.001")

    def test_refresh_failure_is_logged_and_timer_keeps_running(self):
        panel = self.panel([self.sample()])
        failure = RuntimeError("price unavailable")
        with patch.object(panel, "sync_alerts", side_effect=failure), patch.object(TP, "log_swallow") as logged:
            self.assertTrue(self.wait_for(lambda: self.app.processEvents() or logged.called))
            logged.assert_called_with("target_panel._refresh_distance", failure)
        self.price = 120.0
        def recovered():
            self.app.processEvents()
            return panel.findChild(QLabel, "targetDistance").text() == "1.00"
        self.assertTrue(self.wait_for(recovered))

    def test_hit_transition_preserves_pressed_delete_and_focus(self):
        target = self.sample()
        panel = self.panel([target])
        delete = panel.findChildren(TP.DeleteIconButton)[0]
        delete.setFocus()
        delete.setDown(True)
        panel.sync_alerts([dict(target, state="hit")])
        self.app.processEvents()
        self.assertIs(panel.findChildren(TP.DeleteIconButton)[0], delete)
        self.assertTrue(delete.hasFocus())
        self.assertTrue(delete.isDown())
        marker = panel.findChild(QLabel, "targetHit")
        self.assertTrue(marker.isVisible())
        for name in ("targetDirection", "targetPrice", "targetPct"):
            self.assertEqual(panel.findChild(QLabel, name).palette().color(QPalette.ColorRole.WindowText),
                             QColor(C.FG_HINT_HEX))
        delete.setDown(False)
        delete.click()
        self.app.processEvents()
        self.assertFalse(panel.findChild(QLabel, "targetHit").isVisible())

    def test_design_typography_placeholders_and_spacing(self):
        panel = self.panel([self.sample()])
        for button in (panel.btn_clear, panel.btn_apply):
            self.assertEqual(button.width(), (panel.width() - 2 * TP.MARGIN_X - TP.BUTTON_GAP) // 2)
            self.assertEqual(button.font(), panel.btn_apply.font())
            self.assertEqual(button.palette().color(QPalette.ColorRole.ButtonText), QColor(C.FG_BRIGHT_HEX))
        for edit, color in ((panel.edit_up, C.TARGET_PLACEHOLDER_UP_HEX),
                            (panel.edit_dn, C.TARGET_PLACEHOLDER_DOWN_HEX)):
            self.assertEqual(edit.palette().color(QPalette.ColorRole.PlaceholderText), QColor(color))
        distance = panel.findChild(QLabel, "targetDistance")
        unit = panel.findChild(QLabel, "targetDistanceUnit")
        self.assertEqual(unit.x() - distance.geometry().right() - 1, 2)
        price = panel.findChild(QLabel, "targetPrice")
        pct = panel.findChild(QLabel, "targetPct")
        def baseline(label):
            return label.y() + (label.height() - label.fontMetrics().height()) / 2 + label.fontMetrics().ascent()
        self.assertLessEqual(abs(baseline(price) - baseline(pct)), 1)

    def test_delete_preserves_other_rows_and_apply_draft(self):
        panel = self.panel([self.sample(), self.sample(C.TARGET_DIR_BELOW, target=113.63)])
        panel.findChildren(TP.DeleteIconButton)[0].click()
        self.app.processEvents()
        values = [label.toolTip() for label in panel.findChildren(QLabel, "targetPrice") if label.isVisible()]
        self.assertEqual(values, ["113.63"])
        self.assertTrue(panel.btn_apply.isEnabled())
        self.assertTrue(panel.has_unsaved_changes())
