# -*- coding: utf-8 -*-
"""sf_glass 覆盖率专项第一批（遗留 C2）。

补的是**玻璃层判定与几何核对**这一整块：它此前 63%（236 行未覆盖），而这块代码的
失效方式恰好是用户最难自查的那类 —— "背景层停在别处""只剩背景""置顶不生效"。
这些分支一旦没用例，改动只靠肉眼在真屏幕上看不出来。

两块具体落点：① 置顶体检与几何核对（`_content_cg_bounds` / `_reconcile_model_with_server`
/ `_top_state_ok` / `_glass_real_state` / `_force_join_active_space`）；② 圆角遮罩的
重建决策与落地（`should_rebuild_glass_mask` / `glass_mask_update_plan` / `_glass_mask_flush`
/ `disable_native_window_shadow`）—— 第二块是"快速拉伸时顶部背景变形、闪烁"那串故障的
决策点，全是纯函数或可用假宿主驱动的短路径。

手法沿用本仓既有做法（`tests/test_v1426_glass_zorder.py`）：私有 CGS/Quartz 一律喂
假对象，不碰真 WindowServer。这里刻意**不**测需要真 cocoa 窗口的 `_setup_glass` /
`_glass_follow_begin` 等大段（那些得走子进程 harness，另批处理）。

被检方法都是 GlassMixin 的私有实现，没有公开出口，别名与豁免集中在本文件顶部。
"""

import ctypes
import os
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import Quartz  # noqa: E402  # 先真导入：桩只打在函数上，绝不动 sys.modules
import sf_glass  # noqa: E402
from sf_glass import GlassMixin  # noqa: E402
from tests._base import SFCase  # noqa: E402

_cg_bounds = GlassMixin._content_cg_bounds              # discipline-ok(R5): 被测对象本身就是私有实现
_real_bounds = GlassMixin._content_real_bounds          # discipline-ok(R5): 同上
_reconcile = GlassMixin._reconcile_model_with_server    # discipline-ok(R5): 同上
_top_ok = GlassMixin._top_state_ok                      # discipline-ok(R5): 同上
_glass_state = GlassMixin._glass_real_state             # discipline-ok(R5): 同上
_covers = GlassMixin._glass_covers_main                  # discipline-ok(R5): 同上
_blind = GlassMixin._log_blind                          # discipline-ok(R5): 同上
_order_below = GlassMixin._order_glass_below_content    # discipline-ok(R5): 同上
_join_space = GlassMixin._force_join_active_space       # discipline-ok(R5): 同上
_mask_flush = GlassMixin._glass_mask_flush           # discipline-ok(R5): 同上
_follow_end = GlassMixin._glass_follow_end           # discipline-ok(R5): 同上

_REPROBE = sf_glass._CGS_REPROBE_INTERVAL      # discipline-ok(R5): 节流窗口是被测行为的一部分，只能从实现取值
_Z_INTERVAL = sf_glass._Z_ORDER_CHECK_INTERVAL  # discipline-ok(R5): 同上


class _Host:
    """只带被检方法用到的成员。

    内部会互调的兄弟方法必须一起挂上（`_glass_covers_main` 要调 `_glass_real_state`、
    `_content_real_bounds` 要调 `_content_cg_bounds`、`_reconcile` 同理）—— 否则测的
    就不是那条真实调用链。
    """

    _content_cg_bounds = _cg_bounds
    _content_real_bounds = _real_bounds
    _glass_real_state = _glass_state

    def __init__(self, x=100, y=200, height=224, **extra):
        self.x = lambda: x
        self.y = lambda: y
        self.height = lambda: height
        self.retop = []
        self.moved = []

        def _move(x, y):
            self.moved.append((x, y))
        self.move = _move
        self._log_retop = lambda reason, detail="": self.retop.append(reason + detail)
        for key, value in extra.items():
            setattr(self, key, value)


class _Win:
    def __init__(self, level=25, visible=True, on_space=True, behavior=None,
                 frame=None, number=7, raise_on=None):
        self._level = level
        self._visible = visible
        self._on_space = on_space
        self._behavior = behavior
        self._frame = frame or ((0, 0), (184, 224))
        self._number = number
        self._raise_on = raise_on or set()
        self.moved = []
        self.ordered = []

    def level(self):
        if "level" in self._raise_on:
            raise RuntimeError("boom")
        return self._level

    def isVisible(self):
        return self._visible

    def hidesOnDeactivate(self):
        return False

    def ignoresMouseEvents(self):
        return False

    def isOnActiveSpace(self):
        return self._on_space

    def collectionBehavior(self):
        return self._behavior

    def frame(self):
        return self._frame

    def windowNumber(self):
        return self._number

    def move(self, x, y):
        self.moved.append((x, y))

    def orderWindow_relativeTo_(self, ordering, relative):
        self.ordered.append((ordering, relative))


