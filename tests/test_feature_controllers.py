from unittest import main

from sf_face_guard_controller import FaceGuardController
from sf_quote_presentation import QuotePresentationController
from sf_target_controller import TargetController

try:
    from _base import SFCase
except ImportError:
    from tests._base import SFCase


class TestFeatureControllers(SFCase):
    def test_quote_presentation_reset_clears_display_state(self):
        controller = QuotePresentationController()
        controller.state.quote = {"price": 1}
        controller.state.minute_points = [(1, 2)]
        controller.reset_for_stock()
        self.assertIsNone(controller.state.quote)
        self.assertEqual(controller.state.minute_points, [])
        self.assertTrue(controller.state.need_redraw)

    def test_target_controller_owns_alert_list(self):
        controller = TargetController()
        controller.alerts = [{"price": 1}]
        self.assertEqual(controller.diagnostics(), {"alerts": 1})
        controller.clear()
        self.assertEqual(controller.alerts, [])

    def test_face_guard_controller_reports_value_state(self):
        controller = FaceGuardController()
        controller.set_preference(True)
        controller.state.paused = True
        self.assertTrue(controller.diagnostics()["preference"])
        self.assertTrue(controller.diagnostics()["paused"])


if __name__ == "__main__":
    main()
