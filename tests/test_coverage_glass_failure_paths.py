"""Native glass orchestration failures, without allocating native windows.

Run-loop and WindowServer adapters are isolated. The real follow-timer and
re-top orchestration methods run, with ordering and cleanup asserted directly.
The real allocation path remains in the existing Cocoa subprocess harness.
"""
from __future__ import annotations

import pathlib
import sys
import types
import unittest
from unittest import mock

import AppKit
import CoreFoundation as cf
import Quartz

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from _base import SFCase
except ImportError:  # pragma: no cover
    from tests._base import SFCase

import sf_glass as glass

follow_begin = glass.GlassMixin._glass_follow_begin  # discipline-ok(R5): native timer registration entry
follow_end = glass.GlassMixin._glass_follow_end  # discipline-ok(R5): timer cleanup contract
force_top = glass.GlassMixin._force_top  # discipline-ok(R5): re-top orchestration under test
relative_order = glass.GlassMixin._order_glass_below_content  # discipline-ok(R5): final compositor invariant
reassert_top = glass.GlassMixin._reassert_top  # discipline-ok(R5): forced re-top latch contract
flush_mask = glass.GlassMixin._glass_mask_flush  # discipline-ok(R5): same-frame mask convergence
TIME = 1000.0
MAIN_NUMBER, GLASS_NUMBER = 47, 48
MODEL_WIDTH, MODEL_HEIGHT = 184, 224


def host(**state):
    return types.SimpleNamespace(**state)


class NativeWindow:
    def __init__(self, events, label, number, fail=()):
        self.events, self.label, self.number = events, label, number
        self.fail = set(fail)
        self.level_value = 0
        self.active = False
        self.visible = True

    def event(self, name):
        self.events.append(self.label + ":" + name)
        if name in self.fail:
            raise RuntimeError(self.label + ":" + name + " unavailable")

    def windowNumber(self):
        self.event("number")
        return self.number

    def level(self):
        return self.level_value

    def setLevel_(self, value):
        self.event("level")
        self.level_value = value

    def setHidesOnDeactivate_(self, value):
        self.event("hides")

    def setCollectionBehavior_(self, value):
        self.event("behavior")

    def setBecomesKeyOnlyIfNeeded_(self, value):
        self.event("key")

    def orderFront_(self, sender):
        self.event("front")

    def orderFrontRegardless(self):
        self.event("front")

    def isOnActiveSpace(self):
        self.event("active")
        return self.active

    def isVisible(self):
        return self.visible

    def orderWindow_relativeTo_(self, direction, number):
        self.event("relative")
        self.relative = (direction, number)


class GlassFollowFailures(SFCase):
    def setUp(self):
        super().setUp()
        self.clock = {"now": TIME}
        self.swallowed = mock.Mock()
        self.track_patch(mock.patch.object(glass, "log_swallow", self.swallowed))
        self.track_patch(mock.patch.object(glass.time, "monotonic", side_effect=lambda: self.clock["now"]))
        self.target = host(_glass_window=object(), _glass_follow_timer=None,
                           _glass_follow_tick=mock.Mock())
        vars(self.target)["_glass_follow_end"] = types.MethodType(follow_end, self.target)
        self.content = host(windowNumber=lambda: MAIN_NUMBER)
        self.track_patch(mock.patch.object(glass, "content_ns_window", return_value=self.content))
        self.track_patch(mock.patch.object(Quartz, "CGDisplayBounds", return_value=host(size=host(height=900))))
        self.track_patch(mock.patch.object(Quartz, "CGMainDisplayID", return_value=1))
        self.native_timer = object()
        self.created, self.added, self.invalidated, self.removed = mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock()
        self.created.return_value = self.native_timer
        for name, value in (("CFRunLoopTimerCreate", self.created), ("CFRunLoopAddTimer", self.added),
                            ("CFRunLoopTimerInvalidate", self.invalidated), ("CFRunLoopRemoveTimer", self.removed)):
            self.track_patch(mock.patch.object(cf, name, value))
        self.track_patch(mock.patch.object(cf, "CFRunLoopGetMain", return_value="main-loop"))
        self.track_patch(mock.patch.object(cf, "CFAbsoluteTimeGetCurrent", return_value=TIME))

    def test_expired_follow_callback_invalidates_timer_and_does_not_tick(self):
        follow_begin(self.target)
        callback = self.created.call_args.args[-2]
        self.clock["now"] = TIME + 100
        callback(self.native_timer, None)
        self.invalidated.assert_called_once_with(self.native_timer)
        self.removed.assert_called_once()
        self.assertIsNone(vars(self.target)["_glass_follow_timer"])
        vars(self.target)["_glass_follow_tick"].assert_not_called()

    def test_active_follow_callback_ticks_and_exception_is_named(self):
        follow_begin(self.target)
        callback = self.created.call_args.args[-2]
        tick = vars(self.target)["_glass_follow_tick"]
        callback(self.native_timer, None)
        tick.assert_called_once_with()
        tick.side_effect = RuntimeError("frame adapter lost")
        callback(self.native_timer, None)
        self.assertIn("glass_follow_tick", self.swallowed.call_args.args[0])

    def test_timer_creation_failure_does_not_claim_active_timer(self):
        self.created.return_value = None
        follow_begin(self.target)
        self.assertIsNone(vars(self.target)["_glass_follow_timer"])
        self.added.assert_not_called()

    def test_timer_registration_failure_cleans_partial_timer_and_can_retry(self):
        self.added.side_effect = RuntimeError("registration failed")
        follow_begin(self.target)
        self.assertIsNone(vars(self.target)["_glass_follow_timer"], "failed registration must release its timer")
        self.invalidated.assert_called_once_with(self.native_timer)
        self.removed.assert_called_once()
        self.assertIn("_glass_follow_begin", self.swallowed.call_args.args[0])
        self.added.side_effect = None
        follow_begin(self.target)
        self.assertIs(vars(self.target)["_glass_follow_timer"], self.native_timer)

    def test_missing_native_content_does_not_register_timer(self):
        with mock.patch.object(glass, "content_ns_window", return_value=None):
            follow_begin(self.target)
        self.created.assert_not_called()
        self.added.assert_not_called()

    def test_follow_end_failure_still_clears_host_state(self):
        vars(self.target).update(_glass_follow_timer=self.native_timer, _glass_follow_pos=(5, 6),
                                 _glass_follow_cnum=MAIN_NUMBER, _glass_follow_sh=900)
        self.invalidated.side_effect = RuntimeError("native timer gone")
        follow_end(self.target)
        self.assertIsNone(vars(self.target)["_glass_follow_timer"])
        self.assertIsNone(vars(self.target)["_glass_follow_pos"])
        self.assertIsNone(vars(self.target)["_glass_follow_cnum"])
        self.assertIn("_glass_follow_end", self.swallowed.call_args.args[0])

    def test_failed_mask_flush_clears_pending_and_does_not_advance_geometry_cache(self):
        target = host(_glass_mask_pending=True, _glass_view=object(),
                      _glass_mask_w=100, _glass_mask_h=200, _last_glass_mask_ts=900,
                      width=lambda: MODEL_WIDTH, height=lambda: MODEL_HEIGHT,
                      _set_glass_mask=mock.Mock(side_effect=RuntimeError("mask failed")))
        flush_mask(target)
        self.assertFalse(vars(target)["_glass_mask_pending"])
        self.assertEqual(vars(target)["_glass_mask_w"], 100)
        self.assertEqual(vars(target)["_last_glass_mask_ts"], 900)
        self.assertIn("_glass_mask_flush", self.swallowed.call_args.args[0])


