# -*- coding: utf-8 -*-
"""人脸在场守卫的上半身检测（2026-10-29，用户要求"检测到人体的上半身就隐藏"）。

三层：纯函数（脸框与身体框怎么去重计数）、采集回调（合跑 / 退回只数脸 / 连续失败后放弃）、
可选类装载。人体请求是新增的一路，只会让收窗更容易发生，所以每一处失败都必须退回
"只数脸"的原行为，且不许拖累已有的脸保护。
"""

import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from _base import SFCase                       # noqa: E402
except ImportError:
    from tests._base import SFCase                 # noqa: E402

import sf_presence
import stock_float_window as window
from sf_presence import (EVENT_HIDE, EVENT_RESTORE, FacePresenceMonitor,
                         PresenceArbiter, STATE_RUNNING, count_people,
                         effective_count, qualifying_bodies)


def sf_constants_near_gate():
    """默认的距离闸门（放宽取景之前的值）。"""
    return sf_presence.FACE_GUARD_NEAR_FACE_W


class _GeoBox:
    """检测框替身：只实现 confidence() 与 boundingBox()，形状同 PyObjC 桥出来的 ((x, y), (w, h))。"""

    def __init__(self, conf, x, y, w, h, geo=True):
        self._conf, self._rect, self._geo = conf, (x, y, w, h), geo

    def confidence(self):
        return self._conf

    def boundingBox(self):
        if not self._geo:
            raise RuntimeError("读不出几何")
        x, y, w, h = self._rect
        return ((x, y), (w, h))


def _face(x=0.30, y=0.25, w=0.30, h=0.30, conf=0.85, geo=True):
    return _GeoBox(conf, x, y, w, h, geo)


def _body(x=0.20, y=0.0, w=0.50, h=0.90, conf=0.80):
    return _GeoBox(conf, x, y, w, h)

