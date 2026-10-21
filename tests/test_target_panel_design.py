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
        up_top = panel.input_up_group.mapTo(panel, QPoint()).y()
        dn_top = panel.input_dn_group.mapTo(panel, QPoint()).y()
        self.assertEqual(dn_top - up_top - panel.input_up_group.height(), TP.INPUT_ROW_GAP)
        for label in panel.findChildren(QLabel):
            if label.parentWidget() in (panel.row_up, panel.row_dn):
                self.assertTrue(label.font().bold())

    def test_full_direction_hides_entire_row_and_delete_restores_it(self):
        for direction, edit_name, row_name, other_name, prices in (
                (C.TARGET_DIR_ABOVE, "edit_up", "row_up", "row_dn", (120, 121, 122)),
                (C.TARGET_DIR_BELOW, "edit_dn", "row_dn", "row_up", (110, 111, 112))):
            with self.subTest(direction=direction):
                panel = self.panel()
                edit = getattr(panel, edit_name)
                row = getattr(panel, row_name)
                other = getattr(panel, other_name)
                for price in prices:
                    edit.setText(str(price))
                    panel._add(direction)  # discipline-ok(R5): 驱动现有新增动作，观察真实控件可见性
                self.app.processEvents()
                self.assertFalse(row.isVisible())
                self.assertFalse(edit.isVisible())
                self.assertTrue(other.isVisible())
                self.assertTrue(panel.target_input_area.isVisible())
                panel._remove_one(direction, prices[-1])  # discipline-ok(R5): 驱动删除后恢复的真实刷新链
                self.app.processEvents()
                self.assertTrue(row.isVisible())
                self.assertTrue(edit.isVisible())
                self.assertTrue(edit.isEnabled())
                self.assertTrue(other.isVisible())

    def test_all_six_hide_area_and_delete_restores_only_available_direction(self):
        saved = ([self.sample(target=value) for value in (120, 121, 122)]
                 + [self.sample(C.TARGET_DIR_BELOW, target=value) for value in (110, 111, 112)])
        for direction, available, full, value in (
                (C.TARGET_DIR_ABOVE, "row_up", "row_dn", 122),
                (C.TARGET_DIR_BELOW, "row_dn", "row_up", 112)):
            with self.subTest(direction=direction):
                panel = self.panel(saved)
                self.assertFalse(panel.target_input_area.isVisible())
                first = panel.list_box.itemAt(0).widget()
                full_top = first.mapTo(panel, QPoint()).y()
                panel._remove_one(direction, value)  # discipline-ok(R5): 验证满额删除后整区域恢复
                self.app.processEvents()
                self.assertTrue(panel.target_input_area.isVisible())
                self.assertTrue(getattr(panel, available).isVisible())
                self.assertFalse(getattr(panel, full).isVisible())
                first = panel.list_box.itemAt(0).widget()
                restored_top = first.mapTo(panel, QPoint()).y()
                self.assertEqual(restored_top - full_top, panel.target_input_area.height(),
                                 "隐藏新增区必须移除整块布局高度，不留下占位")
        empty_stock = self.panel(code="600519")
        self.assertTrue(empty_stock.row_up.isVisible())
        self.assertTrue(empty_stock.row_dn.isVisible())
        self.assertTrue(empty_stock.target_input_area.isVisible())

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
