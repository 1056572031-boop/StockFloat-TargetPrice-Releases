# -*- coding: utf-8 -*-
"""模块化拆分 v1.2.13：sf_ui_render —— 由 stock_float_window.py FloatWindow 方法搬移生成。"""

import math
import os
import sys
import time
from collections import deque
from typing import cast
from PyQt6.QtWidgets import QWidget
from PyQt6.QtGui import (QColor, QFont, QFontDatabase, QFontInfo, QFontMetrics, QPainter,
                         QPainterPath, QPen, QBrush, QLinearGradient, QImage)
from PyQt6.QtCore import Qt, QRect, QRectF, QPoint, QPointF, QTimer
from sf_constants import (R, GLASS_INSET, LIQUID_GLASS_INSET, LIQUID_GLASS_RIM_RGB,
                         BG_ALPHA_DEFAULT, FG_DIM, UP_RED_HEX, DOWN_GREEN_HEX,
                         BTN_ICON_PT)
from renderer import liquid_glass_dim_path, _bottom_layout, price_pct_layout
from sf_utils import fmt_pct
from sf_quote import quote_status
from sf_logging import log_line, log_line_throttled, log_swallow, feature_enabled

# ---------- v1.2.24：内置数字字体随包分发（本版 换成 Datto 官方 D-DIN） ----------
# 打包后由 PyInstaller 放入 _MEIPASS/fonts/，源码运行取工程 fonts/ 目录。
# 许可：SIL OFL 1.1，权利人 Datto Inc. 自己在字体 name 表里声明（见 fonts/OFL-D-DIN.txt
# 与 NOTICE.md 的字体段）。旧的 "D-DIN-PRO" 两个 ttf 是第三方重打包件（版权行被换成
# 个人署名 + 无授权的 OFL 声明），已随本版本从分发物与仓库中移除。
BUNDLED_FONT_DIR = "fonts"
BUNDLED_FONT_FILES = ("D-DIN.otf", "D-DIN-Bold.otf")

# ---------- 数字定宽占位（走查 S8 修复） ----------
# D-DIN 的数字是**比例数字**：27pt 实测 `1`=13px、`7`=16px、`8`=19px，逐格极差 6px。
# 逐位滚动时每一位的左边界随位值变化，价格每跳一次，右侧涨跌幅与徽标就整体横移
# （实测最多 20px），这就是老板反复截到的"数字一跨、右边跟着滑"。
# 修法只有一条口径：**数字位一律占 max(advance('0'..'9')) 的格宽，字形在格内居中**，
# 量宽与绘制必须走同一函数（下面两个），否则又变成"布局量 A、绘制画 B"的老问题。
DIGIT_CHARS = "0123456789"


def digit_cell_width(fm):
    """数字单格占位宽 = 该字体该档位下最宽数字的 advance。"""
    return max(fm.horizontalAdvance(c) for c in DIGIT_CHARS)


def tabular_advance(fm, s, cell):
    """串宽（与逐字符绘制同一口径）：数字位占固定格宽，其余字符按自然 advance。

    非数字字符（小数点、正负号、百分号）不参与抖动 —— 它们在串里的位置不随位值变化，
    只有数字换形才会改变宽度，所以只需要把数字位钉平。
    """
    return sum(cell if c in DIGIT_CHARS else fm.horizontalAdvance(c) for c in s)


# v1.2.24：逐位滚动单次滚动时长（秒）。价格插值动画 130~200ms，
# 滚动需在动画归位前完成，0.18s 介于两者之间，滚完即静。
ROLL_DUR = 0.18

# ---------- v1.2.43 第2/4项：价格动画局部重绘 纯函数 ----------

PRICE_PARTIAL_PAD = 4   # 价格/涨跌幅局部重绘条带上下额外刷新的安全区（覆盖逐位滚动滑出字符）


def material_bell_path(rect):
    """把用户指定的 Material Notifications SVG 轮廓映射进现有图标框。

    原 SVG viewBox 为 0 -960 960 960；按正方形等比缩放并居中，因此仍使用
    BTN_ICON_SZ 的 12×12 绘制框，不改变按钮位置、视觉比例或点击热区。
    """
    # 0.84：与右侧搜索、最小化、关闭图标的视觉重量对齐；只缩小铃铛轮廓，
    # 不改按钮中心、热区、间距或其他图标。
    scale = min(rect.width(), rect.height()) / 960.0 * 0.84
    left = rect.center().x() - 480.0 * scale
    top = rect.center().y() - 480.0 * scale

    def pt(x, y):
        return QPointF(left + x * scale, top + (y + 960.0) * scale)

    path = QPainterPath()
    path.moveTo(pt(200, -200))
    path.quadTo(pt(183, -200), pt(171.5, -211.5))
    path.quadTo(pt(160, -223), pt(160, -240))
    path.quadTo(pt(160, -257), pt(171.5, -268.5))
    path.quadTo(pt(183, -280), pt(200, -280))
    path.lineTo(pt(240, -280))
    path.lineTo(pt(240, -560))
    path.quadTo(pt(240, -643), pt(290, -707.5))
    path.quadTo(pt(340, -772.5), pt(420, -792))
    path.lineTo(pt(420, -820))
    path.quadTo(pt(420, -845), pt(437.5, -862.5))
    path.quadTo(pt(455, -880), pt(480, -880))
    path.quadTo(pt(505, -880), pt(522.5, -862.5))
    path.quadTo(pt(540, -845), pt(540, -820))
    path.lineTo(pt(540, -792))
    path.quadTo(pt(620, -772), pt(670, -707.5))
    path.quadTo(pt(720, -643), pt(720, -560))
    path.lineTo(pt(720, -280))
    path.lineTo(pt(760, -280))
    path.quadTo(pt(777, -280), pt(788.5, -268.5))
    path.quadTo(pt(800, -257), pt(800, -240))
    path.quadTo(pt(800, -223), pt(788.5, -211.5))
    path.quadTo(pt(777, -200), pt(760, -200))
    path.lineTo(pt(200, -200))
    path.closeSubpath()

    path.moveTo(pt(480, -80))
    path.quadTo(pt(447, -80), pt(423.5, -103.5))
    path.quadTo(pt(400, -127), pt(400, -160))
    path.lineTo(pt(560, -160))
    path.quadTo(pt(560, -127), pt(536.5, -103.5))
    path.quadTo(pt(513, -80), pt(480, -80))
    path.closeSubpath()
    return path


def rainbow_gradient_axis(rect, phase):
    """彩虹渐变轴：色带固定由右下流向左上。"""
    # 轴长取 2 倍宽/高：彩虹带远长于文字，任一时刻只有一段压在字上，才有流动感；
    # 40/20 是下限 —— 空矩形（0 宽高）会让渐变轴退化成零长，彩虹变纯色。
    span = max(rect.width() * 2.0, 40.0)
    dy = max(rect.height() * 2.0, 20.0)
    x0 = rect.left() + span * phase
    y0 = rect.top() + dy * phase
    return x0, y0, x0 - span, y0 - dy


def _fmt_hl(s):
    """v1.2.48(S8)：最高/最低文本格式化归一 —— 解析为浮点后按原始小数位（1~4 位）
    重新格式化并去除尾 0，避免 "1234.5000" 这类多余尾 0 原样显示；解析异常回落 "--"。"""
    try:
        v = float(s)
        t = str(s).strip()
        frac = len(t.split(".", 1)[1]) if "." in t else 0
        if not (1 <= frac <= 4):
            frac = 2   # 小数位不在 1~4（整数串或超长精度）视为异常，回落到股价常用 2 位
        num = "%%.%df" % frac % v
        if "." in num:
            num = num.rstrip("0").rstrip(".")
        return num
    except Exception:
        return "--"


def _pct_color(v):
    """涨跌幅着色唯一实现：正红 / 负绿 / 零灰（A 股惯例，与涨同色）。"""
    return RED if v > 0 else (GREEN if v < 0 else FG_DIM)


def bottom_rows(quote):
    """底部双行文本的唯一实现（v1.2.71 抽为模块级纯函数，可单测）。

    v1.2.71 前：这段文本构造在 sf_ui_render._paint_bottom 与
    stock_float_window._bottom_texts **各写一份**（逐字复制）。属典型的双份漂移 ——
    一处是「画出来的文本」，另一处是「窗口最小宽度按之计算的文本」，改一处漏
    一处就会让两者不一致，表现为窗口宽度算窄、底部最后一段被静默裁掉。
    现收敛为唯一实现，两处共用。

    S3：行 1 为最大涨幅 + 最高点，行 2 为最大跌幅 + 最低点。
    幅度只相对昨收：涨幅取 max(0, (high-pre_close)/pre_close*100)，
    跌幅取 min(0, (low-pre_close)/pre_close*100)。昨收未知不回退今开。
    无涨/无跌显示灰色 0.00%；四标签始终保留，真实高低价格独立容错、不截断。

    返回 (row1, row2)，每项为 [(标签, 数值, 颜色)]；无行情时返回 None。
    """
    if not quote:
        return None

    def positive_price(key):
        try:
            value = float(quote.get(key))
            return value if math.isfinite(value) and value > 0 else None
        except (TypeError, ValueError, OverflowError):
            return None

    pre_close = positive_price("pre_close")

    def extreme_row(label, price_label, key):
        price = positive_price(key)
        pct = ((price - pre_close) / pre_close * 100
               if price is not None and pre_close is not None else None)
        if pct is not None and math.isfinite(pct):
            pct = max(0.0, pct) if key == "high" else min(0.0, pct)
            percentage = (label, fmt_pct(pct), _pct_color(pct))
        else:
            percentage = (label, "--", FG_DIM)
        price_text = _fmt_hl(quote.get(key)) if price is not None else "--"
        return [percentage, (price_label, price_text, FG_DIM)]

    return (extreme_row("最大涨幅", "最高点", "high"),
            extreme_row("最大跌幅", "最低点", "low"))


def price_update_rect(w):
    """价格滚动动画中间帧的局部重绘脏区 —— 价格大字 + 右侧涨跌幅同一水平行对应的全宽条带。

    返回 (x, y, rw, rh) 逻辑坐标矩形：
    - 条带取整窗宽，保证玻璃暗化背景与右侧状态徽标/涨跌幅/逐位滚动滑出字符都落在重绘范围；
    - v1.2.61：涨跌幅已移至股价右侧（同一水平带），脏区只需覆盖价格行（Y_PRICE~Y_PRICE+H_PRICE），
      不再需要延伸到原 Y_PCT 行；
    - 底部按需截断到分时图顶部（CHART_TOP），绝不侵入分时图区，避免动画期间
      图表顶部被条带背景层反复覆盖出现残余横带。
    """
    x = 0
    y = Y_PRICE - PRICE_PARTIAL_PAD
    bottom = min(Y_PRICE + H_PRICE + PRICE_PARTIAL_PAD, CHART_TOP)
    rw = max(int(w), 1)
    rh = max(bottom - y, 1)
    return x, y, rw, rh


def is_price_partial_redraw(damage, partial, tol=1):
    """判定本次绘制损坏区是否就是价格局部重绘脏区。

    - 损坏区 == 局部脏区（允许 tol px 容差，兼容 Retina 整/半像素取整）→ True，
      paintEvent 走局部重绘（只重画背景+价格，跳过头部/按钮/分时/底部整窗重绘）；
    - 任何更大/更小的损坏（整窗 update、悬停、闪烁、拉伸、布局动画等）→ False，
      按完整 paintEvent 全量绘制，防止悬挂的 _partial_price 吞掉整窗重绘致残留。
    """
    if damage is None or partial is None:
        return False
    dx, dy, dw, dh = (int(v) for v in damage)
    px, py, pw, ph = (int(v) for v in partial)
    return (abs(dx - px) <= tol and abs(dy - py) <= tol
            and abs(dw - pw) <= tol and abs(dh - ph) <= tol)


def valid_price_partial(partial):
    """v1.2.45：局部重绘脏区防御校验 —— partial 必须是长度 4 的数值序列 (x,y,w,h)。

    历史教训：paintEvent 局部重绘曾对 _partial_price 直接 partial[0..3] 下标构造
    QRect，脏数据（None 元素/短/超长/非数值序列）会令下标或构造异常从 Qt 事件循环
    逃逸 —— 曾因漏导 QRect 触发 NameError → 渲染 flush 空指针 SIGSEGV 整 app 闪退。
    此校验保证只有干净的四维有限数值脏区才允许进入局部重绘路径，其余一律回退整窗
    完整重绘（走 paintEvent 兜底分支），任何绘制路径异常都不再向 Qt 逃逸。
    """
    if not isinstance(partial, (tuple, list)):
        return False
    if len(partial) != 4:
        return False
    try:
        return all(
            isinstance(v, (int, float)) and math.isfinite(float(v))
            for v in partial)
    except (TypeError, ValueError):
        return False


def should_draw_chart(price_animating, chart_data_or_geometry_changed):
    """价格动画 tick 期间“图表是否需重画”判定。

    - 价格动画进行中 且 图表数据/几何均未变化 → False（该帧走价格局部重绘，
      分时图跳过本次重绘：图表区域不参与、不损坏、不重画，杜绝每 tick 整窗重画）；
    - 价格动画结束（完成帧整窗重绘） 或 行情数据/窗口几何已变化 → True（重画图表）。
    """
    if not price_animating:
        return True
    return bool(chart_data_or_geometry_changed)


def market_status_rect(w, lab_w, name_w):
    """v1.2.74：休市提示水平布局决策（纯函数，供单测）—— 返回绘制矩形 (rx, rw)。

    用户诉求改为「左边缘与股票名称左边缘(PAD_X)对齐」：仅水平左移，使提示文字的
    左边缘与股票名称左边缘一致；宽度取文字自身宽度 lab_w 不变。垂直位置由调用方
    （_paint_market_status 的 text_cy）决定，本函数不参与。
    w / name_w 仅保留签名以便回测兼容，本决策不再依赖二者。
    """
    return PAD_X, lab_w