class TestCountPeople(SFCase):
    """脸框与身体框怎么折算成"几个人"。纯函数，没有 ObjC 也没有 Qt。"""

    def test_owner_face_and_body_is_one_person(self):
        """同一个人的脸和身体只能算一个，否则主人自己会让窗口永远收着。"""
        self.assertEqual(count_people([_face()], [_body()]), (1, 1))

    def test_body_without_a_face_adds_a_person(self):
        """第二个人背对镜头：脸检不到，身体检得到 → 两个人。"""
        guest = _body(x=0.72, w=0.26)
        self.assertEqual(count_people([_face()], [_body(), guest]), (1, 2))

    def test_two_bodies_and_no_face_are_two_people(self):
        self.assertEqual(count_people([], [_body(x=0.05, w=0.40), _body(x=0.55, w=0.40)]),
                         (0, 2))

    def test_without_bodies_it_is_exactly_the_face_count(self):
        faces = [_face(), _face(x=0.65, w=0.25)]
        self.assertEqual(count_people(faces, []), (2, 2))
        self.assertEqual(count_people(faces, None), (2, 2))

    def test_low_confidence_body_is_ignored(self):
        weak = _body(x=0.72, w=0.26, conf=sf_presence.FACE_GUARD_BODY_CONF_MIN - 0.05)
        self.assertEqual(count_people([_face()], [_body(), weak]), (1, 1))

    def test_narrow_body_is_ignored(self):
        """身体框太窄 = 离得太远，和脸的距离闸门同一个道理。"""
        far = _body(x=0.72, w=sf_presence.FACE_GUARD_BODY_MIN_W * 0.7)
        self.assertEqual(count_people([_face()], [_body(), far]), (1, 1))

    def test_face_without_geometry_disables_pairing(self):
        """脸读不到几何就没法配对：宁可退回只数脸，也不让主人被数成两个人。"""
        self.assertEqual(count_people([_face(geo=False)], [_body()]), (1, 1))

    def test_two_faces_sharing_one_body_are_matched_one_to_one(self):
        """两张脸的中心都落在大身体框里，其中一张还落在小身体框里。

        逐个判断包含 / 贪心配对会让大框被先到的脸占掉，小框落单，多数出一个人；
        真正的最大匹配是大框配一张、小框配另一张，一共两个人。
        """
        big = _body(x=0.05, y=0.0, w=0.90, h=0.95)
        small = _body(x=0.55, y=0.0, w=0.30, h=0.60)
        shared = _face(x=0.60, y=0.20, w=0.12, h=0.12)       # 中心落在 big 与 small 里
        other = _face(x=0.10, y=0.20, w=0.12, h=0.12)        # 中心只落在 big 里
        self.assertEqual(count_people([shared, other], [big, small]), (2, 2))

    def test_side_edge_body_gets_a_discounted_width_floor(self):
        """被左右边界切掉一部分的人，身体框同样会缩，宽度下限按贴边折算。"""
        w = sf_presence.FACE_GUARD_BODY_MIN_W * 0.8
        self.assertEqual(len(qualifying_bodies([_body(x=0.0, w=w)])), 1)
        self.assertEqual(len(qualifying_bodies([_body(x=0.40, w=w)])), 0)

    def test_top_or_bottom_edge_gets_no_discount(self):
        """坐得近的人上半身天然顶到画面底部，那不代表被切窄。"""
        w = sf_presence.FACE_GUARD_BODY_MIN_W * 0.8
        self.assertEqual(len(qualifying_bodies([_body(x=0.40, y=0.0, w=w)])), 0)

    def test_width_floor_follows_the_wide_view_scale(self):
        """取景放宽后人在画面里变小，宽度下限要同比缩小。"""
        w = sf_presence.FACE_GUARD_BODY_MIN_W * 0.7
        self.assertEqual(len(qualifying_bodies([_body(x=0.40, w=w)], near_scale=1.0)), 0)
        self.assertEqual(len(qualifying_bodies([_body(x=0.40, w=w)], near_scale=0.6)), 1)
        near = sf_presence.FACE_GUARD_NEAR_FACE_W * 0.6
        guest = _body(x=0.72, w=w)
        self.assertEqual(count_people([_face()], [_body(), guest], near_w_min=near), (1, 2))

    def test_face_just_outside_its_own_body_box_is_still_one_person(self):
        """主人的脸中心略出自己的身体框（偏头、贴得近）：外扩之内仍认领，不能被数成两个人。

        身体框 x 从 0.50 到 0.80；脸中心落在 0.47，在框外 0.03、外扩 0.05 之内。
        """
        body = _body(x=0.50, w=0.30)
        near_miss = _face(x=0.32, w=0.30)
        self.assertEqual(count_people([near_miss], [body]), (1, 1))

    def test_face_far_outside_a_body_box_does_not_claim_it(self):
        """脸中心在框外 0.10（超出外扩）：那是另一个人的身体，不认领，所以是两个人。"""
        body = _body(x=0.50, w=0.30)
        far_miss = _face(x=0.25, w=0.30)
        self.assertEqual(count_people([far_miss], [body]), (1, 2))

    def test_body_with_unreadable_confidence_is_skipped(self):
        """读不到把握分的身体框不计：身体框是新增的一路，读不出来时不能自动获得计数资格。"""
        class _NoConfidence(_GeoBox):
            def confidence(self):
                raise RuntimeError("读不到把握分")

        broken = _NoConfidence(0.9, 0.72, 0.0, 0.26, 0.9)
        self.assertEqual(count_people([_face()], [_body(), broken]), (1, 1))

    def test_all_bodies_filtered_out_means_the_face_count(self):
        weak = _body(conf=sf_presence.FACE_GUARD_BODY_CONF_MIN - 0.2)
        self.assertEqual(count_people([_face()], [weak]), (1, 1))

    def test_two_faces_competing_for_one_body_leave_the_second_person_uncounted_twice(self):
        """两张脸中心都落在同一个身体框里：一对一配对只能认领一次，剩下那张脸本来就是一个人。"""
        big = _body(x=0.05, y=0.0, w=0.90, h=0.95)
        first = _face(x=0.10, y=0.20, w=0.12, h=0.12)
        second = _face(x=0.60, y=0.20, w=0.12, h=0.12)
        self.assertEqual(count_people([first, second], [big]), (2, 2),
                         "两张脸 + 一个身体框 = 两个人，不能因为抢不到身体框被多数或少数")

    def test_unreadable_body_geometry_is_skipped_and_counted(self):
        before = sf_presence.body_geometry_failures()
        with patch.object(sf_presence, "log_line_throttled", lambda *a, **k: None):
            got = qualifying_bodies([_GeoBox(0.9, 0.2, 0.0, 0.5, 0.9, geo=False), _body()])
        self.assertEqual(len(got), 1, "读不到几何的身体框不计，其余照常")
        self.assertEqual(sf_presence.body_geometry_failures(), before + 1)

