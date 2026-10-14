# -*- coding: utf-8 -*-
"""v1.4.28 跟手性改动的行为用例。

实测依据与改前数字见 outputs/触控跟手性延迟画像-v1.4.27-2026-09-23.md。
守的是四条行为契约，不是代码书写形态：

  P1  主窗移动时工具窗只跟位；抬层只在"主窗真的被前置"之后做。
  P2  工具窗开着不再单独把置顶复核打成"需要重做"（主用例在 test_power_v1410）。
  P3  悬停换态走统一重绘入口（主画钟在跑时搭帧，不另排一帧整窗重绘）；
      但按下态仍必须当场排帧 —— 那是用户点下去的第一声"我收到了"。
  P5  拉伸手势进行中 16ms 合并时钟不停表；松手或保持窗口过期必须停表。

为什么用"普通对象 + 调未绑定方法"而不是真窗口：sip 包起来的 QObject 没跑过
`super().__init__` 时，任何没设置过的属性读取都会抛 RuntimeError，而被测处理函数
里到处是 `getattr(self, "_x", None)` 的容错读法；构造真 FloatWindow 又会把玻璃层
与一串定时器一起拖进来。这里被测函数本体是真的，只是宿主是替身；`_rec` 记录
"意外依赖"，防止后续改动悄悄把重活塞回热路径。
"""
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QPoint, QRect, Qt            # noqa: E402

import sf_glass                                # noqa: E402
import stock_float_window as m                 # noqa: E402
from sf_anim_driver import AnimDriver          # noqa: E402


class _Global:
    def __init__(self, pt):
        self._p = QPoint(int(pt[0]), int(pt[1]))

    def toPoint(self):
        return self._p


class _Ev:
    """够用的鼠标事件替身：处理函数只读 button/buttons/pos/globalPosition。"""

    def __init__(self, local, buttons, button=None, global_pt=None):
        self._local = QPoint(int(local[0]), int(local[1]))
        self._buttons = buttons
        self._button = button if button is not None else buttons
        self._global = _Global(global_pt if global_pt is not None else local)

    def button(self):
        return self._button

    def buttons(self):
        return self._buttons

    def pos(self):
        return self._local

    def globalPosition(self):
        return self._global


class _Bare:
    """替身窗口。没预设过的属性读取会记进 `_rec` 并返回自动 mock。"""

    def __init__(self):
        self.__dict__["_rec"] = []

    def __getattr__(self, name):
        # 未登记的属性**必须抛 AttributeError**：产品代码里到处是
        # `getattr(self, "_sys_drag", False)` 这种带默认值的容错读法，早先这里
        # 返回自动 mock（真值），于是 v1.4.29 复核 F2 —— 新加的 `elif _sys_drag:`
        # 分支在测试里恒真，把悬停用例静默带偏，而产品代码其实没问题。
        # 现在：记一笔名字（供"意外依赖"断言用），然后照常抛错，让默认值生效。
        if not name.startswith("__"):
            self._rec.append(name)
        raise AttributeError(name)


def _win(**kw):
    w = _Bare()
    w._anim_driver = AnimDriver()
    w._anim_timer = MagicMock()
    w._roll_pending, w._roll_prev, w._roll_anim = {}, {}, {}
    w._partial_price = None
    w._disp_price, w._disp_pct = 10.0, 1.0
    w._anim_from_price = w._anim_to_price = 10.0
    w._anim_from_pct = w._anim_to_pct = 1.0
    w._layout_anim_from = w._layout_anim_to = 0
    w._chart_expanded = True
    w._chart_changed = False
    w._resizing = False
    w._resize_target_h = None
    w._resize_hold_until = 0.0
    w._target_panel = None
    w._editor = None
    w._press_btn = None
    w._press_btn_actioned = False
    w._press_chart = False
    w._drag = False
    w._sys_drag = False
    w._hover_btn = None
    w._has_active_roll_animation = lambda: False
    w.width = lambda: 184
    w.height = lambda: 268
    w.minimumHeight = lambda: 145
    w.maximumHeight = lambda: 420
    # v1.4.34：拉伸地板改取语义高度后，替身必须给出同一个规则。这里**借真身实现**
    # 而不是在测试里抄一份数字 —— 抄了就会与产品漂移（产品改了算法，替身还按旧的
    # minimumHeight=145 判定，测出来的就不是产品行为）。
    w._height_collapsed = lambda: 145  # discipline-ok(R5): 替身要顶替真窗口的内部口
    w._stretch_floor_height = lambda: m.FloatWindow._stretch_floor_height(w)  # discipline-ok(R5): 借真身，避免规则副本
    w.pos = lambda: QPoint(500, 300)
    w.x = lambda: 500
    w.y = lambda: 300
    w.update = MagicMock()
    w._repaint_now = MagicMock()
    w.setCursor = MagicMock()
    w.unsetCursor = MagicMock()
    w.move = MagicMock()
    w.resize = MagicMock()
    w._place_panel = MagicMock()
    w._raise_tool_window = MagicMock()
    w._update_target_tooltip = MagicMock()
    w._btn_at = MagicMock(return_value=None)
    w._hit_btn = MagicMock(return_value=None)
    w.code = "sh600519"
    w._bg_cfg = {"effect": True}
    w._snap_to_edge = MagicMock()
    w._after_save_config = MagicMock()
    w._glass_follow_begin = MagicMock()
    w._glass_follow_end = MagicMock()
    w._sync_glass_frame = MagicMock()
    w._chart_hit = MagicMock(return_value=False)
    for k, v in kw.items():
        setattr(w, k, v)
    return w


