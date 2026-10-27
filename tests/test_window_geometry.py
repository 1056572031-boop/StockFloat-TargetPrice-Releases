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

    def test_padding_never_pushes_a_wide_window_past_the_right_edge(self):
        """窗口宽到放不下「右侧留白」时，也只能贴齐右沿，不能为了留白被推出可用区。

        旧写法的左侧下限是 available_left + padding：宽度 998 的窗口会被放到 x=4，
        右沿 1002 越出可用区 2 像素。
        """
        for width in (996, 997, 998, 999, 1000):
            x, _ = keep_on_screen_position(0, 100, width, 100, 0, 0, 1000, 800, padding=4)
            self.assertLessEqual(x + width, 1000, "宽度 %d 的窗口右沿越界" % width)
            self.assertGreaterEqual(x, 0, "宽度 %d 的窗口左沿越界" % width)

    def test_window_as_wide_as_the_area_stays_flush_left(self):
        self.assertEqual(
            keep_on_screen_position(0, 100, 1000, 100, 0, 0, 1000, 800, padding=4),
            (0, 100),
        )

    def test_window_wider_than_the_area_keeps_its_left_edge(self):
        """比可用区还宽时放不下，左沿优先（与无留白时一致）。"""
        self.assertEqual(
            keep_on_screen_position(50, 100, 1200, 100, 0, 0, 1000, 800, padding=4),
            (0, 100),
        )
        self.assertEqual(
            keep_on_screen_position(50, 100, 1200, 100, 0, 0, 1000, 800),
            (0, 100),
        )


if __name__ == "__main__":
    main()
