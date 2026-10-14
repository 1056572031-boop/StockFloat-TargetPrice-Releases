from unittest import main

from sf_window_state import FaceGuardValueState, LifecycleState, QuoteState, UpdateFlowState

try:
    from _base import SFCase
except ImportError:
    from tests._base import SFCase


class TestWindowState(SFCase):
    def test_state_objects_have_independent_mutable_defaults(self):
        first = QuoteState()
        second = QuoteState()
        first.minute_points.append((1, 2))
        self.assertEqual(second.minute_points, [])

        first_face = FaceGuardValueState()
        second_face = FaceGuardValueState()
        first_face.last_stop["reason"] = "closed"
        self.assertEqual(second_face.last_stop["reason"], "")

    def test_lifecycle_and_update_state_are_plain_values(self):
        lifecycle = LifecycleState()
        update = UpdateFlowState()
        self.assertTrue(lifecycle.running)
        self.assertFalse(lifecycle.quitting)
        self.assertFalse(update.checking)
        self.assertFalse(update.downloading)


if __name__ == "__main__":
    main()