class TestToolWindowFollowsButDoesNotRaise(unittest.TestCase):
    """P1：跟位每拍做，抬层不再每拍做。"""

    def _panel(self, visible=True):
        p = MagicMock()
        p.isVisible.return_value = visible
        return p

    def test_move_path_places_panel_without_raising(self):
        w = _win()
        panel = self._panel()
        w._target_panel = panel
        m.FloatWindow._sync_open_tool_windows(w)
        w._place_panel.assert_called_once_with(panel)
        w._raise_tool_window.assert_not_called()

    def test_editor_reanchors_without_raise(self):
        w = _win()
        ed = self._panel()
        w._editor = ed
        m.FloatWindow._sync_open_tool_windows(w)
        ed._move_near.assert_called_once_with()
        w._raise_tool_window.assert_not_called()

    def test_invisible_tools_are_untouched(self):
        w = _win()
        w._target_panel = self._panel(visible=False)
        m.FloatWindow._sync_open_tool_windows(w)
        w._place_panel.assert_not_called()

    def test_move_path_touches_nothing_else(self):
        """热路径防呆：跟随动作不许再引入新依赖（例如又把抬层塞回来）。"""
        w = _win()
        w._target_panel = self._panel()
        m.FloatWindow._sync_open_tool_windows(w)
        self.assertEqual(w._rec, [], "跟位路径碰到了未预期的成员：%s" % w._rec)

    def test_raise_helper_raises_only_visible_tools(self):
        w = _win()
        visible, hidden = self._panel(), self._panel(visible=False)
        w._target_panel, w._editor = visible, hidden
        m.FloatWindow._raise_open_tool_windows(w)
        self.assertEqual([c.args[0] for c in w._raise_tool_window.call_args_list],
                         [visible], "只应抬可见工具窗")

    def test_raise_helper_survives_errors(self):
        """异常不许抛出去：top_timer 那一轮后面还有别的动作，不能断在这里。"""
        w = _win()
        w._target_panel = self._panel()
        w._target_panel.isVisible.side_effect = RuntimeError("objc gone")
        m.FloatWindow._raise_open_tool_windows(w)
        w._raise_tool_window.assert_not_called()


