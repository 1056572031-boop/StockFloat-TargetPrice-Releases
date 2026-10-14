"""窗口几何的纯坐标计算。

Qt 屏幕查询、窗口移动和玻璃层同步仍由主窗负责；本模块只处理给定可用区与窗口
矩形下的目标坐标，方便在不创建 QWidget 的情况下测试四边越界规则。
"""

from __future__ import annotations


def keep_on_screen_position(
    x: int,
    y: int,
    width: int,
    height: int,
    available_left: int,
    available_top: int,
    available_right: int,
    available_bottom: int,
    padding: int = 0,
) -> tuple[int, int]:
    """把窗口完整钳制在可用区内，使用 exclusive right/bottom 边界。"""
    right = x + width
    bottom = y + height
    target_x, target_y = x, y
    if right > available_right - padding:
        target_x = max(available_left + padding, available_right - width)
    if x < available_left:
        target_x = available_left
    if y < available_top:
        target_y = available_top
    if bottom > available_bottom:
        target_y = max(available_top, available_bottom - height)
    return target_x, target_y
