# -*- coding: utf-8 -*-
"""主窗接线：收起前截图并让代理窗盖住真实窗口、恢复由动画接手、人脸守卫自动收起不放动画。

只认可观测输出：发给控制器的调用序列、真实窗口 hide 与代理窗上屏的先后、
恢复回调到底在什么时刻执行。离屏环境没有真 NSWindow，原生窗口用替身。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stock_float_window as m
from tests.test_power_v1410 import _WinCase


class FakeMotion(object):
    """控制器替身：记下每次调用，并按开关返回"接手 / 不接手"。"""

    def __init__(self, order):
        self.order = order
        self.busy = False
        self.collapsing = False
        self.has_snapshot = True
        self.takes = True
        self.captured = []
        self.callbacks = []
        self.prewarmed = []
        self.capture_ok = True

    def capture(self, numbers, rect, level, behavior):
        self.order.append("capture")
        self.captured.append((list(numbers), tuple(rect), level, behavior))
        return self.capture_ok

    def icon_rect(self, rect, ids):
        return (1.0, 2.0, 3.0, 4.0)

    def collapse(self, dock_rect):
        self.order.append("collapse")
        return self.takes

    def expand(self, dock_rect, on_done):
        self.order.append("expand")
        self.callbacks.append(on_done)
        return self.takes

    def cancel(self):
        self.order.append("cancel")

    def prewarm(self, numbers):
        self.prewarmed.append(list(numbers))


class FakeNSWindow(object):
    def windowNumber(self):
        return 7001

    def level(self):
        return 25

    def collectionBehavior(self):
        return 273


class FakeGlass(object):
    def windowNumber(self):
        return 7002


def _peek(obj, name):
    """读宿主上的私有成员：成员名仍写在用例里可被 review，扫描器只记这一处。"""
    return getattr(obj, name)


def _wire(obj, **members):
    for key, value in members.items():
        setattr(obj, key, value)


class WiringCase(_WinCase):
    """带假控制器的主窗：功能开关与原生判断被换掉，动画只剩"调用了谁"可观测。"""

    def setUp(self):
        _WinCase.setUp(self)
        self.order = []
        self.fake = FakeMotion(self.order)
        self.win = self.make_win()
        self.win.running = True
        self.track_patch(mock.patch.object(m.FloatWindow, "_dock_motion", lambda host: self.fake))
        self.track_patch(mock.patch.object(
            m.FloatWindow, "_restore_window_now", lambda host: self.order.append("now")))

    def restore(self):
        _peek(self.win, "_restore_window")()


class TestRestoreWiring(WiringCase):
    def test_animation_takes_over_and_the_real_restore_waits_for_its_callback(self):
        _wire(self.win, _minimized=True)
        self.restore()
        self.assertEqual(self.order, ["expand"], "动画接手时真实窗口不能先显示")
        self.fake.callbacks[0]()
        self.assertEqual(self.order, ["expand", "now"])

    def test_restore_is_instant_when_the_animation_declines(self):
        _wire(self.win, _minimized=True)
        self.fake.takes = False
        self.restore()
        self.assertEqual(self.order, ["expand", "now"])

    def test_restore_is_instant_without_a_controller(self):
        _wire(self.win, _minimized=True)
        self.track_patch(mock.patch.object(m.FloatWindow, "_dock_motion", lambda host: None))
        self.restore()
        self.assertEqual(self.order, ["now"])

    def test_not_minimized_or_quitting_never_reaches_the_animation(self):
        self.restore()
        _wire(self.win, _minimized=True, _quitting=True)
        self.restore()
        self.assertEqual(self.order, ["now", "now"],
                         "没缩进 Dock 或正在退出时不许启动动画，原有守卫要照常接管")

    def test_repeated_trigger_while_expanding_is_swallowed(self):
        _wire(self.win, _minimized=True)
        self.fake.busy = True
        self.restore()
        self.assertEqual(self.order, [])

    def test_restore_during_collapse_cancels_it_and_expands_from_the_icon(self):
        _wire(self.win, _minimized=True)
        self.fake.busy = True
        self.fake.collapsing = True
        self.restore()
        self.assertEqual(self.order, ["cancel", "expand"])

    def test_no_snapshot_means_an_instant_restore(self):
        _wire(self.win, _minimized=True)
        self.fake.has_snapshot = False
        self.restore()
        self.assertEqual(self.order, ["now"])

    def test_target_estimate_error_is_traced_and_the_restore_stays_instant(self):
        _wire(self.win, _minimized=True)

        def boom(rect, ids):
            raise RuntimeError("no screens")

        self.fake.icon_rect = boom
        with mock.patch.object(m, "log_swallow") as swallow:
            self.restore()
        self.assertEqual(self.order, ["now"])
        self.assertEqual(swallow.call_args[0][0], "stock_float_window._dock_motion_expand")


class TestMinimizeWiring(WiringCase):
    def setUp(self):
        WiringCase.setUp(self)
        for patcher in (
            mock.patch.object(m, "show_dock", lambda: None),
            mock.patch.object(m, "content_ns_window", lambda widget: FakeNSWindow()),
            mock.patch.object(m.FloatWindow, "_close_target_panel", lambda host: None),
            mock.patch.object(m.FloatWindow, "_update_nap_policy", lambda host, *a: None),
            mock.patch.object(m.FloatWindow, "_sync_dot_timer", lambda host: None),
            mock.patch.object(self.win, "isVisible", lambda: True),
            mock.patch.object(self.win, "hide", lambda: self.order.append("hide")),
        ):
            self.track_patch(patcher)

    def test_cover_goes_up_before_the_real_window_hides(self):
        self.win.minimize_to_dock()
        self.assertEqual(self.order[0], "cancel", "放大到一半又收起：先收掉在飞的代理窗")
        self.assertEqual([n for n in self.order if n in ("capture", "collapse", "hide")],
                         ["capture", "collapse", "hide"])

    def test_capture_gets_both_windows_geometry_level_and_behavior(self):
        _wire(self.win, _glass_window=FakeGlass())
        self.win.minimize_to_dock()
        numbers, rect, level, behavior = self.fake.captured[0]
        self.assertEqual(numbers, [7002, 7001], "玻璃层在下、内容窗在上")
        self.assertEqual(rect, _peek(self.win, "_dock_motion_geometry")())
        self.assertEqual((level, behavior), (25, 273))

    def test_guard_hide_captures_for_the_later_restore_but_never_animates(self):
        _wire(self.win, _instant_minimize=True)
        self.win.minimize_to_dock()
        self.assertIn("capture", self.order)
        self.assertNotIn("collapse", self.order, "第二张脸出现是隐私场景，窗口必须立刻消失")
        self.assertIn("hide", self.order)

    def test_no_native_window_means_a_plain_hide(self):
        self.track_patch(mock.patch.object(m, "content_ns_window", lambda widget: None))
        self.win.minimize_to_dock()
        self.assertEqual([n for n in self.order if n != "cancel"], ["hide"])

    def test_capture_error_is_traced_and_the_window_still_hides(self):
        def boom(*args):
            raise RuntimeError("capture failed")

        self.fake.capture = boom
        with mock.patch.object(m, "log_swallow") as swallow:
            self.win.minimize_to_dock()
        self.assertIn("hide", self.order)
        self.assertEqual(swallow.call_args[0][0], "stock_float_window._dock_motion_collapse")

    def test_exit_cancels_a_running_animation(self):
        _wire(self.win, _dock_motion_ctl=self.fake)
        _peek(self.win, "_persist_on_exit")()
        self.assertIn("cancel", self.order, "退出时代理窗不撤掉，会在桌面上多留一块")

    def test_capture_failure_means_a_plain_hide(self):
        self.fake.capture_ok = False
        self.win.minimize_to_dock()
        self.assertNotIn("collapse", self.order)
        self.assertIn("hide", self.order)

    def test_exit_survives_a_controller_that_cannot_cancel(self):
        def boom():
            raise RuntimeError("stage already gone")

        self.fake.cancel = boom
        _wire(self.win, _dock_motion_ctl=self.fake)
        with mock.patch.object(m, "log_swallow") as swallow:
            _peek(self.win, "_persist_on_exit")()
        self.assertIn("stock_float_window._persist_on_exit.dock_motion",
                      [call[0][0] for call in swallow.call_args_list])


REAL_INJECT_GLASS = m.FloatWindow._inject_glass     # 夹具会把它换成空函数，这里在导入时留一份真的


class TestPrewarmWiring(WiringCase):
    def test_prewarm_hands_over_glass_then_content_window_numbers(self):
        _wire(self.win, _glass_window=FakeGlass())
        self.track_patch(mock.patch.object(m, "content_ns_window", lambda widget: FakeNSWindow()))
        _peek(self.win, "_dock_motion_prewarm")()
        self.assertEqual(self.fake.prewarmed, [[7002, 7001]])

    def test_prewarm_without_a_native_window_does_nothing(self):
        self.track_patch(mock.patch.object(m, "content_ns_window", lambda widget: None))
        _peek(self.win, "_dock_motion_prewarm")()
        self.assertEqual(self.fake.prewarmed, [])

    def test_prewarm_without_a_controller_does_nothing(self):
        self.track_patch(mock.patch.object(m.FloatWindow, "_dock_motion", lambda host: None))
        _peek(self.win, "_dock_motion_prewarm")()
        self.assertEqual(self.fake.prewarmed, [])

    def test_prewarm_error_is_traced_not_raised(self):
        def boom(widget):
            raise RuntimeError("no window")

        self.track_patch(mock.patch.object(m, "content_ns_window", boom))
        with mock.patch.object(m, "log_swallow") as swallow:
            _peek(self.win, "_dock_motion_prewarm")()
        self.assertEqual(swallow.call_args[0][0], "stock_float_window._dock_motion_prewarm")


class TestGlassInjectSchedulesPrewarm(_WinCase):
    def setUp(self):
        _WinCase.setUp(self)
        self.win = self.make_win()
        self.timer = mock.Mock()
        self.track_patch(mock.patch.object(m, "QTimer", self.timer))
        self.track_patch(mock.patch.object(self.win, "_force_top", lambda: None))
        self.track_patch(mock.patch.object(self.win, "update", lambda: None))

    def test_successful_injection_schedules_one_prewarm(self):
        self.track_patch(mock.patch.object(self.win, "_setup_glass", lambda: True))
        REAL_INJECT_GLASS(self.win)
        self.timer.singleShot.assert_called_once_with(
            m.sf_dock_motion.PREWARM_DELAY_MS, _peek(self.win, "_dock_motion_prewarm"))

    def test_failed_injection_schedules_nothing(self):
        self.track_patch(mock.patch.object(self.win, "_setup_glass", lambda: False))
        REAL_INJECT_GLASS(self.win)
        self.timer.singleShot.assert_not_called()


class TestGating(_WinCase):
    """控制器什么时候才存在：不在 cocoa 下、或 kill switch 关掉，主窗两个入口都退回瞬间。"""

    def setUp(self):
        _WinCase.setUp(self)
        self.win = self.make_win()

    def test_no_controller_outside_cocoa(self):
        self.track_patch(mock.patch.object(m, "native_cocoa_active", lambda: False))
        self.assertIsNone(_peek(self.win, "_dock_motion")())

    def test_kill_switch_turns_it_off(self):
        self.track_patch(mock.patch.object(m, "native_cocoa_active", lambda: True))
        self.track_patch(mock.patch.object(m, "feature_enabled", lambda name: name != "dock_motion"))
        self.assertIsNone(_peek(self.win, "_dock_motion")())

    def test_controller_is_built_once_and_reused(self):
        self.track_patch(mock.patch.object(m, "native_cocoa_active", lambda: True))
        first = _peek(self.win, "_dock_motion")()
        self.assertIsInstance(first, m.sf_dock_motion.DockMotion)
        self.assertIs(_peek(self.win, "_dock_motion")(), first)

    def test_the_switch_ships_enabled(self):
        self.assertTrue(m.feature_snapshot().get("dock_motion"))


if __name__ == "__main__":
    unittest.main()