def _quartz_rows(rows=None, raises=False):
    """把 `CGWindowListCopyWindowInfo` 换成受控返回。

    为什么不打 sys.modules 的假模块：`mock.patch.dict` 退出时会删掉"块内首次导入"
    的条目，把 PyObjC 的 objc._objc 一起冲掉，同一进程后面的真 import 立刻报
    `Reload of objc._objc detected`（实测带崩 test_v1426 的 22 条用例）。
    """

    def fake(option, number):
        if raises:
            raise RuntimeError("window server down")
        return rows if rows is not None else []

    return mock.patch.object(Quartz, "CGWindowListCopyWindowInfo", fake)


def _bounds_rows(bounds):
    return [{"kCGWindowBounds": {"X": bounds[0], "Y": bounds[1],
                                 "Width": bounds[2], "Height": bounds[3]}}]


class CgBoundsTest(SFCase):
    def test_reads_the_rectangle_in_cg_global_coordinates(self):
        host = _Host()
        with _quartz_rows(_bounds_rows((639.0, 348.0, 184.0, 224.0))):
            self.assertEqual(_cg_bounds(host, cnum=7), (639.0, 348.0, 184.0, 224.0))

    def test_unusable_answers_all_return_none(self):
        """查不到 / 没有矩形字段 / 查询抛异常，三种都只能返回 None 让调用方退回模型口径。"""
        host = _Host()
        for label, rows, raises in (("空列表", [], False), ("缺矩形字段", [{}], False),
                                    ("服务端抛错", None, True)):
            with self.subTest(label), _quartz_rows(rows, raises):
                self.assertIsNone(_cg_bounds(host, cnum=7))

    def test_without_a_number_it_asks_the_content_window(self):
        host = _Host()
        with mock.patch.object(sf_glass, "content_ns_window", return_value=None), \
             _quartz_rows(_bounds_rows((0.0, 0.0, 1.0, 1.0))):
            self.assertIsNone(_cg_bounds(host), "拿不到内容窗就不该猜一个矩形")

    def test_ns_conversion_flips_on_the_main_display_height(self):
        """CG 左上原点、NS 左下原点：换算只用主屏高，与窗口在哪块屏无关。"""
        host = _Host()
        with _quartz_rows(_bounds_rows((639.0, 348.0, 184.0, 224.0))):
            x, y_bottom, w, h = _real_bounds(host, cnum=7, sh=1080.0)
        self.assertEqual((x, y_bottom, w, h), (639.0, 508.0, 184.0, 224.0))

    def test_real_bounds_gives_up_quietly_when_cg_lookup_fails(self):
        host = _Host()
        with _quartz_rows(None, True):
            self.assertIsNone(_real_bounds(host, cnum=7, sh=1080.0))


class ReconcileWithServerTest(SFCase):
    def test_not_cocoa_does_nothing(self):
        host = _Host()
        with mock.patch.object(sf_glass, "native_cocoa_active", return_value=False):
            self.assertFalse(_reconcile(host, "unit"))

    def test_no_content_window_does_nothing(self):
        host = _Host()
        with mock.patch.object(sf_glass, "native_cocoa_active", return_value=True), \
             mock.patch.object(sf_glass, "content_ns_window", return_value=None):
            self.assertFalse(_reconcile(host))

    def test_matching_model_does_not_move_the_window(self):
        """模型与服务端一致时必须什么都不做 —— 白写一次几何就是新的抖动源。"""
        host = _Host(x=639, y=348)
        with mock.patch.object(sf_glass, "native_cocoa_active", return_value=True), \
             mock.patch.object(sf_glass, "content_ns_window", lambda _w: _Win(number=7)), \
             _quartz_rows(_bounds_rows((639.0, 348.0, 184.0, 224.0))):
            self.assertFalse(_reconcile(host, "collapse"))

    def test_stale_model_is_resynced_to_the_server_origin(self):
        """拖动后模型过时（实测会瞬移回起点）：这里必须按服务端原点搬回来。"""
        host = _Host(x=639, y=348)
        with mock.patch.object(sf_glass, "native_cocoa_active", return_value=True), \
             mock.patch.object(sf_glass, "content_ns_window",
                               lambda _w: _Win(number=7)), \
             _quartz_rows(_bounds_rows((939.0, 508.0, 184.0, 260.0))):
            self.assertTrue(_reconcile(host, "collapse"))
        self.assertEqual(host.moved, [(939, 508)],
                         "要把宿主搬到服务端原点；尺寸不同只多写一行取证，不在此处改")


