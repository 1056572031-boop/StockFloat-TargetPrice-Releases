# -*- coding: utf-8 -*-
"""sf_animator —— 上帝类 FloatWindow 拆分第二步①（v1.2.40）：动画状态机纯逻辑。

背景：stock_float_window.py 的 FloatWindow 上帝类拆分规划（见该类 docstring 与
sf_scheduler.py 模块 docstring）方向①「动画状态机（价格滚动/闪烁/窗口动画）」。

v1.2.40 完成：把价格/涨跌幅插值（_anim_tick_inner 数值分支）、收展布局高度插值
（_anim_tick_inner 布局分支）、跌破整数关口判定（_crossed_key_level）、微小变动/
动画时长/变化守卫等 *纯逻辑* 抽入本模块。主窗保留状态字段（_disp_* / _anim_*）、
QTimer 与重绘调用，仅作薄委托；行为与拆分前逐位一致。Qt 渲染仍在主窗
RenderMixin，本模块不 import 任何 Qt 渲染相关对象，保证可脱离 Qt 做纯单测。
"""


def ease_out_cubic(t):
    """ease-out cubic：先快后慢。原 _anim_tick_inner 数值插值所用 e=1-(1-t)^3。"""
    return 1.0 - (1.0 - t) ** 3


def smoothstep_inout_cubic(t):
    """smoothstep InOutCubic（3t^2-2t^3）：起终点速度均为零，无速度突变。
    v1.2.19 布局动画（收展分时图改窗口高度）所用平滑插值。"""
    return t * t * (3.0 - 2.0 * t)


def price_interp(disp_price, disp_pct, anim_from_price, anim_to_price,
               anim_from_pct, anim_to_pct, anim_start, anim_dur, now):
    """价格/涨跌幅插值一帧（纯逻辑，原 _anim_tick_inner 数值分支搬移）。

    命名（AGENTS §7 词表；2026-10-13 由 price_tick 改为此名）：本函数算的是**一帧的数值插值**，
    所以用 interp。三个词各管一件事，不许再互相顶：`tick` 只给时钟节拍/定时器，`interp` 给
    逐帧数值插值，`flash` 已经归**破位白闪**（sf_anim_driver 的 start_price_flash /
    price_flash_until / price_flash_on）—— 当初照词表把这里改成 price_flash 正好会撞上它，
    那才是词表真正的失败案例，所以 §7 那一行已经改写得更细。
    改名前实测过动态引用：全仓没有 getattr / 字符串形式按名取用，引用点只有 sf_anim_driver
    （导入 + 逐帧调用）与 tests/test_animator.py（按名直调），一并改完；`backups/` 里那几处是
    冻结历史快照，按 AGENTS §5 不追改。

    返回 (new_disp_price, new_disp_pct, finished)：
    - 无进行中滚动（price 与 pct 均是 from==to）时原样返回当前值、finished=True，
      调用方据此跳过收尾、绝不覆盖 _disp_price（v1.2.38 guard 语义）；
    - t>=1.0 落定到动画目标值，finished=True（调用方应停止定时器并清空 _roll_pending）；
    - 否则 ease-out cubic 插值，finished=False。
    数值公式与拆分前完全一致，保证逐帧显示不漂移。
    """
    if (anim_to_price == anim_from_price
            and anim_to_pct == anim_from_pct):
        return disp_price, disp_pct, True
    # v1.2.95 第9轮走查：anim_dur<=0（配置错误或动画被打断）会除零崩溃。
    # 视为动画已结束，直接落定到目标值，调用方据此停止定时器。
    if anim_dur <= 0:
        return anim_to_price, anim_to_pct, True
    t = (now - anim_start) / anim_dur
    if t >= 1.0:
        return anim_to_price, anim_to_pct, True
    e = ease_out_cubic(t)
    return (anim_from_price + (anim_to_price - anim_from_price) * e,
            anim_from_pct + (anim_to_pct - anim_from_pct) * e, False)


def layout_anim_height(anim_from, anim_to, anim_start, dur, now):
    """布局（收展分时图改窗口高度）插值一帧（纯逻辑，原 _anim_tick_inner 布局分支）。

    返回 (height, finished)：finished=True 时返回目标高度 anim_to，调用方应把
    from/to 归零并统一收口 min/max（展开恢复 MIN_H/MAX_H、收起锁回收起高度）；
    否则返回 smoothstep 插值高度（向上取整到 int）。    仅当存在进行中布局动画
    （anim_to != anim_from）时调用，行为与拆分前一致。"""
    # v1.2.95 第9轮走查：dur<=0 会除零崩溃，视为动画已结束直接返回目标高度。
    if dur <= 0:
        return anim_to, True
    t = (now - anim_start) / dur
    if t >= 1.0:
        return anim_to, True
    e = smoothstep_inout_cubic(t)
    return int(anim_from + (anim_to - anim_from) * e), False


def crossed_key_level(old_price, new_price):
    """向下跌破过程中是否跨过重要整数关卡（50 的整数倍：50/100/150/200/...）。
    只关心向下区间 (new, old] 内是否存在 50 的倍数；普通整数位（如 410）不触发。
    原 FloatWindow._crossed_key_level 纯逻辑搬移。"""
    if new_price >= old_price:
        return False
    oi = int(old_price)
    ni = int(new_price)
    # 截断取整后仍落在同一整数内（如 409.8 → 409.2）不可能跨过 50 的整数倍；
    # 只有整数位确实下降时 oi//50 与 ni//50 才可能不同，故提前返回。
    if ni >= oi:
        return False
    return (oi // 50) > (ni // 50)


def quote_changed(new_price, new_pct, disp_price, disp_pct, eps=1e-9):
    """行情相对当前显示值是否发生变化（任一维度超过 1e-9 即视为变化）。
    原 _apply_quote 进入动画分支的守卫
    `abs(new_price - disp_price) > 1e-9 or abs(new_pct - disp_pct) > 1e-9` 搬移。"""
    return abs(new_price - disp_price) > eps or abs(new_pct - disp_pct) > eps


def is_tiny_move(delta_price, delta_pct, tiny_price):
    """微小变动判定：价格与涨跌幅变动均小于微小阈值时直接赋值不动画。
    原 _apply_quote `if delta_price < ANIM_TINY_PRICE and delta_pct < ANIM_TINY_PRICE`。"""
    # 价格（元）与涨跌幅（百分点）量纲不同却共用同一个 tiny_price 阈值：这是沿用
    # 拆分前原实现的语义，拆开会让"小幅波动不动画"的命中范围变化，属行为变更。
    return delta_price < tiny_price and delta_pct < tiny_price


def anim_duration_ms(delta_pct, large_pct, large_ms, normal_ms):
    """动画时长决策（毫秒）：涨跌幅变动达到 `large_pct` 时用 `large_ms`，
    其余变动用 `normal_ms`。时长由调用方的常量传入，避免在此固化数值。"""
    return large_ms if delta_pct >= large_pct else normal_ms
