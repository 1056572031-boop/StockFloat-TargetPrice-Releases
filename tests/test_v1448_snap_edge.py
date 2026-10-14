# -*- coding: utf-8 -*-
"""屏幕边缘吸附的回归用例（v1.4.48：底部有时不生效）。

用户报的现象：「屏幕吸附边缘的功能，有时候不生效，特别是屏幕底部」。

根因（读代码 + 本文件用例证实，不是猜）：
  1. 拖动路径 `mouseMoveEvent` 里是裸 `self.move(...)`，**没有任何钳制** ——
     卡片可以被拖到 Dock 之下、甚至越过物理屏幕边缘。
  2. 松手判定 `_snap_to_edge` 用 `abs(卡片边 - availableGeometry 的边) <= SNAP_MARGIN`：
     一旦卡片被拖到可用区**之外**（越过物理底边），这个距离反而变大 —— 底部 Dock
     常驻时高约 70pt > 32pt 判定带，于是"贴得越狠越不吸附"。顶栏 24~37pt 有时在
     判定带内有时不在，就是"有时候"的来源；左右两侧没有保留区，所以一直正常。
  3. 顺带一条口径错：Qt 的 `QRect.right()/bottom()` 是**含端点**的（x+w-1），
     而代码拿 `y + h` 去比 —— 贴齐时距离恒为 1px，判定带白占 1px。

落点口径（保持"不许盖住 Dock"）：吸附仍然落在**可用区**边缘，
但判定改用"到物理边或可用边的带符号距离"，越过边缘=最有贴边意图，必须贴回来。
"""
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QWidget

try:
    from _base import SFCase
except ImportError:
    from tests._base import SFCase

# 模块级**持有** QApplication 引用：ensure_qapp() 只是返回对象，不持有就会被 GC，
# 之后任何 QWidget 构造都是 "Must construct a QApplication before a QWidget" 直接 fatal
# （本仓既有教训：Qt 用例必须自己持有 app 引用）。脱离 test_runner 单跑本文件时才看得出来。
from tests._base import SFCase as _SF

_APP = _SF.ensure_qapp()

import stock_float_window as m
from stock_float_window import SNAP_MARGIN

# 一块 1440x900 的屏幕，顶部菜单栏 25pt、底部 Dock 常驻 70pt（用户机器的常见形态）。
SCREEN = QRect(0, 0, 1440, 900)
MENU_BAR_H = 25
DOCK_H = 70
AVAILABLE = QRect(0, MENU_BAR_H, 1440, 900 - MENU_BAR_H - DOCK_H)

CARD_W = 184
CARD_H = 224
# 卡片被拖到物理屏幕底之外若干像素时，仍然必须贴到 Dock 上沿
PAST_BOTTOM_PX = 60


class _FakeScreen:
    """只提供 _snap_to_edge 需要的两个几何：物理屏幕与可用区（去掉菜单栏/Dock）。"""

    def __init__(self, avail=AVAILABLE, full=SCREEN):
        self._avail, self._full = avail, full

    def availableGeometry(self):
        return self._avail

    def geometry(self):
        return self._full


def _patch_screen(case, screen):
    case.track_patch(patch.object(QGuiApplication, "screenAt", staticmethod(lambda p: screen)))
    case.track_patch(patch.object(QGuiApplication, "primaryScreen", staticmethod(lambda: screen)))


class SnapToEdgeCase(SFCase):
    """吸附的两个方向都验：越界要贴回来，中途不许被误吸。"""

    def _card(self):
        """用一个真 QWidget 宿主跑 _snap_to_edge：它只读几何与 move()，不碰其它主窗状态。

        为什么不建整只 FloatWindow：本用例要的是"给定卡片位置 → 落到哪"，
        FloatWindow 会带来行情线程、玻璃层与后台定时器，噪声大且容易把偶发时序
        当成吸附缺陷。宿主直接绑产品方法的**真实现**，不是替身。
        """
        host = QWidget()
        host.resize(CARD_W, CARD_H)
        self.track_widget(host)
        host.snap = lambda: m.FloatWindow._snap_to_edge(host)   # discipline-ok(R5): 产品的真实现，宿主只给了几何与 move
        host.show()
        return host

    def test_snaps_when_dragged_past_the_physical_bottom(self):
        """拖过物理底边 60px（越过 Dock）：当前实现完全不吸附 —— 这就是用户报的现象。"""
        host = self._card()
        _patch_screen(self, _FakeScreen())
        # 卡片底边落到物理屏幕底之下 60px：距可用底边 130px，> 判定带 32px
        y_past = SCREEN.bottom() - CARD_H + PAST_BOTTOM_PX
        host.move(400, y_past)
        self.assertEqual(host.y(), y_past, "宿主没被摆到预期位置，用例本身失真")
        host.snap()
        self.assertLessEqual(abs(host.y() + CARD_H - (AVAILABLE.bottom() + 1)), 1,
                             "越过物理底边必须贴回可用区底边（Dock 上沿），实际停在 y=%d" % host.y())

    def test_snaps_when_dragged_past_the_left_edge(self):
        """越过左边缘同样是最强的贴边意图：现在因为用 abs() 也不生效。"""
        host = self._card()
        _patch_screen(self, _FakeScreen())
        host.move(-40, 300)
        host.snap()
        self.assertEqual(host.x(), AVAILABLE.left(),
                         "越过左边缘应当贴回左边缘，实际 x=%d" % host.x())

    def test_flush_position_stays_flush_after_snap(self):
        """已经贴齐可用底边时，snap 不许再把它挪 1px（Qt right/bottom 含端点的口径）。"""
        host = self._card()
        _patch_screen(self, _FakeScreen())
        flush_y = AVAILABLE.y() + AVAILABLE.height() - CARD_H
        host.move(400, flush_y)
        host.snap()
        self.assertEqual((host.x(), host.y()), (400, flush_y),
                         "贴齐的位置被挪动了 %d px —— 含端点/非含端点混用的典型症状"
                         % (host.y() - flush_y))

    def test_does_not_snap_a_card_sitting_mid_screen(self):
        """反例：屏幕中间的卡片必须原地不动，否则吸附变成了乱跳。"""
        host = self._card()
        _patch_screen(self, _FakeScreen())
        mid_y = AVAILABLE.y() + (AVAILABLE.height() - CARD_H) // 2
        host.move(600, mid_y)
        host.snap()
        self.assertEqual((host.x(), host.y()), (600, mid_y),
                         "中途卡片被误吸附：判定带 %d px 不该覆盖到这里" % SNAP_MARGIN)

    def test_landing_never_leaves_the_available_area(self):
        """卡片高于可用区时，顶部吸附不许把它推到可用区之外（贴顶但仍在屏内）。"""
        host = self._card()
        host.resize(CARD_W, AVAILABLE.height() + 60)
        _patch_screen(self, _FakeScreen())
        host.move(400, AVAILABLE.top() + 4)
        host.snap()
        self.assertGreaterEqual(host.y(), AVAILABLE.top(),
                               "贴顶之后反而越出上沿（y=%d），落点必须再钳一次" % host.y())
        self.assertLessEqual(host.y() + host.height(), AVAILABLE.bottom() + 1 + 200 + 60,
                             "落点计算失控：卡片底部远超合理范围")


if __name__ == "__main__":
    unittest.main(verbosity=2)
