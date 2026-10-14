# -*- coding: utf-8 -*-
"""弹层在第三方全屏 Space 里的补齐（2026-10-13 批）。

现场是装机版 v1.4.68 上量出来的，不是推测：浏览器全屏时发一次**真实 HID 右键**，
菜单那块窗确实建出来了 —— WindowServer 里 layer 101、bounds 152x173、就在点击点旁边、
距点击 6ms —— 但在屏窗口列表里查不到它，之后三秒内每 20ms 采一次，**0/97 在屏**。
用户看到的"右键没反应"就是这一格。主窗能浮在全屏之上，靠的是 `sf_glass._force_top`
里"公开行为 + 私有 CGS"两步都做、还每 250ms 复核一次；弹层此前一步都没做。

判据分四层，从便宜到致命：
  ① 那一位怎么换（纯函数 + 与 AppKit 同名常量对拍 + "两位共存"非法形状扫全区间）
  ② 补齐动作本身（喂假 NSWindow：改没改、CGS 调没调、失败留没留痕）
  ③ 续补节拍（**Show 那一次不够**，见 SpaceWatchdog 的类注释）
  ④ 接线（真 Show 一个顶层窗必须触发补齐，子窗不许；建完主窗必须挂上）

④ 里那条要构造 FloatWindow 的判据，patch 清单照 `tests/test_ui.py` 抄全 ——
AGENTS §6 第 14 条：裸构造会把用户机器上的正式版从 /Applications 拉起来。
"""
import os
import sys
import unittest
from unittest import mock
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_glass  # noqa: E402
import sf_quote  # noqa: E402
import stock_float_window as m  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402
from tests._base import SFCase  # noqa: E402

JOIN = sf_glass.BEHAVIOR_JOIN_ALL_SPACES
MOVE = sf_glass.BEHAVIOR_MOVE_TO_ACTIVE_SPACE


class BehaviorSwap(SFCase):
    """①那一位怎么换：Qt 的 258 必须变成 257，主窗的 273 必须原样。"""

    def test_qt_default_becomes_join_all(self):
        self.assertEqual(sf_glass.popup_join_all_spaces_behavior(258), 257)

    def test_main_window_combo_is_untouched(self):
        # 主窗那套本来就不含 MoveToActiveSpace；改它等于把别的位也动了
        self.assertEqual(sf_glass.popup_join_all_spaces_behavior(273), 273)
        again = sf_glass.popup_join_all_spaces_behavior(273)
        self.assertEqual(sf_glass.popup_join_all_spaces_behavior(again), 273)

    def test_never_produces_the_illegal_pair(self):
        """实测：两位同时为真 AppKit 当场抛 NSInternalInconsistencyException。

        所以这条不是"风格上别这么写"，而是"这样写会崩" —— 扫全区间，一条漏网就红。
        """
        for cur in range(0, 1 << 11):
            out = sf_glass.popup_join_all_spaces_behavior(cur)
            self.assertFalse(out & JOIN and out & MOVE, "现值 %d 改出来两位共存" % cur)
            self.assertTrue(out & JOIN, "现值 %d 改完丢了 CanJoinAllSpaces" % cur)

    def test_constants_match_appkit_names(self):
        """位值写死在常量里，所以必须与 AppKit 的同名常量对拍（改名/改值当场红）。"""
        try:
            import AppKit
        except Exception as exc:               # 非 macOS / 没装 pyobjc：如实跳过
            self.skipTest("取不到 AppKit：%r" % (exc,))
            return
        self.assertEqual(JOIN, AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces)
        self.assertEqual(MOVE, AppKit.NSWindowCollectionBehaviorMoveToActiveSpace)


class _FakeNSWindow:
    """够用的 NSWindow 替身：只记"写了什么、答了什么"，不碰 WindowServer。"""

    def __init__(self, behavior=258, on_active_space=True, number=4242,
                 raise_on_set=False):
        self._behavior = behavior
        self._on_active = on_active_space
        self._number = number
        self.writes = []
        self._raise_on_set = raise_on_set

    def collectionBehavior(self):
        return self._behavior

    def setCollectionBehavior_(self, value):
        if self._raise_on_set:
            raise RuntimeError("AppKit 拒收")
        self._behavior = int(value)
        self.writes.append(("setCollectionBehavior_", int(value)))

    def isOnActiveSpace(self):
        return self._on_active

    def windowNumber(self):
        return self._number