class TestEffectiveCountAndArbiter(SFCase):
    """人数怎么变成喂给迟滞状态机的数；只见身体不见脸时必须既不收窗也不恢复。"""

    def test_two_or_more_people_pass_through(self):
        self.assertEqual(effective_count(1, 2), 2)
        self.assertEqual(effective_count(0, 2), 2)
        self.assertEqual(effective_count(2, 3), 3)

    def test_a_lone_body_counts_as_zero_faces(self):
        """只有一个身体、没有脸：按 0 处理，不是 1。"""
        self.assertEqual(effective_count(0, 1), 0)
        self.assertEqual(effective_count(1, 1), 1)
        self.assertEqual(effective_count(0, 0), 0)

    def test_lone_body_never_restores_but_a_second_body_hides(self):
        a = PresenceArbiter(dwell_sec=0.0, restore_dwell_sec=0.0)
        hide = effective_count(*count_people([_face()], [_body(), _body(x=0.72, w=0.26)]))
        self.assertEqual(hide, 2)
        events = [a.feed(hide, now=0.1 * i) for i in range(a.hide_hits)]
        self.assertIn(EVENT_HIDE, events, "脸 + 另一个人的背影 → 收窗")
        lone = effective_count(*count_people([], [_body()]))
        got = [a.feed(lone, now=1.0 + 0.1 * i) for i in range(a.restore_hits + 3)]
        self.assertNotIn(EVENT_RESTORE, got,
                         "主人走后只剩一个身体时不能把行情亮出来：这正是功能要防的事")
        one_face = effective_count(*count_people([_face()], [_body()]))
        got = [a.feed(one_face, now=3.0 + 0.1 * i) for i in range(a.restore_hits + 1)]
        self.assertIn(EVENT_RESTORE, got, "脸和身体配成一个人，才算主人回来了")


class _Req:
    """Vision 请求替身：alloc().init() 返回自己；results() 给出预设的观测。"""

    def __init__(self, results=()):
        self.observations = list(results)
        self.upper_body_only = None

    def alloc(self):
        return self

    def init(self):
        return self

    def setUpperBodyOnly_(self, flag):
        self.upper_body_only = bool(flag)

    def results(self):
        return list(self.observations)


class _Handler:
    """handler 替身：按 mode 决定"两个请求一起 / 只有脸"各自成败。"""

    def __init__(self):
        self.combined = "ok"        # ok | fail | raise
        self.single = "ok"          # ok | fail
        self.calls = []

    def alloc(self):
        return self

    def initWithCMSampleBuffer_options_(self, buffer, options):
        return self

    def performRequests_error_(self, requests, error):
        self.calls.append(len(requests))
        mode = self.combined if len(requests) > 1 else self.single
        if mode == "raise":
            raise RuntimeError("Vision 抛异常")
        return mode == "ok"


