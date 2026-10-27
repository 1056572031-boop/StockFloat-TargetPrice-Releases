#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时股价悬浮窗 —— 纯绘制辅助函数模块（20.1 职责拆分安全子集）。

只包含【不依赖 FloatWindow 实例状态】的纯计算/构造函数，从主窗口类下沉：
- smooth_round_rect_path     : 超椭圆圆角 QPainterPath（主窗背景路径）
- _glass_squircle_cgpath     : 供 Qt/CGPath 几何对拍的超椭圆路径辅助（当前玻璃层使用原生圆角 mask）
- _bottom_layout             : 底部四段布局唯一实现（paintEvent 与宽度计算共用，
                               从根上消除双份布局漂移，见 20.2）
- price_pct_layout           : 价格行布局唯一实现（价格大字 + 右侧涨跌幅 + 状态徽标，
                               v1.2.63；先扣涨跌幅预算再自适应字号，杜绝位置抖动）

主窗口类仍保留绘制编排、缓存管理与交互逻辑；本模块不持有任何实例状态，
不依赖网络/配置/日志，可独立单测。
"""
import math

from PyQt6.QtGui import QPainterPath
from PyQt6.QtCore import QRectF

# 超椭圆阶数（squircle 曲率参数），Qt 路径与 CGPath 共用，保持四角数学严格一致
_SQUIRCLE_N = 4.0


def liquid_glass_dim_path(w, h, r, inset):
    """Inset circular corners concentric with NSGlassEffectView.cornerRadius."""
    path = QPainterPath()
    radius = max(0.0, r - inset)
    path.addRoundedRect(QRectF(inset, inset, max(w - 2 * inset, 1),
                               max(h - 2 * inset, 1)), radius, radius)
    return path


def _squircle_se_point(cx, cy, t, r):
    """超椭圆圆角曲线的单个参数点（v1.2.22 抽为唯一实现）：
    Qt 侧 smooth_round_rect_path 与 CGPath 侧 _glass_squircle_cgpath 共用，
    保证两端圆角逐点一致（此前各写一份，改一处漏一处）。"""
    c, s = math.cos(t), math.sin(t)
    px = cx + r * (abs(c) ** (2.0 / _SQUIRCLE_N)) * (1 if c >= 0 else -1)
    py = cy + r * (abs(s) ** (2.0 / _SQUIRCLE_N)) * (1 if s >= 0 else -1)
    return px, py


def _squircle_arc_points(cx, cy, t0, t1, r):
    """四角超椭圆弧的采样点（13 点均分）。

    v1.3.46 瘦身：原先 smooth_round_rect_path 与 _glass_squircle_cgpath 各内嵌一份
    逐字相同的 arc 闭包。两处必须永远一致 —— 玻璃层 mask 与主窗圆角一旦算得不同，
    四角就会出现"大圆角套小圆角"的重影。收敛成唯一实现，从根上杜绝两边改歪。
    """
    # 每角 90° 取 12 段（13 点，约 7.5°/段）：再粗会露出多边形棱角，再密只是
    # 徒增每帧路径顶点数。采样数两侧共用，改动会同时影响主窗圆角与玻璃层 mask。
    pts = []
    for i in range(13):
        t = t0 + (t1 - t0) * i / 12
        pts.append(_squircle_se_point(cx, cy, t, r))
    return pts


def smooth_round_rect_path(x, y, w, h, r):
    """超椭圆（squircle）平滑圆角路径：连续曲率过渡，比普通圆弧更柔和"""
    path = QPainterPath()

    TR = (x + w - r, y + r)
    TL = (x + r, y + r)
    BL = (x + r, y + h - r)
    BR = (x + w - r, y + h - r)

    tr_pts = _squircle_arc_points(*TR, -math.pi / 2, 0, r)
    br_pts = _squircle_arc_points(*BR, 0, math.pi / 2, r)
    bl_pts = _squircle_arc_points(*BL, math.pi / 2, math.pi, r)
    tl_pts = _squircle_arc_points(*TL, math.pi, 3 * math.pi / 2, r)

    path.moveTo(*tr_pts[0])
    for pt in tr_pts[1:]:
        path.lineTo(*pt)
    path.lineTo(x + w, y + h - r)
    for pt in br_pts[1:]:
        path.lineTo(*pt)
    path.lineTo(x + r, y + h)
    for pt in bl_pts[1:]:
        path.lineTo(*pt)
    path.lineTo(x, y + h - r)
    for pt in tl_pts[1:]:
        path.lineTo(*pt)
    path.lineTo(x + w - r, y)
    path.closeSubpath()
    return path


def _glass_squircle_cgpath(x, y, w, h, r):
    """构建与 Qt 侧 `smooth_round_rect_path` 对拍的超椭圆圆角 CGPath。

    当前运行时玻璃层使用原生圆角 mask；该辅助保留给几何一致性测试。"""
    # Quartz 仅 macOS 存在：延迟到调用时导入，让本模块在非 macOS / 无 PyObjC
    # 环境下仍可 import（单测只走 Qt 侧路径时不会因缺库直接崩）。
    from Quartz import (CGPathCreateMutable, CGPathMoveToPoint,
                        CGPathAddLineToPoint, CGPathCloseSubpath)
    path = CGPathCreateMutable()

    TR = (x + w - r, y + r)
    BR = (x + w - r, y + h - r)
    BL = (x + r, y + h - r)
    TL = (x + r, y + r)

    tr = _squircle_arc_points(*TR, -math.pi / 2, 0, r)
    br = _squircle_arc_points(*BR, 0, math.pi / 2, r)
    bl = _squircle_arc_points(*BL, math.pi / 2, math.pi, r)
    tl = _squircle_arc_points(*TL, math.pi, 3 * math.pi / 2, r)
    CGPathMoveToPoint(path, None, *tr[0])
    for pt in tr[1:]:
        CGPathAddLineToPoint(path, None, *pt)
    CGPathAddLineToPoint(path, None, x + w, y + h - r)
    for pt in br[1:]:
        CGPathAddLineToPoint(path, None, *pt)
    CGPathAddLineToPoint(path, None, x + r, y + h)
    for pt in bl[1:]:
        CGPathAddLineToPoint(path, None, *pt)
    CGPathAddLineToPoint(path, None, x, y + h - r)
    for pt in tl[1:]:
        CGPathAddLineToPoint(path, None, *pt)
    CGPathAddLineToPoint(path, None, x + w - r, y)
    CGPathCloseSubpath(path)
    return path


def _bottom_layout(width, row1, row2, fm, fm_bold,
                   pad_x=12, inner_gap=2,
                   gap=4, slack=8, base=200):
    """底部双行两段布局唯一实现（v1.2.62）。

    输入：
      row1 : [(label, num, color)]                      上行段（左对齐）
      row2 : [(label, num, color)]                      下行段（左对齐）
      fm / fm_bold : 常规/加粗 QFontMetrics
      间距常量由调用方（主模块）传入，本模块不复制常量，避免双份漂移。
    返回 (segs_row1, segs_row2, min_width)：
      segs_row1 / segs_row2 : 每段 dict（label/num/color/label_w/num_w/seg_w/位置/fits）
      min_width: 两行中任一行完整放下所需最大宽度 + 边距/余量（最小窗口宽度，与 width 无关）
    排布语义：
      每行从左往右、段内 (label, num) 贴紧（inner_gap）。
      段间在给定的 width 内逐段判断 fits；fits=False 即该段放弃绘制。
      两行都偏左（不含右对齐分支）：同属一信息组，作紧凑分层展示。
      两行视觉间距由调用方传入 y 坐标计算（见 sf_ui_render._paint_bottom）。
    """
    def _build(segs):
        out = []
        for label, num, color in segs:
            label_w = fm.horizontalAdvance(label)
            num_w = fm_bold.horizontalAdvance(num)
            out.append({"label": label, "num": num, "color": color,
                        "label_w": label_w, "num_w": num_w,
                        "seg_w": label_w + inner_gap + num_w})
        return out

    def _arrange(segs, w):
        # 全部紧凑左对齐：每行从 pad_x 开始逐段排布，段间 gap 间距。
        # 不做右对齐/两端锚定——用户要求底部数据整体靠左。
        x = pad_x
        right_limit = w - pad_x
        for s in segs:
            s["x"] = x
            s["fits"] = (x + s["seg_w"]) <= right_limit
            # 放不下的段不推进 x：其后各段共用同一 x，也一并判否。
            # 否则会出现「中间空一段、后面又冒出半截文本」的错位排版。
            if s["fits"]:
                x += s["seg_w"] + gap
        return segs

    built1 = _build(row1)
    built2 = _build(row2)
    # 宽度：每行单独排版，min_width 取两行中更大者 + 左右边距/余量
    _arrange(built1, width)
    _arrange(built2, width)
    def _row_min_width(segs):
        if not segs:
            return 0
        mw = pad_x * 2
        for s in segs:
            mw += s["seg_w"]
        mw += gap * max(len(segs) - 1, 0)
        return mw
    row_min_w = max(_row_min_width(built1), _row_min_width(built2))
    # base：窗口宽度下限（文本再短也不窄于此）；slack：给抗锯齿与圆角留的余量。
    # 二者均由调用方按主模块常量传入，本模块只做兜底。
    min_width = int(max(base, row_min_w + slack))
    return built1, built2, min_width


def price_pct_layout(width, measure, pct_w,
                     pad_x=12, right_pad=12, pct_gap=6,
                     badge_w=0, badge_gap=4,
                     size_max=27, size_min=16, step=2,
                     min_price_w=24):
    """价格行布局唯一实现（v1.2.63→v1.2.70）—— 价格大字 + 右侧涨跌幅 + 徽标(在涨跌幅上方)。

    v1.2.70 变更：状态徽标从「价格→徽标→涨跌幅」水平内联改为
    「涨跌幅正上方居中」垂直堆叠。徽标不再消耗水平预算，
    价格字号因此更大（徽标不挤占价格/涨跌幅的水平空间）。

    背景：v1.2.61 把涨跌幅移到股价右侧，用「剩余零头够不够」决定放在右侧还是
    回落到下方。但价格字号自适应本就把预算吃满（price_w >= 价格文本宽），
    零头恒 <= -(pad_x + pct_gap)，判定几乎必然失败 -> v1.2.62 收窄窗口后
    回落到下方成为常态（实测 w=200 时 100%、w=230 时 56%），表现为
    「涨跌幅时不时跑到下方去」。

    修法（本函数的核心不变量）：
      **先扣除涨跌幅预算，再自适应价格字号** —— 价格主动让位，右侧空间恒定
      充足，涨跌幅位置由数据唯一确定，不再依赖"零头"，从而不可能抖动。

    参数：
      width    : 窗口逻辑宽度
      measure  : measure(size) -> 价格字符串在该字号下的像素宽度
                 （由调用方注入字体度量，本模块不持有 QFont）
      pct_w    : 涨跌幅文本像素宽度
      pad_x    : 内容左边距（价格大字起始 x）
      right_pad: 价格行右侧安全边距。注意价格行（Y 56~84）与顶部按钮区
                 （Y 11~23）垂直不重叠，无需沿用 W_PRICE/W_PRICE 的按钮区预留
      badge_w  : 状态徽标宽度（0 表示无徽标）；v1.2.70 起仅用于计算
                 徽标在涨跌幅上方的居中 X 坐标，不消耗水平预算
    返回 dict：
      size      : 价格大字最终字号
      price_x / price_w : 价格大字起点 x 与该字号下的文本宽
      budget    : 价格字号自适应预算（仅扣除涨跌幅；徽标不占水平空间）
      badge_x   : 徽标左上角 x（左对齐涨跌幅区域；无徽标或放不下时 None）
      pct_x / pct_w / pct_avail : 涨跌幅起点 x、宽度、到右侧安全边的可用宽
      on_right  : 涨跌幅是否落在价格行右侧（恒 True；极端兜底夹紧后仍保持）
      right_limit : 价格行可绘制的右边界
    """
    right_limit = width - right_pad
    price_x = pad_x
    # 可用总宽：price_x -> right_limit
    avail = right_limit - price_x
    # v1.2.70：预算只扣涨跌幅（徽标在涨跌幅上方，不占水平空间）
    pct_reserve = pct_w + pct_gap
    budget = max(int(avail - pct_reserve), min_price_w)

    # 自适应字号：在预算内取最大可用字号（步长 step，下限 size_min）
    size = size_max
    price_w = measure(size)
    while size > size_min and price_w > budget:
        size -= step
        price_w = measure(size)
    size = max(size, size_min)
    price_w = measure(size)   # 以最终字号为准（与旧实现"钓回最小字号"语义一致）

    # v1.2.72 第6轮走查：若价格在最小字号仍放不进「价格+涨跌幅」预算，旧逻辑
    # 会把涨跌幅强行左夹到价格文字上方，两个数字叠在一起不可读。此时优先保住
    # 完整价格（核心信息），本帧明确隐藏右侧涨跌幅及其徽标；价格以整行宽度重新
    # 自适应。常规行情完全不走此分支，保持原有并排比例。
    show_pct = price_w <= budget
    if not show_pct:
        budget = max(int(avail), min_price_w)
        size = size_max
        price_w = measure(size)
        while size > size_min and price_w > budget:
            size -= step
            price_w = measure(size)
        size = max(size, size_min)
        price_w = measure(size)

    # 涨跌幅起点：紧贴价格文本右侧（徽标不再插在中间）
    pct_x = price_x + price_w + pct_gap if show_pct else None
    pct_avail = right_limit - pct_x if show_pct else 0

    # v1.2.70 样式调整：徽标左对齐涨跌幅（不居中），不消耗水平预算
    badge_x = None
    if badge_w and show_pct:
        badge_x = pct_x
        # 极端情况：徽标比涨跌幅区域还宽时丢弃
        if badge_x + badge_w > pct_x + pct_avail:
            badge_x = None

    return {
        "size": size,
        "price_x": price_x,
        "price_w": price_w,
        "budget": budget,
        "badge_x": badge_x,
        "pct_x": pct_x,
        "pct_w": pct_w,
        "pct_avail": pct_avail,
        "show_pct": show_pct,
        "on_right": show_pct and pct_w <= pct_avail,
        "right_limit": right_limit,
    }
