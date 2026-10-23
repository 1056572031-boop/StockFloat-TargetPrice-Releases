# -*- coding: utf-8 -*-
"""收起 / 恢复过渡动画（sf_dock_motion）：纯算法与控制器的判据。

数字来源：2026-10-23 在本机用辅助功能（只读）读出的真实 Dock 坐标 ——
屏幕 1470x956、Dock 厚 60、图标边长 40、Finder + 18 个固定应用，Skyrocket 排第 3 格，
条目框 x=347.5 w=42，图标中心 368.5；多开"系统设置"与另一个无标识进程后整排左移 42，
中心 326.55。估算公式在这两种现场与读数的偏差分别是 0.05 与 0.00 个点。
垂直方向的图标图形在条目框里的确切位置读不出来，所以只锁"落在 Dock 条带之内"。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_dock_motion as D
from tests._base import SFCase

SCREEN = (0, 0, 1470, 956)
AVAILABLE = (0, 33, 1470, 863)          # 底部让出 60 = Dock 厚度
MINE = {"com.skyrocket.app", "com.stockfloat.targetprice"}
PINNED = [("app.%02d" % i, 1.0) for i in range(18)]
PINNED[1] = ("com.skyrocket.app", 1.0)
WIN = (560.0, 420.0, 220.0, 100.0)
DOCK = (348.6, 906.0, 40.0, 40.0)


def bezier_y(curve, x):
    """三次贝塞尔（起点 (0,0)、终点 (1,1)）在横坐标 x 处的纵坐标，二分求参数 t。
    跟系统的 CAMediaTimingFunction 同一个定义，用来在没有屏幕的环境里验曲线形状。"""
    c1x, c1y, c2x, c2y = curve
    lo, hi = 0.0, 1.0
    for _ in range(60):
        t = (lo + hi) / 2.0
        bx = 3 * (1 - t) ** 2 * t * c1x + 3 * (1 - t) * t ** 2 * c2x + t ** 3
        lo, hi = (t, hi) if bx < x else (lo, t)
    t = (lo + hi) / 2.0
    return 3 * (1 - t) ** 2 * t * c1y + 3 * (1 - t) * t ** 2 * c2y + t ** 3


CURVES = {"lead": D.CURVE_LEAD, "fall": D.CURVE_FALL, "soft": D.CURVE_SOFT}


class TestCurves(SFCase):
    def test_control_points_keep_time_monotonic(self):
        for name, curve in CURVES.items():
            self.assertTrue(0.0 <= curve[0] <= 1.0 and 0.0 <= curve[2] <= 1.0,
                            "%s：横向控制点出了 0..1，曲线会在时间上折回" % name)

    def test_progress_never_goes_backwards(self):
        for name, curve in CURVES.items():
            values = [bezier_y(curve, i / 100.0) for i in range(101)]
            self.assertEqual(values, sorted(values), "%s：动画会在半路往回抖" % name)
            self.assertAlmostEqual(values[0], 0.0, places=3)
            self.assertAlmostEqual(values[-1], 1.0, places=3)

    def test_reversed_curve_is_the_exact_time_mirror(self):
        for name, curve in CURVES.items():
            back = D.reverse_curve(curve)
            for i in range(0, 101, 5):
                x = i / 100.0
                self.assertAlmostEqual(bezier_y(back, x), 1.0 - bezier_y(curve, 1.0 - x), places=4,
                                       msg="%s：放大不是缩小的镜像" % name)
            for got, want in zip(D.reverse_curve(back), curve):
                self.assertAlmostEqual(got, want, places=12, msg="反转两次应当回到原曲线")



class TestFlightPlan(SFCase):
    def global_center(self, flight, which):
        """舞台局部坐标 → 屏幕坐标，用来核对起终点真的落在窗口与图标中心上。"""
        x, y = flight.centers[which]
        return (x + flight.stage[0], y + flight.stage[1])

    def test_closing_flies_from_the_window_center_to_the_icon_center(self):
        flight = D.plan_flight(WIN, DOCK, True)
        self.assertEqual(self.global_center(flight, 0), (WIN[0] + WIN[2] / 2.0, WIN[1] + WIN[3] / 2.0))
        end = self.global_center(flight, 1)
        self.assertAlmostEqual(end[0], DOCK[0] + DOCK[2] / 2.0)
        self.assertAlmostEqual(end[1], DOCK[1] + DOCK[3] / 2.0)
        self.assertEqual(flight.scales[0], (1.0, 1.0))
        self.assertAlmostEqual(flight.scales[1][0], DOCK[2] / WIN[2])
        self.assertAlmostEqual(flight.scales[1][1], DOCK[3] / WIN[3])

    def test_stage_covers_both_ends_with_a_margin(self):
        flight = D.plan_flight(WIN, DOCK, True)
        sx, sy, sw, sh = flight.stage
        for rect in (WIN, DOCK):
            self.assertLessEqual(sx, rect[0] - D.STAGE_PAD + 1e-9)
            self.assertLessEqual(sy, rect[1] - D.STAGE_PAD + 1e-9)
            self.assertGreaterEqual(sx + sw, rect[0] + rect[2] + D.STAGE_PAD - 1e-9)
            self.assertGreaterEqual(sy + sh, rect[1] + rect[3] + D.STAGE_PAD - 1e-9)

    def test_layer_stays_inside_the_stage_all_the_way(self):
        """横纵缩放快慢不同，中途图层外沿可能越过两端矩形；舞台窗必须仍然装得下。"""
        flight = D.plan_flight(WIN, DOCK, True)
        for i in range(101):
            t = i / 100.0
            cx = flight.centers[0][0] + (flight.centers[1][0] - flight.centers[0][0]) * bezier_y(flight.curves["x"], t)
            cy = flight.centers[0][1] + (flight.centers[1][1] - flight.centers[0][1]) * bezier_y(flight.curves["y"], t)
            sx = flight.scales[0][0] + (flight.scales[1][0] - flight.scales[0][0]) * bezier_y(flight.curves["sx"], t)
            sy = flight.scales[0][1] + (flight.scales[1][1] - flight.scales[0][1]) * bezier_y(flight.curves["sy"], t)
            half_w, half_h = flight.size[0] * sx / 2.0, flight.size[1] * sy / 2.0
            self.assertGreaterEqual(cx - half_w, -1e-6, "t=%.2f 左边被舞台窗裁掉" % t)
            self.assertGreaterEqual(cy - half_h, -1e-6, "t=%.2f 上边被舞台窗裁掉" % t)
            self.assertLessEqual(cx + half_w, flight.stage[2] + 1e-6, "t=%.2f 右边被舞台窗裁掉" % t)
            self.assertLessEqual(cy + half_h, flight.stage[3] + 1e-6, "t=%.2f 下边被舞台窗裁掉" % t)

    def test_durations_and_fade_shape(self):
        close, opened = D.plan_flight(WIN, DOCK, True), D.plan_flight(WIN, DOCK, False)
        self.assertEqual((close.duration, opened.duration), (D.COLLAPSE_SEC, D.EXPAND_SEC))
        self.assertEqual(close.fade, ((1.0, 1.0, 0.0), (0.0, D.FADE_HOLD, 1.0)))
        self.assertEqual(opened.fade[0], (0.0, 1.0, 1.0))
        self.assertAlmostEqual(opened.fade[1][1], 1.0 - D.FADE_HOLD)

    def test_opening_is_the_time_reverse_of_closing(self):
        close, opened = D.plan_flight(WIN, DOCK, True), D.plan_flight(WIN, DOCK, False)
        self.assertEqual(opened.centers, close.centers[::-1])
        self.assertEqual(opened.scales, close.scales[::-1])
        for key, curve in close.curves.items():
            self.assertEqual(opened.curves[key], D.reverse_curve(curve))
        self.assertTrue(close.closing and not opened.closing)


class TestDockGeometry(SFCase):
    def test_bottom_dock_thickness_is_read_from_the_available_area(self):
        self.assertEqual(D.dock_inset(SCREEN, AVAILABLE), ("bottom", 60.0))

    def test_left_and_right_docks(self):
        self.assertEqual(D.dock_inset(SCREEN, (70, 33, 1400, 923)), ("left", 70.0))
        self.assertEqual(D.dock_inset(SCREEN, (0, 33, 1400, 923)), ("right", 70.0))

    def test_hidden_dock_reads_as_bottom_with_zero_thickness(self):
        self.assertEqual(D.dock_inset(SCREEN, (0, 33, 1470, 923)), ("bottom", 0.0))

    def test_pinned_app_slot_matches_the_measured_layout(self):
        before, apps = D.locate_slot(PINNED, MINE, 0)
        self.assertEqual((before, apps), (2.0, 19.0), "Finder 占第 0 格，应用排第 2 格")

    def test_legacy_bundle_id_counts_as_mine(self):
        pinned = [("com.stockfloat.targetprice", 1.0)] + PINNED[2:]
        before, _apps = D.locate_slot(pinned, MINE, 0)
        self.assertEqual(before, 1.0)

    def test_spacer_tiles_take_their_own_width(self):
        pinned = [("", 1.0), ("", 0.5), ("com.skyrocket.app", 1.0)]
        before, apps = D.locate_slot(pinned, MINE, 0)
        self.assertEqual((before, apps), (2.5, 3.5), "Finder 1 + 1 + 0.5 + 本程序 1")

    def test_unpinned_app_sits_after_the_pinned_ones_and_other_running_apps(self):
        before, apps = D.locate_slot(PINNED[:1] + PINNED[2:], MINE, 2)
        self.assertEqual((before, apps), (20.0, 21.0))

    def test_estimate_matches_the_measured_icon_center(self):
        rect = D.estimate_icon_rect(SCREEN, "bottom", 60.0, 40.0, (2.0, 19.0, 3))
        self.assertAlmostEqual(rect[0] + rect[2] / 2.0, 368.5, delta=0.5)
        self.assertGreaterEqual(rect[1], 956 - 60, "图标必须落在 Dock 条带里")
        self.assertLessEqual(rect[1] + rect[3], 956)

    def test_two_extra_running_apps_shift_the_row_by_one_pitch(self):
        one = D.estimate_icon_rect(SCREEN, "bottom", 60.0, 40.0, (2.0, 19.0, 3))
        two = D.estimate_icon_rect(SCREEN, "bottom", 60.0, 40.0, (2.0, 21.0, 3))
        self.assertAlmostEqual(two[0] + two[2] / 2.0, 326.55, delta=0.5)
        self.assertAlmostEqual(one[0] - two[0], 42.0, delta=0.01)

    def test_without_a_slot_it_lands_on_the_bar_center(self):
        rect = D.estimate_icon_rect(SCREEN, "bottom", 60.0, 40.0, None)
        self.assertAlmostEqual(rect[0] + rect[2] / 2.0, 735.0)

    def test_side_docks_land_on_the_edge_middle(self):
        left = D.estimate_icon_rect(SCREEN, "left", 70.0, 40.0)
        right = D.estimate_icon_rect(SCREEN, "right", 70.0, 40.0)
        self.assertAlmostEqual(left[1] + left[3] / 2.0, 478.0)
        self.assertAlmostEqual(left[0] + left[2] / 2.0, 35.0)
        self.assertAlmostEqual(right[0] + right[2] / 2.0, 1470 - 35.0)

    def test_auto_hidden_dock_still_gets_a_reasonable_target(self):
        rect = D.estimate_icon_rect(SCREEN, "bottom", 0.0, 40.0, None)
        self.assertEqual(rect[1], 956 - 60 / 2.0 - 20.0)

    def test_cursor_on_the_dock_pulls_the_target_under_the_pointer(self):
        rect = D.refine_with_cursor(DOCK, (360.0, 930.0), SCREEN, 60.0, 128.0)
        self.assertAlmostEqual(rect[0] + rect[2] / 2.0, 360.0)
        self.assertEqual(rect[1:], DOCK[1:])

    def test_cursor_elsewhere_changes_nothing(self):
        for cursor in ((360.0, 500.0), (900.0, 930.0), (360.0, 960.0)):
            self.assertEqual(D.refine_with_cursor(DOCK, cursor, SCREEN, 60.0, 128.0), DOCK,
                             "鼠标不在程序坞上（或离估算位置太远）时不该信它：%r" % (cursor,))


class FakeBackend(object):
    """后端替身：记下每个发给系统的调用，按开关在指定位置抛错。"""

    def __init__(self):
        self.calls = []
        self.image = "IMG"
        self.reduce = False
        self.fail = set()
        self.prefs = {"tile": 40.0, "magnified": True, "large": 128.0, "recents": False,
                      "pinned": list(PINNED), "tail": 3}
        self.running = []

    def _step(self, name, *args):
        self.calls.append((name,) + args)
        if name in self.fail:
            raise RuntimeError("fake %s failure" % name)

    def snapshot(self, numbers):
        self._step("snapshot", list(numbers))
        return self.image

    def warm(self, numbers):
        self._step("warm", list(numbers))

    def open_stage(self, image, flight, level, behavior):
        self._step("open_stage", flight, level, behavior)
        return "STAGE"

    def dissolve(self, stage, seconds):
        self._step("dissolve", stage, seconds)

    def close(self, stage):
        self._step("close", stage)

    def reduce_motion(self):
        return self.reduce

    def running_ids(self):
        return list(self.running)

    def dock_prefs(self):
        self._step("dock_prefs")
        return dict(self.prefs)

    def names(self):
        return [call[0] for call in self.calls]


class ControllerCase(SFCase):
    def setUp(self):
        SFCase.setUp(self)
        # 必须持有引用：QApplication 一被回收，QTimer 就启动不了（isActive 恒为假）
        self.app = self.ensure_qapp()
        self.backend = FakeBackend()
        self.ctl = D.DockMotion(backend=self.backend)
        self.addCleanup(self.ctl.cancel)

    def primed(self):
        self.assertTrue(self.ctl.capture([7002, 7001], WIN, 25, 273))
        self.backend.calls.clear()
        return self.ctl

    def opened(self):
        """open_stage 收到的那份飞行计划。"""
        return [c for c in self.backend.calls if c[0] == "open_stage"][-1][1]


class TestCapture(ControllerCase):
    def test_capture_keeps_the_image_and_geometry(self):
        self.assertTrue(self.ctl.capture([7002, 7001], WIN, 25, 273))
        self.assertEqual(self.backend.calls, [("snapshot", [7002, 7001])])
        self.assertEqual(self.ctl.snapshot, ("IMG", WIN, 25, 273))

    def test_missing_image_leaves_no_snapshot_and_logs_a_warning(self):
        self.backend.image = None
        with mock.patch.object(D, "log_line") as log:
            self.assertFalse(self.ctl.capture([1], WIN, 25, 273))
        self.assertFalse(self.ctl.has_snapshot)
        self.assertEqual(log.call_args[0][1], "WARNING")

    def test_snapshot_error_is_swallowed_with_a_trace(self):
        self.backend.fail.add("snapshot")
        with mock.patch.object(D, "log_swallow") as swallow:
            self.assertFalse(self.ctl.capture([1], WIN, 25, 273))
        self.assertEqual(swallow.call_args[0][0], "sf_dock_motion.capture")

    def test_new_capture_replaces_the_old_one(self):
        self.ctl.capture([1], WIN, 25, 273)
        self.backend.image = None
        self.ctl.capture([1], WIN, 25, 273)
        self.assertFalse(self.ctl.has_snapshot, "旧截图留着会让恢复动画放出一张过期的画面")


class TestCollapse(ControllerCase):
    def test_needs_a_snapshot_and_a_target(self):
        self.assertFalse(self.ctl.collapse(DOCK))
        self.primed()
        self.assertFalse(self.ctl.collapse(None))
        self.assertEqual(self.backend.calls, [])

    def test_stage_opens_over_the_window_with_the_closing_plan(self):
        ctl = self.primed()
        self.assertTrue(ctl.collapse(DOCK))
        self.assertEqual(self.backend.names(), ["open_stage"])
        self.assertEqual(self.opened(), D.plan_flight(WIN, DOCK, True))
        self.assertEqual(self.backend.calls[0][2:], (25, 273), "舞台窗要带上真实窗口的层级与 Space 行为")
        self.assertTrue(ctl.busy and ctl.collapsing)

    def test_one_shot_timer_waits_for_the_flight_plus_slack(self):
        ctl = self.primed()
        ctl.collapse(DOCK)
        self.assertTrue(ctl.timer.isSingleShot(), "动画由系统合成进程播，主线程不该逐帧响")
        self.assertTrue(ctl.timer.isActive())
        self.assertEqual(ctl.timer.interval(), round((D.COLLAPSE_SEC + D.FINISH_SLACK) * 1000))
        self.assertEqual(ctl.timer.timerType(), D.Qt.TimerType.PreciseTimer)

    def test_landing_removes_the_stage_and_keeps_the_snapshot(self):
        ctl = self.primed()
        ctl.collapse(DOCK)
        ctl.advance()
        self.assertEqual(self.backend.names(), ["open_stage", "close"])
        self.assertFalse(ctl.busy)
        self.assertFalse(ctl.timer.isActive())
        self.assertTrue(ctl.has_snapshot, "恢复动画还要用这张图")

    def test_second_start_while_running_is_ignored(self):
        ctl = self.primed()
        self.assertTrue(ctl.collapse(DOCK))
        self.assertFalse(ctl.collapse(DOCK))
        self.assertFalse(ctl.expand(DOCK, lambda: None))
        self.assertEqual(self.backend.names().count("open_stage"), 1)


class TestExpand(ControllerCase):
    def test_stage_opens_at_the_icon_with_the_opening_plan(self):
        ctl = self.primed()
        self.assertTrue(ctl.expand(DOCK, lambda: None))
        self.assertEqual(self.backend.names(), ["open_stage"])
        self.assertEqual(self.opened(), D.plan_flight(WIN, DOCK, False))
        self.assertTrue(ctl.busy and not ctl.collapsing)
        self.assertEqual(ctl.timer.interval(), round((D.EXPAND_SEC + D.FINISH_SLACK) * 1000))

    def test_real_window_shows_after_landing_then_the_stage_dissolves(self):
        ctl = self.primed()
        ctl.expand(DOCK, lambda: self.backend.calls.append(("on_done",)))
        ctl.advance()
        self.assertEqual(self.backend.names(), ["open_stage", "on_done", "dissolve"],
                         "先显示真实窗口，舞台层再盖在它上面淡出，中间才不会露出一帧空桌面")
        self.assertEqual(self.backend.calls[-1][2], D.DISSOLVE_SEC)
        self.assertTrue(ctl.busy, "溶解期间重复点击仍要被吞掉")
        self.assertEqual(ctl.timer.interval(), round((D.DISSOLVE_SEC + D.FINISH_SLACK) * 1000))

    def test_dissolve_end_removes_the_stage_and_releases_the_snapshot(self):
        ctl = self.primed()
        ctl.expand(DOCK, lambda: None)
        ctl.advance()
        ctl.advance()
        self.assertEqual(self.backend.names()[-1], "close")
        self.assertFalse(ctl.busy)
        self.assertFalse(ctl.has_snapshot, "放大播完就该释放截图")

    def test_callback_error_still_removes_the_stage_and_resets_state(self):
        ctl = self.primed()

        def boom():
            raise RuntimeError("restore failed")

        ctl.expand(DOCK, boom)
        with mock.patch.object(D, "log_swallow") as swallow:
            ctl.advance()
        self.assertEqual(self.backend.names(), ["open_stage", "close"], "回调失败就别再溶解了，直接撤舞台")
        self.assertFalse(ctl.busy)
        self.assertFalse(ctl.has_snapshot)
        self.assertEqual(swallow.call_args[0][0], "sf_dock_motion.on_done")

    def test_dissolve_error_still_removes_the_stage(self):
        ctl = self.primed()
        ctl.expand(DOCK, lambda: None)
        self.backend.fail.add("dissolve")
        with mock.patch.object(D, "log_swallow") as swallow:
            ctl.advance()
        self.assertEqual(self.backend.names()[-1], "close")
        self.assertFalse(ctl.busy)
        self.assertEqual(swallow.call_args[0][0], "sf_dock_motion.dissolve")


class TestFailureAndCancel(ControllerCase):
    def test_stage_close_error_is_swallowed_and_the_run_still_ends(self):
        ctl = self.primed()
        ctl.collapse(DOCK)
        self.backend.fail.add("close")
        with mock.patch.object(D, "log_swallow") as swallow:
            ctl.advance()
        self.assertFalse(ctl.busy)
        self.assertEqual(swallow.call_args[0][0], "sf_dock_motion.close_stage")

    def test_cancel_mid_flight_closes_the_stage_without_calling_back(self):
        ctl = self.primed()
        done = []
        ctl.expand(DOCK, lambda: done.append(1))
        ctl.cancel()
        self.assertEqual((done, ctl.busy, ctl.has_snapshot), ([], False, True))
        self.assertEqual(self.backend.names()[-1], "close")
        self.assertFalse(ctl.timer.isActive())

    def test_cancel_during_dissolve_removes_the_leftover_stage(self):
        ctl = self.primed()
        ctl.expand(DOCK, lambda: None)
        ctl.advance()
        ctl.cancel()
        self.assertEqual(self.backend.names(), ["open_stage", "dissolve", "close"])
        self.assertFalse(ctl.busy)

    def test_callback_that_cancels_leaves_nothing_to_dissolve(self):
        ctl = self.primed()
        ctl.expand(DOCK, ctl.cancel)
        ctl.advance()
        self.assertEqual(self.backend.names(), ["open_stage", "close"], "回调里已撤掉的舞台不能再溶解")
        self.assertFalse(ctl.busy)

    def test_callback_that_cancels_then_fails_does_not_close_twice(self):
        ctl = self.primed()

        def cancel_then_boom():
            ctl.cancel()
            raise RuntimeError("late failure")

        ctl.expand(DOCK, cancel_then_boom)
        with mock.patch.object(D, "log_swallow"):
            ctl.advance()
        self.assertEqual(self.backend.names(), ["open_stage", "close"])
        self.assertFalse(ctl.busy)

    def test_cancel_when_idle_is_harmless(self):
        self.ctl.cancel()
        self.assertEqual(self.backend.calls, [])

    def test_reduce_motion_skips_the_animation_entirely(self):
        ctl = self.primed()
        self.backend.reduce = True
        self.assertFalse(ctl.collapse(DOCK))
        self.assertFalse(ctl.expand(DOCK, lambda: None))
        self.assertEqual((self.backend.calls, ctl.busy), ([], False))

    def test_stage_creation_failure_is_swallowed_and_leaves_no_run(self):
        ctl = self.primed()
        self.backend.fail.add("open_stage")
        with mock.patch.object(D, "log_swallow") as swallow:
            self.assertFalse(ctl.collapse(DOCK))
            self.assertFalse(ctl.expand(DOCK, lambda: None))
        self.assertFalse(ctl.busy)
        self.assertEqual(swallow.call_args[0][0], "sf_dock_motion.start")

    def test_idle_advance_does_nothing(self):
        self.ctl.advance()
        self.assertEqual(self.backend.calls, [])


class TestPrewarm(ControllerCase):
    def test_warms_in_a_background_thread_exactly_once(self):
        thread = self.ctl.prewarm([7002, 7001])
        thread.join(2)
        self.assertEqual(self.backend.calls, [("warm", [7002, 7001])])
        self.assertTrue(thread.daemon, "预热线程不能拖住退出")
        self.assertIsNone(self.ctl.prewarm([7002, 7001]), "第二次不再预热")
        self.assertEqual(self.backend.names(), ["warm"])

    def test_warm_error_is_swallowed_with_a_trace(self):
        self.backend.fail.add("warm")
        with mock.patch.object(D, "log_swallow") as swallow:
            self.ctl.prewarm([1]).join(2)
        self.assertEqual(swallow.call_args[0][0], "sf_dock_motion.prewarm")


class TestIconRect(ControllerCase):
    """icon_rect 把偏好、运行中的应用、屏幕与鼠标位置拼成一个落点。"""

    def setUp(self):
        ControllerCase.setUp(self)
        self.screens = [(SCREEN, AVAILABLE)]
        # 鼠标放在远离 Dock 的地方，免得放大镜修正掺进纯估算的判据
        for patcher in (mock.patch.object(D, "qt_screens", lambda: self.screens),
                        mock.patch.object(D, "qt_cursor", lambda: (700, 300))):
            self.track_patch(patcher)

    def center_x(self):
        rect = self.ctl.icon_rect(WIN, MINE)
        return rect[0] + rect[2] / 2.0

    def test_pinned_app_lands_on_the_measured_icon(self):
        self.assertAlmostEqual(self.center_x(), 368.5, delta=0.5)

    def test_other_running_apps_shift_the_row(self):
        self.backend.running = ["com.apple.systempreferences", "probe.tile", D.FINDER_ID,
                                "com.skyrocket.app", "app.03"]
        self.assertAlmostEqual(self.center_x(), 326.55, delta=0.5)

    def test_visible_recents_fall_back_to_the_bar_center(self):
        self.backend.prefs["recents"] = True
        self.assertAlmostEqual(self.center_x(), 735.0)

    def test_backend_failure_gives_no_target_and_a_trace(self):
        self.backend.fail.add("dock_prefs")
        with mock.patch.object(D, "log_swallow") as swallow:
            self.assertIsNone(self.ctl.icon_rect(WIN, MINE))
        self.assertEqual(swallow.call_args[0][0], "sf_dock_motion.icon_rect")

    def test_no_screens_gives_no_target(self):
        self.screens = []
        self.assertIsNone(self.ctl.icon_rect(WIN, MINE))

    def test_auto_hidden_dock_still_gets_a_target_on_the_window_screen(self):
        self.screens = [(SCREEN, (0, 33, 1470, 923))]
        rect = self.ctl.icon_rect(WIN, MINE)
        self.assertEqual(rect[1], 956 - 30.0 - 20.0)

    def test_the_screen_with_the_dock_wins_on_a_multi_display_setup(self):
        self.screens = [(SCREEN, (0, 33, 1470, 923)),
                        ((1470, 0, 1470, 956), (1470, 33, 1470, 863))]
        rect = self.ctl.icon_rect(WIN, MINE)
        self.assertGreater(rect[0], 1470)
        self.assertLess(rect[0] + rect[2], 2940)
        self.assertEqual(rect[1], 906.0)

    def test_pointer_resting_on_the_dock_pulls_the_target_under_it(self):
        self.track_patch(mock.patch.object(D, "qt_cursor", lambda: (360, 930)))
        self.assertAlmostEqual(self.center_x(), 360.0)

    def test_default_controller_uses_the_real_backend(self):
        self.assertIsInstance(D.DockMotion().backend, D.CocoaBackend)


if __name__ == "__main__":
    unittest.main()
