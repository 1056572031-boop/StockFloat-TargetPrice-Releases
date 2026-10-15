# -*- coding: utf-8 -*-
"""主窗交互收口补测（遗留：`stock_float_window.py` 缺口的第二批）。

为什么挑这三块（本轮为扩大自动化覆盖而加）
------------------------------------------
它们都是「用户的手正在做的那个动作」的**收口**，坏掉的形态完全一致：动作看起来做了，
但状态没落盘、或者下一步把上一步又推翻。

  · `mouseReleaseEvent` —— 松手这一拍要按**固定顺序**做完五件事（停跟随 → 把模型
    对齐到 WindowServer 真值 → 边缘吸附 → 背景层按服务端位置对齐 → 落盘）。
    v1.4.36 的实测教训就写在源码注释里：吸附读的是 Qt 缓存的原点，缓存还没追上时
    "贴边落点"是照着**旧位置**算的，一次 `move()` 把卡片搬回半路。所以本文件守的是
    **顺序**，不只是「调用过」。
  · `_open_target_panel` / `_place_panel` —— 铃铛的开关语义（二次点收起）、
    互斥闸门、切股后旧面板必须重建、右侧放不下翻到左侧并做边界收敛。
    坏掉的形态是「点了没反应」或「面板生成到屏幕外拿不回来」。

手法沿用本仓既有做法（`tests/test_v1443_update_feedback.py`）：把被检方法绑到
一个只装配相关成员的宿主上，协作者用记录器。这样每条判据都是确定性的，
不依赖屏幕上真有窗口。被检的都是私有实现，别名与豁免集中在文件顶部。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QPoint, Qt

import stock_float_window as m
import target_panel
from tests._base import SFCase

_QAPP = None


def _ensure_qapp():
    """QApplication 必须由模块级引用持有，否则离屏下会中途 abort（同 test_v1443 的做法）。"""
    global _QAPP
    if _QAPP is None:
        _QAPP = SFCase.ensure_qapp()
    return _QAPP


_release = m.FloatWindow.mouseReleaseEvent            # discipline-ok(R5): 被测对象本身就是事件收口
_place = m.FloatWindow._place_panel                   # discipline-ok(R5): 同上
_open_panel = m.FloatWindow._open_target_panel        # discipline-ok(R5): 同上
_close_panel = m.FloatWindow._close_target_panel      # discipline-ok(R5): 同上

# 主窗默认卡片尺寸与面板尺寸：断言相对关系用，不写裸数字
CARD_W, CARD_H = 184, 224
PANEL_W, PANEL_H = 244, 240
GAP = 12                                              # 面板与主窗的固定间距（源码里就这一个来源）
EDGE_X, EDGE_Y, SCREEN_W, SCREEN_H = 0, 0, 1440, 900  # 注入的屏幕可用区，够算翻转与收敛
COLLAPSED_H = 96
TARGET_H = 320                            # "拉伸还没应用完"的那个目标高度
PRICE = 12.34                             # 面板取实时价时用到的现价                              # 收起态高度：只用来判"这次拉伸算不算展开态"


class _Mouse:
    """一条最小 QMouseEvent：只回答松手这一拍真的会读的四样。"""

    def __init__(self, pos=(60, 60), button=Qt.MouseButton.LeftButton):
        self._pos = QPoint(*pos)
        self._button = button

    def button(self):
        return self._button

    def pos(self):
        return self._pos

    def globalPosition(self):
        return mock.Mock(toPoint=lambda: QPoint(1000 + self._pos.x(), 1000 + self._pos.y()))


class _Rec:
    """把协作者调用按顺序记下来：本文件一半的判据判的是**顺序**，不是「发生过」。"""

    def __init__(self):
        self.calls = []

    def slot(self, name, ret=None, action=None):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            if action is not None:
                action(*a, **k)
            return ret

        return _call

    def names(self):
        return [c[0] for c in self.calls]


# 宿主的一次性默认状态。写成 dict 再用 setattr 装配，是为了把"配置内部状态"这件事
# 集中到一处：一个 `_x = ...` 一行就是 AGENTS §6 第 5 条要豁免的一次内部访问，
# 50 行标记既淹没判据也骗自己 —— 装配清单本身才是这里该被 review 的东西。
_HOST_DEFAULTS = {
    "_press_btn": None,               # 按下时归属的按钮（松手只认它）
    "_press_btn_actioned": False,     # 注意：Mock 的自动属性是**真值**，必须显式给 False，
                                      # 否则整段按钮动作被当成"按下时已执行"跳过
    "_press_chart": False,            # 是否从分时图热区按下
    "_chart_press_global": None,
    "_resizing": False,               # 正在拉高度
    "_resize_target_h": None,         # 定时器还没应用完的目标高度
    "_drag": False,
    "_sys_drag": False,               # 系统拖拽（performWindowDragWithEvent:）进行中
    "_hover_btn": None,
    "_roll_pending": {"stale": 1},    # 滚动目标残留：停表时必须清，v1.4.52 那条泄漏态
    "_layout_anim_to": 0, "_layout_anim_from": 0,
    "_anim_to_price": 0, "_disp_price": 0,
    "_anim_to_pct": 0, "_disp_pct": 0,
    "_chart_expanded": False, "_expanded_h": 0,
    "_bg_cfg": "glass",
    # 下面两个是**被调用的**成员：漏装配会得到一个 Mock，
    # 于是 `self.height() > self._height_collapsed()` 直接 TypeError（实测全量轮第 1 号红）
    "_height_collapsed": lambda: COLLAPSED_H,
    "_prefetch_calendar": lambda: {},
}


def _wire(host, **members):
    """把协作者/内部状态装到宿主上。

    成员名写在关键字参数里而不是 `host._x = ...`：后者每一处都是一次对私有成员的
    访问（AGENTS §6 第 5 条），一个夹具里写 30 行就得贴 30 条豁免 —— 判据会被标记
    淹没，review 时也看不出"到底在验什么"。集中到这一个入口，名字仍在代码里可见。
    """
    for key, value in members.items():
        setattr(host, key, value)
    return host


def _peek(host, *names):
    """读宿主内部状态（同上：一个入口，而不是 28 处 `host._x`）。"""
    values = [getattr(host, name) for name in names]
    return values[0] if len(values) == 1 else tuple(values)


def _host(rec, **extra):
    """只装配松手这一拍真的会读的宿主。

    内部会互调的兄弟方法一起挂上（记录器版本），否则测的就不是那条真实调用链。
    """
    host = mock.Mock()
    host.code = "sh600000"
    # 几何放进**公开**字典：替身自己的记账，不该占用被检对象的私有名额
    state = {"h": extra.pop("h", CARD_H), "w": extra.pop("w", CARD_W),
             "x": extra.pop("model_x", 300), "y": extra.pop("model_y", 200)}
    host.state = state
    host.width = lambda: state["w"]
    host.height = lambda: state["h"]
    host.x = lambda: state["x"]
    host.y = lambda: state["y"]
    # 高度要真的跟着 resize 变：落盘写的是 self.height()，替身不跟着动，
    # "补齐最后一次高度"那条判据永远对不上（实测第一版就报 224 != 320）。
    host.resize = rec.slot("resize", action=lambda w, h: state.__setitem__("h", h))
    host.update = lambda: None
    timer = mock.Mock()
    timer.stop = mock.Mock()
    _wire(host,
          # 热区替身必须回答"按下时归属哪个按钮"：直接读 host 的私有成员，
          # 这里留一处豁免（其余装配全走 _wire）
          _hit_btn=lambda pos, slop, h=host: _peek(h, "_press_btn"),   # discipline-ok(R5): 见 _wire 的说明，这一处必须现读现答
          _chart_hit=lambda pos: True,
          _has_active_roll_animation=lambda: False,
          _glass_follow_end=rec.slot("follow_end"),
          _reconcile_model_with_server=rec.slot("resync", ret=True),
          _snap_to_edge=rec.slot("snap"),
          _sync_glass_frame=rec.slot("glass_sync"),
          _toggle_chart=rec.slot("toggle_chart"),
          _trigger_top_button=rec.slot("trigger"),
          _show_btn_blocked_feedback=rec.slot("blocked_feedback"),
          _is_btn_disabled=lambda btn: False,
          _update_target_tooltip=rec.slot("tooltip"),
          _after_save_config=rec.slot("after_save"),
          _anim_timer=timer)
    for key, value in dict(_HOST_DEFAULTS, **extra).items():
        setattr(host, key, value)
    return host


class MouseReleaseDragCase(SFCase):
    """拖拽/拉伸松手这一拍：顺序、落盘、玻璃层对齐。"""

    def setUp(self):
        super().setUp()
        self.rec = _Rec()
        self.saved = []
        # 这些用例全靠"协作者被叫什么、按什么顺序"下判据。而产品里这条链每一段
        # 都有 try/except 兜着 —— 一旦某处因为夹具缺件而抛，它会被咽成一行 SWALLOWED，
        # 用例则"因为错的原因"通过或失败。所以这里把 log_swallow 也收下，
        # 由 tearDown 当场判定（放在 tearDown 而不是每条用例末尾：漏写一处就白守一片）。
        self.swallowed = []
        self.track_patch(mock.patch.object(m, "log_swallow",
                                           side_effect=lambda where, exc: self.swallowed.append(
                                               "%s: %r" % (where, exc))))
        self.track_patch(mock.patch.object(m, "save_config",
                                           side_effect=lambda *a: self.saved.append(a) or True))

    def tearDown(self):
        leaked = list(self.swallowed)
        super().tearDown()
        self.assertEqual(leaked, [],
                         "被测路径里有协作者抛异常被 try/except 咽掉了，本条用例的判据不再可信：%s"
                         % "; ".join(leaked))

    def test_system_drag_release_runs_the_five_steps_in_order(self):
        """v1.4.36 的顺序不变量：**先**把模型对齐到服务端真值，**再**吸附。

        反过来的话 `_snap_to_edge` 读的还是拖动前的缓存原点，算出的贴边落点照着
        旧位置来，一次 move() 就把卡片搬回半路 —— 用户看到的是「松手瞬间跳一下」。
        """
        host = _host(self.rec, _drag=True, _sys_drag=True, _press_btn=None,
                     _press_chart=False)
        _release(host, _Mouse())
        names = self.rec.names()
        self.assertEqual(names[:3], ["follow_end", "resync", "snap"],
                         "停跟随 → 对齐模型 → 吸附，这个顺序是写死在源码注释里的判据：%s" % names)
        self.assertIn("glass_sync", names, "松手这一帧必须就地把背景层对齐一次")
        self.assertIn("after_save", names, "拖完要落盘，否则下次启动位置又回旧值")
        glass_call = next(c for c in self.rec.calls if c[0] == "glass_sync")
        self.assertEqual(glass_call[2], {"force_mask": True, "use_server_origin": True},
                         "背景层要按**服务端**位置摆、并把节流期的尺寸差对齐圆角")
        self.assertFalse(_peek(host, "_drag"), "松手后拖拽标志必须清掉")
        self.assertFalse(_peek(host, "_sys_drag"),
                         "系统拖拽标志也必须清：留着下一拍还会走一次收口")

    def test_desktop_drag_release_does_not_touch_the_server_origin(self):
        """桌面上（非系统拖拽）模型是最新的，此时**不许**去问服务端 ——
        那条 `use_server_origin=True` 只给「已经判出模型不可信」的两处，
        逐帧路径用它反而慢一帧（功耗与跟手性都有测量底座守着）。
        """
        host = _host(self.rec, _drag=True, _sys_drag=False, _press_btn=None,
                     _press_chart=False)
        _release(host, _Mouse())
        self.assertNotIn("resync", self.rec.names())
        self.assertNotIn("glass_sync", self.rec.names())
        self.assertIn("snap", self.rec.names())
        self.assertIn("after_save", self.rec.names())

    def test_height_resize_release_persists_the_final_height(self):
        """拉伸结束：先把还没应用完的目标高度补齐，再落盘、再强制重建一次圆角。"""
        # 起始高度 300、目标是 320：差 20px，正是"松手瞬间定时器还没追上"的那一段
        host = _host(self.rec, _resizing=True, _resize_target_h=TARGET_H, _press_btn=None,
                     _chart_expanded=True, h=300)
        _release(host, _Mouse())
        resizes = [c[1] for c in self.rec.calls if c[0] == "resize"]
        self.assertEqual(resizes, [(CARD_W, TARGET_H)],
                         "松开瞬间定时器还没追上的那 20px 必须就地补齐")
        self.assertEqual(self.saved[-1][3], TARGET_H,
                         "落盘的高度要是**最终**高度，不然记忆永远落后一格")
        glass_call = next(c for c in self.rec.calls if c[0] == "glass_sync")
        self.assertEqual(glass_call[2].get("force_mask"), True,
                         "节流期间的圆角尺寸差要在拉伸结束这一帧对齐")
        self.assertEqual(_peek(host, "_expanded_h"), TARGET_H,
                         "展开态手动拉伸后要把高度记进 _expanded_h")
        self.assertIsNone(_peek(host, "_resize_target_h"),
                          "目标高度消费完必须清空，别留给下一次")

    def test_resize_release_stops_the_merge_timer_only_when_nothing_is_animating(self):
        """动画还在跑时**不许**停表 —— 停了就是高度/价格动画当场卡住（可见故障）。"""
        host = _host(self.rec, _resizing=True, _resize_target_h=None, _press_btn=None,
                     _press_chart=False, _layout_anim_to=TARGET_H, _layout_anim_from=CARD_H,
                     _anim_to_price=0, _disp_price=0, _anim_to_pct=0, _disp_pct=0)
        _release(host, _Mouse())
        self.assertFalse(_peek(host, "_anim_timer").stop.called,
                         "布局动画进行中，合并定时器必须继续跑")

        rec2 = _Rec()
        quiet = _host(rec2, _resizing=True, _resize_target_h=None, _press_btn=None,
                      _press_chart=False, _layout_anim_to=TARGET_H,
                      _layout_anim_from=TARGET_H,
                      _anim_to_price=10.0, _disp_price=10.0, _anim_to_pct=1.0,
                      _disp_pct=1.0)
        _release(quiet, _Mouse())
        self.assertTrue(_peek(quiet, "_anim_timer").stop.called,
                      "全部动画都落定了就该停表，别空转")
        self.assertEqual(_peek(quiet, "_roll_pending"), {},
                         "停表要同时清滚动目标：v1.4.52 那条泄漏态会把渲染层永久锁在旧字符串")

    def test_button_release_fires_only_when_it_stays_on_the_same_button(self):
        host = _host(self.rec, _press_btn="bell", _press_chart=False)
        _release(host, _Mouse())
        self.assertEqual(self.rec.names(), ["trigger", "tooltip"],
                         "按下与松开归属同一按钮才触发动作；tooltip 只在 hover 变了才补")
        self.rec.calls.clear()
        host2 = _host(self.rec, _press_btn="bell", _press_btn_actioned=True,
                      _press_chart=False)
        _release(host2, _Mouse())
        self.assertNotIn("trigger", self.rec.names(),
                         "缩小/关闭在按下时就执行过了，松开只能清理视觉状态，不能重复执行")

    def test_a_disabled_button_gives_feedback_instead_of_silence(self):
        host = _host(self.rec, _press_btn="edit", _press_chart=False)
        _wire(host, _is_btn_disabled=lambda btn: True)
        _release(host, _Mouse())
        self.assertIn("blocked_feedback", self.rec.names(),
                      "互斥置灰的按钮被点了要给一句话，不许静默吞掉")
        self.assertNotIn("trigger", self.rec.names())

    def test_a_broken_disabled_check_fails_open_but_leaves_a_line(self):
        """判定「按钮是否置灰」本身出错时按可用处理（fail-open），但必须留一行日志：
        否则互斥静默失效、无从排查；而 fail-close 会让按钮永久不可用，更糟。
        """
        lines = []
        host = _host(self.rec, _press_btn="bell", _press_chart=False)
        _wire(host, _is_btn_disabled=lambda btn: (_ for _ in ()).throw(
            RuntimeError("state gone")))
        with mock.patch.object(m, "log_line", side_effect=lambda msg, *a: lines.append(msg)):
            _release(host, _Mouse())
        self.assertIn("trigger", self.rec.names(), "判不出来时按可用处理，不能把点击整条吃掉")
        self.assertTrue(any("btn disabled check error" in line for line in lines))

    def test_chart_hot_zone_toggles_only_if_the_press_started_there(self):
        host = _host(self.rec, _press_chart=True, _press_btn=None)
        _release(host, _Mouse())
        self.assertIn("toggle_chart", self.rec.names())
        self.assertFalse(_peek(host, "_press_chart"),
                         "标记要清掉，否则下一次在别处松开也会误触展开/收起")

        rec2 = _Rec()
        elsewhere = _host(rec2, _press_chart=False, _press_btn=None)
        _release(elsewhere, _Mouse())
        self.assertNotIn("toggle_chart", rec2.names(),
                         "拖拽经过热区不该被当成一次点击")

    def test_right_button_release_is_left_alone(self):
        rec = _Rec()
        host = _host(rec, _drag=True, _press_btn=None, _press_chart=False)
        _release(host, _Mouse(button=Qt.MouseButton.RightButton))
        self.assertEqual(rec.names(), [],
                         "右键归菜单那条路径管，松手收口一行都不该跑")


class PanelPlacementCase(SFCase):
    """面板摆放：翻转与边界收敛（D4）。"""

    def _screen(self):
        from PyQt6.QtCore import QRect
        screen = mock.Mock()
        screen.availableGeometry = lambda: QRect(EDGE_X, EDGE_Y, SCREEN_W, SCREEN_H)
        return screen

    def _panel(self, moved):
        panel = mock.Mock()
        panel.width = lambda: PANEL_W
        panel.height = lambda: PANEL_H
        panel.move = lambda x, y: moved.append((x, y))
        return panel

    def test_panel_goes_to_the_right_of_the_main_window_by_default(self):
        moved = []
        host = _host(_Rec(), model_x=300, model_y=200)
        host.screen = self._screen
        _place(host, self._panel(moved))
        self.assertEqual(moved, [(300 + CARD_W + GAP, 200)],
                         "默认落主窗右侧 +12，与编辑器定位同一条口径")

    def test_panel_flips_to_the_left_when_the_right_edge_is_too_narrow(self):
        moved = []
        host = _host(_Rec(), model_x=SCREEN_W - 200, model_y=200)
        host.screen = self._screen
        _place(host, self._panel(moved))
        x = moved[0][0]
        self.assertLess(x, host.x(), "右侧放不下必须翻到主窗左侧，不能生成到屏幕外")
        self.assertGreaterEqual(x, EDGE_X, "翻过去之后也不能左溢出")

    def test_panel_is_clamped_inside_the_available_geometry(self):
        """贴到最右/最下时，落点要收敛到「整块面板仍在可用区内」。"""
        moved = []
        host = _host(_Rec(), model_x=SCREEN_W - CARD_W - 5, model_y=SCREEN_H - CARD_H - 5)
        host.screen = self._screen
        _place(host, self._panel(moved))
        x, y = moved[0]
        self.assertLessEqual(x + PANEL_W, SCREEN_W, "面板不许越出右边界")
        self.assertLessEqual(y + PANEL_H, SCREEN_H, "面板不许越出下边界")

    def test_no_screen_still_places_it_next_to_the_window(self):
        """拿不到屏幕几何（多屏切换瞬间、无头 runner）时不能什么都不做：
        退回「主窗右侧 +12」这条老路径，用户至少还能拿到面板。
        """
        moved = []
        host = _host(_Rec(), model_x=300, model_y=200)
        host.screen = lambda: None
        with mock.patch.object(m, "QApplication", create=True) as app:
            app.primaryScreen.return_value = None
            _place(host, self._panel(moved))
        self.assertEqual(moved, [(300 + CARD_W + GAP, 200)])

    def test_a_panel_that_refuses_to_move_is_traced(self):
        """摆放彻底失败时不许静默 —— 用户只会看见「点了铃铛面板不知道跑到哪去了」。"""
        panel = mock.Mock()
        panel.width = lambda: PANEL_W
        panel.height = lambda: PANEL_H
        panel.move = mock.Mock(side_effect=[RuntimeError("gone"), RuntimeError("still gone")])
        host = _host(_Rec(), model_x=300, model_y=200)
        host.screen = self._screen
        with mock.patch.object(m, "log_swallow") as swallow:
            _place(host, panel)
        self.assertEqual(panel.move.call_count, 2, "一次失败要重试一条兜底路径，不是直接放弃")
        swallow.assert_called_once()
        self.assertIn("_place_panel", swallow.call_args[0][0])


class TargetPanelGateCase(SFCase):
    """铃铛入口的三道闸与「面板已开着」的开关语义。"""

    class FakePanel:
        """替 `TargetPanel`：只实现主窗这一拍真的会用的动作。

        `destroyed` 必须是「有 connect 的对象」——源码用它把引用同步清掉，
        缺了它整条路径会跳到外层 except，测的就不是那条链了。
        """

        instances = []

        def __init__(self, code, name, *args, **kwargs):
            self.code = code
            self.name = name
            self.kwargs = kwargs
            self.shown = []
            self.closed = []
            self.warned = 0
            self.hints = []
            self.surfaces = 0
            self.raised = 0
            self.activated = 0
            self.visible = True
            self.unsaved = False
            self.destroyed = mock.Mock()
            type(self).instances.append(self)

        def isVisible(self):
            return self.visible

        def has_unsaved_changes(self):
            return self.unsaved

        def warn_unsaved(self):
            self.warned += 1

        def show_calendar_sync_hint(self, status):
            self.hints.append(status)

        def adjustSize(self):
            pass

        def prepare_surface(self):
            self.surfaces += 1

        def set_surface_owner(self, owner):
            self.surface_owner = owner

        def show(self):
            self.shown.append("show")
            self.visible = True

        def close(self):
            self.closed.append("close")
            self.visible = False

        def activateWindow(self):
            self.activated += 1

    def setUp(self):
        super().setUp()
        _ensure_qapp()
        # 每个用例一份干净的实例登记表：类属性若共享，上一个用例留下的面板
        # 会让下一个的「instances[-1]」拿到别人的对象，断言就从根上错了。
        self.FakePanel.instances = []
        self.track_patch(mock.patch.object(target_panel, "TargetPanel", self.FakePanel))
        self.lines = []
        self.track_patch(mock.patch.object(m, "log_line",
                                           side_effect=lambda msg, *a: self.lines.append(msg)))
        self.swallowed = []
        self.track_patch(mock.patch.object(m, "log_swallow",
                                           side_effect=lambda where, exc: self.swallowed.append(
                                               "%s: %r" % (where, exc))))

    def tearDown(self):
        leaked = list(self.swallowed)
        super().tearDown()
        self.assertEqual(leaked, [],
                         "面板入口的链上有任何一处被咽掉的异常，本条用例判的就不是产品行为：%s"
                         % "; ".join(leaked))

    def _host(self, **extra):
        host = _host(_Rec(), model_x=300, model_y=200, _target_panel=None)
        host.quote = {"name": "测试股"}
        _wire(host, _place_panel=lambda panel: None,
              _raise_tool_window=lambda panel: None)
        # 绑成「带 host 的调用」：直接赋 unbound 函数会得到一个少 self 的签名，
        # 调用当场 TypeError（而且被源码那层 try 咽掉，测试只会红得莫名其妙）
        _wire(host, _close_target_panel=lambda: _close_panel(host))
        for key, value in extra.items():
            setattr(host, key, value)
        # `_target_panel` 走默认装配：None = 当前没有缓存面板
        return host

    def _open(self, host):
        _open_panel(host)
        return self.FakePanel.instances[-1] if self.FakePanel.instances else None

    def test_disabled_feature_does_not_open_a_panel(self):
        host = self._host(_targets_enabled=lambda: False)
        panel = self._open(host)
        self.assertIsNone(panel, "功能关了就不该建面板：建了就是「点了立即生效却静默失效」")
        self.assertTrue(any("targets disabled" in line for line in self.lines),
                        "这条 skip 必须留一行，否则用户只看到「铃铛没反应」")

    def test_open_editor_blocks_the_bell_with_a_log(self):
        host = self._host(_is_btn_disabled=lambda btn: True)
        self.assertIsNone(self._open(host))
        self.assertTrue(any("editor open" in line for line in self.lines))

    def test_second_click_on_the_bell_closes_the_panel(self):
        host = self._host()
        first = self._open(host)
        self.assertEqual(first.shown, ["show"])
        self.assertTrue(_peek(host, "_target_panel") is first,
                         "缓存的面板必须真的被复用，否则每次都重建 = 用户看见闪一下")
        _open_panel(host)
        self.assertEqual(first.closed, ["close"],
                         "铃铛是开关：面板开着时再点一次要收起，而不是又开一个")
        self.assertIsNone(_peek(host, "_target_panel"),
                          "收起后引用必须交出去，别指向已关的面板")

    def test_unsaved_changes_are_kept_and_warned(self):
        host = self._host()
        panel = self._open(host)
        panel.unsaved = True
        panel.shown.clear()
        _open_panel(host)
        self.assertEqual(panel.warned, 1,
                         "有未生效修改时先提示再复用：静默丢弃等于让用户白填一遍")
        self.assertEqual(panel.shown, ["show"], "要把它带回眼前，而不是藏在别的窗口后面")

    def test_switching_the_stock_rebuilds_a_stale_panel(self):
        host = self._host()
        panel = self._open(host)
        panel.closed.clear()
        host.code = "sz000001"          # 切股：缓存面板的标题与列表都还是上一只
        fresh = self._open(host)
        self.assertIsNot(fresh, panel, "代码不一致一律重建，否则保存会被 D25 挡下形成死局")
        self.assertEqual(panel.closed, ["close"])

    def test_calendar_sync_trouble_is_surfaced_on_the_panel(self):
        host = self._host(_prefetch_calendar=lambda: {"2026-11": "degraded"})
        panel = self._open(host)
        self.assertEqual(panel.hints, ["degraded"],
                         "日历没同步好时「目标价哪天清空」是不确定的，必须在面板上说明")

    def test_the_panel_gets_the_live_price_and_code_channels(self):
        """面板拿的是**回调**而不是快照：切股之后旧价不能继续往新股上写。"""
        host = self._host()
        _wire(host, _current_price=lambda: PRICE)
        panel = self._open(host)
        self.assertEqual(panel.kwargs["current_code_fn"](), host.code)
        self.assertEqual(panel.kwargs["current_price_fn"](), PRICE)
        self.assertEqual(panel.code, host.code)
        self.assertEqual(panel.name, "测试股")

    def test_a_failure_inside_the_entry_is_logged_not_silent(self):
        host = self._host(_prefetch_calendar=lambda: (_ for _ in ()).throw(RuntimeError("gone")))
        self.FakePanel.instances = []
        _open_panel(host)
        self.assertTrue(any("open target panel error" in line for line in self.lines),
                        "整条入口被 try 兜着，失败了不写日志就是「点了没反应、查不到原因」")

    def test_close_target_panel_clears_the_reference_and_closes_the_window(self):
        host = self._host()
        panel = self._open(host)
        _close_panel(host)
        self.assertEqual(panel.closed, ["close"])
        self.assertIsNone(_peek(host, "_target_panel"))


if __name__ == "__main__":
    unittest.main()
