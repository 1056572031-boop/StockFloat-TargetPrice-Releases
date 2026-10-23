# -*- coding: utf-8 -*-
"""sf_dock_motion.CocoaBackend：换上假的 AppKit / Quartz / Foundation，只看发给系统的调用序列。

真 AppKit 的窗口只能在隔离子进程里测（见 AGENTS 第 6 节第 10 条），而这里要验的是
"调了哪些系统接口、按什么顺序、传了什么"，不是系统自己的行为。真机上的落点与动画轨迹由
2026-10-23 的实机探针量过（终点与 Dock 图标估算位置偏差 0.5 点以内，代理窗 16 帧/约 300 毫秒）。
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_dock_motion as D
from tests._base import SFCase

WIN_RECT = (560.0, 420.0, 220.0, 100.0)
DOCK_RECT = (348.6, 906.0, 40.0, 40.0)


def _fake_appkit(screen_h=956.0):
    appkit = mock.MagicMock()
    appkit.NSScreen.screens.return_value = [
        SimpleNamespace(frame=lambda: SimpleNamespace(size=SimpleNamespace(height=screen_h)))]
    appkit.NSWindowStyleMaskBorderless = 0
    appkit.NSBackingStoreBuffered = 2
    appkit.NSViewWidthSizable = 2
    appkit.NSViewHeightSizable = 16
    appkit.NSApplicationActivationPolicyRegular = 0
    return appkit


class TestBackendCalls(SFCase):
    def _with(self, appkit=None, quartz=None, foundation=None):
        modules = {"AppKit": appkit or _fake_appkit(), "Quartz": quartz or mock.MagicMock(),
                   "Foundation": foundation or mock.MagicMock()}
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        return modules

    def test_flip_turns_qt_top_left_into_appkit_bottom_left(self):
        self._with()
        origin, size = D.CocoaBackend.flip((100.0, 200.0, 220.0, 100.0))
        self.assertEqual((origin, size), ((100.0, 956.0 - 200.0 - 100.0), (220.0, 100.0)))

    def test_snapshot_asks_for_exactly_the_given_windows(self):
        mods = self._with()
        quartz = mods["Quartz"]
        quartz.CGWindowListCreateImageFromArray.return_value = "IMG"
        quartz.CGImageGetWidth.return_value = 440
        self.assertEqual(D.CocoaBackend().snapshot([7002, 7001]), "IMG")
        mods["AppKit"].NSArray.arrayWithArray_.assert_called_once_with([7002, 7001])
        args = quartz.CGWindowListCreateImageFromArray.call_args[0]
        self.assertIs(args[0], quartz.CGRectNull)
        self.assertIs(args[2], quartz.kCGWindowImageBoundsIgnoreFraming)

    def test_snapshot_without_an_image_is_none(self):
        mods = self._with()
        quartz = mods["Quartz"]
        quartz.CGWindowListCreateImageFromArray.return_value = None
        self.assertIsNone(D.CocoaBackend().snapshot([1]))

    def test_snapshot_of_an_empty_image_is_none(self):
        mods = self._with()
        quartz = mods["Quartz"]
        quartz.CGWindowListCreateImageFromArray.return_value = "IMG"
        quartz.CGImageGetWidth.return_value = 0
        self.assertIsNone(D.CocoaBackend().snapshot([1]))

    def _open(self, closing=True):
        """用假的 AppKit / Quartz 打开一次舞台；order 记下关键调用的先后。"""
        mods = self._with()
        quartz = mods["Quartz"]
        order = []
        quartz.CGImageGetWidth.return_value = 440
        quartz.CATransform3DMakeScale.side_effect = lambda sx, sy, sz: ("scale", sx, sy)
        quartz.CATransaction.flush.side_effect = lambda: order.append("flush")
        quartz.CAMediaTimingFunction.functionWithControlPoints____.side_effect = lambda *p: ("curve",) + p
        anims = {}

        def basic(key):
            anim = mock.Mock(name="anim:" + key)
            anims[key] = anim
            return anim

        quartz.CABasicAnimation.animationWithKeyPath_.side_effect = basic
        quartz.CAKeyframeAnimation.animationWithKeyPath_.side_effect = basic
        win = mods["AppKit"].NSWindow.alloc.return_value.initWithContentRect_styleMask_backing_defer_.return_value
        win.orderFrontRegardless.side_effect = lambda: order.append("front")
        view = mods["AppKit"].NSView.alloc.return_value.initWithFrame_.return_value
        flier = quartz.CALayer.layer.return_value
        flier.addAnimation_forKey_.side_effect = lambda anim, key: order.append(key)
        flight = D.plan_flight(WIN_RECT, DOCK_RECT, closing)
        stage = D.CocoaBackend().open_stage("IMG", flight, 25, 273)
        return SimpleNamespace(stage=stage, win=win, view=view, flier=flier, flight=flight,
                               order=order, anims=anims, quartz=quartz, appkit=mods["AppKit"])

    def test_stage_window_is_click_through_shadowless_and_borderless(self):
        run = self._open()
        self.assertIs(run.stage.window, run.win)
        self.assertIs(run.stage.layer, run.flier)
        run.win.setIgnoresMouseEvents_.assert_called_once_with(True)
        run.win.setHasShadow_.assert_called_once_with(False)
        run.win.setOpaque_.assert_called_once_with(False)
        run.win.setLevel_.assert_called_once_with(25)
        run.win.setCollectionBehavior_.assert_called_once_with(273)
        run.win.setContentView_.assert_called_once_with(run.view)
        run.view.setWantsLayer_.assert_called_once_with(True)
        init = run.appkit.NSWindow.alloc.return_value.initWithContentRect_styleMask_backing_defer_
        self.assertEqual(init.call_args[0][0], D.CocoaBackend.flip(run.flight.stage))

    def test_first_frame_is_flushed_before_any_animation_starts(self):
        run = self._open()
        self.assertEqual(run.order[:2], ["front", "flush"], "起点一帧先 flush 出去，隐藏真窗口时舞台上已有画面")
        self.assertLess(run.order.index("flush"), run.order.index("fly.position.x"))
    def test_flier_carries_the_image_at_native_resolution(self):
        run = self._open()
        run.flier.setContents_.assert_called_once_with("IMG")
        run.flier.setContentsScale_.assert_called_once_with(440 / 220.0)
        run.flier.setBounds_.assert_called_once_with(((0, 0), run.flight.size))
        run.flier.setContentsGravity_.assert_called_once_with(run.quartz.kCAGravityResize)
        run.flier.setMinificationFilter_.assert_called_once_with(run.quartz.kCAFilterTrilinear)
        run.flier.setMagnificationFilter_.assert_called_once_with(run.quartz.kCAFilterLinear)

    def test_layer_starts_at_the_source_pose_and_the_model_ends_at_the_destination(self):
        run = self._open()
        flight, stage_h = run.flight, run.flight.stage[3]
        (x0, y0), (x1, y1) = flight.centers

        def args(method):
            return [c[0][0] for c in method.call_args_list]

        self.assertEqual(args(run.flier.setPosition_), [(x0, stage_h - y0), (x1, stage_h - y1)])
        self.assertEqual(args(run.flier.setTransform_),
                         [("scale",) + flight.scales[0], ("scale",) + flight.scales[1]])
        self.assertEqual(args(run.flier.setOpacity_), [flight.fade[0][0], flight.fade[0][-1]])

    def test_close_orders_out_before_closing(self):
        stage = D.Stage(mock.Mock(), mock.Mock())
        D.CocoaBackend().close(stage)
        self.assertEqual([c[0] for c in stage.window.mock_calls], ["orderOut_", "close"])

    def test_every_axis_gets_its_own_curve_and_the_y_axis_is_flipped(self):
        run = self._open()
        flight, stage_h = run.flight, run.flight.stage[3]
        (x0, y0), (x1, y1) = flight.centers
        (sx0, sy0), (sx1, sy1) = flight.scales
        expected = {"position.x": (x0, x1, "x"), "position.y": (stage_h - y0, stage_h - y1, "y"),
                    "transform.scale.x": (sx0, sx1, "sx"), "transform.scale.y": (sy0, sy1, "sy")}
        for key, (start, end, curve) in expected.items():
            anim = run.anims[key]
            anim.setFromValue_.assert_called_once_with(start)
            anim.setToValue_.assert_called_once_with(end)
            anim.setDuration_.assert_called_once_with(flight.duration)
            anim.setTimingFunction_.assert_called_once_with(("curve",) + flight.curves[curve])

    def test_opacity_keyframes_follow_the_plan(self):
        run = self._open()
        anim = run.anims["opacity"]
        anim.setValues_.assert_called_once_with(list(run.flight.fade[0]))
        anim.setKeyTimes_.assert_called_once_with(list(run.flight.fade[1]))
        anim.setDuration_.assert_called_once_with(run.flight.duration)

    def test_all_five_animations_are_attached_after_the_first_frame(self):
        run = self._open()
        self.assertEqual(run.order[2:], ["fly.position.x", "fly.position.y", "fly.transform.scale.x",
                                         "fly.transform.scale.y", "fly.opacity"])

    def test_opening_flies_on_the_reversed_curves(self):
        run = self._open(closing=False)
        run.anims["position.x"].setTimingFunction_.assert_called_once_with(
            ("curve",) + D.reverse_curve(D.CURVE_LEAD))

    def test_reduce_motion_reads_the_accessibility_switch(self):
        mods = self._with()
        workspace = mods["AppKit"].NSWorkspace.sharedWorkspace.return_value
        workspace.accessibilityDisplayShouldReduceMotion.return_value = True
        self.assertTrue(D.CocoaBackend().reduce_motion())
        workspace.accessibilityDisplayShouldReduceMotion.return_value = False
        self.assertFalse(D.CocoaBackend().reduce_motion())

    def test_running_ids_filters_in_objective_c_with_one_predicate(self):
        mods = self._with()
        workspace = mods["AppKit"].NSWorkspace.sharedWorkspace.return_value
        found = workspace.runningApplications.return_value.filteredArrayUsingPredicate_.return_value
        found.valueForKey_.return_value = ["a.regular", "c.regular"]
        self.assertEqual(D.CocoaBackend().running_ids(), ["a.regular", "c.regular"])
        pattern = mods["Foundation"].NSPredicate.predicateWithFormat_.call_args[0][0]
        self.assertEqual(pattern, "activationPolicy == 0 AND bundleIdentifier != nil")
        found.valueForKey_.assert_called_once_with("bundleIdentifier")

    def test_dissolve_raises_the_stage_then_fades_the_layer_out(self):
        mods = self._with()
        order = []
        stage = D.Stage(mock.Mock(), mock.Mock())
        stage.window.orderFrontRegardless.side_effect = lambda: order.append("front")
        stage.layer.addAnimation_forKey_.side_effect = lambda anim, key: order.append(key)
        anim = mods["Quartz"].CABasicAnimation.animationWithKeyPath_.return_value
        D.CocoaBackend().dissolve(stage, 0.1)
        self.assertEqual(order, ["front", "dissolve"], "先抬到最上面盖住真窗，再淡出")
        stage.layer.setOpacity_.assert_called_once_with(0.0)
        anim.setFromValue_.assert_called_once_with(1.0)
        anim.setToValue_.assert_called_once_with(0.0)
        anim.setDuration_.assert_called_once_with(0.1)

    def test_warm_takes_the_snapshot_inside_an_autorelease_pool(self):
        objc = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"objc": objc}), \
                mock.patch.object(D.CocoaBackend, "snapshot") as snapshot:
            D.CocoaBackend().warm([7002, 7001])
        snapshot.assert_called_once_with([7002, 7001])
        objc.autorelease_pool.return_value.__enter__.assert_called_once_with()
        objc.autorelease_pool.return_value.__exit__.assert_called_once()

    def _dock_domain(self, domain):
        foundation = mock.MagicMock()
        defaults = foundation.NSUserDefaults.standardUserDefaults.return_value
        defaults.persistentDomainForName_.return_value = domain
        self._with(foundation=foundation)
        return defaults

    def test_dock_prefs_turns_the_domain_into_plain_numbers(self):
        tiles = [{"tile-type": "file-tile", "tile-data": {"bundle-identifier": "a.b"}},
                 {"tile-type": "spacer-tile", "tile-data": {}},
                 {"tile-type": "small-spacer-tile", "tile-data": {}},
                 {"tile-type": "url-tile"}]
        defaults = self._dock_domain({"tilesize": 48, "magnification": 1, "largesize": 96,
                                      "show-recents": False, "persistent-apps": tiles,
                                      "persistent-others": [{}, {}]})
        prefs = D.CocoaBackend().dock_prefs()
        defaults.persistentDomainForName_.assert_called_once_with("com.apple.dock")
        self.assertEqual(prefs, {"tile": 48.0, "magnified": True, "large": 96.0, "recents": False,
                                 "pinned": [("a.b", 1.0), ("", 1.0), ("", 0.5), ("", 1.0)],
                                 "tail": 3})

    def test_dock_prefs_defaults_when_the_user_never_touched_the_dock(self):
        self._dock_domain(None)
        self.assertEqual(D.CocoaBackend().dock_prefs(),
                         {"tile": D.DOCK_DEFAULT_TILE, "magnified": False, "large": 0.0,
                          "recents": True, "pinned": [], "tail": 1})


class TestRealFrameworks(SFCase):
    """真框架上的两条契约：mock 测不出 PyObjC 选择器名写错、谓词写错。不开窗口，不碰窗口服务。"""

    def test_batch_running_ids_equals_the_per_app_loop(self):
        import AppKit
        loop = sorted(str(a.bundleIdentifier()) for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                      if a.bundleIdentifier() and a.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular)
        self.assertEqual(sorted(D.CocoaBackend().running_ids()), loop)

    def test_real_core_animation_accepts_every_selector_the_flight_uses(self):
        import Quartz
        flight = D.plan_flight(WIN_RECT, DOCK_RECT, True)
        layer = Quartz.CALayer.layer()
        layer.setBounds_(((0, 0), flight.size))      # 与 open_stage 一致：先有尺寸，系统才收动画
        D.CocoaBackend()._fly(Quartz, layer, flight, flight.stage[3])  # discipline-ok(R5): 在真图层上验选择器，不需要窗口
        self.assertEqual(sorted(layer.animationKeys()), [
            "fly.opacity", "fly.position.x", "fly.position.y", "fly.transform.scale.x", "fly.transform.scale.y"])
        self.assertAlmostEqual(layer.position().x, flight.centers[1][0])
        self.assertEqual(layer.opacity(), 0.0, "模型值要先落到终点，动画播完才不会弹回起点")


class TestQtHelpers(SFCase):
    def setUp(self):
        SFCase.setUp(self)
        self.app = self.ensure_qapp()      # 持有引用，否则 QApplication 被回收后屏幕列表为空

    def test_screens_report_geometry_and_available_area(self):
        screens = D.qt_screens()
        self.assertTrue(screens)
        for geometry, available in screens:
            self.assertEqual((len(geometry), len(available)), (4, 4))
            self.assertGreater(geometry[2], 0)

    def test_cursor_is_an_integer_pair(self):
        position = D.qt_cursor()
        self.assertEqual(len(position), 2)
        self.assertTrue(all(isinstance(v, int) for v in position))


if __name__ == "__main__":
    unittest.main()