class TopStateTest(SFCase):
    """`_top_state_ok` 的判保守性：任何一项读不出/不符都要重做。

    跟随行为直接用**生产同一份常量**（`public_overlay_collection_behavior()`）。
    本进程离屏也能真 `import AppKit`（实测取到 CanJoinAllSpaces|Stationary|
    FullScreenAuxiliary 的组合值），所以不必桩一个自创数字 —— 桩了之后"体检比的那一位"
    和"抬层写进去的那一位"各改各的也不会红，正好漏掉这条不变量。
    """

    BEHAVIOR = sf_glass.public_overlay_collection_behavior()

    def _win(self, **kw):
        kw.setdefault("behavior", self.BEHAVIOR)
        return _Win(**kw)

    def test_all_five_properties_right_needs_no_work(self):
        self.assertTrue(_top_ok(_Host(), self._win(), None))

    def test_every_single_wrong_property_forces_a_redo(self):
        """漏判只是白干一次；误判成"已经对了"就是用户看得见的故障。"""
        cases = [
            ("level 不是 25", self._win(level=0)),
            ("内容窗不可见", self._win(visible=False)),
            ("不在活动 Space", self._win(on_space=False)),
            ("跟随行为不对", self._win(behavior=1)),
            ("读属性抛异常", self._win(raise_on={"level"})),
        ]
        for why, win in cases:
            with self.subTest(why):
                self.assertFalse(_top_ok(_Host(), win, None))

    def test_glass_layer_must_match_main_window(self):
        bad = [
            {"visible": False},
            {"level": 20},
            {"frame": ((0, 0), (184, 200))},
            {"on_space": False},
        ]
        for patch_kwargs in bad:
            with self.subTest(**patch_kwargs):
                glass = _Win(behavior=self.BEHAVIOR, **patch_kwargs)
                self.assertFalse(_top_ok(_Host(), self._win(), glass))
        self.assertTrue(_top_ok(_Host(), self._win(),
                                _Win(behavior=self.BEHAVIOR)))


class GlassRealStateThrottleTest(SFCase):
    def test_no_glass_layer_concludes_nothing(self):
        host = _Host(_last_z_order_check=0.0)
        self.assertIsNone(_glass_state(host, _Win(), None))

    def test_unasked_and_not_found_are_different_things(self):
        """v1.4.35：'这轮没到节流什么都没问' 与 '问了但查不到' 必须能区分，
        否则每 250ms 里有三拍在写假的"看不见这一对窗"。"""
        host = _Host(_last_z_order_check=time.monotonic(), _z_probe_asked=False)
        self.assertIsNone(_glass_state(host, _Win(), _Win(number=8)))
        self.assertFalse(host._z_probe_asked,   # discipline-ok(R5): 这个标记存在的唯一用途就是给调用方区分"没问/问了没查到"
                                             "没到节流就不该把「问了」记成真")

        host2 = _Host(_last_z_order_check=0.0, _z_probe_asked=False)
        with mock.patch.object(sf_glass, "glass_main_real_state",
                               side_effect=RuntimeError("no pair")) as probe:
            self.assertIsNone(_glass_state(host2, _Win(), _Win(number=8)))
        probe.assert_called_once()
        self.assertTrue(host2._z_probe_asked,  # discipline-ok(R5): 同上，这条判据读的就是它
                            "确实发过查询却没查到，这才是真「盲区」")

    def test_covers_main_only_when_glass_is_on_top(self):
        host = _Host(_last_z_order_check=0.0)
        for state, expect in (({"glass_on_top": True}, True),
                              ({"glass_on_top": False}, False),
                              (None, False)):
            with self.subTest(state=state), \
                 mock.patch.object(sf_glass, "glass_main_real_state", return_value=state):
                h = _Host(_last_z_order_check=0.0)
                self.assertEqual(_covers(h, _Win(), _Win(number=8)), expect)

    def test_blind_probe_leaves_a_trace(self):
        host = _Host()
        _blind(host, _Win(number=7), _Win(number=8))
        self.assertEqual(len(host.retop), 1)
        self.assertIn("pair not both on-screen", host.retop[0])
        self.assertIn("main=7 glass=8", host.retop[0])


