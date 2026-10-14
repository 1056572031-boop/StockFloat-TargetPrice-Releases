# -*- coding: utf-8 -*-
"""v1.4.34 回归：拖主界面下沿的把手，不得把窗口压到 0 高。

现场（用户 2026-09-24 报告 + 截图）：手动拖底部把手**偶尔**能一路拖到顶，主界面高度
变成 0。

以下都是离屏合成事件的实测数字（不是推断）：
  · 展开动画那 ~200ms 里 `minimumHeight()` 被**主动**降到 0 —— 不降的话中间帧会被
    MIN_H 钳住、收展过渡不连续，那是 v1.2.35 的既定设计。而旧限高公式
    `max(self.minimumHeight(), …)` 恰好拿它当地板：动画期间上拖 400px，实测
    height 一路到 **0**（min 采样 10 次全为 0）。
  · 静止展开态做同一个手势，实测最低停在 **224**（=MIN_H），不复现 —— 用户说的
    "偶尔"就是"手势落进了那 200 毫秒"。
  · 旧帧驱动里拉伸手势会**无条件挤掉**布局动画的完成帧，而那正是恢复 min 的一帧：
    持续拖动 1.2 秒后动画仍停在 (155,224)、min 仍为 0 —— 0 高的窗口期从 200ms
    被拉长成整段手势。
  · 再叠一层：macOS 在置顶/跨 Space 时偶发不投 mouseRelease（本仓多处注释已记录），
    `_resizing` 因此粘滞 → 2 秒后合并时钟停表 → 收展动画被**永久**遗弃、min 永久 0，
    实测二次上拖仍到 0；而 0 高的窗口没有可点区域，鼠标救不回来。

修法（两层，都落在"高度轴归谁管"上）：
  ① 限高的地板取**语义高度**（展开 MIN_H / 收起 collapsed_height），不取那个临时为 0
     的 Qt 下限；
  ② 拉伸手势这一拍把在飞的收展动画**就地收敛到终态、并照常发出 min/max 收口帧**，
     让"恢复下限"不再依赖动画自己跑完。
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent

import sf_anim_driver as D
import stock_float_window as m
from sf_constants import MAX_H, MIN_H
from tests._base import SFCase

COLLAPSED_H = 155      # 收起态高度（实测值，仅用于造"动画在飞"的形状）


def _ensure_app():
    from PyQt6.QtWidgets import QApplication
    # 必须自己持有引用：进程里没有别处存着 QApplication 时它会被回收
    app = QApplication.instance() or QApplication([])
    _ensure_app.app = app
    return app


class _WinCase(SFCase):
    """与 test_power_v1410 同口径的最小 patch 清单：让窗口能在离屏下被安全构造，
    不碰网络、不写配置、不起图标线程。这些 patch 不是被测对象。"""

    def make_win(self):
        _ensure_app()
        import sf_quote as _q
        self.track_patch(mock.patch.object(
            m, "load_config", return_value=("sh600519", 100, 100, MIN_H, "glass")))
        self.track_patch(mock.patch.object(m, "save_config", lambda *a, **k: True))
        self.track_patch(mock.patch.object(_q, "http_get", lambda *a, **k: ""))
        self.track_patch(mock.patch.object(m, "start_icon_worker", lambda: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_force_top",
                                           lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_inject_glass",
                                           lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_setup_reopen_handler",
                                           lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "raise_", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "activateWindow",
                                           lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_quote_loop_wrapper",
                                           lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_minute_loop_wrapper",
                                           lambda self: None))
        win = self.track_widget(m.FloatWindow())
        return win

    # ---- 事件合成 ----
    def _ev(self, kind, local, glob, buttons):
        return QMouseEvent(kind, QPointF(float(local[0]), float(local[1])),
                           QPointF(float(glob[0]), float(glob[1])),
                           Qt.MouseButton.LeftButton, buttons,
                           Qt.KeyboardModifier.NoModifier)

    def _press_bottom_edge(self, win):
        local = (win.width() - 20.0, win.height() - 4.0)
        g = win.mapToGlobal(QPointF(*local))
        glob = (g.x(), g.y())
        win.mousePressEvent(self._ev(QEvent.Type.MouseButtonPress, local, glob,
                                     Qt.MouseButton.LeftButton))
        return glob

    def _move_up(self, win, glob, px):
        target = (glob[0], glob[1] - px)
        loc = win.mapFromGlobal(QPointF(*target))
        win.mouseMoveEvent(self._ev(QEvent.Type.MouseMove,
                                    (loc.x(), loc.y()), target,
                                    Qt.MouseButton.LeftButton))
        return target

    def _release(self, win, glob):
        loc = win.mapFromGlobal(QPointF(*glob))
        win.mouseReleaseEvent(self._ev(QEvent.Type.MouseButtonRelease,
                                       (loc.x(), loc.y()), glob,
                                       Qt.MouseButton.NoButton))

    def _enter_expand_anim(self, win):
        """把窗口摆成"展开动画在飞"的形状：min 已被主动降到 0 + 动画已登记。"""
        win._chart_expanded = True  # discipline-ok(R5): 收展状态没有公开写入口，摆动画期间形状必须直接置位
        win.resize(win.width(), COLLAPSED_H)
        win.setMinimumHeight(0)
        win._anim_driver.begin_layout(COLLAPSED_H, MIN_H, time.monotonic())  # discipline-ok(R5): 动画登记只有这一条路


class TestStretchFloor(_WinCase):
    def test_press_arms_stretch_not_chart_hit(self):
        """前置：下沿 8px 内按下必须进入拉伸模式，否则后面几条测的就不是这条路。"""
        win = self.make_win()
        self._enter_expand_anim(win)
        glob = self._press_bottom_edge(win)
        self.assertTrue(win._resizing,  # discipline-ok(R5): 手势模式是内部状态，没有公开读取口
                        "按下下沿没进拉伸模式，用例前提不成立")

    def test_drag_floor_is_semantic_not_live_minimum(self):
        """限高的地板必须是语义高度：动画期间 Qt 下限是 0，照它算就能拖到 0 高。"""
        win = self.make_win()
        self._enter_expand_anim(win)
        glob = self._press_bottom_edge(win)
        self._move_up(win, glob, 400)
        # 直接调定时器槽：合并高度本来就是 16ms 时钟干的活。用 sleep 等时钟既不
        # 确定、也违反用例书写纪律第 3 条 —— 这里要的是"这一拍应用目标高度"。
        win._anim_tick()  # discipline-ok(R5): 16ms 合并时钟没有公开驱动口；用 sleep 等它会退化成抖动用例
        self.assertGreaterEqual(
            win.height(), MIN_H,
            "上拖 400px 后窗口矮于展开态语义下限（旧实现实测为 0）")

    def test_stretch_restores_minimum_height(self):
        """拉伸手势必须把被主动降为 0 的 Qt 下限恢复回来。"""
        win = self.make_win()
        self._enter_expand_anim(win)
        glob = self._press_bottom_edge(win)
        self._move_up(win, glob, 400)
        win._anim_tick()  # discipline-ok(R5): 16ms 合并时钟没有公开驱动口；用 sleep 等它会退化成抖动用例
        self.assertEqual(win.minimumHeight(), MIN_H,
                         "动画的 min/max 收口帧被拉伸挤掉了，下限会一直停在 0")

    def test_lost_release_cannot_park_min_at_zero(self):
        """丢掉 mouseRelease（macOS 置顶/跨 Space 偶发）后，不得把下限永久留在 0。

        这条守的是"主界面从此不见了、鼠标救不回来"那一支：0 高窗口没有可点区域。
        """
        win = self.make_win()
        self._enter_expand_anim(win)
        glob = self._press_bottom_edge(win)
        self._move_up(win, glob, 400)
        win._anim_tick()  # discipline-ok(R5): 16ms 合并时钟没有公开驱动口；用 sleep 等它会退化成抖动用例
        # 不 release，再连打若干拍（等价于合并时钟自己跑完）
        for _ in range(5):
            win._anim_tick()  # discipline-ok(R5): 16ms 合并时钟没有公开驱动口；用 sleep 等它会退化成抖动用例
        self.assertEqual(win.minimumHeight(), MIN_H)
        self.assertGreaterEqual(win.height(), MIN_H,
                                "无松开的情况下窗口停在语义下限之下")
        self.assertEqual(win._anim_driver.layout_anim_to,  # discipline-ok(R5): 动画是否被遗弃只能看登记值
                         win._anim_driver.layout_anim_from,  # discipline-ok(R5): 同上，无公开读取口
                         "收展动画被拉伸手势永久遗弃，完成帧再也不会发出来")

    def test_settled_expand_drag_unchanged(self):
        """对照：静止展开态上拖的行为不变（仍被钳在 MIN_H），别为了修 bug 改坏手感。"""
        win = self.make_win()
        win._chart_expanded = True  # discipline-ok(R5): 收展状态没有公开写入口，摆动画期间形状必须直接置位
        win.setMinimumHeight(MIN_H)
        win.setMaximumHeight(MAX_H)
        win.resize(win.width(), MIN_H + 60)
        h0 = win.height()
        glob = self._press_bottom_edge(win)
        self._move_up(win, glob, 400)
        win._anim_tick()  # discipline-ok(R5): 16ms 合并时钟没有公开驱动口；用 sleep 等它会退化成抖动用例
        self.assertEqual(win.height(), MIN_H,
                         "静止态上拖应收敛到 MIN_H（旧行为即如此，不该变）")
        self.assertGreater(h0, MIN_H)

    def test_floor_helper_ignores_transient_zero_minimum(self):
        """语义地板 helper：Qt 下限被动画降到 0 时，它必须仍然回答 MIN_H。

        被测的是这条规则本身（不是某个手势的结果），所以直接摆出"min=0 且展开"
        这个动画期间的形状。
        """
        win = self.make_win()
        win._chart_expanded = True  # discipline-ok(R5): 收展状态没有公开写入口，摆动画期间形状必须直接置位
        win.setMinimumHeight(0)
        self.assertEqual(win._stretch_floor_height(), MIN_H)  # discipline-ok(R5): 被测对象本身就是这个内部规则
        win._chart_expanded = False  # discipline-ok(R5): 同上，没有公开写入口
        self.assertEqual(win._stretch_floor_height(), win._height_collapsed())  # discipline-ok(R5): 同上


class TestDriverFrameOwnership(SFCase):
    """纯状态机层：同一拍里拉伸与收展动画抢高度轴时，谁说了算。"""

    def _driver(self):
        d = D.AnimDriver()
        d.begin_layout(COLLAPSED_H, MIN_H, 0.0)
        return d

    def test_settle_is_noop_without_inflight_layout(self):
        """没有动画在飞时必须返回空：否则每个拖拽拍都会无端重写一遍 min/max。"""
        d = D.AnimDriver()
        self.assertEqual(d.settle_layout_for_stretch(True), [])

    def test_stretch_frame_settles_inflight_layout(self):
        d = self._driver()
        acts = d.tick(True, MIN_H, COLLAPSED_H, 184, True, False,
                      0.05, 0.0, 0.0, 0.20)
        self.assertIn(("layout_minmax", True), acts,
                      "拉伸这一拍必须把 min/max 收口帧发出来，否则下限停在 0")
        self.assertEqual(d.layout_anim_from, d.layout_anim_to,
                         "动画应就地收敛，不再等自己跑完")

    def test_stretch_frame_still_owns_the_height(self):
        d = self._driver()
        acts = d.tick(True, 300, COLLAPSED_H, 184, True, False,
                      0.05, 0.0, 0.0, 0.20)
        self.assertIn(("resize", (184, 300)), acts,
                      "高度归拉伸说了算：动画收敛不能反过来盖掉用户拖到的位置")

    def test_no_drag_layout_animation_path_unchanged(self):
        """不拖的时候，布局动画仍按逐帧插值走（别把收展动画改成瞬跳）。"""
        d = self._driver()
        acts = d.tick(False, None, COLLAPSED_H, 184, True, False,
                      0.05, 0.0, 0.0, 0.20)
        self.assertTrue(any(a[0] == "resize" for a in acts))
        self.assertNotIn(("layout_minmax", True), acts,
                         "动画没跑完就收口，等于把 200ms 过渡吞掉")
        self.assertEqual(d.layout_anim_to, MIN_H, "动画登记必须还在")


if __name__ == "__main__":
    unittest.main(verbosity=2)