def _bundled_font_path():
    """返回内置字体所在目录：优先 PyInstaller 解包目录（sys._MEIPASS），
    否则回退到工程 fonts/ 目录（源码运行）。两者都不存在返回 None。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        d = os.path.join(base, BUNDLED_FONT_DIR)
        if os.path.isdir(d):
            return d
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), BUNDLED_FONT_DIR)
    return d if os.path.isdir(d) else None


def load_bundled_fonts():
    """启动时调用：用 QFontDatabase 注册内置 D-DIN 字体（Regular/Bold）。

    返回已成功注册的字体族名列表；重复调用幂等（已注册族名不再重复注册）。
    任意一台 Mac 上只要 app 包带 fonts/ 资源，即可渲染出等宽数字字体，
    不依赖目标机是否已安装该字体。
    """
    fams = []
    d = _bundled_font_path()
    if not d:
        return fams
    for fname in BUNDLED_FONT_FILES:
        path = os.path.join(d, fname)
        if not os.path.isfile(path):
            continue
        try:
            fid = QFontDatabase.addApplicationFont(path)
            if fid >= 0:
                fams.extend(QFontDatabase.applicationFontFamilies(fid))
        except Exception:
            continue
    return fams


def warm_font_metrics():
    """v1.3.45：把「进程内首次字体度量」的一次性开销挪到窗口露出之前。

    实测：首次对内置字体 D-DIN 取 QFontMetrics 要 ~71 ms —— Qt 要为该字体
    建立整形/度量缓存，这是**每个进程一次**的固定成本，躲不掉，但可以选在哪里付。
    原先它落在**首帧绘制**里：窗口已经露出来了，内容却要晚约 70ms 才定稿，
    看起来就是"先空一下 / 先扁一下再填上"。

    放在 win.show() 之前付掉：总耗时不变，但用户看到的第一帧已经是排版正确的
    成品，不存在"先画一版、再重排一版"的中间态。
    """
    try:
        for family in (NUM_FONT, FONT):
            for size in (FONT_PRICE_MAX, FONT_PCT, FONT_CODE,
                         LABEL_FONT_PT, AVG_LABEL_FONT_PT):
                for weight in (QFont.Weight.Bold, QFont.Weight.Normal):
                    QFontMetrics(QFont(family, size, weight)).horizontalAdvance("0123456789")
    except Exception:
        pass


TOP_GAP = 22   # 2026-10：按确认稿收紧顶部留白，左侧内容整体上移 6pt，按钮区不动。
                # 价格区与图表同步上移，保持内容之间的原有相对间距。
NAME_TOP_PAD = 4   # 名称额外下移量（不改变其他元素位置，仅提升名称与顶部栏的垂直层次）
FONT_CODE_DESCENT = 3   # v1.2.7：PingFang SC 9pt descent 实测值
BASELINE = QColor("#ffffff")

CHART_TOP = 80 + TOP_GAP  # 分时图顶部 y：价格区底→走势图缩为 6px，随内容整体下移

CHART_MIN_H = 50     # 分时图最小高度

# v1.3.51：图带可用高度的下限闸门 —— 低于此高度整幅分时图（基准线/均价线/
# 渐变填充/折线/末端呼吸点/"加载中"提示）一律不画，收得干干净净。
# 背景：折叠态窗口高 155，图带 = 155 - CHART_TOP(108) - BOTTOM_SECTION_HIDDEN(46)
# = 1px，画出来是一条既不像"已收起"、也不像走势图的怪线；展开动画首几帧
# （_chart_expanded 立刻置 True、高度还停在 155 附近）同样会经过这个区间。
# 只改"画不画"，不改任何高度常量（折叠 155 / 展开最小 224 / CHART_MIN_H 均原值）。
CHART_MIN_DRAW_H = 8


# ---------- 交易时段边界（分钟数），分时横轴映射与时段判定的唯一来源 ----------
_T_OPEN_AM = 9 * 60 + 30     # 570  09:30 开盘
_T_CLOSE_AM = 11 * 60 + 30   # 690  11:30 上午收盘
_T_OPEN_PM = 13 * 60         # 780  13:00 下午开盘
_T_CLOSE_PM = 15 * 60        # 900  15:00 收盘
_T_TOTAL_MIN = 240           # 全天连续竞价总分钟数（120 + 120）


def _chart_minute_offset(hhmm, i, n):
    """分时横坐标唯一换算（v1.2.22 抽公共函数）：hhmm（如 "0931"）→ 交易分钟偏移 0~240。
    v1.2.2 修复按点数均分导致盘中折线拉满全宽；此前换算逻辑在 _chart_x 与
    _chart_pts_cache 各写一份，易改一处漏一处，现收敛为唯一实现。
    09:30~11:30 → 0~120；13:00~15:00 → 120~240；解析失败回退按序号均分。

    v1.2.71 第4轮走查修复：**午休时段被错误映射到图表左侧**。
    原实现只有二分（`t <= 11:30` 走上午段，否则走 `120 + (t - 13:00)`），
    于是 11:31~12:59 这 89 个午休分钟被算成 `120 + (t - 780)` 的负值区间，
    实测 1131→31、1200→60、1259→119 —— 折线会从上午收盘处**向左回折到图表
    左侧四分之一处**。只要数据源在午休时段带点（东财备源存在该形态），
    盘中走势就会出现一段逆向折返，视觉上像价格"倒流"。
    现改为四段夹紧：开盘前→0、上午→0~120、午休→夹紧 120、下午→120~240、收盘后→240。
    """
    try:
        # 第1轮走查修复：防御归一 —— 备用源（东财）时间串可能带冒号（"09:30"），
        # 直接 int 切片会招 ValueError 退化为按序号均分（v1.2.2 已修缺陷的复活）。
        hhmm = hhmm.replace(":", "")
        t = int(hhmm[:2]) * 60 + int(hhmm[2:4])
        if t <= _T_OPEN_AM:
            trade_min = 0                                    # 开盘前/集合竞价 → 夹紧 0
        elif t <= _T_CLOSE_AM:
            trade_min = t - _T_OPEN_AM                       # 09:30~11:30 → 0~120
        elif t < _T_OPEN_PM:
            trade_min = _T_CLOSE_AM - _T_OPEN_AM             # 午休 11:31~12:59 → 夹紧 120
        elif t <= _T_CLOSE_PM:
            trade_min = 120 + (t - _T_OPEN_PM)               # 13:00~15:00 → 120~240
        else:
            trade_min = _T_TOTAL_MIN                         # 收盘后 → 夹紧 240
        return max(0, min(trade_min, _T_TOTAL_MIN))
    except Exception:
        return i * 240.0 / max(n - 1, 1)


FG_NAME = QColor("#ffffff")

FG_CODE = QColor("#9aa2b8")

MARKET = QColor("#f5c542")

RED = QColor(UP_RED_HEX)          # v1.4.59：值来自 sf_constants.UP_RED_HEX（同屏只此一种红）

GREEN = QColor(DOWN_GREEN_HEX)    # 同上，值来自 sf_constants.DOWN_GREEN_HEX

FONT = "PingFang SC"

NUM_FONT = "D-DIN"

# ---------- v1.2.39：渲染魔法数字收拢（散落数值统一命名，视觉微调只改此处）----------
AVG_LABEL_FONT_PT = 9        # 均价标注字号（pt）
AVG_LABEL_DX = 4             # 均价标注相对均价线右端点的水平偏移
AVG_LABEL_RIGHT_M = 8        # 均价标注距图表右缘的最小留白
AVG_LABEL_HPAD = 3           # 均价标注水平内边距（宽 = 文本宽 + 2*HPAD）
AVG_LABEL_OUTSET = 1         # 均价标注底板外扩量
AVG_LABEL_RADIUS = 3         # 均价标注底板圆角
AVG_LABEL_BG_ALPHA = 160     # 均价标注底板黑色背景透明度（0-255）
CHART_FILL_ALPHA = 76        # 折线渐变填充最浓处 alpha（曲线附近，约 30% 不透明）
                             # v1.3.62 前为 40 且渐变锚图顶——填充只吃到渐变最淡的尾巴，
                             # 老板实测"颜色太淡"。现渐变锚曲线本身 + 浓度提到主流水平。
# v1.3.64：绿色（跌）单独加浓度，红色（涨）保持 76 不动。
# 同 alpha 下绿色在深底上发灰、观感比红淡，老板明确要求只加绿、红不变。
CHART_FILL_ALPHA_GREEN = 108  # 绿填充最浓处 alpha（约 42% 不透明）
CHART_LINE_WIDTH = 1.5       # 分时折线线宽：轻微调细，保持清晰度与走势存在感
CHART_DOT_GLOW_ALPHA = 55    # 当前点呼吸灯光晕 alpha 上限
CHART_DOT_GLOW_ALPHA_MIN = 15  # 当前点呼吸灯光晕 alpha 下限
CHART_DOT_GLOW_R = 6         # 当前点呼吸灯光晕半径上限
CHART_DOT_GLOW_R_MIN = 4.5   # 当前点呼吸灯光晕半径下限
CHART_DOT_BREATH_PERIOD = 2.0  # 呼吸灯完整周期（秒）
CHART_DOT_HL_ALPHA = 200     # 当前点白描边 alpha 上限
CHART_DOT_HL_ALPHA_MIN = 140 # 当前点白描边 alpha 下限
CHART_DOT_HL_WIDTH = 1       # 当前点白描边线宽
CHART_DOT_R = 3              # 当前点半径
LABEL_FONT_PT = 10           # 涨跌幅/高/低标注字号（pt）
LABEL_OUTSET_X = 3           # 标注底板水平外扩
LABEL_OUTSET_Y = 1           # 标注底板垂直外扩
LABEL_RADIUS = 4             # 标注底板圆角
# 液态玻璃交互只覆盖顶部操作区：不扩大点击范围、不影响行情数字与分时图。
HOVER_BG_ALPHA = 58          # 悬停受光底板 alpha
HOVER_STROKE_ALPHA = 108     # 悬停受光描边 alpha
HOVER_PRESSED_BG_ALPHA = 84
HOVER_PRESSED_STROKE_ALPHA = 132
HOVER_SIZE = 20              # 顶部按钮悬停底板边长
HOVER_RADIUS = 6             # 顶部按钮悬停底板圆角
GLASS_RIM_INSET = 1.0        # 主窗玻璃受光边内缩，避免外缘双描边
ICON_SEARCH_WIDTH = 2.8      # 自绘放大镜图标线宽（24×24 资源坐标）
ICON_CLOSE_WIDTH = 1.8       # 自绘叉号图标线宽
ICON_CLOSE_R = 5.3           # 自绘叉号图标半边长
ICON_MIN_WIDTH = 2.0         # 自绘减号图标线宽

BTN_X = {"edit": 68, "min": 46, "close": 24, "bell": 90}  # 关闭保持右侧位置；相邻中心间隔调整为 22px

BTN_CY = 17        # 按钮中心 y

PAD_X = 14         # 主界面内容左右统一留白；文字与数字样式不变

W_NAME = 2 * PAD_X  # 名称/代码位于按钮下方，不再为顶部按钮横向让位；仅保留左右对称边距

W_PRICE = 82       # 价格/涨跌幅右侧留白（避开右上角按钮区）—— 历史值，仅顶部按钮区语义

# v1.2.63：价格行的右侧安全边距。
# 原实现价格行沿用 W_PRICE(82) 作右边界，但价格行位于 Y 56~84、右上角三按钮位于
# Y 11~23，两者垂直不重叠 —— 价格行无需为按钮区让出 82px。该过度预留叠加
# "价格字号自适应把预算吃满"，使涨跌幅右侧可用空间恒 ≤ -(PAD_X+PCT_GAP)，
# 判定必然失败而回落到下方（v1.2.62 收窄窗口后 w=200 时回落率 100%）。
# 价格行右边界改为 PAD_X（与内容左边距对称），空间由 118px 增至 176px（w=200）。
PRICE_ROW_RIGHT_PAD = PAD_X

PCT_GAP = 6        # v1.2.63：价格大字与右侧涨跌幅之间的间距

Y_NAME = 7 + TOP_GAP

Y_CODE = 26 + TOP_GAP

Y_PRICE = 46 + TOP_GAP

Y_PCT = 76 + TOP_GAP

H_NAME = 20

H_CODE = 14

H_PRICE = 28
# 价格带矩形的高度："--" 与降级态徽标都按它垂直居中（v1.4.64，S49）。
# 此前这个 +10 只写在 drawText 的参数里，谁都不知道价格带其实比 H_PRICE 高一截。
PRICE_BAND_H = H_PRICE + 10

H_PCT = 18

FONT_NAME = 12

FONT_CODE = 9
BOTTOM_LABEL_PT = 8  # S3：只缩底部中文标签，代码行与默认数字仍为 9pt

FONT_PRICE_MAX = 27

FONT_PRICE_MIN = 14  # 价格字号的下限档。注意主窗宽度被 setFixedWidth 锁在 W=184px，
                     # 「160px 窄版」在当前代码里不可达（v1.4.60 复核 F4 改正这句陈旧注释）：
                     # 184px 下三位数价格取 25pt、四位整数价格取 19pt，兜底档只在直调布局
                     # 或未来改版窄窗时才会用到

FONT_PCT = 13

FONT_CHART_HINT = 10

# v1.4.7：历史分时滞后遮罩的透明度。保留常量名以兼容诊断/外部探针；
# 当前滞后状态改在顶部标签展示，分时曲线不再被遮罩压暗。
CHART_STALE_MASK_ALPHA = 120

STALE_LABEL_TEXT = "分时滞后·重试中"
STALE_LABEL_H = 16
STALE_LABEL_FONT = 7
STALE_LABEL_PAD_X = 5
STALE_LABEL_RADIUS = 5
STALE_LABEL_FG = QColor(230, 199, 148)
STALE_LABEL_BG = QColor(255, 193, 97, 18)
STALE_LABEL_BORDER = QColor(230, 199, 148, 55)


def stale_label_rect(w, text_width):
    """分时滞后标签右对齐内容边缘，位于名称行和价格行之间。"""
    width = min(w - 2 * PAD_X, text_width + 2 * STALE_LABEL_PAD_X)
    return QRectF(w - PAD_X - width, Y_NAME + NAME_TOP_PAD, width, STALE_LABEL_H)

# v1.4.34（用户要求："文本显示不能与白色虚线贴近或重叠"）：滞后提示**底板**外沿与
# 0% 基准白色虚线之间必须留出的最小净空（px）。判据按底板外沿算（底板又比文字矩形大），
# 所以视觉上比这个数更松。
CHART_HINT_MIN_GAP_PX = 6.0


# ---- 分时图区间的唯一实现（v1.4.59，D08）------------------------------------------
# 以前这段"撑开一字板 + 上下各留 10% 余量 + 价格→y"的公式在测试里被手抄了一份
# （tests/test_v1434_hint_gap.py::_baseline_y）。手抄的那份不会跟着产品改：
# 余量比例一改，用例算出来的虚线位置就是错的，而它照样绿 —— 一条会自我证明的断言。
# 现在产品自己走这两个函数，用例也走这两个函数。
CHART_SPAN_PAD_RATIO = 0.10


def chart_price_span(lo, hi):
    """把 [lo, hi] 撑成绘图区间：一字板先撑开 1 元，再上下各留 10% 余量。"""
    lo, hi = float(lo), float(hi)
    if hi - lo < 1e-6:
        hi = lo + 1.0
    pad = (hi - lo) * CHART_SPAN_PAD_RATIO
    return lo - pad, hi + pad


def chart_y_for_price(rect, lo, hi, price):
    """价格 → y 坐标（非含端点，夹在区间内；span 退化时按 1 处理，绝不除零）。"""
    span = float(hi) - float(lo)
    if span <= 0:
        span = 1.0
    v = min(max(float(price), float(lo)), float(hi))
    return rect.top() + (float(hi) - v) / span * rect.height()


# v1.2.47：右上角三按钮图标放大 2px（10→12，资源 20px 仍足够下采样）。
# 图标保持以各按钮中心（w-BTN_X[name], BTN_CY=17）居中；相邻中心间隔为 22px，
# 图标本体尺寸不变。
BTN_ICON_X = {"edit": 74, "min": 52, "close": 30, "bell": 96}  # 图标左上角 x = w - offset

BTN_ICON_Y = 11

BTN_ICON_SZ = BTN_ICON_PT    # 值来自 sf_constants（与图标出图倍率同源，见那里注释）

# 底部留白（固定值）：数据行下方收紧后的留白 —— baseline 距底 17，
# glyph 底 = baseline - FONT_CODE_DESCENT(3)，即高/低行数字底距窗口底约 14px。
BOTTOM_BASE_HIDDEN = 17  # 2026-10：底部两行距窗口底再收紧 3pt，保持行内布局不变。
# S3：双行数据布局（两行左对齐）：
#   行 1（上）：最大涨幅（相对昨收，下限零）+ 最高点
#   行 2（下）：最大跌幅（相对昨收，上限零）+ 最低点
# BOTTOM_ROW_H = 单行数据文字可视高（≈9pt 字号 glyph 顶到底 + 1px 视觉留白）；
# BOTTOM_ROW_GAP = 两行之间的垂直间距（v1.2.64：用户指定 2px，视觉净间距即此值）。
#   y_row1 = h - BOTTOM_BASE_HIDDEN - BOTTOM_ROW_H - BOTTOM_ROW_GAP
#   y_row2 = h - BOTTOM_BASE_HIDDEN
#   行1 字形底 = y_row1 + descent(3)；行2 字形顶 = y_row2 - ascent(10)
#   → 净间距 = BOTTOM_ROW_H - descent - ascent + BOTTOM_ROW_GAP
#            = 13 - 3 - 10 + BOTTOM_ROW_GAP = BOTTOM_ROW_GAP（即常量值本身）
# 总占用高度自此基础上扩 BOTTOM_ROW_H + BOTTOM_ROW_GAP。
BOTTOM_ROW_H = 13
BOTTOM_ROW_GAP = 2
# 底部数据行 9pt 字形的 ascent（PingFang SC 实测值）。
# 与同文件的 FONT_CODE_DESCENT(=3) 是一对，二者共同决定两行的视觉净间距。
# v1.2.64：全项目 ascent 唯一来源 —— 已删除 stock_float_window 里零引用的
# FONT_CODE_ASCENT（同值第二份定义，属双份漂移隐患）。
BOTTOM_ROW_ASCENT = 10
# 底部保留区：分时图下沿须止于行 1 字形顶部之上，避免图覆盖数据行。
# = 行2 baseline 距底 + 行高 + 行间距 + 行1 ascent，再留 1px 视觉余量。
# v1.2.64：改为由 BOTTOM_ROW_GAP 推导（此前硬编码 50，改间距时需同步手改，易漂移）。
BOTTOM_SECTION_EXTRA = 1
BOTTOM_SECTION_HIDDEN = (BOTTOM_BASE_HIDDEN + BOTTOM_ROW_H + BOTTOM_ROW_GAP
                         + BOTTOM_ROW_ASCENT + BOTTOM_SECTION_EXTRA)

# v1.2.65 第1轮走查修复：价格区底 → 底部行 1 字形顶的设计间距。
# 此前是散落在 stock_float_window._height_collapsed / _height_expanded 里的裸
# 字面量 8（两处逐字复制粘贴），无常量名、无推导来源。
PRICE_TO_BOTTOM_GAP = 8


def hint_line_gap_px(y, hint_h, line_y, pad=LABEL_OUTSET_Y):
    """提示**底板外沿**与 0% 基准白色虚线之间的净空（px）；虚线穿过底板记 0。

    为什么单独成一层（而不是只留在 `stale_hint_y` 里当内嵌函数）：诊断包要记录
    "这一帧实际留出多少净空"，如果绘制侧再抄一遍公式，就成了同一个物理量两份定义
    —— 那正是 AGENTS §5.2 里 collapsed_height 那条 7 vs 10 事故的形状。
    现在选位与留痕共用这一个实现。
    """
    if line_y is None:
        return float("inf")
    top, bottom = y - pad, y + hint_h + pad
    if line_y < top:
        return top - line_y
    if line_y > bottom:
        return line_y - bottom
    return 0.0


def stale_hint_y(rect_top, rect_bottom, hint_h, line_y,
                 min_gap=CHART_HINT_MIN_GAP_PX, pad=LABEL_OUTSET_Y):
    """历史图内滞后提示的选位纯函数，供测试与诊断使用。

    当前界面在名称行右侧绘制滞后标签，不调用本函数。以下规则保留为可复现的历史选位口径：
    默认贴顶（rect_top + 2）；只有顶部这一档的净空不足 `min_gap` 时才翻到图带底部；
    若底部更近（极矮图带、虚线落在中间偏下）则仍留顶部。取不到虚线（`line_y is None`）
    时一律贴顶。

    净空按**底板外沿**算：文字矩形再上下各外扩 `pad`（= 画底板用的 LABEL_OUTSET_Y），
    穿过底板记 0 —— 判"贴不贴近"要判用户真看见的那块深色底，而不是字身。
    """
    top = rect_top + 2
    bottom = max(top, rect_bottom - 2 - hint_h)
    if line_y is None:
        return top

    def gap(y):
        return hint_line_gap_px(y, hint_h, line_y, pad)

    if gap(top) >= min_gap:
        return top
    return bottom if gap(bottom) > gap(top) else top


def collapsed_height():
    """收起态窗口总高的唯一实现（v1.2.65 抽为纯函数，可单测）。

    构成（自顶向下，与 _paint_bottom 的行定位严格互逆）：
        价格区框底      Y_PRICE + H_PRICE
      + PRICE_TO_BOTTOM_GAP   价格区底 → 行1 字形顶（设计间距）
      + BOTTOM_ROW_ASCENT     行1 字形顶 → 行1 baseline
      + BOTTOM_ROW_H + BOTTOM_ROW_GAP   行1 baseline → 行2 baseline
      + BOTTOM_BASE_HIDDEN    行2 baseline → 窗口底

    历史缺陷：原公式用 COLLAPSED_ROW_H(=7) 充当「行1 字形顶→baseline」，但该常量
    是**纯数字**的视觉高度（cap height≈7），而行 1 实际内容是「最大涨幅 +1.23%」
    这类**含汉字**的文本，其字形顶由 BOTTOM_ROW_ASCENT(=10) 决定。两者相差 3px，
    导致高度少算 3px —— 实测价格行底 y=82、行1 顶 y=89，间隙仅 6px，短于设计 8px。
    同一个物理量却有两份定义（7 与 10），属典型的双份漂移，故统一收敛到 ascent。
    """
    return int((Y_PRICE + H_PRICE) + PRICE_TO_BOTTOM_GAP + BOTTOM_ROW_ASCENT
               + BOTTOM_ROW_H + BOTTOM_ROW_GAP + BOTTOM_BASE_HIDDEN)

BOTTOM_INNER_GAP = 3  # 行内段间距：标签与右侧数字（涨幅↔百分比）贴紧（v1.2.66 由 1 增到 3）

BOTTOM_GAP = 12    # S3：每行百分比与价格两段之间的间距

# 底部数字的缩号档位（v1.4.59 走查 S8 第三条，本机离屏真字体度量实测）
BOTTOM_NUM_SHRINK_STEPS = (0, -1, -2)

CHART_LINE_GRADIENT_LIGHTEN = 125   # 折线渐变起点相对趋势色的提亮档（Qt 的 100 = 不变）


def chart_line_stop_colors(color):
    """分时折线横向渐变的 (起点, 终点)。

    **终点必须就是趋势色本身** —— 这是这条函数存在的全部理由。
    v1.4.59 之前这里写死两组 Material 采样色（涨 #f44336→#b71c1c、跌 #a5d6a7→#2e7d32），
    绕过了 RED/GREEN token：于是折线的右端是深红 #b71c1c，而钉在同一个坐标上的
    呼吸点是 #ff5252，线尾和端点两种颜色，端点看着像贴错了色（走查 S8 倒数第二条）。
    现在起点=趋势色提亮、终点=趋势色，"浅→深"的读法不变，端点与呼吸点必然同色。
    """
    base = QColor(color)
    return base.lighter(CHART_LINE_GRADIENT_LIGHTEN), base


# 渲染降级的末态留痕（诊断导出用，paint_diagnostics() 读取）。
# 为什么放在模块而不是窗口实例：这几件事都发生在 mixin 绘制方法里，逐帧写实例
# 属性要动 __init__ 且随窗口重建丢失；模块级一份"最后一次的答案"对诊断刚好够用。
# 只在值变化时写（bottom_*）或命中罕见分支时累加（badge_*），热路径代价可忽略。
_PAINT_DIAG: dict = {
    "bottom_shrink": None,    # 底部数字缩到第几档（None = 还没画过；真值可为负，别用 -1 当哨兵）
    "bottom_dropped": None,   # 底部放不下被放弃的段数（S39：「最低点 1666.66」整段消失）
    "badge_degraded_drawn": 0,   # 降级态「停牌」徽标实际画出的次数（S49 死支路）
    "badge_degraded_blocked": 0, # 降级态徽标判定要画但放不下的次数（0 常态，非 0 要查）
    "stale_hint_frames": 0,        # 画过多少帧"数据暂缺 · 重试中"提示
    "stale_hint_tight_frames": 0,  # 其中净空低于 CHART_HINT_MIN_GAP_PX 的帧数（v1.4.34 的判据）
    "stale_hint_line_missing": 0,  # 虚线本身取不到的帧数（退回贴顶，另一种"保证不了"）
    "stale_hint_last_gap_px": None,  # 最后一帧的实际净空；None = 虚线取不到
}


# 「加载中...」出现过几次、每次多久（用户反馈“有时候显示加载中”之前，包里只能看到现在
# 有没有数据，回答不了“多久一次、一次多久”）。chart = 分时图区，header = 顶部名称栏。
# 只在状态翻转时取一次时钟：不显示且没有进行中的段时，每帧只多两次字典读取。
_LOADING: dict = {k: {"open_since": None, "frames": 0, "episodes": 0, "longest_sec": 0.0}
                  for k in ("chart", "header")}
_LOADING_RECENT: deque = deque(maxlen=8)


def _loading_note(kind, showing):
    """记一帧：这一帧画的是不是「加载中...」。翻回非加载时把这一段收成一次记录。"""
    st = _LOADING[kind]
    if showing:
        st["frames"] += 1
        if st["open_since"] is None:
            st["open_since"] = time.monotonic()
        return
    since = st["open_since"]
    if since is None:
        return
    st["open_since"] = None
    dur = max(0.0, time.monotonic() - since)
    st["episodes"] += 1
    if dur > st["longest_sec"]:
        st["longest_sec"] = dur
    _LOADING_RECENT.append({"kind": kind, "start_monotonic": since, "duration_sec": dur})


def paint_diagnostics():
    """诊断导出用：渲染降级的末态与计数（见 _PAINT_DIAG 各键注释）。

    `loading` 里的 open_sec 是「最近一次画出加载中之后，到现在还没画出过别的」的时长；
    窗口被藏起或最小化时不再重绘，它会高估，读的时候要结合 start_monotonic 与窗口状态。
    start_monotonic 与 performance.window 里的 *_monotonic 同一个时钟。
    """
    out = dict(_PAINT_DIAG)
    now = time.monotonic()
    loading: dict = {}
    for kind, st in _LOADING.items():
        since = st["open_since"]
        loading[kind] = {"frames": st["frames"], "episodes": st["episodes"],
                         "longest_closed_sec": st["longest_sec"],
                         "open_since_monotonic": since,
                         "open_sec": (max(0.0, now - since) if since is not None else None)}
    loading["recent"] = list(_LOADING_RECENT)
    out["loading"] = loading
    return out


def font_diagnostics():
    """诊断导出用（S35/1.4.58 那一族的现场）：内置数字字体真注册到了没有。

    `bundled fonts loaded` 过去只在成功时写一行 log_line，失败一行都没有 ——
    "数字又跳了/宽度不对了"需要区分"字体没进包"与"系统替换了字体"，
    resolved 给的是 QFontInfo 此刻真解析到的族名，不是我们请求的族名。
    """
    try:
        fams = list(load_bundled_fonts())
    except Exception as exc:
        return {"bundled": [], "resolved": "", "error": repr(exc)}
    resolved = ""
    try:
        fi = QFontInfo(QFont(NUM_FONT, 12))
        resolved = fi.family()
    except Exception:
        resolved = ""
    return {"bundled": fams, "resolved": resolved}


def bottom_rows_fitting(width, row1, row2, fm_label, fm_num_at, pad_x, inner_gap, gap,
                        slack=8, base=0, steps=BOTTOM_NUM_SHRINK_STEPS):
    """底部双行布局：放不下就逐级缩**数字**字号，返回 (行1段, 行2段, 采用的档位, min_width)。

    要修的现场：窗口宽被用户明确定死为 184（sf_constants.W = 原 200px 的 92%，
    `_content_min_width` 恒返回 W，注释还写了"不再因个别长文本反向撑宽"），而
    renderer._bottom_layout 对放不下的段判 fits=False，绘制侧是 `continue` ——
    于是默认股茅台（当日高 1688.88 / 低 1666.66，两段同为 7 字符）时
    **「最低点 1666.66」整段凭空消失**：没有省略号、没有提示，用户看到的就是
    "少了一个数"。本机实测同一份文本所需宽度：
      9pt（现字号）→ 需要 193px > 184  → 丢段
      8pt          → 需要 187px（含 8px 余量口径，逐段已放得下）→ 不丢
    撑宽与用户那条决定冲突，缩号才与"宽度固定"这个前提一致。下限取两档：
    再小就低到 7pt，与状态徽标 BADGE_FONT_SIZE 同量级，层级就读不出来了。
    两档都不够时如实返回最后一档（仍可能丢段）—— 宁可少一段，也不把字画到窗口外。

    `fm_num_at(delta)` 由调用方提供：数字字号随档位变化，标签字号不动
    （标签是固定 2~3 字的"最高点/最低点"，缩它只会在已经很难读的一行里再抖一次）。
    """
    last = None
    for delta in steps:
        segs1, segs2, min_w = _bottom_layout(
            width, row1, row2, fm_label, fm_num_at(delta),
            pad_x=pad_x, inner_gap=inner_gap, gap=gap, slack=slack, base=base)
        last = (segs1, segs2, delta, min_w)
        if all(s["fits"] for s in segs1) and all(s["fits"] for s in segs2):
            return last
    return last



STATUS_BADGE_H = 12   # v1.2.70：从 16 缩小

STATUS_BADGE_R = 3   # v1.2.70：圆角半径随高度缩小（原 4）

BADGE_ABOVE_PCT_GAP = 2  # v1.2.70：徽标底边与涨跌幅顶边的间距

# v1.2.71：徽标顶边的**下限** —— 不得低于代码行矩形底再留 1px。
# 背景：徽标锚定在涨跌幅上方，而涨跌幅基线随价格字号浮动。价格字号降到 21pt
# 以下时（窄窗/超长价格触发自适应降级），徽标顶会顶进代码行字形区
# （实测 size=16 时徽标顶 48.5、代码行字形底 51.5，重叠 3px）。
# 该值由代码行的 y 与高度推导，不写死数值（避免改代码行位置后漂移）。
BADGE_MIN_TOP_GAP = 1
BADGE_MIN_TOP = Y_CODE + H_CODE + BADGE_MIN_TOP_GAP


def badge_width(badge, fm):
    """状态徽标宽度的唯一实现（v1.2.71）：文字像素宽 + 左右内边距。

    背景：v1.2.70 起徽标改为宽度自适应，但**布局阶段**仍按常量
    STATUS_BADGE_W(=34) 判定"放得下吗"，**绘制阶段**才按文字算实际宽度
    （实测"涨停/跌停/停牌"均 26px）—— 同一物理量两份定义，判定比实际宽 8px，
    剩余空间 27~33px 时徽标被无谓丢弃。现由此函数统一，STATUS_BADGE_W 已删除。
    """
    return fm.horizontalAdvance(badge) + 2 * BADGE_PAD_X


def badge_vertical(pct_rect_top, badge_h=STATUS_BADGE_H, gap=BADGE_ABOVE_PCT_GAP,
                   min_top=BADGE_MIN_TOP):
    """状态徽标垂直位置的唯一实现（v1.2.71 抽为纯函数，可单测）。

    徽标置于涨跌幅正上方、间隔 gap。向上顶到代码行时夹到 min_top；
    夹紧后若与涨跌幅顶边相撞（该字号下垂直空间不足以同时容纳徽标与间隔），
    返回 None 表示本次不绘制徽标 —— 与「极端窄窗丢弃徽标」同一降级策略，
    保证价格与涨跌幅这两个主信息永不被徽标挤压。
    """
    y = pct_rect_top - badge_h - gap
    if y >= min_top:
        return y
    y = min_top
    if y + badge_h + gap > pct_rect_top:
        return None
    return y

def badge_in_band_top(band_top, band_h, badge_h=STATUS_BADGE_H):
    """降级态（价格显示 "--"）里徽标的顶边：在价格带内垂直居中。

    为什么不复用 badge_vertical（台账 S49，v1.3.42 起这条支路一次都没画出来）：
    那个函数是为"涨跌幅正上方"设计的，它要求 `pct_rect_top - 徽标高 - 间距`
    不低于代码行底；而降态**根本没有涨跌幅行**，把价格带顶当涨跌幅顶传进去，
    结果永远是"夹到下限后再与传入值相撞"→ 返回 None → 停牌股取不到价时
    与"断网/加载中"在界面上长得一样 —— 而这正是 v1.2.30 立这条要区分的两件事。

    价格带（Y_PRICE 起、高 PRICE_BAND_H）在降级态是空的：右边整块涨跌幅区域
    没有内容，上下又都不与代码行底（68）和涨跌幅行顶（104）相邻，
    所以徽标放这里既不需要压别的元素，也不用改任何既有常量。
    """
    if band_h < badge_h:
        return None
    return band_top + (band_h - badge_h) // 2

BADGE_FONT_SIZE = 7   # v1.2.70：标签内文字字号（比 FONT_CODE=9 更小）
BADGE_FONT_WEIGHT = "Light"  # v1.2.70：标签字重（细体，非 Bold）
BADGE_PAD_X = 4   # v1.2.70：标签水平内边距（左右各 4px，总宽 = 文字宽 + 2*PAD）

FLASH_PRICE = QColor("#ffffff")

FLASH_DROP_COLOR = FLASH_PRICE  # v1.2.58：涨跌幅提醒统一用白色，不与涨跌颜色混淆

class RenderMixin:
        """主窗绘制一族的宿主 mixin：paintEvent 主体、价格/涨跌幅排版、底部两行、徽标、分时条带。

        FloatWindow 组合本 mixin；两个编辑弹窗经 `sf_glass.paint_popup_surface`
        直接共用 `_paint_background`，其余行情内容绘制仍只属于主窗。
        和 GlassMixin 同样的契约：本类不带任何 Qt 构造，成员全挂在宿主实例上（`self.width()`、
        `self._disp_price`、`self.minute_points` 等），所以它**不能被单独实例化**，
        离屏像素核验也只能通过真 FloatWindow 或按 AGENTS §3.1 的方式直调 `_paint_bottom`。
        字体必须先过 `load_bundled_fonts()` 再谈度量 —— 否则度量断言跑在兜底字体上，
        本机看着对、云端 1x/2x 屏一换字形就漂（AGENTS §6 第 12 条配套口径）。

        ⚠ 类体缩进是 8 空格（历史如此），往里加类属性必须跟齐，否则整类语法错。
        """
        # 首帧锚点标志（v1.4.33）。必须**声明在这里**而不是只在 paintEvent 里
        # 赋值：本类的类体缩进是 8 空格（历史如此），插入属性必须跟齐，否则整类语法错。
        # 静态层面：mixin 的属性由宿主窗口提供，不声明就会被算进 attr-defined 债务；
        # 这条属性是能被子类实例共享、静态可查的那种，值得声明。
        _sf_first_frame_logged = False

        # v1.3.51：均价线 QPainterPath 缓存（key, path, 末点下标, 末点均价）。
        # 类级默认 None —— 真正的窗口在 __init__ 里也会置 None，这里兜住未走
        # 初始化的裸对象/旧实例；方法内另有 getattr 防御，双重保险。
        _avg_path_cache = None
        _chart_fill_cache: tuple | None = None
        _bg_rim_cache: tuple | None = None
        _bottom_layout_cache: tuple | None = None
        _line_path_key: tuple | None = None

        def paintEvent(self, e):
            # 绘制异常不得逃逸到 Qt flush 链；计时仅用于有界诊断，不改变绘制节拍。
            monitor = getattr(self, "_performance_monitor", None)
            paint_started = time.monotonic() if monitor is not None else 0.0
            try:
                self._paint_event_inner(e)
                # v1.4.33：首帧锚点（一次）。没有它，"进程活着但屏幕上什么都没有"
                # 与"根本没起来"在日志里长得一模一样；产物冒烟关卡就断言这一行。
                if not self._sf_first_frame_logged:
                    self._sf_first_frame_logged = True
                    log_line("first-frame painted %dx%d"
                             % (self.width(), self.height()))
                    session = vars(self).get("_runtime_session")
                    session_id = vars(self).get("_runtime_session_id")
                    if session is not None and isinstance(session_id, str):
                        try:
                            session.mark_frame_ready(session_id)
                        except Exception as exc:
                            log_swallow("sf_ui_render.first_frame_session", exc)
            except Exception:
                # v1.2.95 第3轮走查：绘制路径在动画/拖动时可达 60Hz，此处
                # 无条件日志会把日志文件刷满。改为首条 + 每 60 条（约 1 秒
                # 一条）—— 既保留故障可见性，又不挤掉别的日志。
                try:
                    log_line_throttled("paintEvent",
                                       "paintEvent error: %r" % (sys.exc_info()[1],),
                                       level="ERROR", every=60)
                except Exception:
                    pass
                # 兜底：清悬挂局部脏区，并在下一事件循环强制整窗重绘。
                # 过去这里直接返回：透明窗口会保留原生玻璃外壳，却没有任何 Qt 正文，
                # 截图上就是“可操作的空壳”。必须把恢复帧排到异常栈退出之后，避免在
                # 正在处理的 paintEvent 中递归重绘。
                try:
                    self._partial_price = None
                    if not getattr(self, "_paint_recovery_pending", False):
                        self._paint_recovery_pending = True
                        QTimer.singleShot(0, self._recover_after_paint_error)
                except Exception:
                    pass
            finally:
                if monitor is not None:
                    try:
                        monitor.record_paint((time.monotonic() - paint_started) * 1000.0)
                    except Exception as exc:
                        log_swallow("sf_ui_render.performance_record", exc)

        def _recover_after_paint_error(self):
            """绘制异常后的下一帧恢复：不让临时绘制故障变成长期空白窗口。"""
            self._paint_recovery_pending = False
            self._partial_price = None
            self._chart_changed = True
            self.update()

        def _paint_event_inner(self, e):
            # 玻璃圆角刷新属于附加效果，失败只能降级玻璃，不得阻断 Qt 正文绘制。
            try:
                self._glass_mask_flush()
            except Exception as exc:
                try:
                    self._bg_effect = False
                    self._glass_mask_pending = False
                    log_line("glass mask flush error, fallback translucent: %r" % (exc,), "WARNING")
                except Exception:
                    pass
            w = self.width()
            # v1.2.43 第2/4项：价格动画局部重绘路由 —— 当本次损坏区恰好等于
            # _partial_price（价格/涨跌幅条带）时，只重绘该区域（背景+价格），
            # 头部/按钮/分时/底部完全跳过，分时图区域零参与；
            # 任何更大/更小的损坏（整窗 update/悬停/闪烁/拉伸/布局动画）都走
            # 下方完整绘制，防止悬挂的局部脏区吞掉整窗重绘导致残留/闪烁。
            partial = getattr(self, "_partial_price", None)
            er = e.rect()
            # v1.2.45：命中局部重绘前先做 partial 类型/长度/数值防御校验，
            # 脏数据一律退回完整重绘，防止下标/QRect 构造异常逃逸（曾致整 app 崩溃）。
            transition = getattr(self, "_stock_transition", None)
            transitioning = transition is not None and transition.active
            if not transitioning and partial is not None and valid_price_partial(partial) \
                    and is_price_partial_redraw(
                    (er.x(), er.y(), er.width(), er.height()), partial):
                self._partial_price = None
                # 局部绘制：以条带为裁剪区重建背景+价格。裁剪到脏区防止把
                # 整窗暗化叠加在未被系统清除的头部/分时/底部像素上（累积变暗）。
                p = QPainter(self)
                try:
                    p.setRenderHint(QPainter.RenderHint.Antialiasing)
                    p.setClipRect(QRect(partial[0], partial[1], partial[2], partial[3]))
                    self._paint_background(p, w)
                    self._paint_price(p, w)
                finally:
                    # 任一绘制子步骤抛错也必须释放 painter；否则 Qt 会带着 active
                    # painter 结束 QBackingStore 帧，原生层可能在退出/下一帧崩溃。
                    p.end()
                return

            self._partial_price = None
            p = QPainter(self)
            try:
                p.setRenderHint(QPainter.RenderHint.Antialiasing)

                # Keep native material and controls sharp while stock content changes.
                self._paint_background(p, w)
                if transitioning and transition is not None:
                    p.save()
                    try:
                        p.setClipPath(liquid_glass_dim_path(w, cast(QWidget, self).height(), R, 0))
                        widget = cast(QWidget, self)
                        content_key = (widget.width(), widget.height(), widget.devicePixelRatioF(),
                                       tuple(getattr(self, field, None) for field in (
                                           "code", "_line_ver", "_disp_price", "_disp_pct",
                                           "_pct_unknown", "_minute_stale", "_last_ok_time", "_net_ok")),
                                       id(getattr(self, "quote", None)))
                        p.drawImage(0, 0, transition.frame_from(self._stock_content_image, content_key))
                    except Exception as exc:
                        log_swallow("sf_ui_render.stock_transition", exc)
                        transition.stop()
                        self._paint_stock_content(p, w)
                    finally:
                        p.restore()
                else:
                    self._paint_stock_content(p, w)
                self._paint_buttons(p, w)
                self._chart_changed = False
                # 目标价提示由 stock_float_window 的系统气泡承载，不再绘制在主卡片内部。
            finally:
                # paintEvent 的外层虽会吞异常，但不能依赖它来结束 Qt 原生 painter。
                p.end()

        def _paint_stock_content(self, painter, width):
            self._paint_header(painter, width)
            self._paint_stale_label(painter, width)
            self._paint_price(painter, width)
            self._paint_chart(painter, width)
            self._paint_bottom(painter, width)

        def _stock_content_image(self):
            widget = cast(QWidget, self)
            scale = widget.devicePixelRatioF()
            image = QImage(round(widget.width() * scale), round(widget.height() * scale),
                           QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(scale)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                self._paint_stock_content(painter, widget.width())
            finally:
                painter.end()
            return image

        def _paint_background(self, p, w):
            """主窗、两代玻璃和编辑弹窗共用同心普通圆角。"""
            h = self.height()
            if self._bg_path is None or self._bg_path_wh != (w, h):
                self._bg_path = liquid_glass_dim_path(w, h, R, 0)
                self._bg_path_wh = (w, h)
            path = self._bg_path
            if self._bg_effect:
                liquid = getattr(self, "_glass_backend", "legacy") == "liquid"
                key = (w, h, "liquid") if liquid else (w, h)
                # 各材质只保留不同的内缩量，曲线与外轮廓保持同心。
                if self._bg_path_ins is None or self._bg_path_ins_wh != key:
                    if liquid:
                        self._bg_path_ins = liquid_glass_dim_path(w, h, R, LIQUID_GLASS_INSET)
                    else:
                        ins = GLASS_INSET
                        self._bg_path_ins = liquid_glass_dim_path(w, h, R, ins)
                    self._bg_path_ins_wh = key
                p.fillPath(self._bg_path_ins, QColor(0, 0, 0, self._glass_dim()))
                if liquid:
                    # One concentric ring joins all four sides and corners. An
                    # opaque neutral color replaces the uneven native highlight.
                    rim_key = (w, h, LIQUID_GLASS_INSET, R)
                    rim_cache = getattr(self, "_bg_rim_cache", None)
                    if rim_cache is None or rim_cache[0] != rim_key:
                        rim = QPainterPath(path)
                        rim.addPath(self._bg_path_ins)
                        rim.setFillRule(Qt.FillRule.OddEvenFill)
                        rim_cache = (rim_key, rim)
                        self._bg_rim_cache = rim_cache
                    p.fillPath(rim_cache[1], QColor(*LIQUID_GLASS_RIM_RGB))
                    return
                # 玻璃材质的受光只留在内侧 1px，随系统深浅外观实时变化；背景的
                # 暖冷色仍由原生 NSVisualEffectView 负责，避免人为染色干扰行情可读性。
                rim_key = (w, h, GLASS_RIM_INSET, R)
                if self._bg_rim_cache is None or self._bg_rim_cache[0] != rim_key:
                    rim = liquid_glass_dim_path(w, h, R, GLASS_RIM_INSET)
                    self._bg_rim_cache = (rim_key, rim)
                rim = self._bg_rim_cache[1]
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(*self._glass_surface_rim()), 1.0))
                p.drawPath(rim)
            elif self._bg_mode == "solid":
                p.fillPath(path, QColor("#000000"))
            else:
                p.fillPath(path, QColor(0, 0, 0, int(BG_ALPHA_DEFAULT * 255)))

        def _paint_header(self, p, w):
            """20.1 v1.2.27：顶部名称 + 代码行（含断网"网络异常"提示）。
            返回 elided 名称，供休市提示复用（行为与拆分前一致）。"""
            no_quote = False
            if self.quote:
                name = self.quote["name"]
                code = self.quote["code"]
            else:
                name, code = "加载中...", ""
                no_quote = True
            # 网络连续失败时名称栏提示"网络异常"（价格显示最后成功值）
            net_bad = not self._net_ok
            _loading_note("header", no_quote and not net_bad)
            if net_bad:
                name = "网络异常"
                # 2.0 v1.2.0：断网时附加最近一次成功行情更新时间（放不下则省略时间）
                if self._last_ok_time:
                    cand = "网络异常 %s" % self._last_ok_time
                    if self._fm(self._font(FONT, FONT_NAME, QFont.Weight.Bold)).horizontalAdvance(cand) <= w - W_NAME - 4:
                        name = cand
                    else:
                        name = "网络异常"
            f_name = self._font(FONT, FONT_NAME, QFont.Weight.Bold)
            p.setFont(f_name)
            p.setPen(FG_DIM if net_bad else FG_NAME)
            fm = self._fm(f_name)
            name_w = w - W_NAME
            if getattr(self, "_minute_stale", False):
                label_fm = self._fm(self._font(FONT, STALE_LABEL_FONT, QFont.Weight.Medium))
                label = stale_label_rect(w, label_fm.horizontalAdvance(STALE_LABEL_TEXT))
                name_w = max(0, int(min(name_w, label.left() - PAD_X - 4)))
            elided = fm.elidedText(name, Qt.TextElideMode.ElideRight, name_w)
            # v1.2.66：名称绘制矩形整体下移 NAME_TOP_PAD、高度同步减 NAME_TOP_PAD，
            # 视觉中心下移但矩形底仍落于原 Y_NAME+H_NAME，不侵入代码区（其余元素不动）。
            p.drawText(QRectF(PAD_X, Y_NAME + NAME_TOP_PAD, name_w, H_NAME - NAME_TOP_PAD),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)
            p.setFont(self._font(FONT, FONT_CODE, QFont.Weight.Bold))
            p.setPen(FG_CODE)
            p.drawText(QRectF(PAD_X, Y_CODE, w - W_NAME, H_CODE), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, code)
            return elided

        def _paint_stale_label(self, p, w):
            """把分时滞后状态移到名称/代码右侧，以紧凑标签显示。"""
            if not getattr(self, "_minute_stale", False):
                return
            text = STALE_LABEL_TEXT
            font = self._font(FONT, STALE_LABEL_FONT, QFont.Weight.Medium)
            fm = self._fm(font)
            label = stale_label_rect(w, fm.horizontalAdvance(text))
            if label.width() < fm.horizontalAdvance(text) + 2 * STALE_LABEL_PAD_X:
                text = fm.elidedText(text, Qt.TextElideMode.ElideRight,
                                     int(label.width() - 2 * STALE_LABEL_PAD_X))
            p.save()
            try:
                p.setPen(QPen(STALE_LABEL_BORDER, 0.5))
                p.setBrush(STALE_LABEL_BG)
                p.drawRoundedRect(label.adjusted(0.5, 0.5, -0.5, -0.5),
                                  STALE_LABEL_RADIUS, STALE_LABEL_RADIUS)
                p.setFont(font)
                p.setPen(STALE_LABEL_FG)
                p.drawText(label, Qt.AlignmentFlag.AlignCenter, text)
            finally:
                p.restore()

        def _paint_buttons(self, p, w):
            """20.1 v1.2.27：右上角编辑 + 最小化到 Dock + 关闭按钮（SF Symbols，加粗 10px）。
            v6：额外绘制「目标价」铃铛（常显三态）。
            v1.2.87：互斥置灰 —— 替换股票编辑器开启时铃铛置灰且不可点；
            目标价面板开启时编辑图标置灰且不可点（由 _is_btn_disabled 判定）。"""
            try:
                edit_disabled = self._is_btn_disabled("edit")
                bell_disabled = self._is_btn_disabled("bell")
            except Exception as exc:
                # 保持 fail-open（按可用绘制），但必须记日志 —— 原本静默吞掉，
                # 互斥一旦失效完全没有线索可查。
                log_line("btn disabled paint check error: %r" % (exc,), "WARNING")
                edit_disabled = bell_disabled = False
            hover = self._hover_btn
            pressed = getattr(self, "_press_btn", None)
            active = pressed or hover
            # 悬停或按下均给出底板反馈；按下态更明显，让用户立刻知道动作已被接收。
            if active and active in BTN_X and not (
                (active == "edit" and edit_disabled) or (active == "bell" and bell_disabled)):
                self._draw_icon_hover(p, w - BTN_X[active], BTN_CY,
                                      pressed=(active == pressed))
            self._draw_edit_icon(p, QRectF(w - BTN_ICON_X["edit"], BTN_ICON_Y, BTN_ICON_SZ, BTN_ICON_SZ),
                                 disabled=edit_disabled)
            self._draw_min_icon(p, QRectF(w - BTN_ICON_X["min"], BTN_ICON_Y, BTN_ICON_SZ, BTN_ICON_SZ))
            self._draw_close_icon(p, QRectF(w - BTN_ICON_X["close"], BTN_ICON_Y, BTN_ICON_SZ, BTN_ICON_SZ))
            # v6：「目标价」铃铛（功能开启时绘制；无目标灰、待触发常亮、已提醒暗）
            if feature_enabled("targets"):
                try:
                    self._draw_bell_icon(p, QRectF(w - BTN_ICON_X["bell"], BTN_ICON_Y, BTN_ICON_SZ, BTN_ICON_SZ),
                                         self._bell_state(), disabled=bell_disabled)
                except Exception:
                    # 铃铛只是附加入口：单个图标画失败就静默降级，不牵连整帧绘制
                    # （外层 paintEvent 已统一兜底记日志）。
                    pass
                # v1.2.83：主窗「目标价」标签（铃铛左侧）按用户要求隐藏
                # （铃铛已作为入口，标签为冗余信息展示）。不再绘制。
                pass

        def _paint_target_label(self, p, w):
            """v1.2.83 起不再绘制：按用户要求隐藏主窗「目标价」标签。

            原 P0-1/P0-2/P1-1 在铃铛左侧绘制单行标签（设目标/已触发 ↑X/↑X +Y%），
            点击打开面板。v1.2.83 起该信息展示隐藏，入口由铃铛承担，故此处为空实现，
            仅保留方法签名以避免调用方改动；不再设置 _target_label_rect（无残留热区）。
            """
            return

        def _paint_price(self, p, w):
            """20.1 v1.2.27：价格大字 + 涨跌幅（逐位滚动）+ 状态徽标；无数据/0 值显示 --。"""
            if (self.quote and self._disp_price is not None and self._disp_pct is not None
                    and self._disp_price > 0):
                # 12.x v1.2.11：下跌闪烁优先级最高，其次破位白闪，最后回到涨跌趋势色
                if self._flash_drop_on:
                    color = FLASH_DROP_COLOR
                elif self._price_flash_on:
                    color = FLASH_PRICE
                else:
                    color = self._trend_color()
                price = ("%%.%df" % self._anim_decimals) % self._disp_price
                # 本版 走查 S8：布局量的是"这一帧真正会被画出来的串"，不是插值中间串。
                price_drawn = self._roll_drawn_string("price", price)
                # v1.3.49：涨跌幅未知（主源缺 pct 字段且昨收也不可用、无法自算）时
                # 显示 "--" —— 兜底值 0.0 只是为了让动画算术不崩，不是真实涨跌幅，
                # 不能渲染成 "0.00%"。用 getattr 取值：部分单元测试会用未初始化
                # 的 FloatWindow 调渲染，属性可能不存在。
                _pct_unknown = getattr(self, "_pct_unknown", False)
                pct = "--" if _pct_unknown else fmt_pct(self._disp_pct)
                # 1.1 v1.2.0：价格区右上角状态徽标（涨停/跌停/停牌），无状态时价格不缩让。
                # 目标价提示已移到铃铛系统气泡，不再占用卡片内状态徽标槽位。
                badge = quote_status(self.quote)
                # v1.2.63：价格行布局走 renderer.price_pct_layout 唯一实现。
                # 关键不变量 —— **先扣除涨跌幅预算，再自适应价格字号**：价格主动为
                # 涨跌幅让位，右侧空间恒定充足，涨跌幅位置由数据唯一确定，
                # 不再由"剩余零头"决定（旧实现零头恒 ≤ -(PAD_X+PCT_GAP)，
                # 判定几乎必然失败 → 涨跌幅回落到 Y_PCT 下方，即本 BUG）。
                pct_font = self._font(NUM_FONT, FONT_PCT, QFont.Weight.Bold)
                pct_fm = self._fm(pct_font)
                # 本版 走查 S8：量宽与 _draw_roll_text 同口径 —— 数字占统一格宽，
                # 且按绘制真正会用的串量（滚动预置期间是目标串，不是中间串）。
                pct_drawn = self._roll_drawn_string("pct", pct)
                pct_w = tabular_advance(pct_fm, pct_drawn, self._digit_cell(pct_font))

                def _measure(sz):
                    f = self._font(NUM_FONT, sz, QFont.Weight.Bold)
                    return tabular_advance(self._fm(f), price_drawn, self._digit_cell(f))

                # v1.2.71：徽标宽度**先算实际值再交给布局**。
                # 此前布局按常量 STATUS_BADGE_W(=34) 判定"放得下吗"，而绘制时却用
                # 动态宽度（文字宽 + 2*BADGE_PAD_X，实测"涨停/跌停/停牌"均 26px）——
                # 同一物理量两份定义，判定比实际宽 8px，导致剩余空间 27~33px 时
                # 徽标被**无谓丢弃**（用户只看到徽标偶尔失踪，无从判断原因）。
                # 现已删除 STATUS_BADGE_W，实际宽度成为唯一来源。
                badge_w_actual = 0
                if badge:
                    badge_fm = self._fm(self._font(FONT, BADGE_FONT_SIZE, QFont.Weight.Light))
                    badge_w_actual = badge_width(badge, badge_fm)

                lay = price_pct_layout(
                    w, _measure, pct_w,
                    pad_x=PAD_X, right_pad=PRICE_ROW_RIGHT_PAD, pct_gap=PCT_GAP,
                    badge_w=badge_w_actual, badge_gap=4,
                    size_max=FONT_PRICE_MAX, size_min=FONT_PRICE_MIN, step=2)
                # 价格大字：字号由统一布局在预算内自适应（已让出涨跌幅与徽标空间）
                size = lay["size"]
                price_rect = QRectF(lay["price_x"], Y_PRICE, lay["budget"], H_PRICE)
                # 目标价命中时，流动彩虹只作用于价格大字；涨跌幅和风险闪烁色保持原样。
                # 风险型白闪（下跌/破位）优先，避免彩虹掩盖已有风险提醒。
                if (not self._flash_drop_on and not self._price_flash_on
                        and self._rainbow_active()):
                    price_color = self._rainbow_pen(price_rect)
                else:
                    price_color = color
                pct_color = FG_DIM if _pct_unknown else color
                p.setFont(self._font(NUM_FONT, size, QFont.Weight.Bold))
                p.setPen(price_color)
                # v1.2.24：价格大字升级为逐位滚动（旧数字滑出/新数字滑入），
                # 逐字符 advance 累加宽度 == 原整串宽度，徽标定位与等宽对齐不变
                self._draw_roll_text(p, price_rect, price,
                                     self._font(NUM_FONT, size, QFont.Weight.Bold),
                                     price_color, align_bottom=False, roll_key="price")
                # 涨跌幅/徽标垂直对齐基准：先算出行基线，徽标 y 与涨跌幅顶边都依赖它
                fm_price = self._fm(self._font(NUM_FONT, size, QFont.Weight.Bold))
                price_baseline = Y_PRICE + (H_PRICE - fm_price.height()) / 2.0 + fm_price.ascent()
                # v1.3.60：两组按"可见墨水底边"对齐，而不是数学基线。
                # 27pt 墨水底边贴 baseline-1，13pt 悬在 baseline-3（hinting 量化差），
                # 数学上同基线时涨跌幅看起来比股价高出约 2px（老板截图实测
                # 股价底边 192 vs 涨跌幅 189~191）。把涨跌幅下移两者的 δ 差，
                # 让两条看得见的底边压在同一条线上。
                price_font_used = self._font(NUM_FONT, size, QFont.Weight.Bold)
                d_price = self._ink_delta(price_font_used, price_baseline)
                d_pct = self._ink_delta(pct_font, price_baseline)
                pct_shift = d_pct - d_price
                pct_rect_bottom = price_baseline + pct_fm.descent() + pct_shift
                pct_rect_top = price_baseline - pct_fm.ascent() + pct_shift
                # 状态徽标：v1.2.70 样式调整——
                #   ① 左边缘与涨跌幅对齐（badge_x = pct_x，非居中）；
                #   ② 宽度自适应文字（文字宽 + 2*BADGE_PAD_X）；
                #   ③ 颜色取 _trend_color() 后 darker(140)（纯色填充比文字看起来亮得多，需压暗）；
                #   ④ 字号 BADGE_FONT_SIZE(7) + 字重 Light（比正文更细小）。
                #   ⑤ v1.2.71：垂直位置走 badge_vertical 唯一实现 —— 顶到代码行时
                #      夹紧，夹紧后仍与涨跌幅相撞则本帧不绘制（垂直空间不足）。
                # 极端场景（窄窗 + 超长价格）统一布局会丢弃徽标
                # （badge_x=None）以保证价格与涨跌幅不堆叠覆盖。
                badge_y = badge_vertical(pct_rect_top) if badge else None
                if badge and lay["badge_x"] is not None and badge_y is not None:
                    badge_x = lay["pct_x"]          # 左对齐涨跌幅
                    # 颜色与涨跌幅同源但压暗（solid fill 比 text 看起来亮很多）
                    bcol_raw = self._trend_color() if badge in ("涨停", "跌停") else FG_DIM
                    bcol = bcol_raw.darker(140)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(bcol)
                    p.drawRoundedRect(QRectF(badge_x, badge_y, badge_w_actual, STATUS_BADGE_H),
                                      STATUS_BADGE_R, STATUS_BADGE_R)
                    p.setFont(self._font(FONT, BADGE_FONT_SIZE, QFont.Weight.Light))
                    p.setPen(QColor("#ffffff"))
                    p.drawText(QRectF(badge_x, badge_y, badge_w_actual, STATUS_BADGE_H),
                               Qt.AlignmentFlag.AlignCenter, badge)
                # 涨跌幅：常规情况下固定在价格右侧。v1.2.72：极端长价格在最小
                # 字号仍会挤压到涨跌幅时，布局返回 show_pct=False；此帧只显示完整
                # 价格（不再把两个数字硬叠在一起），徽标也随之隐藏。
                if lay["show_pct"]:
                    # 基线与股价精确对齐：pct_rect 底 = price_baseline + pct_fm.descent()，
                    # 使 align_bottom=True 时百分比基线 == price_baseline，真正的视觉底部对齐。
                    pct_rect = QRectF(lay["pct_x"], pct_rect_top, lay["pct_avail"],
                                      pct_rect_bottom - pct_rect_top)
                    p.setFont(pct_font)
                    p.setPen(pct_color)
                    # v1.2.24：涨跌幅同样逐位滚动（含 +/- 与 % 位）
                    # v1.2.56：% 符号使用较小字号（FONT_PCT-3pt），并与数字底对齐
                    pct_small_font = self._font(NUM_FONT, max(FONT_PCT - 3, 8), QFont.Weight.Bold)
                    self._draw_roll_text(p, pct_rect, pct,
                                         pct_font,
                                         # v1.3.49：未知态用中性灰 pct_color（否则沿用涨跌色）
                                         pct_color, align_bottom=True, roll_key="pct",
                                         pct_small_font=pct_small_font)
            else:
                p.setFont(self._font(NUM_FONT, FONT_PRICE_MAX, QFont.Weight.Bold))
                p.setPen(FG_NAME)
                p.drawText(QRectF(PAD_X, Y_PRICE, 120, PRICE_BAND_H), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "--")
                # v1.2.30：无有效价格显示 "--"，但若股票处于停牌状态仍在其右侧（上方）
                # 绘制灰色"停牌"徽标（与加载中/断网区分；价格区绝不显示 0/0.00/0%）。
                # v1.2.70 样式调整：徽标左对齐涨跌幅区域、字号缩小+细字重、宽度自适应、颜色压暗。
                if self.quote:
                    badge = quote_status(self.quote)
                    if badge == "停牌":
                        fm_p = self._fm(self._font(NUM_FONT, FONT_PRICE_MAX, QFont.Weight.Bold))
                        text_w = fm_p.horizontalAdvance("--")
                        # 涨跌幅会接在 "--" 右侧 + PCT_GAP 处；徽标左对齐该区域
                        pct_x_fallback = PAD_X + text_w + PCT_GAP
                        max_pct_x = (w - PRICE_ROW_RIGHT_PAD)
                        pct_avail_fb = max(0, max_pct_x - pct_x_fallback)
                        badge_x_fb = pct_x_fallback   # 左对齐涨跌幅区域
                        # 动态宽度
                        badge_fm_fb = self._fm(self._font(FONT, BADGE_FONT_SIZE, QFont.Weight.Light))
                        badge_w_fb = badge_fm_fb.horizontalAdvance(badge) + 2 * BADGE_PAD_X
                        # 只有徽标能完整放入涨跌幅区域时才绘制。
                        # v1.4.64（台账 S49）：这里此前与正常路径同口径走
                        # badge_vertical(价格带顶)，而降态没有涨跌幅行 —— 那一层
                        # 的 min_top 钳制于是恒判"垂直空间不够"，返回 None，
                        # 于是"停牌 + 取不到价"这一组合自 v1.3.42 起一次都没画出
                        # 过徽标（最需要标识的时刻恰好看不见）。改成价格带内垂直
                        # 居中：不压代码行也不压涨跌幅行，且无需动任何既有常量。
                        badge_y = badge_in_band_top(Y_PRICE, PRICE_BAND_H)
                        # 诊断留痕（S49）：这条降级支路死过 20+ 个版本而零痕迹。
                        # "判定要画"与"真画出来"分开记 —— 徽标画没画、被几何挡没挡，
                        # 诊断包各给一个数，不再让两条失败在包里长得一样。
                        _badge_drawable = (badge_y is not None and
                                           badge_x_fb + badge_w_fb <= pct_x_fallback + pct_avail_fb)
                        if _badge_drawable:
                            _PAINT_DIAG["badge_degraded_drawn"] += 1
                        else:
                            _PAINT_DIAG["badge_degraded_blocked"] += 1
                        if _badge_drawable:
                            bcol_fb = FG_DIM.darker(140)
                            p.setPen(Qt.PenStyle.NoPen)
                            p.setBrush(bcol_fb)
                            p.drawRoundedRect(QRectF(badge_x_fb, badge_y, badge_w_fb, STATUS_BADGE_H),
                                              STATUS_BADGE_R, STATUS_BADGE_R)
                            p.setFont(self._font(FONT, BADGE_FONT_SIZE, QFont.Weight.Light))
                            p.setPen(QColor("#ffffff"))
                            p.drawText(QRectF(badge_x_fb, badge_y, badge_w_fb, STATUS_BADGE_H),
                                       Qt.AlignmentFlag.AlignCenter, badge)

        def _paint_market_status(self, p, w, elided):
            """历史保留的非交易时段状态绘制辅助。

            当前主卡绘制路径不调用本方法；水平位置由 `market_status_rect` 定义，
            左边缘与股票名称左边缘对齐。
            """
            mst = self._market_status()
            if mst:
                lab, col = mst
                f_mst = self._font(FONT, FONT_CODE, QFont.Weight.Bold)
                p.setFont(f_mst)
                p.setPen(col)
                fm_s = self._fm(f_mst)
                lab_w = fm_s.horizontalAdvance(lab)
                fm_n = self._fm(self._font(FONT, FONT_NAME, QFont.Weight.Bold))
                name_w = fm_n.horizontalAdvance(elided) if self.quote else 0
                # 与右上三按钮同一行：文字垂直中心对齐按钮图标中心（BTN_ICON_Y + BTN_ICON_SZ/2）
                text_cy = BTN_ICON_Y + BTN_ICON_SZ / 2.0
                rx, rw = market_status_rect(w, lab_w, name_w)
                p.drawText(QRectF(rx, text_cy - H_NAME / 2.0, rw, H_NAME),
                           Qt.AlignmentFlag.AlignCenter, lab)

        def _paint_chart(self, p, w):
            """20.1 v1.2.27：分时图（收起跳过；图高动态自适应，绝不挤压底部数据行）。

            v1.3.51：图带高度 < CHART_MIN_DRAW_H 的闸门在 _draw_chart 里（收干净）。
            _chart_hit（stock_float_window）结论：**保持原语义，不随闸门失效** ——
              1) 折叠态 _chart_expanded=False，_chart_hit 走的是收起分支
                 [Y_PRICE, h - BOTTOM_BASE_HIDDEN]（股价行 → 窗口底），即"点股价
                 展开分时图"的既有交互，与分时图是否绘制无关，必须保留；
              2) 展开态分支只在 _chart_expanded=True 时生效，此时即便图带 <8px，
                 区间 [CHART_TOP, h-BOTTOM_SECTION_HIDDEN] 也只有 1px 甚至为空
                 （h - BOTTOM_SECTION_HIDDEN < CHART_TOP 时 <= 判定恒 False），
                 既不会误吞点击，也不会与 y>=h-8 的底部拉伸把手冲突；
              3) 若让热区跟着闸门一起失效，展开动画期间会出现"点了没反应"的空窗，
                 反而更差。
            """
            if self._chart_expanded:
                chart_h = max(0, self.height() - CHART_TOP - BOTTOM_SECTION_HIDDEN)
                self._draw_chart(p, QRectF(PAD_X, CHART_TOP, w - 2 * PAD_X, chart_h))

        def _paint_bottom(self, p, w):
            """S3 底部双行布局，两行左对齐，中文标签 8pt，数字默认 9pt。

            行 1（上）：最大涨幅（相对昨收，下限零）+ 最高点
            行 2（下）：最大跌幅（相对昨收，上限零）+ 最低点；零和价格均为灰 FG_DIM
            两行均按 renderer._bottom_layout(row1, row2) 唯一布局实现，与宽度计算共用。
            文本本身由 bottom_rows() 唯一提供（宽度计算 _bottom_texts 共用同一份，
            v1.2.71 消除双份定义）。
            """
            rows = bottom_rows(self.quote)
            if rows:
                row1, row2 = rows
                f_code_n = self._font(FONT, BOTTOM_LABEL_PT)
                # v1.2.95 第9轮走查：底部数字（最高/最低涨跌幅、最高点/最低点）此前用
                # PingFang 绘制，与价格行（D-DIN）字形不一致。数字改走 NUM_FONT
                # 与价格行统一；底部中文标签仍用 PingFang（D-DIN 无中文字形）。
                f_code_b = self._font(NUM_FONT, FONT_CODE, QFont.Weight.Bold)
                p.setFont(f_code_n)
                fm = self._fm(f_code_n)
                fm_bold = self._fm(f_code_b)
                # baseline 锚点：
                #   y_row2（下方，紧贴窗口底）= 旧布局 baseline 行 = h - BOTTOM_BASE_HIDDEN
                #   y_row1（上方，比 y_row2 再退一行 + 行间距）= h - BOTTOM_BASE_HIDDEN - BOTTOM_ROW_H - BOTTOM_ROW_GAP
                y_row1 = self.height() - BOTTOM_BASE_HIDDEN - BOTTOM_ROW_H - BOTTOM_ROW_GAP
                y_row2 = self.height() - BOTTOM_BASE_HIDDEN
                def _fm_num_at(delta):
                    """数字字号的第 delta 档度量（0 档复用已缓存的那一份）。"""
                    if delta == 0:
                        return fm_bold
                    return self._fm(self._font(NUM_FONT, FONT_CODE + delta,
                                               QFont.Weight.Bold))
                layout_key = (w, tuple(row1), tuple(row2), fm, fm_bold, bottom_rows_fitting,
                              FONT, NUM_FONT, FONT_CODE, BOTTOM_LABEL_PT,
                              PAD_X, BOTTOM_INNER_GAP, BOTTOM_GAP)
                cache = self._bottom_layout_cache
                if cache is None or cache[0] != layout_key:
                    layout = bottom_rows_fitting(
                        w, row1, row2, fm, _fm_num_at, PAD_X, BOTTOM_INNER_GAP, BOTTOM_GAP)
                    cache = (layout_key, layout)
                    self._bottom_layout_cache = cache
                segs_row1, segs_row2, shrink, _min_w = cache[1]
                # base=0：底部不设宽度下限（窗口最小宽由价格行布局决定）；
                # slack=8：给抗锯齿与圆角留余量，避免末段数字贴住右缘被裁。
                # 诊断留痕（S39 那一族的现场）：缩到第几档、放不下被放弃几段。
                # 只在值变化时写模块级末态，逐帧代价是一次元组比较。
                _dropped = sum(1 for s in segs_row1 if not s["fits"]) \
                    + sum(1 for s in segs_row2 if not s["fits"])
                if (int(shrink), _dropped) != (_PAINT_DIAG["bottom_shrink"],
                                               _PAINT_DIAG["bottom_dropped"]):
                    _PAINT_DIAG["bottom_shrink"] = int(shrink)
                    _PAINT_DIAG["bottom_dropped"] = _dropped
                if shrink:
                    # 缩过号就必须用同一字号画，否则"按 8pt 排、按 9pt 画"会重新压回去
                    f_code_b = self._font(NUM_FONT, FONT_CODE + shrink, QFont.Weight.Bold)
                    log_line_throttled(
                        "bottom_num_shrunk",
                        "底部数字缩到 %dpt 才放下（窗口宽 %dpx，需要 %dpx）"
                        % (FONT_CODE + shrink, w, _min_w))
                for s in segs_row1:
                    if not s["fits"]:
                        continue
                    p.setPen(s["color"])
                    p.setFont(f_code_n)
                    p.drawText(s["x"], y_row1, s["label"])
                    p.setFont(f_code_b)
                    p.drawText(s["x"] + s["label_w"] + BOTTOM_INNER_GAP, y_row1, s["num"])
                for s in segs_row2:
                    if not s["fits"]:
                        continue
                    p.setPen(s["color"])
                    p.setFont(f_code_n)
                    p.drawText(s["x"], y_row2, s["label"])
                    p.setFont(f_code_b)
                    p.drawText(s["x"] + s["label_w"] + BOTTOM_INNER_GAP, y_row2, s["num"])

        def _font(self, family, size, weight=QFont.Weight.Normal):
            """字体对象缓存：paintEvent 每帧复用，避免反复构造 QFont（性能优化）"""
            key = (family, size, weight)
            f = self._fonts.get(key)
            if f is None:
                f = QFont(family, size, weight)
                self._fonts[key] = f
            return f

        def _fm(self, font):
            """QFontMetrics 缓存：测量结果与直接构造一致，避免每帧重建（性能优化）"""
            key = (font.family(), font.pointSizeF(), font.weight())
            m = self._fms.get(key)
            if m is None:
                m = QFontMetrics(font)
                self._fms[key] = m
            return m

        # v1.3.60：数字字形"可见墨水底边"相对 baseline 的偏移（px）。
        # 背景（老板截图逐像素实测）：27pt 数字墨水底边贴着基线，13pt 数字
        # 却悬在基线上方约 2px —— 两组数学基线相同，看得见的底边却差 2px，
        # 涨跌幅看起来"悬空"。字体 hinting 的落点依赖基线的小数相位，
        # QFontMetrics 给不出墨水底边，只能离屏画一次 '0' 实测，
        # 且必须按真实基线相位测量（整数相位测不出差异）。
        # 结果按字体 key + 相位缓存，窗口字号固定 3~4 个，开销可忽略。
        def _glyph_slot(self, fm, ch, cell):
            """返回 (该字符占位宽, 字形在占位内的左偏移)。数字占统一格宽并居中。"""
            natural = fm.horizontalAdvance(ch)
            if ch in DIGIT_CHARS:
                return cell, (cell - natural) / 2.0
            return natural, 0.0

        def _digit_cell(self, font):
            """数字单格占位宽（按字体 key 缓存，热路径不重复量 10 次）。"""
            key = (font.family(), font.pointSizeF(), font.weight())
            lazy = getattr(self, "_digit_cells", None)
            if lazy is None:
                cells: dict = {}
                lazy = self._digit_cells = cells
            v = lazy.get(key)
            if v is None:
                v = digit_cell_width(self._fm(font))
                lazy[key] = v
            return v

        def _roll_drawn_string(self, key, s):
            """绘制层这一帧真正会画出来的串 —— 量宽必须按它，不按插值中间串。

            走查 S8：窗口在行情更新时预置 `_roll_pending[key]=目标串`，`_roll_text`
            会按目标串绘制，但布局却拿插值中间串（如 "99.98"）量宽。位数一跨
            （"99.98"→"100.00"）绘制串比量出的宽出约 8px，价格直接压住涨跌幅，
            持续 2~3 帧才被下一帧纠正。本方法把两侧统一到同一个解析上。

            与 `_draw_roll_text` 的 v1.2.52 兜底同口径：动画定时器已停时预置串作废
            （那里就地清除，这里返回实际串），两侧不会一个画新串一个量旧串。
            """
            pending_map = getattr(self, "_roll_pending", None)
            pending = pending_map.get(key) if pending_map else None
            if pending is None:
                return s
            timer = getattr(self, "_anim_timer", None)
            if timer is not None and not timer.isActive():
                return s
            return pending

        def _ink_delta(self, font, baseline_ref):
            key = (font.family(), font.pointSizeF(), font.weight(),
                   round(float(baseline_ref) % 1.0, 3))
            lazy = getattr(self, "_ink_deltas", None)
            if lazy is None:
                lazy = self._ink_deltas = {}
            d = lazy.get(key)
            if d is not None:
                return d
            fm = self._fm(font)
            phase = float(baseline_ref) % 1.0
            w = max(8, fm.horizontalAdvance("0") + 4)
            h = fm.height() + 8
            img = QImage(w, h, QImage.Format.Format_ARGB32)
            img.fill(QColor(0, 0, 0))
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            draw_base = math.floor(fm.ascent() + 2.0) + phase
            p.setFont(font)
            p.setPen(QColor(0, 255, 0))
            p.drawText(QPointF(2.0, draw_base), "0")
            p.end()
            d = 0.0
            for y in range(h - 1, -1, -1):
                hit = False
                for x in range(w):
                    c = img.pixelColor(x, y)
                    if c.green() > 100 and c.red() < 130 and c.blue() < 150:
                        hit = True
                        break
                if hit:
                    d = draw_base - (y + 1.0)   # 最后一行墨水的下缘
                    break
            lazy[key] = d
            return d

        # ---------- v1.2.24：价格/涨跌幅逐位滚动 ----------
        def _roll_update(self, key, s):
            """对比上一帧显示串，记录位值变化 → 触发逐位滚动。

            每个 key（"price"/"pct"）独立维护基线串与滚动位，互不串扰；
            仅在长度相同的连续帧间逐位对比（避免切换股票/首次渲染时整串乱滚）；
            长度变化时仅更新基线串并清空进行中的滚动位。
            """
            prev = self._roll_prev.get(key)
            anim = self._roll_anim.setdefault(key, {})
            self._roll_prev[key] = s
            if prev is None:
                anim.clear()
                return
            if len(prev) != len(s):
                anim.clear()
                return
            now = time.monotonic()
            for i, ch in enumerate(s):
                if prev[i] != ch:
                    # 位值变化：新开/刷新该位滚动（含符号/小数点，方向由字符序决定）
                    anim[i] = [prev[i], ch, now, ROLL_DUR]

        def _roll_progress(self, now, entry):
            """返回 (e, dur)：e 为 ease-out 进度 0~1；dur 剩余秒数（<0 表示已结束）"""
            dur = entry[2] + entry[3] - now
            if dur <= 0:
                return 1.0, dur
            t = (now - entry[2]) / entry[3]
            e = 1.0 - (1.0 - t) ** 3   # ease-out cubic：与价格插值同款缓动，视觉一致
            return max(0.0, min(e, 1.0)), dur

        def _draw_static_text(self, p, rect, s, font, color,
                              align_bottom=False, pct_small_font=None):
            """静态逐字符绘制，复用滚动文本的底边与末位 % 缩小口径。

            用于涨跌幅：数值可随行情帧刷新，但字形绝不离开对齐线。
            """
            fm = self._fm(font)
            baseline = (rect.bottom() - fm.descent() if align_bottom
                        else rect.top() + (rect.height() - fm.height()) / 2.0 + fm.ascent())
            x = rect.left()
            p.setPen(color)
            cell = self._digit_cell(font)
            for i, ch in enumerate(s):
                is_pct_symbol = (pct_small_font is not None and ch == "%" and i == len(s) - 1)
                current_font = pct_small_font if is_pct_symbol else font
                # 本版 走查 S8：数字位占最宽数字的格宽、字形在格内居中，
                # 与 tabular_advance（量宽侧）同一口径。
                adv, dx = self._glyph_slot(fm, ch, cell)
                p.setFont(current_font)
                p.drawText(QPointF(x + dx, baseline), ch)
                x += adv
            return x - rect.left()

        def _draw_roll_text(self, p, rect, s, font, color,
                            align_bottom=False, roll_key="text", pct_small_font=None):
            """逐位滚动绘制：非滚动位单字符静态绘制，滚动位双字符垂直滑动
            （旧字符滑出 + 新字符滑入，老虎机式逐位翻滚）。

            - roll_key 区分不同绘制项（价格/涨跌幅），各自独立维护滚动状态
            - 逐字符 advance 累加；本版起数字位占统一格宽（走查 S8），
              整串宽度只由位数与非数字字符决定，与位值无关 → 右侧涨跌幅/徽标不横移
            - 滚动方向按字符序：增大时新字符自下滚入（轮子上翻），减少时自上滚入
            - 返回渲染文本总宽度（px）
            - pct_small_font: 可选参数，当最后一个字符为 % 时使用此小字号字体绘制，并底对齐

            v1.2.25 修复「滚动乱跳」：价格插值动画（130~200ms，16ms/帧）期间
            _disp_price 每帧都在变，若每帧都重启 0.18s 的逐位滚动，滚动动画永远
            播不完 → 数字持续翻滚乱跳。窗口层在行情更新时预置 _roll_pending[key]
            = 最终目标串，本方法在预置目标期间一律按该目标串绘制：首帧从旧显示
            值触发一次滚动，中间帧不重启，插值结束（窗口清 _roll_pending）后落定。

            v1.2.51 修复「滚动卡中途」：高频行情覆盖 _roll_pending 时，必须清空
            旧滚动条目（可能基于过期目标串），并更新 _roll_prev 为旧 pending 值，
            确保下次 _roll_update 能正确对比新旧目标串、创建正确的滚动条目。

            v1.2.52 终极兑底：预置目标仅在插值动画运行期间有效。若动画定时器已停
            （被微小变动直赋/拉伸停表等路径打断）而 _roll_pending 仍残留，就地清除，
            回到实际显示值 —— 否则渲染层永久锁定旧目标串，价格冻结不再跟随行情。
            """
            pending = self._roll_pending.get(roll_key) if self._roll_pending else None
            if pending is not None:
                # v1.2.52：定时器已停 → 预置目标已失去意义（动画不会再推进），
                # 属泄漏残留，立即清除防锁定（正常动画期间定时器必在运行，
                # _apply_quote 先 start 定时器再置 pending，同帧无间隙，不误伤）
                timer = getattr(self, "_anim_timer", None)
                if timer is not None and not timer.isActive():
                    self._roll_pending.pop(roll_key, None)
                    pending = None
            if pending is not None:
                # v1.2.51：pending 切换时，清空旧滚动条目（避免新旧目标串长度/
                # 字符位置不一致导致错位渲染），并更新 _roll_prev 为旧 pending
                prev_val = self._roll_prev.get(roll_key)
                if prev_val != pending:
                    self._roll_anim.pop(roll_key, None)  # 清空旧条目
                    # _roll_prev 设为旧 pending（或当前 s 若无旧 pending），
                    # 使下次 _roll_update 能正确对比
                    self._roll_prev[roll_key] = prev_val if prev_val is not None else s
                s = pending
            self._roll_update(roll_key, s)
            anim = self._roll_anim.get(roll_key, {})
            fm = self._fm(font)
            # baseline：VCenter 与 AlignBottom 两套，与原 drawText(rect, flags) 逐字对齐
            if align_bottom:
                baseline = rect.bottom() - fm.descent()
            else:
                baseline = rect.top() + (rect.height() - fm.height()) / 2.0 + fm.ascent()
            h = fm.height()
            now = time.monotonic()
            x = rect.left()
            p.setFont(font)
            p.setPen(color)
            # v1.2.26 修复「滚动跳出边界」：滚动位垂直滑动时旧/新字符会越出字符条带
            # （如价格大字滑入滑出时与上下行文字错位重叠）。将滚动绘制裁剪到
            # 「字符行条带」：滑出部分被裁掉，保持老虎机效果（字符从条带上下缘
            # 滚入滚出），绝不越界/重叠。
            # v1.3.59：条带底边从 baseline+descent 收紧到 baseline（数字静止时的
            # 可见底边线）。实测（V1.3.56 三张实拍截图逐像素测量）：静止数字底边
            # 全部落在 baseline；但旧条带允许滚动中的新字符最多探到 baseline+
            # descent（约 10px@27pt）才被裁掉——股价末位数字会周期性"沉"到底边
            # 之下，就是老板反复截到的"中途错位"。底边钉死在 baseline 后，新旧
            # 字符照常翻滚，但任何一帧的可见底边都不会低于静止底边线。
            # v1.3.60：条带底边再钉到"可见墨水底边"= baseline - ink_delta(font)，
            # 与 _paint_price 的两组可见底边对齐口径一致（13pt 墨水底边在
            # baseline-3，若只钉 baseline，涨跌幅滚动位仍会比股价滚动位低 2px）。
            d_ink = self._ink_delta(font, baseline)
            p.save()
            p.setClipRect(QRectF(rect.left(), baseline - fm.ascent(),
                                 rect.width(), fm.ascent() - d_ink))
            dead = []
            cell = self._digit_cell(font)
            for i, ch in enumerate(s):
                # v1.2.56：最后一个字符为 % 时使用较小字号绘制，并底对齐
                is_pct_symbol = (pct_small_font is not None and ch == "%" and i == len(s) - 1)
                current_font = pct_small_font if is_pct_symbol else font
                entry = anim.get(i)
                # advance 恒按主字体度量累加：末位 % 即使换小字号绘制，占位仍按
                # 大字号算，整串宽度不随字号抖动，涨跌幅/徽标位置才稳定。
                # 本版 走查 S8：数字位再钉到统一格宽，整串宽度只由位数决定。
                adv, dx = self._glyph_slot(fm, ch, cell)
                x += adv
                left = x - adv
                if entry is None:
                    p.setFont(current_font)
                    p.drawText(QPointF(left + dx, baseline), ch)
                    continue
                e, dur = self._roll_progress(now, entry)
                if dur <= 0:
                    p.setFont(current_font)
                    p.drawText(QPointF(left + dx, baseline), ch)
                    dead.append(i)
                    continue
                from_ch, to_ch = entry[0], entry[1]
                # 滚动位：旧字与新字各自在自己的格内居中，翻动过程中左右边界不外溢
                _, dx_from = self._glyph_slot(fm, from_ch, cell)
                _, dx_to = self._glyph_slot(fm, to_ch, cell)
                # v1.3.61：滚动方向统一为"旧字向上翻出、新字从底边线下方翻上来"
                # （里程表式单向滚动）。旧实现按字符序定方向：数值减小时新字
                # 自上滚入，翻到一半整个悬在对齐线上方——老板三张截图实测尾位
                # 底边 190 vs 其他位 193（悬空 3px），全部是"减小方向"的中途帧；
                # 图3 同价格落定帧底边 193 齐，证明静止对齐已修好、残余错位
                # 全部来自该方向。统一后新字永远从下方滚入、被底边裁切线托住，
                # 任何一帧可见墨水的最低点 = 对齐线，不再出现悬空。
                from_dy = -e * h        # 旧字向上翻出（顶边裁掉）
                to_dy = (1.0 - e) * h   # 新字从下翻入（底边裁切线托住）
                p.setFont(current_font)
                p.drawText(QPointF(left + dx_from, baseline + from_dy), from_ch)
                p.drawText(QPointF(left + dx_to, baseline + to_dy), to_ch)
            p.restore()
            for i in dead:
                anim.pop(i, None)
            return x - rect.left()

        def _trend_color(self):
            if not self.quote:
                return FG_NAME
            try:
                chg = float(self.quote["change"])
            except Exception:
                return FG_NAME
            return RED if chg > 0 else (GREEN if chg < 0 else FG_NAME)

        def _chart_metrics(self, pts, rect):
            """计算分时图绘制所需统计量：y 轴范围、价格映射闭包参数"""
            pre_close = None
            if self.quote:
                try:
                    pre_close = float(self.quote["pre_close"])
                except Exception:
                    pre_close = None

            # v1.2.95 第4轮走查：只收有限数值。此前直接 min()/max() 原始点值，
            # 分时数据里一旦出现 inf（脏数据/除零产物），lo/hi 就变成 ∓inf，
            # 下游 int(nan) 抛 ValueError —— 整块分时图会从界面上消失，且绘制
            # 兜底每帧记一条日志。同文件其他统计（最高/最低）早有 isfinite
            # 保护，此处是漏网的一个。
            prices = []
            for pt in pts:
                v = pt[1]
                if isinstance(v, (int, float)) and math.isfinite(float(v)):
                    prices.append(float(v))
            if isinstance(pre_close, (int, float)) and math.isfinite(float(pre_close)):
                prices.append(float(pre_close))
            for pt in pts:
                if len(pt) > 2 and pt[2] is not None:
                    a = pt[2]
                    if isinstance(a, (int, float)) and math.isfinite(float(a)):
                        prices.append(float(a))
            if not prices:
                # 全部点都不可用：给一个安全的空区间。宁可画一条平线，
                # 也绝不能让坐标退化为 nan 而导致整块图消失。
                prices = [0.0, 1.0]
            lo, hi = min(prices), max(prices)
            # 一字板/全天无波动时 lo == hi，span 为 0 会让下游归一化除零 —— 人为
            # 撑开 1 元画出一条平线；再上下各留 10% 余量，防止折线与光晕贴边被裁。
            # v1.4.59（D08）：这段公式搬到模块级 chart_price_span，与用例共用一份。
            lo, hi = chart_price_span(lo, hi)
            return {
                "pre_close": pre_close, "lo": lo, "hi": hi,
                "n": len(pts), "color": self._trend_color(),
            }

        def _chart_y(self, rect, m, price):
            """价格 → y 坐标。v1.2.95 第4轮走查：本函数是所有分时绘制的公共
            出口，必须保证返回值恒为有限数 —— inf/nan 会让 int() 抛
            ValueError，导致整块分时图从界面消失。故在此统一做净化与夹取，
            而不是要求每个调用点各自防一遍。"""
            hi, lo = m["hi"], m["lo"]
            if not (isinstance(hi, (int, float)) and isinstance(lo, (int, float))
                    and math.isfinite(float(hi)) and math.isfinite(float(lo))):
                hi, lo = 1.0, 0.0
            hi, lo = float(hi), float(lo)
            if hi < lo:
                hi, lo = lo, hi
            try:
                v = float(price)
            except Exception:
                v = float("nan")
            if not math.isfinite(v):
                pc = m.get("pre_close")
                v = float(pc) if isinstance(pc, (int, float)) and math.isfinite(float(pc)) \
                    else (hi + lo) / 2.0
            if not math.isfinite(v):
                v = (hi + lo) / 2.0
            # 撑区间与"价格→y"都走模块级函数（D08：全仓只有一份这个公式）
            return chart_y_for_price(rect, lo, hi, v)

        def _clamp_price(self, v, m):
            """把任意输入夹成位于 [lo, hi] 内的有限价格（脏值兜底）。

            v1.2.95 第4轮走查：供坐标缓存逐点净化使用 —— 单个 inf/nan 点
            会让归一化结果退化成 nan/inf，最终同样是 int() 抛异常、整图消失。
            """
            hi, lo = m["hi"], m["lo"]
            if not (isinstance(hi, (int, float)) and isinstance(lo, (int, float))
                    and math.isfinite(float(hi)) and math.isfinite(float(lo))):
                hi, lo = 1.0, 0.0
            hi, lo = float(hi), float(lo)
            if hi < lo:
                hi, lo = lo, hi
            try:
                f = float(v)
            except Exception:
                f = float("nan")
            if not math.isfinite(f):
                pc = m.get("pre_close")
                f = float(pc) if isinstance(pc, (int, float)) and math.isfinite(float(pc)) \
                    else (hi + lo) / 2.0
            if not math.isfinite(f):
                f = (hi + lo) / 2.0
            return min(max(f, lo), hi)

        def _chart_x(self, rect, m, i):
            """分时 X 轴按真实交易时间比例映射（09:30~11:30 / 13:00~15:00 共 240 分钟）。
            v1.2.2：修复按数据点数均分导致盘中折线被拉满全宽、进度失真的问题。
            v1.2.22：换算逻辑收敛到模块级 _chart_minute_offset，与缓存路径共用。"""
            pts = self.minute_points
            trade_min = _chart_minute_offset(pts[i][0], i, m["n"])
            return rect.left() + trade_min / 240.0 * rect.width()

        def _chart_pts_cache(self, rect, m):
            """v1.2.20：分时坐标缓存 —— 返回 (xs, np, na)：
            - xs：x 绝对坐标数组（随数据/窗口宽变化，拉伸高度不变则复用）
            - np：价格归一化 (hi-price)/(hi-lo)，与窗口高度无关
            - na：均价归一化（无均价点为 0，调用侧按 pts[i] 有无过滤）
            拉伸时高度逐帧变化，但归一化 y 不变，仅需 top+norm*height 缩放，
            省去每帧 240 点 hhmm 解析与 _chart_y 双重乘除（拉伸卡顿辅助优化）。"""
            key = (self._line_ver, m["n"], m["lo"], m["hi"], rect.x(), rect.width())
            if self._chart_cache_key != key:
                pts = self.minute_points
                span = m["hi"] - m["lo"]
                if not math.isfinite(span) or span <= 0:
                    span = 1.0
                n = m["n"]
                xs = [0.0] * n
                np_ = [0.0] * n
                na_ = [0.0] * n
                left = rect.left()
                w = rect.width()
                for i in range(n):
                    trade_min = _chart_minute_offset(pts[i][0], i, n)
                    xs[i] = left + trade_min / 240.0 * w
                    # v1.2.95 第4轮走查：单点脏值同样会污染归一化结果，
                    # 这里夹取回 [lo, hi] 区间，保证归一化恒为有限数。
                    np_[i] = (m["hi"] - self._clamp_price(pts[i][1], m)) / span
                    a = pts[i][2] if len(pts[i]) > 2 else None
                    na_[i] = (m["hi"] - self._clamp_price(a, m)) / span if a is not None else 0.0
                self._chart_cache_key = key
                self._chart_cache_xs = xs
                self._chart_cache_np = np_
                self._chart_cache_na = na_
            return self._chart_cache_xs, self._chart_cache_np, self._chart_cache_na

        def _draw_chart_base(self, p, rect, m):
            """0% 基准虚线（白色）"""
            if m["pre_close"] is None:
                return
            yy = self._chart_y(rect, m, m["pre_close"])
            p.setPen(QPen(BASELINE, 1, Qt.PenStyle.DashLine))
            p.drawLine(QPoint(int(rect.left()), int(yy)), QPoint(int(rect.right()), int(yy)))

        def _draw_chart_avg(self, p, rect, m):
            """均价线（黄色）

            v1.3.51：均价线 QPainterPath 缓存 —— 此前每帧重建 240 次
            moveTo/lineTo + 一个新 QPainterPath，实测 0.49ms/帧、占整块重绘
            28%。均价线只随「数据 / 归一化区间 / 绘制区域」三者变化，与折线
            _line_path 同模式：命中则直接复用 path，不再逐点重建。
            """
            pts = self.minute_points
            n = m["n"]

            def _avg_at(i):
                """取第 i 点的均价（可能缺失 → None），O(1) 供缓存键使用。"""
                row = pts[i]
                return row[2] if len(row) > 2 else None

            # 键取首/中/末三点均价：只取首末会在「首尾恰好同为 None」的脏数据下
            # 退化（[None, 10.0, None] 与 [None, 20.0, None] 键相同却图形不同），
            # 故补一个中间点参与比对；全 None 时路径恒为空，命中反而是正确的。
            first_a = _avg_at(0) if pts else None
            last_a = _avg_at(n - 1) if pts else None
            mid_a = _avg_at(n // 2) if pts else None
            # 键中每一项防的失效：
            #  _line_ver —— minute_points 整表替换（数据刷新的真实路径，代替
            #                id(pts)：整表替换时 id 恒变会让缓存恒失效）；
            #  n / lo / hi —— 点数与归一化区间变化（lo/hi 含昨收，行情更新会变）；
            #  首/中/末均价 —— 同上，且兜住未递增 _line_ver 的原地改动；
            #  m["color"] —— 趋势色翻转（与折线缓存口径一致）；
            #  rect x/y/w/h —— path 里存的是绝对坐标 y = top + na*h，区域一变必失效。
            key = (self._line_ver, n, m["lo"], m["hi"],
                   first_a, mid_a, last_a, m["color"],
                   rect.x(), rect.y(), rect.width(), rect.height())
            cache = getattr(self, "_avg_path_cache", None)   # 未初始化的裸对象也能安全渲染
            if cache is not None and cache[0] == key:
                avg_path, last_avg_i, last_avg = cache[1], cache[2], cache[3]
            else:
                xs, _np, na = self._chart_pts_cache(rect, m)
                top = rect.top()
                h = rect.height()
                avg_path = QPainterPath()
                avg_started = False
                last_avg_i, last_avg = -1, None
                for i in range(n):
                    a = pts[i][2] if len(pts[i]) > 2 else None
                    # v1.2.95 第4轮走查：非有限的均价点直接跳过（不画、也不标注），
                    # 而不是画成一条飞出图外的线或 nan 坐标。
                    if a is None or not (isinstance(a, (int, float)) and math.isfinite(float(a))):
                        continue
                    last_avg_i, last_avg = i, a
                    y = top + na[i] * h
                    if not avg_started:
                        avg_path.moveTo(xs[i], y)
                        avg_started = True
                    else:
                        avg_path.lineTo(xs[i], y)
                # 末点均价一并入缓存：命中时无需再扫一遍即可画右端标注
                self._avg_path_cache = (key, avg_path, last_avg_i, last_avg)
            if not avg_path.isEmpty():
                p.setPen(QPen(MARKET, 1.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(avg_path)
                # v1.2.4：最新实时均价标注，位于均线最右端点右侧 4pt，跟随均线实时移动
                self._draw_avg_label(p, rect, m, last_avg_i, last_avg)

        def _draw_avg_label(self, p, rect, m, i, avg):
            """在均价线最右端点右侧 4pt 处标注最新均价数值；接近图表右边界时左移防溢出"""
            if avg is None or i < 0:
                return
            text = f"{avg:.2f}"
            p.setFont(self._font(NUM_FONT, AVG_LABEL_FONT_PT, QFont.Weight.Bold))   # v1.2.22：走字体缓存避免每帧重建
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(text)
            th = fm.height()
            # 锚点 = 均线最右端点右侧 AVG_LABEL_DX pt，垂直居中对齐均线
            x = self._chart_x(rect, m, i) + AVG_LABEL_DX
            if x + tw + AVG_LABEL_RIGHT_M > rect.right():
                x = rect.right() - tw - AVG_LABEL_RIGHT_M
            label = QRectF(x, self._chart_y(rect, m, avg) - th / 2.0, tw + AVG_LABEL_HPAD * 2, th)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, AVG_LABEL_BG_ALPHA))
            p.drawRoundedRect(label.adjusted(-AVG_LABEL_OUTSET, -AVG_LABEL_OUTSET, AVG_LABEL_OUTSET, AVG_LABEL_OUTSET), AVG_LABEL_RADIUS, AVG_LABEL_RADIUS)
            p.setPen(QPen(MARKET, 1))
            p.drawText(label, Qt.AlignmentFlag.AlignCenter, text)

        def _draw_chart_fill(self, p, rect, m):
            """折线下方渐变填充。
            v1.3.62：改为行业主流方案（同花顺/东方财富/雪球）——从曲线本身
            向下铺满到图底的大面积渐变，随曲线起伏形成"山峦剪影"。
            旧实现把填充底边锚在昨收基准线，价格贴近昨收时只剩一条与曲线
            形状脱节的横带（老板截图实测：绿色填充带与昨收虚线重合、
            曲线下方大片留空），观感不像主流分时图。
            垂直渐变保持：图顶趋势色 → 图底透明。"""
            path = self._chart_price_path(rect, m)
            color = m["color"]
            key = (self._line_path_key, CHART_FILL_ALPHA, CHART_FILL_ALPHA_GREEN)
            cache = self._chart_fill_cache
            if cache is None or cache[0] != key:
                xs, np_, _na = self._chart_pts_cache(rect, m)
                fill = QPainterPath(path)
                fill.lineTo(xs[m["n"] - 1], rect.bottom())
                fill.lineTo(rect.left(), rect.bottom())
                fill.closeSubpath()
                grad_top = rect.top() + min(np_) * rect.height()
                if grad_top >= rect.bottom():
                    grad_top = rect.top()
                fill_grad = QLinearGradient(0, grad_top, 0, rect.bottom())
                top_c = QColor(color)
                top_c.setAlpha(CHART_FILL_ALPHA_GREEN if color == GREEN else CHART_FILL_ALPHA)
                fill_grad.setColorAt(0.0, top_c)
                fill_grad.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
                cache = (key, fill, fill_grad)
                self._chart_fill_cache = cache
            _, fill, fill_grad = cache
            p.fillPath(fill, fill_grad)

        def _chart_price_path(self, rect, m):
            """One bounded geometry cache shared by the line and its gradient fill."""
            pts = self.minute_points
            xs, np_, _na = self._chart_pts_cache(rect, m)
            top = rect.top()
            h = rect.height()
            # 渲染缓存：数据版本号、长度、首尾价、颜色、绘制区域均未变时复用 QPainterPath
            # （用 _line_ver 而非 id(pts)：minute_points 每次整表替换时 id 恒变，导致缓存恒失效）
            # v1.3.51：补 lo/hi —— 归一化区间含昨收（pre_close），行情刷新时昨收可能
            # 变化而 _line_ver 不变（版本号只在 minute_points 整表替换时 +1），此时
            # np_ 全变、path 几何全变，键里没有 lo/hi 就会沿用旧折线画错位置。
            key = (self._line_ver, len(pts), m["lo"], m["hi"],
                   pts[0][1] if pts else None, pts[-1][1] if pts else None,
                   m["color"], rect.x(), rect.y(), rect.width(), rect.height())
            if self._line_path is None or self._line_path_key != key:
                path = QPainterPath()
                path.moveTo(xs[0], top + np_[0] * h)
                for i in range(1, m["n"]):
                    path.lineTo(xs[i], top + np_[i] * h)
                self._line_path = path
                self._line_path_key = key
            return self._line_path

        def _draw_chart_line(self, p, rect, m):
            """折线（横向渐变过渡，细线保证锐利），后画保证渐变可见"""
            path = self._chart_price_path(rect, m)
            color = m["color"]
            line_grad = QLinearGradient(rect.left(), 0, rect.right(), 0)
            # 渐变两端一律从趋势色派生，理由与"线尾必须与呼吸点同色"这条不变量
            # 一起写在 chart_line_stop_colors 的 docstring 里（判据同处，不复制两遍）。
            start, stop = chart_line_stop_colors(color)
            line_grad.setColorAt(0.0, start)
            line_grad.setColorAt(1.0, stop)
            pen = QPen(QBrush(line_grad), CHART_LINE_WIDTH)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

        def _draw_chart_dot(self, p, rect, m):
            """当前点 —— 持续呼吸灯效果（平滑透明度/大小周期性变化）"""
            pts = self.minute_points
            cpt = QPoint(int(self._chart_x(rect, m, m["n"] - 1)), int(self._chart_y(rect, m, pts[-1][1])))
            color = m["color"]
            # 呼吸相位：0→1 平滑循环，周期约 CHART_DOT_BREATH_PERIOD 秒
            phase = (time.monotonic() % CHART_DOT_BREATH_PERIOD) / CHART_DOT_BREATH_PERIOD
            # sin 曲线映射到 0~1，实现平滑的吸入→呼出过渡
            breath = (math.sin(phase * 2 * math.pi - math.pi / 2) + 1) / 2  # 0→1→0
            if getattr(self, "_minute_stale", False):
                # v1.4.7：呼吸灯是"实时"的暗示 —— 数据滞后时冻结为最小态，
                # 与顶部状态标签共同表达"这不是最新走势，正在重连"。
                breath = 0.0
            # 光晕 alpha 和半径在上下限之间平滑插值
            glow_alpha = int(CHART_DOT_GLOW_ALPHA_MIN + breath * (CHART_DOT_GLOW_ALPHA - CHART_DOT_GLOW_ALPHA_MIN))
            glow_r = CHART_DOT_GLOW_R_MIN + breath * (CHART_DOT_GLOW_R - CHART_DOT_GLOW_R_MIN)
            # 始终绘制光晕层 + 实心圆点（呼吸灯效果）
            glow = QColor(color)
            glow.setAlpha(glow_alpha)
            p.setBrush(glow)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(cpt), glow_r, glow_r)
            # 实心圆点 + 柔和白边（alpha 也随呼吸微弱变化）
            p.setBrush(color)
            hl_alpha = int(CHART_DOT_HL_ALPHA_MIN + breath * (CHART_DOT_HL_ALPHA - CHART_DOT_HL_ALPHA_MIN))
            p.setPen(QPen(QColor(255, 255, 255, hl_alpha), CHART_DOT_HL_WIDTH))
            p.drawEllipse(cpt, CHART_DOT_R, CHART_DOT_R)

        def _draw_chart(self, p, rect):
            """分时图总入口：加载中提示 或 按 基准线→均价线→填充→折线→当前点 顺序绘制"""
            # v1.3.51 闸门：图带太矮（折叠态 1px / 展开动画首几帧）时整幅收干净 ——
            # 基准线、均价线、渐变填充、折线、末端呼吸点、"加载中"提示全不画，
            # 也不读写折线/均价线的 path 缓存（跳过即不碰，不会留脏值）。
            if rect.height() < CHART_MIN_DRAW_H:
                _loading_note("chart", False)   # 图区没画出来，用户看不到“加载中”
                return
            pts = self.minute_points
            if not pts:
                p.setFont(self._font(FONT, FONT_CHART_HINT))
                p.setPen(FG_DIM)
                # 非交易时段（休市/未开盘/午间休市/已收盘）数据为空属正常，显示市场状态；
                # 仅交易时段内无数据才提示"加载中"
                st = self._market_status()
                _loading_note("chart", not st)
                if st:
                    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, st[0])
                else:
                    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "加载中...")
                return
            _loading_note("chart", False)
            m = self._chart_metrics(pts, rect)
            # v1.4.14：这里**试过**把静态四项（基准线/均价线/渐变填充/折线）缓存成
            # QPixmap，好让呼吸灯的 60 帧/秒不必然带来 6 倍绘制开销 —— 实测**不通过**：
            # 主窗是 WA_TranslucentBackground，背景层本身带 alpha。把图表元素先合成到
            # 透明底、再整块贴回来，等于让背景那一层被重复合成一次。逐像素比对结果是
            # 13432 个采样像素里 46.2% 有差异、最大单通道差 255、差异像素平均差 63.2
            # —— 肉眼可见，不是舍入噪声。要做对必须连背景一起缓存并用
            # CompositionMode_Source 覆盖式贴回，那是另一场改动。
            # 老板定的优先级是"帧率第一、功耗第二"，所以这里保留直接重绘，
            # 用 CPU 换呼吸灯满帧。缓存方案留作后续（见 DELIVERY_CHECKLIST v1.4.14）。
            self._draw_chart_base(p, rect, m)
            self._draw_chart_avg(p, rect, m)
            self._draw_chart_fill(p, rect, m)
            self._draw_chart_line(p, rect, m)
            self._draw_chart_dot(p, rect, m)
            if getattr(self, "_minute_stale", False):
                # v1.4.7：盘中重连中 —— 曲线保留，状态由顶部标签标识，
                # 不再用遮罩压暗走势。
                self._draw_chart_stale(p, rect, m)

        def _draw_chart_stale(self, p, rect, m=None):
            """只记录顶部滞后标签与基准线的净空，不向图内绘制任何像素。"""
            # 诊断仍记录滞后现场，但走势图本身保持原亮度和完整可见；状态标签
            # 已由 _paint_stale_label 绘制到顶部，不再在图内重复盖住曲线。
            hint_h = STALE_LABEL_H
            line_y = None
            if m is not None and m.get("pre_close") is not None:
                try:
                    line_y = self._chart_y(rect, m, m["pre_close"])
                except Exception as exc:
                    # 基准线缺失只影响诊断，不影响顶部标签或曲线。
                    log_swallow("sf_ui_render._draw_chart_stale.line_y", exc)
                    line_y = None
            y = Y_NAME + NAME_TOP_PAD
            # 诊断留痕（v1.4.34 那条"文本不能与白色虚线贴近或重叠"的现场）：
            # 这一帧**实际**留出多少净空、有多少帧低于最小净空、有多少帧虚线根本取不到
            # （虚线取不到时规则退回贴顶，那是另一种"没法保证不压线"，必须分开数）。
            # 界面上不许加提示（v1.4.52 口径），日志又会被 5MB×3 轮转挤掉，
            # 所以末态只能落在这里。
            _gap = hint_line_gap_px(y, hint_h, line_y)
            _PAINT_DIAG["stale_hint_frames"] += 1
            if line_y is None:
                _PAINT_DIAG["stale_hint_line_missing"] += 1
                _PAINT_DIAG["stale_hint_last_gap_px"] = None
            else:
                if _gap < CHART_HINT_MIN_GAP_PX:
                    _PAINT_DIAG["stale_hint_tight_frames"] += 1
                _PAINT_DIAG["stale_hint_last_gap_px"] = round(_gap, 1)

        def _draw_label(self, p, rect, text, color):
            """标注文本：半透明深色圆角底板 + 彩色文字，保证任何位置都不被折线遮挡"""
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 175))
            p.drawRoundedRect(rect.adjusted(-LABEL_OUTSET_X, -LABEL_OUTSET_Y, LABEL_OUTSET_X, LABEL_OUTSET_Y), LABEL_RADIUS, LABEL_RADIUS)
            p.setFont(self._font(NUM_FONT, LABEL_FONT_PT, QFont.Weight.Bold))   # v1.2.22：走字体缓存避免每帧重建
            p.setPen(color)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

        def _draw_icon_hover(self, p, cx, cy, pressed=False):
            """顶部操作区玻璃受光反馈：悬停轻亮、按下稍实，不改变图标位置或热区。"""
            bg = QColor(255, 255, 255,
                        HOVER_PRESSED_BG_ALPHA if pressed else HOVER_BG_ALPHA)
            stroke = QColor(255, 255, 255,
                            HOVER_PRESSED_STROKE_ALPHA if pressed else HOVER_STROKE_ALPHA)
            size = HOVER_SIZE + (2 if pressed else 0)
            p.setPen(QPen(stroke, 1))
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(cx - size / 2, cy - size / 2, size, size), HOVER_RADIUS, HOVER_RADIUS)

        def _draw_edit_icon(self, p, rect, disabled=False):
            if self._edit_pm is not None:
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                if disabled:
                    # 置灰态：整体降透明度，视觉上"变暗不可用"
                    p.save()
                    p.setOpacity(0.3)
                    p.drawPixmap(rect.toRect(), self._edit_pm)
                    p.restore()
                else:
                    p.drawPixmap(rect.toRect(), self._edit_pm)
                return
            # 回退：自绘放大镜图标，与正常 SVG 入口保持同一语义和视觉重量。
            scale = min(rect.width(), rect.height()) / 24.0
            left = rect.center().x() - 12.0 * scale
            top = rect.center().y() - 12.0 * scale
            color = QColor("#5a6072") if disabled else QColor("#9aa3b5")
            pen = QPen(color, ICON_SEARCH_WIDTH * scale, Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(left + 10.5 * scale, top + 10.5 * scale),
                          4.5 * scale, 4.5 * scale)
            p.drawLine(QPointF(left + 14.0 * scale, top + 14.0 * scale),
                       QPointF(left + 18.5 * scale, top + 18.5 * scale))

        def _draw_close_icon(self, p, rect):
            if self._close_pm is not None:
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                p.drawPixmap(rect.toRect(), self._close_pm)
                return
            # 回退：自绘叉号图标
            c = rect.center()
            r = ICON_CLOSE_R
            pen = QPen(QColor("#9aa3b5"), ICON_CLOSE_WIDTH, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawLine(QPoint(int(c.x() - r), int(c.y() - r)),
                       QPoint(int(c.x() + r), int(c.y() + r)))
            p.drawLine(QPoint(int(c.x() + r), int(c.y() - r)),
                       QPoint(int(c.x() - r), int(c.y() + r)))

        def _draw_min_icon(self, p, rect):
            if self._min_pm is not None:
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                p.drawPixmap(rect.toRect(), self._min_pm)
                return
            # 回退：自绘减号图标
            c = rect.center()
            pen = QPen(QColor("#9aa3b5"), ICON_MIN_WIDTH, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawLine(QPoint(int(c.x() - 4.5), int(c.y())),
                       QPoint(int(c.x() + 4.5), int(c.y())))

        # ---------- v6：目标价彩虹与铃铛 ----------
        def _rainbow_active(self):
            """目标价命中后的流动彩虹是否仍在播放。"""
            return self._anim_driver.rainbow_active(time.monotonic())

        def _rainbow_pen(self, rect):
            """C 宝石色谱闭环流动，并从当前行情色平滑进入和退出。"""
            now = time.monotonic()
            driver = self._anim_driver
            phase = driver.rainbow_phase(now)
            strength = driver.rainbow_strength(now)
            x0, y0, x1, y1 = rainbow_gradient_axis(rect, 0.03 - phase)
            grad = QLinearGradient(x0, y0, x1, y1)
            colors = ("#FF4269", "#FF8C42", "#F5CC48", "#20C98B",
                      "#12CDE0", "#4B83FF", "#B665F4")
            base = self._trend_color()
            # 重复首色闭合循环，避免相位回到零时跳色；只需八个原生色标。
            for index, value in enumerate((*colors, colors[0])):
                color = QColor(value)
                if strength == 0.0:
                    color = base
                elif strength < 1.0:
                    color = QColor.fromRgbF(*(
                        a * (1.0 - strength) + b * strength
                        for a, b in zip(base.getRgbF(), color.getRgbF())))
                grad.setColorAt(index / len(colors), color)
            grad.setSpread(QLinearGradient.Spread.RepeatSpread)
            return QPen(QBrush(grad), 1)

        def _draw_bell_icon(self, p, rect, state, disabled=False):
            """按用户提供的 Material Notifications SVG 绘制铃铛造型。

            继续使用现有 12×12 绘制框与按钮热区。待触发为金黄，已触发为灰色；
            没有目标时仍保持浅白灰，禁用时保持深灰。
            """
            if disabled:
                col = QColor("#5a6072")          # 互斥置灰
            elif state == "pending":
                col = QColor("#f5c542")          # 待触发：金黄
            elif state == "fired":
                col = QColor("#9aa2b8")          # 已触发：灰色
            else:
                col = QColor("#EFEFEF")          # 无目标：与右侧三图标一致
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawPath(material_bell_path(rect))