class OrderBelowTest(SFCase):
    def test_missing_window_does_nothing(self):
        _order_below(_Host(), None, _Win())
        _order_below(_Host(), _Win(), None)

    def test_invisible_content_window_is_not_used_as_reference(self):
        glass = _Win(number=8)
        _order_below(_Host(), _Win(visible=False), glass)
        self.assertEqual(glass.ordered, [], "参照窗不在屏时宁可不挪")

    def test_orders_glass_below_the_content_window(self):
        """玻璃层要"压在内容窗之下"这件事说给 WindowServer，而不是赌谁最后 orderFront。

        断言用真常量 `AppKit.NSWindowBelow`：本进程离屏也能 `import AppKit`（实测
        `NSMakeRect`、`NSWindowBelow` 都取得到），所以没必要抄一个字面量 -1 ——
        抄数字的断言在 Apple 改了常量值时会一起骗过我们。
        真正不能做的是往 `sys.modules` 塞**假** AppKit：`mock.patch.dict` 退出时会把
        块内首次导入的条目一起删掉，同进程后面的真 import 立刻报
        `Reload of objc._objc detected`（实测带崩 `test_v1426_glass_zorder` 22 条）。
        """
        import AppKit
        glass = _Win(number=8)
        _order_below(_Host(), _Win(number=7), glass)
        self.assertEqual(glass.ordered, [(AppKit.NSWindowBelow, 7)],
                         "参照窗必须是内容窗的窗口号，否则摆的是猜的位置")


class ActiveSpaceProbeTest(SFCase):
    def test_returns_none_when_the_library_answers_nothing(self):
        cg = types.SimpleNamespace(
            CGSMainConnectionID=lambda: 0, CGSGetActiveSpace=lambda c: 0)
        with mock.patch.object(sf_glass, "_cgs_libs",
                               return_value=(ctypes, cg, types.SimpleNamespace())), \
             mock.patch.object(sf_glass, "_CGS_CONN", [None]):
            self.assertIsNone(sf_glass.active_space_id())

    def test_reads_and_caches_the_connection(self):
        calls = []
        cg = types.SimpleNamespace(
            CGSMainConnectionID=lambda: (calls.append("conn") or 7),
            CGSGetActiveSpace=lambda conn: (calls.append(("space", conn)) or 42))
        with mock.patch.object(sf_glass, "_cgs_libs",
                               return_value=(ctypes, cg, types.SimpleNamespace())), \
             mock.patch.object(sf_glass, "_CGS_CONN", [None]):
            self.assertEqual(sf_glass.active_space_id(), 42)
            self.assertEqual(sf_glass.active_space_id(), 42)
        self.assertEqual(calls.count("conn"), 1, "连接句柄要缓存，不能每拍重取")