class TestBodyInTheCaptureCallback(SFCase):
    """直接驱动 `_on_frame`：这是采集线程唯一的入口，走公开 API 就得真起摄像头会话。"""

    def setUp(self):
        super().setUp()
        self.swallowed = []
        self.logged = []
        self.track_patch(patch.object(sf_presence, "log_swallow",
                                      lambda *a, **k: self.swallowed.append(a)))
        self.track_patch(patch.object(sf_presence, "log_line",
                                      lambda *a, **k: self.logged.append(a)))
        self.track_patch(patch.object(sf_presence, "log_line_throttled",
                                      lambda *a, **k: None))
        self.track_patch(patch.object(sf_presence, "feature_enabled", lambda name: True))
        self.faces = _Req([_face()])
        self.bodies = _Req([_body()])
        self.handler = _Handler()
        self.mon = FacePresenceMonitor()
        self.mon._cls = {                                  # discipline-ok(R5): 采集会话的唯一注入口
            "VNDetectFaceRectanglesRequest": self.faces,
            "VNDetectHumanRectanglesRequest": self.bodies,
            "VNImageRequestHandler": self.handler}
        self.mon.state = STATE_RUNNING

    def frame(self):
        self.mon._last_detect = 0.0                        # discipline-ok(R5): 不设就被节拍丢掉
        self.mon._on_frame(None, object(), None)           # discipline-ok(R5): 同上
        return self.mon.snapshot()[0]

    def test_owner_alone_counts_one_and_requests_upper_body_only(self):
        self.assertEqual(self.frame(), 1)
        self.assertEqual(self.handler.calls, [2], "脸和人体请求必须放进同一次 performRequests")
        self.assertTrue(self.bodies.upper_body_only, "只要上半身，别跑全身检测")
        self.assertEqual((self.mon.body_frames, self.mon.body_assisted), (1, 0))

    def test_guest_seen_only_from_behind_makes_two(self):
        self.bodies.observations = [_body(), _body(x=0.72, w=0.26)]
        self.assertEqual(self.frame(), 2)
        assisted = self.mon.body_assisted
        self.assertEqual(assisted, 1, "身体框补出了人，这一次要有记录")

    def test_lone_body_without_a_face_is_zero_not_one(self):
        self.faces.observations = []
        self.assertEqual(self.frame(), 0)
        assisted = self.mon.body_assisted
        self.assertEqual(assisted, 0)

    def test_missing_body_class_means_faces_only(self):
        del self.mon._cls["VNDetectHumanRectanglesRequest"]    # discipline-ok(R5): 模拟系统没有这个类
        self.bodies.observations = [_body(), _body(x=0.72, w=0.26)]
        self.assertEqual(self.frame(), 1)
        self.assertEqual(self.handler.calls, [1])

    def test_feature_switch_off_means_faces_only(self):
        with patch.object(sf_presence, "feature_enabled", lambda name: False):
            self.bodies.observations = [_body(), _body(x=0.72, w=0.26)]
            self.assertEqual(self.frame(), 1)
        self.assertEqual(self.handler.calls, [1], "开关关掉后连请求都不该发")

    def test_combined_failure_falls_back_to_faces_in_the_same_frame(self):
        """人体请求不能拖累已有的脸保护：同一帧退回只数脸，这一帧的脸照样数得出来。"""
        self.handler.combined = "fail"
        self.assertEqual(self.frame(), 1)
        self.assertEqual(self.handler.calls, [2, 1])
        self.assertEqual((self.mon.body_detect_errors, self.mon.detect_errors), (1, 0))

    def test_combined_exception_is_swallowed_with_a_trace(self):
        self.handler.combined = "raise"
        self.assertEqual(self.frame(), 1)
        self.assertTrue(self.swallowed, "兜异常必须留 SWALLOWED 痕")
        failures = self.mon.body_detect_errors
        self.assertEqual(failures, 1)

    def test_body_result_read_failure_falls_back_to_faces(self):
        """合跑完成后人体 results() 坏了，脸已经数完，不能把脸保护一起关掉。"""
        class _BadResults(_Req):
            def results(self):
                raise RuntimeError("人体结果读取失败")

        self.mon._cls["VNDetectHumanRectanglesRequest"] = _BadResults([_body()])  # discipline-ok(R5): 模拟系统结果桥接异常
        self.assertEqual(self.frame(), 1, "人体结果坏了仍要保留这一帧的脸计数")
        self.assertEqual((self.mon.detect_errors, self.mon.body_detect_errors), (0, 1))
        self.assertTrue(self.swallowed, "人体结果读取异常必须留痕")

    def test_body_request_that_cannot_be_built_means_faces_only_and_a_failure_count(self):
        """alloc().init() 返回 None：这一帧退回只数脸，并按人体失败记账。"""
        class _NoInit(_Req):
            def init(self):
                return None

        self.mon._cls["VNDetectHumanRectanglesRequest"] = _NoInit()   # discipline-ok(R5): 模拟系统给不出请求对象
        self.assertEqual(self.frame(), 1)
        self.assertEqual(self.handler.calls, [1], "造不出人体请求就只发脸请求")
        failures = self.mon.body_detect_errors
        self.assertEqual(failures, 1)

    def test_body_request_construction_error_is_swallowed_with_a_trace(self):
        class _Explodes(_Req):
            def alloc(self):
                raise RuntimeError("构造炸了")

        self.mon._cls["VNDetectHumanRectanglesRequest"] = _Explodes()  # discipline-ok(R5): 模拟系统构造请求时抛错
        self.assertEqual(self.frame(), 1, "人体请求构造出错不能拖累脸保护")
        self.assertTrue(self.swallowed, "兜异常必须留 SWALLOWED 痕")
        failures = self.mon.body_detect_errors
        self.assertEqual(failures, 1)

    def test_restart_does_not_carry_the_previous_session_count(self):
        """上一段会话的人数不能带进下一段：否则启动瞬间就是两个人会被当成"刚从一个人变成两个人"。"""
        self.assertEqual(self.frame(), 1)
        self.assertTrue(self.mon.stop(wait=True))
        self.mon.state = STATE_RUNNING
        self.faces.observations = [_face(), _face(x=0.65, w=0.25)]
        self.assertEqual(self.frame(), 2)
        opened_until = self.mon._burst_until                          # discipline-ok(R5): 复检窗口唯一状态，无公开口
        self.assertEqual(opened_until, 0.0, "首个样本没有变化证据，不该开逐帧复检窗口")

    def test_failure_from_a_stale_session_is_not_counted(self):
        """会话已经换代（停过又起）的迟到失败，不许记到新会话头上。"""
        stale = self.mon._session_gen - 1                            # discipline-ok(R5): 造一个已过期的会话代号
        self.mon._note_body_failure(stale)                           # discipline-ok(R5): 迟到失败的唯一入口
        failures = self.mon.body_detect_errors
        self.assertEqual(failures, 0)

    def test_when_even_faces_alone_fail_it_is_a_generic_error(self):
        """连只数脸也失败，不能怪人体请求，也不能把快照写成 0。"""
        self.handler.combined = "fail"
        self.handler.single = "fail"
        self.frame()
        self.assertEqual((self.mon.detect_errors, self.mon.body_detect_errors), (1, 0))
        self.assertEqual(self.mon.snapshot(), (0, 0.0), "失败帧不许写进快照")

    def test_three_failures_in_a_row_give_up_bodies_for_the_session(self):
        self.handler.combined = "fail"
        for _ in range(sf_presence.FACE_GUARD_BODY_MAX_FAILS):
            self.frame()
        self.assertTrue(self.mon.body_disabled)
        self.assertTrue(any("只数脸" in str(x) for x in self.logged), "放弃要留一行日志")
        self.handler.calls.clear()
        self.handler.combined = "ok"
        self.bodies.observations = [_body(), _body(x=0.72, w=0.26)]
        self.assertEqual(self.frame(), 1, "放弃之后只数脸")
        self.assertEqual(self.handler.calls, [1], "放弃之后不再发人体请求")

    def test_a_success_resets_the_failure_streak(self):
        limit = sf_presence.FACE_GUARD_BODY_MAX_FAILS
        self.handler.combined = "fail"
        for _ in range(limit - 1):
            self.frame()
        self.handler.combined = "ok"
        self.frame()
        self.handler.combined = "fail"
        for _ in range(limit - 1):
            self.frame()
        self.assertFalse(self.mon.body_disabled, "只有连续失败才放弃，间隔的失败不该累计")

    def test_stop_clears_the_body_counters_and_the_give_up_flag(self):
        self.handler.combined = "fail"
        for _ in range(sf_presence.FACE_GUARD_BODY_MAX_FAILS):
            self.frame()
        self.assertTrue(self.mon.stop(wait=True))
        self.assertEqual((self.mon.body_frames, self.mon.body_assisted,
                          self.mon.body_detect_errors), (0, 0, 0))
        self.assertFalse(self.mon.body_disabled, "上一段会话放弃的人体检测不带进新会话")

