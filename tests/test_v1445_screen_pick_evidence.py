# -*- coding: utf-8 -*-
"""v1.4.45 复核 B2：把"选屏选得对"从自指断言变成可证判据。

原来那条用例（tests/test_v1438_alert_center.py::ScreenAnchorTest
.test_anchor_inside_a_screen_selects_that_screen）是**自指**的：先问
`target_screen()` 选到哪块屏，再把锚点设成那块屏的中心，然后断言
`target_screen()` 还选它。单屏机器上这条断言永远成立，多屏时也只能证明
"它没变卦"，证明不了"副屏上点检查更新会弹在副屏"。

这里改成注入两块假屏：判据是"锚点在哪块屏的矩形里，就必须返回那块屏"，
与实现选了什么无关。顺带覆盖拔掉副屏、鼠标兜底、主屏不在原点这几条退化路径。

只用假对象，不碰真窗口服务，所以任何机器都能跑（无 AppKit 时才跳过）。
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_system_alert as s  # noqa: E402
from tests._base import SFCase  # noqa: E402

# 坐标换算是模块私有实现，没有公开出口；豁免集中这一处，用例正文用别名。
_qt_to_ns = s._qt_to_ns  # discipline-ok(R5): 被测对象本身就是私有实现


class _Point:
    def __init__(self, x, y):
        self.x, self.y = float(x), float(y)


class _Size:
    def __init__(self, w, h):
        self.width, self.height = float(w), float(h)


class _Rect:
    def __init__(self, x, y, w, h):
        self.origin, self.size = _Point(x, y), _Size(w, h)


class _FakeScreen:
    def __init__(self, name, frame, visible=None):
        self.name = name
        self._frame = frame
        self._visible = visible or frame

    def frame(self):
        return self._frame

    def visibleFrame(self):
        return self._visible

    def __repr__(self):
        return "<Screen %s>" % self.name


class _FakeNSScreen:
    """NSScreen 的假替身：屏幕列表与主屏都由用例决定。"""

    screens_list = []
    main_screen = None

    @classmethod
    def screens(cls):
        return list(cls.screens_list)

    @classmethod
    def mainScreen(cls):
        return cls.main_screen


class _FakeNSEvent:
    mouse = (0.0, 0.0)

    @classmethod
    def mouseLocation(cls):
        return _Point(*cls.mouse)


class _FakeAppKit:
    NSScreen = _FakeNSScreen
    NSEvent = _FakeNSEvent


def _two_screens():
    """主屏 1512x982 在原点，副屏同样大小摆在右边（x 从 1512 起）。"""
    main = _FakeScreen("main", _Rect(0, 0, 1512, 982))
    side = _FakeScreen("side", _Rect(1512, 120, 1512, 982))
    _FakeNSScreen.screens_list = [main, side]
    _FakeNSScreen.main_screen = main
    return main, side


class ScreenPickWithFakeDisplays(SFCase):
    def setUp(self):
        super().setUp()
        self._patches = [patch.object(s, "AppKit", _FakeAppKit)]
        for p in self._patches:
            self.track_patch(p)
        s.set_screen_anchor(None, None)
        _FakeNSEvent.mouse = (0.0, 0.0)

    def tearDown(self):
        s.set_screen_anchor(None, None)
        _FakeNSScreen.screens_list = []
        _FakeNSScreen.main_screen = None
        _FakeNSEvent.mouse = (0.0, 0.0)
        super().tearDown()

    def test_anchor_on_the_second_display_picks_the_second_display(self):
        """用户在副屏上点「检查更新」：框必须弹在副屏，而不是主屏正中。"""
        main, side = _two_screens()
        s.set_screen_anchor(1512 + 756, 120 + 491)      # 副屏中心（Qt 全局坐标）
        got = s.target_screen()
        self.assertIs(got, side, "锚点落在副屏矩形内就必须选副屏")
        self.assertIsNot(got, main)

    def test_anchor_on_the_first_display_still_picks_the_first(self):
        main, side = _two_screens()
        s.set_screen_anchor(756, 491)                   # 主屏中心
        self.assertIs(s.target_screen(), main)

    def test_stale_anchor_after_unplugging_falls_back_to_the_mouse_screen(self):
        """刚拔掉副屏：登记的锚点飘在屏外，也不能不弹 —— 退回鼠标所在屏。"""
        main, side = _two_screens()
        _FakeNSScreen.screens_list = [main]             # 副屏没了
        s.set_screen_anchor(2268, 611)                  # 飘在原副屏位置的旧锚点
        _FakeNSEvent.mouse = (100.0, 100.0)             # 鼠标在主屏
        self.assertIs(s.target_screen(), main)

    def test_no_anchor_uses_the_mouse_screen_not_the_main_one(self):
        """没登记锚点时按鼠标选屏：副屏上用程序的人不该被弹回主屏。"""
        main, side = _two_screens()
        _FakeNSEvent.mouse = (1600.0, 200.0)
        self.assertIs(s.target_screen(), side)

    def test_everything_weird_still_returns_a_screen(self):
        """最坏退化：屏列表里没有主屏、鼠标也不在任何屏内 → 仍要给出兜底屏。"""
        orphan = _FakeScreen("orphan", _Rect(500, 500, 800, 600))
        _FakeNSScreen.screens_list = [orphan]
        _FakeNSScreen.main_screen = None
        _FakeNSEvent.mouse = (99999.0, 99999.0)
        s.set_screen_anchor(99999.0, 99999.0)
        self.assertIs(s.target_screen(), orphan, "拿不到主屏时用第一块屏兜底，绝不能不弹")

    def test_qt_to_ns_flips_on_the_screen_at_the_origin(self):
        """坐标换算的翻转基准必须是"原点在那块屏"的高度（Qt 与 NS 只差 y 方向）。"""
        main, side = _two_screens()
        self.assertIsNotNone(main)
        # 主屏高 982：Qt 的 y=0（屏顶）→ NS 的 y=982
        self.assertEqual(_qt_to_ns(0, 0), (0.0, 982.0))
        self.assertEqual(_qt_to_ns(756, 491), (756.0, 491.0))

    def test_qt_to_ns_survives_a_screen_table_without_an_origin_screen(self):
        """异常配置（没有一块屏的原点是 (0,0)）也要算得出来，不能抛。"""
        _FakeNSScreen.screens_list = [_FakeScreen("a", _Rect(300, 40, 800, 600))]
        flipped = _qt_to_ns(0, 0)
        self.assertIsNotNone(flipped)


if __name__ == "__main__":
    unittest.main()
