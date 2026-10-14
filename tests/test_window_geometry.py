from unittest import main

from sf_window_geometry import keep_on_screen_position

try:
    from _base import SFCase
except ImportError:
    from tests._base import SFCase


class TestWindowGeometry(SFCase):
    def test_already_visible_window_is_not_moved(self):
        self.assertEqual(
            keep_on_screen_position(100, 120, 200, 100, 0, 0, 1000, 800),
            (100, 120),
        )

    def test_all_four_edges_are_clamped(self):
        self.assertEqual(
            keep_on_screen_position(-50, -30, 200, 100, 0, 0, 1000, 800),
            (0, 0),
        )
        self.assertEqual(
            keep_on_screen_position(950, 750, 200, 100, 0, 0, 1000, 800),
            (800, 700),
        )

    def test_right_padding_does_not_change_left_or_top_contract(self):
        self.assertEqual(
            keep_on_screen_position(800, 100, 200, 100, 0, 0, 1000, 800, padding=4),
            (800, 100),
        )


if __name__ == "__main__":
    main()