class JoinActiveSpace(SFCase):
    """②补齐动作本身。

    为什么要 patch `native_cocoa_active`：全量跑在 offscreen 下，那条判定为假时整段
    提前 return —— 不补这一句，测的就只是"它没做事"，而不是"它做的事对不对"
    （AGENTS §5.1 与玻璃层那批"因为错的原因通过"的教训同族）。
    """

    def _run(self, fake, cgs=None):
        calls = []
        with mock.patch.object(sf_glass, "native_cocoa_active", lambda: True), \
                mock.patch.object(sf_glass, "content_ns_window", lambda w: fake), \
                mock.patch.object(sf_glass, "cgs_add_window_to_space",
                                  cgs or (lambda num: calls.append(num))):
            out = sf_glass.join_active_space(object())
        return out, calls

    def test_offscreen_returns_before_touching_appkit(self):
        with mock.patch.object(sf_glass, "native_cocoa_active", lambda: False), \
                mock.patch.object(sf_glass, "content_ns_window") as cnw:
            out = sf_glass.join_active_space(object())
        self.assertEqual(out, "skip:not_cocoa")
        # winId() 在 offscreen 下不是 NSView 指针，伸手进去是不可捕获的段错误
        cnw.assert_not_called()

    def test_sets_public_behavior(self):
        fake = _FakeNSWindow(behavior=258, on_active_space=True)
        out, cgs = self._run(fake)
        self.assertIn("257", out)
        self.assertIn(("setCollectionBehavior_", 257), fake.writes)
        self.assertEqual(cgs, [], "已在活动 Space 还去调私有 CGS = 白跑一次系统调用")

    def test_adds_to_active_space_when_public_behavior_is_not_enough(self):
        """本机实测：273/level25 的 Qt 窗在第三方全屏 Space 里 isOnActiveSpace 仍为假。

        公开那一步做完不等于事情做完 —— 私有 CGS 这条必须跟着补上。
        """
        fake = _FakeNSWindow(behavior=258, on_active_space=False, number=99)
        out, cgs = self._run(fake)
        self.assertEqual(cgs, [99])
        self.assertIn("cgs_added", out)

    def test_no_nswindow_is_reported_not_raised(self):
        with mock.patch.object(sf_glass, "native_cocoa_active", lambda: True), \
                mock.patch.object(sf_glass, "content_ns_window", lambda w: None):
            self.assertEqual(sf_glass.join_active_space(object()), "skip:no_nswindow")

    def test_failure_leaves_a_trace(self):
        """这一族失效的长相是"功能不见了、界面上零痕迹"，所以失败必须留 SWALLOWED。"""
        fake = _FakeNSWindow(behavior=258, on_active_space=False, raise_on_set=True)
        swallowed = []
        with mock.patch.object(sf_glass, "log_swallow",
                               lambda where, exc: swallowed.append(where)):
            out, _ = self._run(fake)
        self.assertTrue(out.startswith("error:"), out)
        self.assertEqual(swallowed, ["sf_glass.join_active_space"])


class _Popup:
    """够用的弹层替身：只需要"还在不在屏"这一问。"""

    def __init__(self, visible=True):
        self.visible = visible

    def isVisible(self):
        return self.visible

    def __repr__(self):
        return "<_Popup visible=%s>" % self.visible


class SpaceWatchdog(SFCase):
    """③**Show 那一次不够**，所以要按节拍续补 —— 这条是整个修复的核心。

    实测（源码版 + 真实 HID 右键，2026-10-13）：Show 事件里改完公开行为、调过一次
    私有 CGS，`isOnActiveSpace()` 仍是假、在屏列表 0/97 查不到；再补一拍才落地，
    截图上取得菜单自己的底色 (38,43,56)，从右键到在屏 0.27 秒。
    只测"补了一次"的用例会把这条测丢。
    """

    def setUp(self):
        super().setUp()
        sf_glass.clear_popup_space_watch()
        self.track_patch(mock.patch.object(sf_glass, "native_cocoa_active", lambda: True))

    def tearDown(self):
        sf_glass.clear_popup_space_watch()
        super().tearDown()

    def _watch(self, outcomes, widget):
        seq = list(outcomes)

        def fake_join(w):
            return seq.pop(0) if seq else outcomes[-1]
        self.track_patch(mock.patch.object(sf_glass, "join_active_space", fake_join))
        return sf_glass.watch_active_space(widget)

    def test_already_on_active_space_starts_no_timer(self):
        """桌面上一次就够 —— 不许为它起一张 30ms 的空转表（功耗口径）。"""
        out = self._watch(["ok:behavior=257"], _Popup())
        self.assertTrue(out.startswith("ok:behavior"))
        self.assertEqual(sf_glass.popup_space_pending(), [])
        self.assertFalse(sf_glass.popup_space_timer_running())

    def test_keeps_tickling_until_it_lands(self):
        popup = _Popup()
        self._watch(["ok:cgs_added behavior=257"], popup)
        self.assertEqual([w for w, _tries in sf_glass.popup_space_pending()], [popup])
        self.assertTrue(sf_glass.popup_space_timer_running())
        swallowed = []
        self.track_patch(mock.patch.object(sf_glass, "join_active_space",
                                           lambda w: "ok:behavior=257"))
        self.track_patch(mock.patch.object(sf_glass, "log_swallow",
                                           lambda *a: swallowed.append(a)))
        sf_glass.popup_space_tick()
        self.assertEqual(sf_glass.popup_space_pending(), [], "落地了还留在名单里")
        self.assertFalse(sf_glass.popup_space_timer_running(), "名单空了表没停")
        self.assertEqual(swallowed, [], "正常收工不该留下任何 SWALLOWED")

    def test_closed_popup_is_dropped_without_another_call(self):
        gone = _Popup(visible=False)
        self._watch(["ok:cgs_added behavior=257"], gone)      # 第一次补不上 → 进名单
        with mock.patch.object(sf_glass, "join_active_space") as join:
            sf_glass.popup_space_tick()
        join.assert_not_called()
        self.assertEqual(sf_glass.popup_space_pending(), [])

    def test_gives_up_at_the_cap_and_says_so(self):
        """补不上就一直补，到上限收手并说一声 —— 不靠翻内部计数，真跑够拍数。"""
        popup = _Popup()
        self._watch(["ok:cgs_added behavior=257"], popup)
        warned = []
        self.track_patch(mock.patch.object(sf_glass, "log_line",
                                           lambda msg, lvl="INFO": warned.append(lvl)))
        for _ in range(sf_glass.POPUP_SPACE_MAX_TICKS):
            sf_glass.popup_space_tick()
        self.assertEqual(sf_glass.popup_space_pending(), [], "到上限还不退=永远在补")
        self.assertEqual(warned, ["WARNING"], "放弃必须说，不许静默")
        self.assertFalse(sf_glass.popup_space_timer_running(), "放弃之后表也得停")

    def test_failure_in_tick_leaves_a_trace(self):
        boom = _Popup()

        def raise_join(w):
            raise RuntimeError("AppKit 拒收")
        self._watch(["ok:cgs_added behavior=257"], boom)
        swallowed = []
        self.track_patch(mock.patch.object(sf_glass, "join_active_space", raise_join))
        self.track_patch(mock.patch.object(sf_glass, "log_swallow",
                                           lambda where, exc: swallowed.append(where)))
        sf_glass.popup_space_tick()
        self.assertEqual(swallowed, ["sf_glass.popup_space_tick"])