class JoinActiveSpaceBackoffTest(SFCase):
    """私有 CGS 兼容路径的退避：连错 3 次停 60 秒，之后自动重探。"""

    def _libs(self, space=99, raise_at=None):
        def CGSAddWindowsToSpaces(conn, wins, spaces):
            return None

        cg = types.SimpleNamespace(
            CGSMainConnectionID=lambda: 7,
            CGSGetActiveSpace=lambda conn: space,
            CGSAddWindowsToSpaces=CGSAddWindowsToSpaces)

        def CFNumberCreate(*a, **k):
            return ctypes.c_void_p(1)

        def CFArrayCreate(*a, **k):
            return ctypes.c_void_p(2)

        released = []
        cf = types.SimpleNamespace(CFNumberCreate=CFNumberCreate,
                                   CFArrayCreate=CFArrayCreate,
                                   CFRelease=lambda ref: released.append(ref))
        return (ctypes, cg, cf)

    def test_success_clears_the_failure_streak(self):
        host = _Host()
        with mock.patch.object(sf_glass, "_cgs_libs", lambda: self._libs()):
            _join_space(host, _Win(number=7))
        self.assertTrue(getattr(host, "_cgs_ok", None))
        self.assertEqual(getattr(host, "_cgs_fail", 0), 0)

    def test_unusable_space_counts_as_failure(self):
        host = _Host()
        with mock.patch.object(sf_glass, "_cgs_libs", lambda: self._libs(space=0)):
            _join_space(host, _Win(number=7))
        self.assertEqual(getattr(host, "_cgs_fail", 0), 1)
        self.assertIsNone(getattr(host, "_cgs_ok", None))

    def test_three_failures_disable_then_backoff_skips_the_call(self):
        host = _Host()
        hits = []

        def libs():
            hits.append(1)
            return self._libs(space=0)

        with mock.patch.object(sf_glass, "_cgs_libs", libs):
            for _ in range(3):
                _join_space(host, _Win(number=7))
        self.assertIs(getattr(host, "_cgs_ok", True), False,
                        "连错三次要停手，不然每秒两次撞同一个墙")
        before = len(hits)
        with mock.patch.object(sf_glass, "_cgs_libs", libs):
            _join_space(host, _Win(number=7))
        self.assertEqual(len(hits), before, "退避期内连库都不该加载")

    def test_reprobe_after_the_backoff_window(self):
        host = _Host(_cgs_ok=False,
                     _cgs_disabled_at=time.monotonic() - _REPROBE - 1)
        with mock.patch.object(sf_glass, "_cgs_libs", lambda: self._libs()):
            _join_space(host, _Win(number=7))
        self.assertTrue(getattr(host, "_cgs_ok", None),
                        "过了 60 秒要能自动重探，否则一次抖动永久失效")

    def test_broken_library_is_swallowed_with_counter(self):
        def boom():
            raise OSError("no CoreGraphics")

        host = _Host()
        with mock.patch.object(sf_glass, "_cgs_libs", boom):
            _join_space(host, _Win(number=7))
        self.assertEqual(getattr(host, "_cgs_fail", 0), 1)


class MaskPlanPureTest(SFCase):
    """圆角 mask 的"要不要重建 / 什么时候重建"两个纯函数。

    这是快速拉伸时顶部背景变形、闪烁那一串故障的决策点，逻辑必须能单测：
    v1.2.26/27 逐帧重建 → 两条渲染流水线帧错位；v1.2.43 降频；v1.2.46 改三态计划。
    """

    def test_force_rebuild_at_key_moments(self):
        self.assertTrue(sf_glass.should_rebuild_glass_mask(184, 224, 184, 224, 0.0, 0.0,
                                                           force_mask=True))
        self.assertEqual(sf_glass.glass_mask_update_plan(184, 224, 184, 224, 0.0, 0.0,
                                                         force_mask=True), "immediate")

    def test_same_size_uses_the_cache(self):
        """≤1px 的中间帧差异不重建 —— 动画/拉伸逐帧微调时这是主要的省开销来源。"""
        last = time.monotonic() - 10.0
        self.assertFalse(sf_glass.should_rebuild_glass_mask(185, 224, 184, 224, last,
                                                            time.monotonic()))
        self.assertEqual(sf_glass.glass_mask_update_plan(185, 224, 184, 224,
                                                         time.monotonic(), last), "skip")

    def test_real_change_is_deferred_to_the_next_paint(self):
        """尺寸真变了：计划是 pending（等下一绘制帧同帧做），而不是马上做。"""
        now = time.monotonic()
        self.assertEqual(sf_glass.glass_mask_update_plan(184, 300, 184, 224, now,
                                                         now - 10.0), "pending")
        # 但"降频重建"那条老契约仍要成立：超节流窗才重建
        self.assertTrue(sf_glass.should_rebuild_glass_mask(184, 300, 184, 224,
                                                           now - 10.0, now))
        self.assertFalse(sf_glass.should_rebuild_glass_mask(
            184, 300, 184, 224, now, now,
            throttle=sf_glass.GLASS_MASK_THROTTLE * 10))

    def test_first_ever_frame_waits_for_the_throttle(self):
        """没有历史尺寸时：超过节流窗立即建，否则并入下一绘制帧。"""
        now = time.monotonic()
        self.assertEqual(sf_glass.glass_mask_update_plan(184, 224, 0, 0, now, 0.0),
                         "immediate")
        self.assertEqual(sf_glass.glass_mask_update_plan(184, 224, 0, 0, now, now),
                         "pending")