class TestOptionalBodyClass(SFCase):
    """人体检测类是可选的：取不到只退回只数脸，不能让整个功能起不来。"""

    def test_real_framework_exposes_the_body_class_on_this_mac(self):
        cls, why = sf_presence._load_frameworks()          # discipline-ok(R5): 真装载一次，取类不需要摄像头权限
        if cls is None:
            self.skipTest("这台机器装载不了 Vision / AVFoundation：%s" % why)
        self.assertIn("VNDetectHumanRectanglesRequest", cls,
                      "本机应当取得到上半身检测类；取不到则上半身检测在这台机器上是关的")

    def test_lookup_failure_of_the_optional_class_keeps_the_rest_working(self):
        import objc
        real = objc.lookUpClass

        def flaky(name):
            if name == "VNDetectHumanRectanglesRequest":
                raise RuntimeError("类不存在")
            return real(name)

        self.track_patch(patch.object(sf_presence, "log_line", lambda *a, **k: None))
        self.track_patch(patch.object(sf_presence, "log_line_throttled", lambda *a, **k: None))
        with patch.object(objc, "lookUpClass", flaky):
            cls, why = sf_presence._load_frameworks()      # discipline-ok(R5): 要证的就是装载函数本身的降级
        if cls is None:
            self.skipTest("这台机器装载不了 Vision / AVFoundation：%s" % why)
        self.assertNotIn("VNDetectHumanRectanglesRequest", cls)
        self.assertIn("VNDetectFaceRectanglesRequest", cls, "必需的脸检测类不受影响")

    def test_missing_class_warning_is_throttled_not_logged_every_call(self):
        """装载函数会被授权查询、重试反复调用：同一原因必须走限流日志，不能每次都刷一行。"""
        import objc
        real = objc.lookUpClass

        def flaky(name):
            if name == "VNDetectHumanRectanglesRequest":
                raise RuntimeError("类不存在")
            return real(name)

        plain, throttled = [], []
        self.track_patch(patch.object(sf_presence, "log_line",
                                      lambda *a, **k: plain.append(a)))
        self.track_patch(patch.object(sf_presence, "log_line_throttled",
                                      lambda *a, **k: throttled.append(a)))
        with patch.object(objc, "lookUpClass", flaky):
            cls, why = sf_presence._load_frameworks()      # discipline-ok(R5): 要证的就是装载函数本身的日志口径
        if cls is None:
            self.skipTest("这台机器装载不了 Vision / AVFoundation：%s" % why)
        self.assertEqual(len(throttled), 1, "类不可用要留一条限流日志")
        self.assertFalse(any("上半身检测类" in str(a) for a in plain),
                         "不能再走不限流的 log_line：每次调用都会刷一行")