class FilterWiring(SFCase):
    """④接线：过滤器挂在 app 上，顶层窗一 Show 就补齐。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])   # 必须自己持有引用

    def setUp(self):
        super().setUp()
        sf_glass.clear_space_join_filter()   # 不先清，"挂上了"会因为别的用例建过主窗而恒真

    def tearDown(self):
        sf_glass.clear_space_join_filter()
        super().tearDown()

    def test_show_of_a_top_level_window_triggers_it(self):
        self.assertTrue(sf_glass.install_space_join_filter(self._app))
        with mock.patch.object(sf_glass, "watch_active_space") as watch:
            top = self.track_widget(QWidget(None))
            top.show()
            QApplication.processEvents()
        self.assertIn(top, [c.args[0] for c in watch.call_args_list],
                      "顶层窗 Show 了却没补齐 —— 右键菜单就是这一条路")

    def test_child_widget_show_does_not_trigger_it(self):
        self.assertTrue(sf_glass.install_space_join_filter(self._app))
        parent = self.track_widget(QWidget(None))
        parent.show()
        with mock.patch.object(sf_glass, "watch_active_space") as watch:
            child = QWidget(parent)
            child.show()
            QApplication.processEvents()
            child.deleteLater()
        watch.assert_not_called()

    def test_install_is_idempotent_per_app(self):
        self.assertTrue(sf_glass.install_space_join_filter(self._app))
        self.assertFalse(sf_glass.install_space_join_filter(self._app))
        self.assertTrue(sf_glass.space_join_filter_installed())
        self.assertFalse(sf_glass.install_space_join_filter(None))


def _fake_http_get(*a, **k):
    """够真的行情载荷：返回空串会让主窗在构造后刷两行 ERROR，把噪音留给下一位。"""
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


class MainWindowWiresIt(SFCase):
    """建完主窗，这条补齐必须已经挂上（写在主窗构造里，判据才不用读源码）。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        sf_glass.clear_space_join_filter()
        for one in (
                patch.object(m, "load_config",
                             return_value=("sh600519", 100, 100, 200, "translucent")),
                patch.object(m, "save_config", lambda *a, **k: True),
                patch.object(sf_quote, "http_get", _fake_http_get),
                patch.object(m, "start_icon_worker", lambda: None),
                patch.object(m.FloatWindow, "_force_top", lambda self: None),
                patch.object(m.FloatWindow, "_inject_glass", lambda self: None),
                patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
                patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
        ):
            self.track_patch(one)

    def tearDown(self):
        win = getattr(self, "win", None)
        if win is not None:
            # §6.11：关窗必须排在撤桩之前，否则 closeEvent 的落盘走真实实现
            win._exit_persisted = True      # discipline-ok(R5): 幂等标记没有公开写口
            win.close()
        super().tearDown()

    def test_constructing_the_window_installs_the_filter(self):
        self.assertFalse(sf_glass.space_join_filter_installed(),
                         "前置不成立：还没建主窗就已经挂上了，这条判据会恒真")
        self.win = self.track_widget(m.FloatWindow())
        self.win.running = False
        self.win.timer.stop()
        self.win.top_timer.stop()
        self.assertTrue(sf_glass.space_join_filter_installed())


if __name__ == "__main__":
    unittest.main(verbosity=2)