class GlassRetopFailures(SFCase):
    def setUp(self):
        super().setUp()
        self.events = []
        self.main = NativeWindow(self.events, "main", MAIN_NUMBER)
        self.background = NativeWindow(self.events, "glass", GLASS_NUMBER)
        self.swallowed = mock.Mock()
        self.track_patch(mock.patch.object(glass, "log_swallow", self.swallowed))
        self.track_patch(mock.patch.object(glass, "content_ns_window", return_value=self.main))
        self.track_patch(mock.patch.object(glass, "active_space_id", return_value=99))
        self.target = host(
            _minimized=False, _quitting=False, _glass_window=self.background, _force_top_forced=False,
            _top_state_ok=lambda main, background: False,
            _repair_main_interaction=mock.Mock(), _sync_glass_frame=mock.Mock(),
            _sync_open_tool_windows=mock.Mock(), _raise_open_tool_windows=mock.Mock(),
            _force_join_active_space=lambda window: self.events.append("join:" + window.label))
        vars(self.target)["_order_glass_below_content"] = types.MethodType(relative_order, self.target)

    def test_glass_failure_does_not_abort_main_foregrounding_and_final_relative_order(self):
        for failing in ("level", "hides", "behavior", "front", "active"):
            with self.subTest(failing=failing):
                self.events.clear()
                self.swallowed.reset_mock()
                self.background.fail = {failing}
                force_top(self.target)
                self.assertIn("main:front", self.events)
                self.assertIn("glass:relative", self.events)
                self.assertLess(self.events.index("main:front"), self.events.index("glass:relative"))
                self.assertIn("_force_top", self.swallowed.call_args.args[0])

    def test_broken_tool_windows_do_not_skip_final_compositor_order(self):
        for member in ("_sync_open_tool_windows", "_raise_open_tool_windows"):
            with self.subTest(member=member):
                callback = vars(self.target)[member]
                callback.side_effect = RuntimeError("tool already deleted")
                self.events.clear()
                force_top(self.target)
                self.assertIn("glass:relative", self.events)
                self.assertEqual(self.background.relative, (AppKit.NSWindowBelow, MAIN_NUMBER))
                callback.side_effect = None

    def test_main_attribute_failure_still_orders_background_below_visible_content(self):
        for failing in ("key", "behavior", "active"):
            with self.subTest(failing=failing):
                self.main.fail = {failing}
                self.events.clear()
                force_top(self.target)
                self.assertIn("glass:relative", self.events)
                self.assertTrue(self.swallowed.called)

    def test_main_level_failure_is_named_and_can_retry(self):
        self.main.fail = {"level"}
        force_top(self.target)
        self.assertNotIn("main:front", self.events)
        self.assertIn("_force_top", self.swallowed.call_args.args[0])
        self.main.fail.clear()
        force_top(self.target)
        self.assertIn("main:front", self.events)
        self.assertIn("glass:relative", self.events)

    def test_forced_top_latch_resets_even_if_collaborator_raises(self):
        target = host(_force_top_forced=False, _force_top=mock.Mock(side_effect=RuntimeError("repair failed")))
        with self.assertRaisesRegex(RuntimeError, "repair failed"):
            reassert_top(target)
        self.assertFalse(vars(target)["_force_top_forced"])

    def test_invisible_main_is_not_used_as_relative_order_reference(self):
        self.main.visible = False
        relative_order(self.target, self.main, self.background)
        self.assertNotIn("glass:relative", self.events)

    def test_native_relative_order_failure_is_named(self):
        self.background.fail = {"relative"}
        relative_order(self.target, self.main, self.background)
        self.assertIn("_order_glass_below_content", self.swallowed.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