class TestStatusSentenceIsHonestAboutBodies(SFCase):
    """状态文案不能对没在跑的保护说有：上半身那半句只在它真的可用时才写。"""

    def data(self, **changes):
        result = dict(available=True, enabled_by_user=True, stalled=False,
                      paused=False, screen_off=False, state=STATE_RUNNING,
                      stale_sec=30, max_idle_sec=120, detect_errors=0,
                      zero_frame_sec=0, hidden_by_guard=False, tick_sec=0.25,
                      hide_hits=2, dwell_sec=0.25, conf_min=0.6, edge_conf_min=0.55,
                      near_face_w=0.065, edge_shrink=0.61, restore_hits=5, reason="")
        result.update(changes)
        return result

    def text(self, **changes):
        return window.FloatWindow.face_guard_status_text(types.SimpleNamespace(),
                                                         self.data(**changes))

    def test_body_available_says_upper_body(self):
        self.assertIn("脸看不到时也认上半身", self.text(body_available=True, body_disabled=False))

    def test_body_unavailable_falls_back_to_the_face_only_wording(self):
        text = self.text(body_available=False, body_disabled=False)
        self.assertNotIn("上半身", text, "类缺失或开关关掉时不能声称认上半身")
        self.assertIn("第二张脸", text)

    def test_old_diagnostic_data_without_body_keys_is_face_only(self):
        self.assertNotIn("上半身", self.text())

    def test_session_that_gave_up_bodies_says_so_and_drops_the_claim(self):
        text = self.text(body_available=True, body_disabled=True)
        self.assertNotIn("脸看不到时也认上半身", text)
        self.assertIn("已放弃", text, "放弃了人体检测要如实说出来")


class _ZoomLockDevice:
    """相机设备替身：记录调用顺序，变焦可被设置，锁和设置结果可切换。"""

    def __init__(self, zoom):
        self.zoom = zoom
        self.calls = []
        self.lock_result = True
        self.set_error = None
        self.unlock_error = None
        self.read_error_after_set = False

    def videoZoomFactor(self):
        if self.read_error_after_set and "set" in self.calls:
            raise RuntimeError("设置之后读不回")
        return self.zoom

    def lockForConfiguration_(self, error):
        self.calls.append("lock")
        return self.lock_result

    def setVideoZoomFactor_(self, value):
        self.calls.append("set")
        if self.set_error is not None:
            raise self.set_error
        self.zoom = value

    def unlockForConfiguration(self):
        self.calls.append("unlock")
        if self.unlock_error is not None:
            raise self.unlock_error


