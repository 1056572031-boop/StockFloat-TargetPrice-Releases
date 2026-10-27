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
    """将窗口钳制到可用区边界内，right/bottom 使用 exclusive 边界。

    窗口尺寸不超过可用区时可完整显示；尺寸更大时放不下，只保证左上角落在边界内。
    padding 只是右沿的「贴齐容差」：窗口右沿离可用区右沿不足 padding 像素时，
    就地贴齐到右沿；已经贴齐或离得更远的窗口不动。它不是左侧留白，
    所以窗口宽到放不下时（宽度达到或超过可用区），左沿优先、落在可用区左沿，
    不会为了凑留白被推出右边界。
    """
    right = x + width
    bottom = y + height
    target_x, target_y = x, y
    if right > available_right - padding:
        target_x = max(available_left, available_right - width)
    if x < available_left:
        target_x = available_left
    if y < available_top:
        target_y = available_top
    if bottom > available_bottom:
        target_y = max(available_top, available_bottom - height)
    return target_x, target_y