class ShadowAndFlushTest(SFCase):
    """子窗阴影关闭与 pending 遮罩的落地：四条路径都不能留后患。"""

    def test_offscreen_never_touches_winid_as_a_pointer(self):
        """非 cocoa 平台下 winId() 不是 NSView*，伸手进去是段错误而不是异常 —— 必须直接返回。"""
        calls = []
        with mock.patch.object(sf_glass, "native_cocoa_active", return_value=False), \
             mock.patch.object(sf_glass, "content_ns_window",
                               lambda w: calls.append("不该被调")):
            sf_glass.disable_native_window_shadow(object())
        self.assertEqual(calls, [])

    def test_cocoa_turns_shadow_and_opaqueness_off(self):
        calls = []
        nsw = _Win()
        nsw.setHasShadow_ = lambda flag: calls.append(("shadow", flag))
        nsw.setOpaque_ = lambda flag: calls.append(("opaque", flag))
        with mock.patch.object(sf_glass, "native_cocoa_active", return_value=True), \
             mock.patch.object(sf_glass, "content_ns_window", lambda _w: nsw):
            sf_glass.disable_native_window_shadow(object())
        self.assertEqual(calls, [("shadow", False), ("opaque", False)])

    def test_broken_appkit_is_not_fatal(self):
        def boom(_w):
            raise RuntimeError("no nsview")

        with mock.patch.object(sf_glass, "native_cocoa_active", return_value=True), \
             mock.patch.object(sf_glass, "content_ns_window", boom):
            self.assertIsNone(sf_glass.disable_native_window_shadow(object()))

    def test_pending_flag_never_survives_the_flush(self):
        """pending 必须被清掉：留着就是每帧重建，退回 v1.2.26 那个逐帧卡顿。"""
        done = []
        host = _Host(height=224, _glass_mask_pending=True, _glass_view=object(),
                     _glass_mask_w=0, _glass_mask_h=0, _last_glass_mask_ts=0.0,
                     width=lambda: 184,
                     _set_glass_mask=lambda v, w, h: done.append((w, h)))
        _mask_flush(host)
        self.assertEqual(done, [(184, 224)])
        self.assertFalse(getattr(host, "_glass_mask_pending", True),
                        "pending 标志必须清掉")
        self.assertEqual((getattr(host, "_glass_mask_w", None),
                      getattr(host, "_glass_mask_h", None)), (184, 224))

    def test_flush_without_a_glass_view_only_clears_the_flag(self):
        host = _Host(_glass_mask_pending=True, _glass_view=None)
        _mask_flush(host)
        self.assertFalse(getattr(host, "_glass_mask_pending", True),
                        "pending 标志必须清掉")

    def test_failed_rebuild_is_traced_and_still_clears_the_flag(self):
        def boom(*a):
            raise RuntimeError("mask rasterize failed")

        host = _Host(height=224, _glass_mask_pending=True, _glass_view=object(),
                     _set_glass_mask=boom, _glass_mask_w=0, _glass_mask_h=0,
                     _last_glass_mask_ts=0.0, width=lambda: 184)
        with mock.patch.object(sf_glass, "log_swallow") as swallow:
            _mask_flush(host)
        swallow.assert_called_once()
        self.assertFalse(getattr(host, "_glass_mask_pending", True),
                        "失败也要清标志，否则永久逐帧重建")

    def test_follow_end_without_a_timer_only_resets_state(self):
        host = _Host(_glass_follow_timer=None, _glass_follow_pos=(1, 2, 3, 4),
                     _glass_follow_cnum=7, _glass_follow_sh=900.0)
        _follow_end(host)
        self.assertIsNone(getattr(host, "_glass_follow_pos", "未重置"))
        self.assertIsNone(getattr(host, "_glass_follow_cnum", "未重置"))
        self.assertEqual(getattr(host, "_glass_follow_sh", None), 0.0)

    def test_follow_end_invalidates_the_real_timer(self):
        try:
            import CoreFoundation
        except Exception:
            self.skipTest("本机取不到 CoreFoundation 桥接")
        host = _Host(_glass_follow_timer=object(), _glass_follow_pos=None,
                     _glass_follow_cnum=None, _glass_follow_sh=0.0)
        with mock.patch.object(CoreFoundation, "CFRunLoopTimerInvalidate") as inv, \
             mock.patch.object(CoreFoundation, "CFRunLoopRemoveTimer") as rm, \
             mock.patch.object(CoreFoundation, "CFRunLoopGetMain",
                               return_value="main-loop", create=False):
            _follow_end(host)
        inv.assert_called_once()
        self.assertEqual(rm.call_count, 1, "不摘掉就是松手之后还在 8ms 一跑")
        self.assertIsNone(getattr(host, "_glass_follow_timer", "still-here"),
                          "定时器引用必须交出去")


if __name__ == "__main__":
    unittest.main()