class TestZoomIsRestoredWhenTheSessionEnds(SFCase):
    """变焦是设备属性，不一定随会话消失；改过就要还原，别让别的应用继承被拉宽的画面。"""

    def setUp(self):
        super().setUp()
        self.track_patch(patch.object(sf_presence, "log_line", lambda *a, **k: None))
        self.track_patch(patch.object(sf_presence, "log_swallow", lambda *a, **k: None))
        self.mon = FacePresenceMonitor()

    def widen(self, device):
        self.mon._apply_wide_view(device)                  # discipline-ok(R5): 取景放宽的唯一入口，没有公开口

    def test_stop_puts_the_zoom_back(self):
        device = _ZoomLockDevice(1.5)
        self.widen(device)
        self.assertEqual(device.zoom, sf_presence.FACE_GUARD_WIDE_ZOOM)
        self.assertTrue(self.mon.stop(wait=True))
        self.assertEqual(device.zoom, 1.5, "会话结束必须还原成改之前的变焦")
        self.assertTrue(self.mon.applied_profile["zoom_restored"])
        self.assertEqual(device.calls[-2:], ["set", "unlock"], "还原也要先上锁、最后一定解锁")

    def test_nothing_to_restore_when_the_zoom_was_never_changed(self):
        device = _ZoomLockDevice(1.0)
        self.widen(device)
        device.calls.clear()
        self.assertTrue(self.mon.stop(wait=True))
        self.assertEqual(device.calls, [], "没改过就不许碰设备")

    def test_does_not_overwrite_a_zoom_someone_else_changed(self):
        device = _ZoomLockDevice(1.5)
        self.widen(device)
        device.zoom = 2.0                                  # 别的程序在会话期间改了变焦
        device.calls.clear()
        self.assertTrue(self.mon.stop(wait=True))
        self.assertEqual(device.zoom, 2.0, "当前值已不是我们设的，就不去覆盖别人的")
        self.assertNotIn("set", device.calls)

    def test_denied_lock_never_blocks_the_stop(self):
        device = _ZoomLockDevice(1.5)
        self.widen(device)
        device.lock_result = False
        self.assertTrue(self.mon.stop(wait=True), "还原失败不能让停会话失败")
        self.assertFalse(self.mon.applied_profile["zoom_restored"])

    def test_restore_failure_still_unlocks_and_never_blocks_the_stop(self):
        device = _ZoomLockDevice(1.5)
        self.widen(device)
        device.set_error = RuntimeError("设备拒绝")
        device.calls.clear()
        self.assertTrue(self.mon.stop(wait=True))
        self.assertEqual(device.calls[-1], "unlock", "设置抛错也必须解锁")

    def test_unlock_failure_while_widening_does_not_undo_the_widening(self):
        """设置已经成功，只是解锁抛错：照样算放宽成功，异常被吞掉不外抛。"""
        device = _ZoomLockDevice(1.5)
        device.unlock_error = RuntimeError("解锁失败")
        self.widen(device)
        self.assertTrue(self.mon.applied_profile["zoom_applied"])
        self.assertLess(self.mon.near_w_min, sf_constants_near_gate())

    def test_unreadable_zoom_after_setting_keeps_the_default_gate(self):
        """设置后读不回，就没法证明生效：距离闸门维持原值，也不声称放宽成功。"""
        device = _ZoomLockDevice(1.5)
        device.read_error_after_set = True
        self.widen(device)
        self.assertFalse(self.mon.applied_profile["zoom_applied"])
        self.assertEqual(self.mon.near_w_min, sf_constants_near_gate())

    def test_restores_only_once(self):
        device = _ZoomLockDevice(1.5)
        self.widen(device)
        self.mon.stop(wait=True)
        # 让"当前值恰好又等于我们当初设的值"：此时只有"还原后清掉待还原标记"能拦住二次还原，
        # 否则"当前值不是我们设的"那道保护会替它挡住，这条用例就测不到清标记。
        device.zoom = sf_presence.FACE_GUARD_WIDE_ZOOM
        device.calls.clear()
        self.mon.stop(wait=True)
        self.assertEqual(device.calls, [], "第二次停表不该再碰设备")


if __name__ == "__main__":
    unittest.main()