class TestResizeClockKeepsRunning(unittest.TestCase):
    """P5：拉伸期间不停表，让目标高度的应用率稳定在每帧一次。"""

    def test_holds_timer_while_resizing(self):
        w = _win(_resizing=True, _resize_target_h=268,
                 _resize_hold_until=time.monotonic() + m.RESIZE_HOLD_SEC)
        m.FloatWindow._anim_tick_inner(w)
        w._anim_timer.stop.assert_not_called()

    def test_stops_when_hold_expired(self):
        """保持窗口过期（例如 macOS 偶发不投 mouseRelease）必须照常停表，
        否则就退化成 v1.2.65 修掉的"16ms 定时器永久空转"。"""
        w = _win(_resizing=True, _resize_target_h=268,
                 _resize_hold_until=time.monotonic() - 1.0)
        m.FloatWindow._anim_tick_inner(w)
        w._anim_timer.stop.assert_called_once_with()

    def test_stops_when_not_resizing(self):
        w = _win()
        m.FloatWindow._anim_tick_inner(w)
        w._anim_timer.stop.assert_called_once_with()

    def test_press_on_bottom_edge_arms_hold(self):
        w = _win(_chart_hit=lambda p: False)
        ev = _Ev((12, 265), Qt.MouseButton.LeftButton,
                 button=Qt.MouseButton.LeftButton, global_pt=(512, 565))
        m.FloatWindow.mousePressEvent(w, ev)
        self.assertTrue(w._resizing, "按下展开态底部边缘应进入拉伸模式")
        self.assertGreater(w._resize_hold_until, time.monotonic(),
                           "进入拉伸就要登记保持窗口，否则第一帧就停表")

    def test_move_defers_resize_and_renews_hold(self):
        """每个拉伸事件只记目标高度 + 续期；真正的 resize 交给 16ms 时钟。"""
        w = _win(_resizing=True, _resize_start=QPoint(512, 565),
                 _resize_h0=268, _resize_target_h=None,
                 _resize_hold_until=time.monotonic())
        hold0 = w._resize_hold_until
        ev = _Ev((12, 280), Qt.MouseButton.LeftButton,
                 button=Qt.MouseButton.NoButton, global_pt=(512, 580))
        m.FloatWindow.mouseMoveEvent(w, ev)
        w.resize.assert_not_called()
        self.assertEqual(w._resize_target_h, 268 + 15, "目标高度 = 起始高度 + 鼠标位移")
        self.assertGreater(w._resize_hold_until, hold0)


class TestInputRepaintRouting(unittest.TestCase):
    """P3：悬停搭主画钟；按下态仍当场排帧。"""

    def test_hover_change_uses_repaint_now(self):
        w = _win(_btn_at=lambda p: "bell")
        ev = _Ev((90, 8), Qt.MouseButton.NoButton)
        m.FloatWindow.mouseMoveEvent(w, ev)
        w._repaint_now.assert_called_once_with()
        w.update.assert_not_called()

    def test_hover_no_change_repaints_nothing(self):
        w = _win(_btn_at=lambda p: "bell", _hover_btn="bell")
        ev = _Ev((90, 8), Qt.MouseButton.NoButton)
        m.FloatWindow.mouseMoveEvent(w, ev)
        w._repaint_now.assert_not_called()
        w.update.assert_not_called()

    def test_press_feedback_still_repaints_immediately(self):
        """按下态不能为了搭帧而延后：实测 5.2ms 上屏，是"点下去有反应"的全部依据。"""
        w = _win(_btn_at=lambda p: "bell")
        ev = _Ev((90, 8), Qt.MouseButton.LeftButton,
                 button=Qt.MouseButton.LeftButton)
        m.FloatWindow.mousePressEvent(w, ev)
        w.update.assert_called_once_with()
        w._repaint_now.assert_not_called()

    def test_dragging_window_does_not_queue_a_paint(self):
        """拖动只改位置：每拍再排一帧整窗重绘就是白画（主画钟本来就在跑）。"""
        w = _win(_drag=True, _offset=QPoint(20, 8))
        ev = _Ev((20, 8), Qt.MouseButton.LeftButton,
                 button=Qt.MouseButton.NoButton, global_pt=(520, 308))
        m.FloatWindow.mouseMoveEvent(w, ev)
        w.move.assert_called_once()
        w.update.assert_not_called()
        w._repaint_now.assert_not_called()


class TestTopStateFallbackStaysConservative(unittest.TestCase):
    """P2 的兜底不能跟着松掉：属性读不出来仍要判"需要重做"。"""

    def test_read_failure_needs_work(self):
        w = _win()
        bad = MagicMock()
        bad.level.side_effect = RuntimeError("objc boom")
        self.assertFalse(sf_glass.GlassMixin._top_state_ok(w, bad, None))

    def test_steady_state_with_open_panel_is_ok(self):
        """面板开着但状态全对 → 判"不必动手"。改前这里恒 False，等于每 250ms
        白跑一次 2.6~3.5ms 的完整置顶，正好落在用户拖动/打字的时候。"""
        behavior = sf_glass.public_overlay_collection_behavior()
        frame = object()
        main = MagicMock()
        main.level.return_value = 25
        main.isVisible.return_value = True
        main.hidesOnDeactivate.return_value = False
        main.ignoresMouseEvents.return_value = False
        main.isOnActiveSpace.return_value = True
        main.collectionBehavior.return_value = behavior
        main.frame.return_value = frame
        w = _win()
        w._target_panel = MagicMock()
        w._target_panel.isVisible.return_value = True
        self.assertTrue(sf_glass.GlassMixin._top_state_ok(w, main, main))


