# -*- coding: utf-8 -*-
"""v1.4.59（S2）：窗口被甩出屏幕之后的三条收口路径。

30 轮走查 S2 的实测形态是"用户只能强退"：
  ① 缩在 Dock 期间拔掉外接屏 / 改分辨率 → 点 Dock 恢复，桌面上什么都不出现
     （`_restore_window` 从 show() 到结束没有任何钳制，而 `_on_screen_changed`
      在隐藏期开头就 return）；
  ② 贴底展开分时图 → 卡片向下长到 Dock 之下，还被 save_config 写盘固化
     （`_clamp_to_screen` 判据是 `not ag.intersects(g)`，剩 1px 在屏内就不管）；
  ③ `_keep_on_screen` 用 Qt 含端点的 right()/bottom()，与 v1.4.48 刚改成
     非含端点的 `_snap_to_edge` 差 1px —— 吸附刚贴齐，下一次回移就把它推翻，
     而 v1.4.48 对用户承诺的是"贴齐不许挪 1 像素"。

几何数学用轻量宿主（绑产品真方法 + 假屏），恢复路径用真窗口验接线。
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QRect                                # noqa: E402
from PyQt6.QtGui import QGuiApplication                       # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget             # noqa: E402

import stock_float_window as m                                # noqa: E402

try:
    from _base import SFCase
except ImportError:                                            # pragma: no cover
    from tests._base import SFCase

# 假屏：主屏 0,0 1512x982，菜单栏 37pt、Dock 70pt → 可用区 (0,37,1512,875)
SCREEN = QRect(0, 0, 1512, 982)
USABLE = QRect(0, 37, 1512, 875)


class _FakeScreen:
    def __init__(self, avail, full=None):
        self._avail, self._full = avail, (full or avail)

    def availableGeometry(self):
        return self._avail

    def geometry(self):
        return self._full


class _GeomHost:
    """只装几何成员；`_keep_on_screen` / `_snap_to_edge` 都是产品真方法。"""

    _keep_on_screen = m.FloatWindow._keep_on_screen   # discipline-ok(R5): 宿主按名字绑产品真方法，这条路径没有公开入口
    _snap_to_edge = m.FloatWindow._snap_to_edge   # discipline-ok(R5): 宿主按名字绑产品真方法，这条路径没有公开入口

    def __init__(self, x, y, w=184, h=224):
        self._x, self._y, self._w, self._h = x, y, w, h
        self.moved = []

    def x(self):
        return self._x

    def y(self):
        return self._y

    def width(self):
        return self._w

    def height(self):
        return self._h

    def frameGeometry(self):
        return QRect(self._x, self._y, self._w, self._h)

    def move(self, x, y):
        self.moved.append((x, y))
        self._x, self._y = int(x), int(y)


class KeepOnScreenMath(SFCase):
    """`_keep_on_screen` 与 `_snap_to_edge` 必须用同一套几何口径。"""

    def _patch_screen(self):
        return patch.object(QGuiApplication, "screenAt",
                            staticmethod(lambda point: _FakeScreen(USABLE, SCREEN)))

    def test_flush_card_is_not_nudged_one_pixel(self):
        """贴齐可用区右沿（x+w == 右沿）时不许再挪 —— 旧实现会往左挪 1px。"""
        h = _GeomHost(USABLE.right() + 1 - 184, 100)     # x + w == ag.x()+ag.width()
        with self._patch_screen():
            h._keep_on_screen()   # discipline-ok(R5): 同上：调的是宿主上绑的产品真方法
        self.assertEqual(h.moved, [],
                         "已经贴齐却被挪动 %r —— 吸附与回移互相推翻（S2 第三处）" % (h.moved,))

    def test_card_below_the_dock_is_pulled_back_up(self):
        h = _GeomHost(200, USABLE.bottom() + 60)
        with self._patch_screen():
            h._keep_on_screen()   # discipline-ok(R5): 同上：调的是宿主上绑的产品真方法
        self.assertEqual(len(h.moved), 1, "整体长到 Dock 之下必须上移")
        self.assertLessEqual(h.y() + h.height(), USABLE.bottom() + 1,
                             "回移之后底沿不许还越界")

    def test_snap_still_works_when_dragged_past_the_dock(self):
        """守 ④ 那条死代码的删除**不改变行为**：越过可用区底沿 60px 仍要贴回去。

        真正让它吸回来的是"带符号距离 + 任何负数都算在判定带内"，
        而不是那条从来没生效过的物理边候选。
        """
        ag_bottom = USABLE.y() + USABLE.height()          # 产品用非含端点口径
        h = _GeomHost(200, ag_bottom + 60 - 224, w=184, h=224)
        with self._patch_screen():
            h._snap_to_edge()   # discipline-ok(R5): 同上：调的是宿主上绑的产品真方法
        self.assertEqual(h.moved, [(200, ag_bottom - 224)],
                         "越界 60px 必须被吸回可用区底沿，而不是留在 Dock 之下")


class RestoreClampsToScreen(SFCase):
    """①：点 Dock 恢复之后必须钳一次屏 —— 旧实现整条路径没有任何钳制。"""

    def setUp(self):
        super().setUp()
        self.app = QApplication.instance() or QApplication([])
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 224, "glass")),
            patch.object(m, "save_config", lambda *a, **k: True),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m, "show_dock", lambda: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_force_fetch", lambda self: None),
            patch.object(m.FloatWindow, "_reassert_top", lambda self: None),
            patch.object(m.FloatWindow, "_sync_glass_frame", lambda self, **k: None),
        ]
        for p in self._patches:
            self.track_patch(p)
        self.win = m.FloatWindow()
        self.track_widget(self.win)

    def test_restore_clamps_and_repositions(self):
        self.win._minimized = True   # discipline-ok(R5): offscreen 下没有公开入口能构造这个现场
        with patch.object(m.FloatWindow, "_clamp_to_screen") as clamp, \
                patch.object(m.FloatWindow, "_keep_on_screen") as keep:
            self.win._restore_window()   # discipline-ok(R5): 同上：调的是宿主上绑的产品真方法
        self.assertTrue(clamp.call_args.kwargs.get("force") is True,
                        "恢复路径必须**强制**钳制：一次性闸门那时已经用掉了：%r"
                        % (clamp.call_args_list,))
        self.assertEqual(keep.call_count, 1,
                         "钳完还要回移一次（intersects 判据放过'还剩 1px 在屏内'）")

    def test_height_settle_repositions_too(self):
        """②：布局动画完成帧（高度收口）之后也要回移 —— 宽度变化早就有这一步。"""
        self.win._anim_driver.tick = lambda *a, **k: [("layout_minmax", True)]   # discipline-ok(R5): offscreen 下没有公开入口能构造这个现场
        with patch.object(m.FloatWindow, "_keep_on_screen") as keep, \
                patch.object(m.FloatWindow, "_repaint_now", lambda self, *a: None):
            self.win._anim_tick_inner()   # discipline-ok(R5): 同上：调的是宿主上绑的产品真方法
        self.assertGreaterEqual(keep.call_count, 1,
                                "展开分时图长高之后没人回移，底部会长到 Dock 之下")


if __name__ == "__main__":
    unittest.main()
