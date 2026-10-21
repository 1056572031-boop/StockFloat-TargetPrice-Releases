# -*- coding: utf-8 -*-
"""收展动画期间呼吸灯时钟与 resizeEvent 的重复整窗帧合并。

背景（2026-10-21 帧率审计，真实窗口 30 秒 x 2 轮）：收展动画每一帧的 resize 会在
resizeEvent 里排一次整窗帧，呼吸灯 16ms 时钟自己又排一次，两者落在不同事件循环轮次，
Qt 合并不了。展开方向约 30% 的相邻绘制间隔小于 8ms，而屏幕只有 60Hz，多出来的帧
根本不会被显示。合并后同场景降到约 5%，帧间隔中位数回到 16ms。

守三件事（全部按 update() 调用次数这一可观测行为断言）：
  1. 动画进行中、上个节拍以来确有 resizeEvent：呼吸灯这一拍不再重复排帧；
  2. 没有 resizeEvent（动画某帧高度没变）：呼吸灯照常排帧，绝不因此少一帧；
  3. 不在收展动画里（拖动拉伸、静止）：行为与以前完全一致，两路各排各的。

内部状态只经 _peek / _poke 访问（理由同 test_coverage_window_interactions：
成员没有公开读写口，且需要摆出「动画进行中」这个前提）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QResizeEvent

from tests.test_power_v1410 import _WinCase


def _peek(obj, name):
    return getattr(obj, name)


def _poke(obj, name, value):
    setattr(obj, name, value)


def _call(obj, name, *args):
    return getattr(obj, name)(*args)


class DotTickCoalescing(_WinCase):
    """呼吸灯时钟 x resizeEvent。"""

    def armed_win(self):
        """摆成「盘中 + 图表展开 + 有分时数据」，呼吸灯时钟已武装，update() 被记录。"""
        win = self.make_win()
        _poke(win, "_chart_expanded", True)
        _poke(win, "_minute_stale", False)
        win.minute_points = [(9 * 60 + i, 20.0 + i * 0.01) for i in range(60)]
        self.track_patch(mock.patch.object(win, "isVisible", return_value=True))
        self.track_patch(mock.patch.object(win, "_is_trading_time", return_value=True))
        _call(win, "_sync_dot_timer")
        self.assertTrue(_peek(win, "_dot_timer").isActive(), "前置条件：呼吸灯时钟应在跑")
        self.updates = []
        self.track_patch(mock.patch.object(
            win, "update", side_effect=lambda *a: self.updates.append(a)))
        return win

    @staticmethod
    def layout_animating(win, on=True):
        """摆出「收展布局动画进行中」：起点与目标高度不同。"""
        _poke(win, "_layout_anim_from", 145 if on else 0)
        _poke(win, "_layout_anim_to", 260 if on else 0)

    @staticmethod
    def fire_resize(win, height):
        """走真实的 resizeEvent（动画每帧 resize 触发的就是它）。"""
        win.resizeEvent(QResizeEvent(QSize(win.width(), height), QSize(win.width(), height - 3)))

    def test_resize_in_animation_covers_the_dot_tick(self):
        win = self.armed_win()
        self.layout_animating(win)
        self.fire_resize(win, 200)
        self.assertEqual(len(self.updates), 1, "resizeEvent 应排一次整窗帧")
        _call(win, "_dot_tick")
        self.assertEqual(len(self.updates), 1,
                         "同一周期里呼吸灯又排了第二帧：60Hz 屏上这一帧不会被显示")

    def test_tick_without_resize_still_paints(self):
        """动画某帧高度没变化、没有 resizeEvent 时，呼吸灯必须自己排帧。"""
        win = self.armed_win()
        self.layout_animating(win)
        _call(win, "_dot_tick")
        self.assertEqual(len(self.updates), 1, "没有 resizeEvent 却不排帧：呼吸灯会少一帧")

    def test_skipped_tick_never_starves_the_next_one(self):
        """被覆盖的那一拍跳过之后，下一拍若没有新的 resizeEvent，必须照常排帧。"""
        win = self.armed_win()
        self.layout_animating(win)
        self.fire_resize(win, 200)
        _call(win, "_dot_tick")                      # 被覆盖，跳过
        before = len(self.updates)
        _call(win, "_dot_tick")                      # 没有新 resize
        self.assertEqual(len(self.updates), before + 1,
                         "已见序号没随拍更新：覆盖状态粘住，呼吸灯会连续丢帧")

    def test_outside_layout_animation_both_paths_paint(self):
        """拖动拉伸、静止时：resizeEvent 与呼吸灯各排各的，行为与以前一致。"""
        win = self.armed_win()
        self.layout_animating(win, on=False)
        self.fire_resize(win, 200)
        _call(win, "_dot_tick")
        self.assertEqual(len(self.updates), 2,
                         "非收展动画期间呼吸灯被跳过：拖动拉伸时呼吸灯会变稀")

    def test_resize_while_timer_stopped_does_not_count_as_covering(self):
        """停表期间发生的 resizeEvent 不能算作「本周期已覆盖」，起表后第一拍要排帧。"""
        win = self.armed_win()
        _peek(win, "_dot_timer").stop()
        self.layout_animating(win)
        self.fire_resize(win, 200)
        _call(win, "_sync_dot_timer")                # 起表，应把已见序号对齐
        self.assertTrue(_peek(win, "_dot_timer").isActive())
        before = len(self.updates)
        _call(win, "_dot_tick")
        self.assertEqual(len(self.updates), before + 1,
                         "起表时没对齐已见序号：停表期间的旧 resize 被当成覆盖，首拍丢帧")


if __name__ == "__main__":
    unittest.main()