class TestToolWindowRaiseBody(unittest.TestCase):
    """v1.4.29 复核 F4：抬层的**函数体**以前从没被用例进去过（所有用例都把它整块
    patch 掉），这正是 `_objc_deps` 的 NameError 能潜伏几十个版本的原因。"""

    def _fake_objc(self, ns):
        objc = MagicMock()
        objc.objc_object.return_value.window.return_value = ns
        return objc

    def test_level_is_above_main_window_level(self):
        import sf_glass
        self.assertGreater(sf_glass.TOOL_WINDOW_LEVEL, sf_glass._TOP_WINDOW_LEVEL,
                           "工具窗必须比主窗高一层：同级时先后只由最后一次 orderFront 决定，"
                           "抬层之后主窗还会再前置若干次")

    def test_raise_body_actually_calls_appkit(self):
        w = _win()
        ns = MagicMock()
        tool = MagicMock()
        with patch.object(m, "native_cocoa_active", return_value=True), \
             patch.object(m, "_objc_deps", return_value=(self._fake_objc(ns), MagicMock())):
            m.FloatWindow._raise_tool_window(w, tool)
        tool.raise_.assert_called_once_with()
        ns.setLevel_.assert_called_once_with(m.TOOL_WINDOW_LEVEL)
        ns.orderFrontRegardless.assert_called_once_with()

    def test_offscreen_never_touches_objc(self):
        """v1.4.29 复核 F1 的回归网：离屏下 winId() 不是 NSView 指针，转 objc_object
        再 .window() 会直接段错误（当时全量测试进程 exit=139）。"""
        w = _win()
        deps = MagicMock(side_effect=AssertionError("离屏不该碰 objc"))
        with patch.object(m, "native_cocoa_active", return_value=False), \
             patch.object(m, "_objc_deps", deps):
            m.FloatWindow._raise_tool_window(w, MagicMock())
        deps.assert_not_called()

    def test_failure_leaves_a_log(self):
        """静默失效必须留下探测器：这条日志就是"抬层又坏了"的唯一信号。"""
        w = _win()
        with patch.object(m, "native_cocoa_active", return_value=True), \
             patch.object(m, "_objc_deps", side_effect=NameError("_objc_deps")):
            with patch.object(m, "log_line_throttled") as throttled:
                m.FloatWindow._raise_tool_window(w, MagicMock())
        self.assertTrue(throttled.called, "异常被吞掉了，没有任何日志")


class TestSystemDragHandoff(unittest.TestCase):
    """v1.4.29 复核 F3/F4：位移交给系统的两条分支与"丢松开事件"的兜底。"""

    def _press(self, w, ok):
        from PyQt6.QtCore import QRect
        wh = MagicMock()
        wh.startSystemMove.return_value = ok
        w.windowHandle = lambda: wh
        w.frameGeometry = lambda: QRect(500, 300, 184, 268)
        # 背景层跟随是这条路径的**必要组成**（系统拖拽期间 Qt 定时器不跑，
        # 不挂原生定时器实测背景掉队 44px）：替身必须提供，否则会被
        # mousePressEvent 的兜异常吞成一条 SWALLOWED 日志而用例仍"绿"。
        w._glass_follow_begin = MagicMock()
        w._glass_follow_end = MagicMock()
        w._sync_glass_frame = MagicMock()
        ev = _Ev((30, 6), Qt.MouseButton.LeftButton,
                 button=Qt.MouseButton.LeftButton, global_pt=(530, 306))
        with patch.object(m, "feature_enabled", lambda name: True):
            m.FloatWindow.mousePressEvent(w, ev)
        return wh

    def test_handoff_when_system_accepts(self):
        w = _win()
        wh = self._press(w, True)
        wh.startSystemMove.assert_called_once_with()
        self.assertTrue(w._sys_drag)
        self.assertFalse(w._drag, "已交给系统就不该再自绘移动，否则两边抢位置")
        w._glass_follow_begin.assert_called_once_with()

    def test_release_stops_follow_and_resyncs_glass(self):
        """松手必须停掉原生定时器并把背景层就地对齐一次：拖拽期间的任何残余误差
        不能留到下一帧，否则用户看到的是永久错位。"""
        from PyQt6.QtCore import QRect
        w = _win(_drag=False, _sys_drag=True)
        self._press(w, True)          # 复用打桩：置起 _sys_drag 与跟随桩
        w._glass_follow_begin.reset_mock()
        ev = _Ev((30, 6), Qt.MouseButton.NoButton,
                 button=Qt.MouseButton.LeftButton, global_pt=(530, 306))
        with patch.object(m, "save_config", return_value=True):
            m.FloatWindow.mouseReleaseEvent(w, ev)
        w._glass_follow_end.assert_called_once_with()
        # v1.4.33：松手这一帧以 WindowServer 的真实位置对齐，不再信任可能过时的
        # 应用侧模型（系统拖拽期间它可以差几百 px，见 sf_glass._content_real_bounds）
        w._sync_glass_frame.assert_called_once_with(force_mask=True,
                                                    use_server_origin=True)
        self.assertFalse(w._sys_drag)

    def test_app_drag_release_does_not_touch_follow_timer(self):
        """自绘拖拽（开关关闭或系统拒绝）不该去动那个原生定时器。"""
        w = _win(_drag=True, _sys_drag=False, _offset=QPoint(20, 8))
        ev = _Ev((30, 6), Qt.MouseButton.NoButton,
                 button=Qt.MouseButton.LeftButton, global_pt=(530, 306))
        with patch.object(m, "save_config", return_value=True):
            m.FloatWindow.mouseReleaseEvent(w, ev)
        w._glass_follow_end.assert_not_called()
        w._sync_glass_frame.assert_not_called()
        w._snap_to_edge.assert_called_once_with()      # 自绘拖拽的收口本身不变

    def test_fallback_when_system_refuses(self):
        """合成事件/非 cocoa 下 startSystemMove 返回 False（实测）：必须退回自绘拖拽，
        不能出现"既不起系统拖拽也不自己移动"的窗口拖不动。"""
        w = _win()
        self._press(w, False)
        self.assertFalse(w._sys_drag)
        self.assertTrue(w._drag)

    def test_disabled_by_default(self):
        w = _win()
        wh = MagicMock()
        w.windowHandle = lambda: wh
        w.frameGeometry = lambda: QRect(500, 300, 184, 268)
        ev = _Ev((30, 6), Qt.MouseButton.LeftButton,
                 button=Qt.MouseButton.LeftButton, global_pt=(530, 306))
        with patch.object(m, "feature_enabled", lambda name: False):
            m.FloatWindow.mousePressEvent(w, ev)
        wh.startSystemMove.assert_not_called()
        self.assertTrue(w._drag)

    def test_system_drag_is_default_off(self):
        """默认值由老板 2026-09-24 拍板为"关"（v1.4.37），这条把它钉住：
        翻回 True 必须是有意的决定，而不是某次改动顺手带回去。

        理由是被量出来的，不是感觉：系统拖拽下背景层永远晚一帧，装机版真鼠标逐帧
        实测掉队 常速 10px / 快速 55px / 甩动 88px（卡片宽 184px）；关掉后同一批
        手势 0px。用户明确说重影"无可容忍"，宁可回到偶发一帧的停顿感。
        """
        import sf_constants
        self.assertFalse(sf_constants._FEATURE_DEFAULTS.get("system_drag"),
                         "system_drag 默认已改回关闭（消除背景层重影）；"
                         "要重新开启请连同交付记录与上面这段理由一起改")

    def test_lost_release_does_not_kill_hover(self):
        """复核 F3：左键已抬起（macOS 偶发不投 mouseRelease）时，`_sys_drag` 残留
        不得永久吞掉悬停/光标，否则用户看到的是"鼠标划过去不再有反应"。"""
        w = _win(_drag=False, _sys_drag=True, _btn_at=lambda p: "bell")
        ev = _Ev((90, 8), Qt.MouseButton.NoButton,
                 button=Qt.MouseButton.NoButton, global_pt=(590, 308))
        m.FloatWindow.mouseMoveEvent(w, ev)
        w._repaint_now.assert_called_once_with()

    def test_hover_still_suppressed_while_button_held(self):
        w = _win(_drag=False, _sys_drag=True, _btn_at=lambda p: "bell")
        # buttons()（当前按住的键）才是分支依据，button()（本次事件涉及的键）不是
        ev = _Ev((90, 8), Qt.MouseButton.LeftButton,
                 button=Qt.MouseButton.LeftButton, global_pt=(590, 308))
        m.FloatWindow.mouseMoveEvent(w, ev)
        w._repaint_now.assert_not_called()


if __name__ == "__main__":
    unittest.main()
