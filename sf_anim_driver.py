# -*- coding: utf-8 -*-
"""sf_anim_driver —— 上帝类拆分第四步（v1.2.48）：动画驱动状态机
（由 QTimer 驱动的 UI 层动画帧逻辑）。

原 FloatWindow 上帝类中由三个 QTimer（_anim_timer / _flash_timer / _flash_drop_timer）
驱动的动画帧逻辑沉入本模块：价格滚动、破位白闪、下跌红闪、布局动画。
主窗仅保留薄委托调用（启动/停止 timer + 执行 UI 副作用），对外行为 100% 不变。

与 sf_animator（插值/判定纯函数）的边界：
- sf_animator：一帧的数值插值（price_interp / layout_anim_height）与触发判定
  （crossed_key_level / quote_changed / is_tiny_move / anim_duration_ms）—— 无状态。
- 本模块：持有动画驱动状态（from/to/start/dur/相位/冷却），把「QTimer 的一帧」
  转化为「动作列表」；UI 副作用（resize / update / 停表 / min-max 收口 /
  玻璃 mask 对齐）由主窗逐条执行。

本模块不 import Qt，依赖可注入（now / dur / phases / levels / cooldown 均由调用方
传入），便于脱离 Qt / QTimer 做纯逻辑单测（对齐 sf_runtime 的注入风格）。
"""

from sf_animator import layout_anim_height, price_interp
from sf_utils import _drop_flash_due


class AnimDriver(object):
    """动画驱动状态机：持有动画状态 + 帧决策，返回动作列表。

    动作协议（tuple 列表，主窗按序执行副作用）：
      ("resize", (w, h))            主窗 resize(w, h)
      ("update",)                   主窗 update()
      ("update_rect", (x,y,w,h))    主窗 update(区域)
      ("price", (p, pct))           落 _disp_price / _disp_pct
      ("price_finish",)             价格滚动完成：停 _anim_timer、清 _roll_pending/
                                    _partial_price、整窗 update
      ("price_mid", (chart_changed, width))  价格滚动中间帧：按 should_draw_chart
                                    决策局部条带重绘或整窗重绘
      ("layout_minmax", expanded)   布局动画完成帧：min/max 收口
      ("glass_mask",)               布局动画完成帧：强制对齐玻璃层圆角 mask
    """

    def __init__(self):
        # 价格滚动（原主窗 _anim_* 字段，__init__ 默认值逐位一致）
        self.anim_from_price = 0.0
        self.anim_to_price = 0.0
        self.anim_from_pct = 0.0
        self.anim_to_pct = 0.0
        self.anim_start = 0.0        # 动画开始时间（monotonic）
        self.anim_dur = 0.2          # 动画时长（秒）
        self.anim_decimals = 2       # 价格小数位数
        # 破位白闪（原主窗 _price_flash_* 字段）
        self.price_flash_until = 0.0  # 闪烁截止时刻
        self.price_flash_on = False   # 闪烁相位：交替显示高亮/正常色
        # 下跌红闪（原主窗 _flash_drop_* 字段）
        self.flash_drop_last = {}     # {档位key: 上次触发时刻}，缺省 None 表示从未触发
        self.flash_drop_on = False    # 当前是否处于下跌闪烁相位
        self.flash_drop_count = 0     # 已交替相位计数
        # 布局动画（原主窗 _layout_anim_* 字段）
        self.layout_anim_from = 0     # 布局动画起始高度（0 表示无进行中动画）
        self.layout_anim_to = 0       # 布局动画目标高度
        self.layout_anim_start = 0.0  # 布局动画开始时间（monotonic）
        # 「目标价」彩虹高亮：7 色彩虹仅作用于价格大字，6 秒 ≈ 180 帧。
        # 这是一次性运行时提醒，不写入配置，切股时必须复位。
        self.rainbow_start = 0.0
        self.rainbow_until = 0.0

    # ---- 布局动画启动（原 _toggle_chart 尾部字段赋值） ----------------------
    def begin_layout(self, from_h, to_h, now):
        """登记一次收展布局动画（180ms ease-out，复用 _anim_timer 驱动）。"""
        self.layout_anim_from = from_h
        self.layout_anim_to = to_h
        self.layout_anim_start = now

    # ---- 拉伸手势接管高度轴时，收敛在飞的收展动画 ---------------------------
    def settle_layout_for_stretch(self, chart_expanded):
        """把进行中的收展布局动画就地收敛到终态，返回这一拍需要补发的动作。

        为什么必须有这一步（v1.4.34）：展开分支在动画起飞时把 `minimumHeight()`
        **主动**降到 0（不降会被 MIN_H 钳住、收展过渡不连续），而恢复它的那一帧正是
        布局动画的完成帧。旧实现里拉伸手势无条件 `return`，把完成帧无限期挤掉 ——
        于是"上拖能把窗口压到 0 高"的窗口期从 200ms 拉长成整段手势；再丢一次
        mouseRelease（macOS 置顶/跨 Space 偶发）就连同停表一起永久化，0 高窗口没有
        可点区域，鼠标救不回来。

        收敛只动**动画状态与 min/max**，不动高度：高度这一拍仍由拉伸目标说了算。
        """
        if self.layout_anim_to == self.layout_anim_from:
            return []
        self.layout_anim_from = 0
        self.layout_anim_to = 0
        return [("layout_minmax", chart_expanded)]

    # ---- 主帧驱动（原 _anim_tick_inner 决策部分） ---------------------------
    def tick(self, resizing, resize_target_h, height, width,
             chart_expanded, chart_changed, now,
             disp_price, disp_pct,
             layout_dur=0.20):
        """推进一帧，返回待执行动作列表（顺序即原 _anim_tick_inner 副作用顺序）。

        分支与拆分前逐位一致：
        1. 拉伸帧合并 —— _resizing 期间由本定时器以稳定帧率（16ms）单次应用目标高度；
        2. 布局动画帧 —— 高度插值 + 完成帧状态归零/min-max 收口/玻璃 mask 对齐；
        3. 价格滚动 guard —— 无进行中滚动（from==to 且 pct 相等）直接跳过，
           绝不覆盖 _disp_price（v1.2.38 冷启动 "--" 修复语义）；
        4. 价格插值帧 —— 落值 + 完成帧停表/清滚动目标，中间帧按图表变化决策重绘。
        """
        actions = []
        # 1) 拉伸帧合并：若当前帧已达到目标高度则跳过（松开时由 mouseReleaseEvent
        #    补齐最后一步并停止定时器）
        if resizing and resize_target_h is not None:
            # v1.4.34：拉伸手势与收展动画抢的是同一根高度轴。此前本分支无条件
            # `return actions`，把布局动画的**完成帧**（正是恢复 minimumHeight 的那一帧）
            # 无限期挤掉，实测能把主窗拖到 height=0 并永久停在那儿。
            # 现在先就地收敛动画、照常补发 min/max 收口，高度仍归拉伸说了算。
            actions.extend(self.settle_layout_for_stretch(chart_expanded))
            if height != resize_target_h:
                actions.append(("resize", (width, resize_target_h)))
                actions.append(("update",))
            elif actions:
                actions.append(("update",))
            return actions
        # 2) 布局动画帧：布局和价格动画分别只操作高度与数字，能够同帧推进。
        # v1.2.72 第6轮走查：旧逻辑在布局期间直接 return，价格动画的时钟仍在
        # 走，收展结束后下一帧直接 t>=1 落到目标，用户看到价格/涨跌幅跳变。
        # 现在保留布局动作并继续进入价格分支；完成帧的 min/max 与 glass 收口不变。
        if self.layout_anim_to != self.layout_anim_from:
            h, fin = layout_anim_height(self.layout_anim_from, self.layout_anim_to,
                                        self.layout_anim_start, layout_dur, now)
            if fin:
                self.layout_anim_from = 0
                self.layout_anim_to = 0
                # v1.2.35/37：完成帧统一收口 min/max —— 展开态恢复 MIN_H/MAX_H，
                # 收起态把 max 锁回收起高度（防系统缩放把手复活）
                actions.append(("layout_minmax", chart_expanded))
            actions.append(("resize", (width, h)))
            actions.append(("update",))
            if fin:
                # v1.2.26：动画结束帧强制对齐玻璃层圆角 mask（此刻 from/to 已归零，
                # resize 已落到最终高度，force_mask 一次即与主窗精确对齐）
                actions.append(("glass_mask",))
        # 3) 价格滚动 guard（v1.2.38）
        if (self.anim_to_price == self.anim_from_price
                and self.anim_to_pct == self.anim_from_pct):
            return actions
        # 4) 价格插值帧
        new_price, new_pct, fin = price_interp(
            disp_price, disp_pct, self.anim_from_price, self.anim_to_price,
            self.anim_from_pct, self.anim_to_pct, self.anim_start, self.anim_dur,
            now)
        actions.append(("price", (new_price, new_pct)))
        if fin:
            actions.append(("price_finish",))
        else:
            # v1.2.43 第2/4项：中间帧局部重绘 vs 整窗重绘的决策交给主窗
            # （图表数据/几何未变 → 只重绘价格条带，分时图每 tick 零重绘）
            actions.append(("price_mid", (chart_changed, width)))
        return actions

    # ---- 破位白闪（原 _start_price_flash / _flash_tick 决策部分） -----------
    def start_price_flash(self, now, dur=0.6):
        """记录一次破位白闪起始（截止时刻 + 起始相位亮）。"""
        self.price_flash_until = now + dur
        self.price_flash_on = True

    def flash_tick(self, now):
        """推进一个破位白闪相位。

        返回 (active, on)：
        - 已到期 → (False, False)：主窗停表并整窗重绘恢复正确色；
        - 未到期 → (True, **本帧相位**)：主窗按该相位显示并重绘。

        v1.2.65 第1轮走查修复：原实现「先翻转再返回」，导致 start_price_flash
        置亮的起始相位在第一个 tick 就被翻成灭 —— 首帧白闪被吞（0.6s 内少闪
        一次，且提醒出现晚一个 tick）。改为「先返回当前相位，再翻转供下一帧」，
        首帧即为亮，与 start_price_flash 的「起始相位亮」语义一致。
        """
        if now >= self.price_flash_until:
            self.price_flash_on = False
            return False, False
        on = self.price_flash_on
        self.price_flash_on = not on
        return True, on

    # ---- 下跌红闪（原 _maybe_trigger_drop_flash/_start_drop_flash/_drop_flash_tick）-
    def check_drop_flash(self, pct, now, levels, cooldown):
        """遍历跌幅档位判定阈值穿越，命中则更新该档冷却并返回 True。

        基准为涨跌幅为负时的绝对值；两档独立判断、独立冷却。
        """
        hit = False
        # 命中后不 break：各档冷却互相独立，必须让每个已穿越的档位都记下本次时刻，
        # 否则只更新最先命中的那档，其余档下一帧仍被判为"到期"而重复触发。
        for lv in levels:
            key, threshold = lv["key"], lv["pct"]
            if _drop_flash_due(pct, threshold,
                               self.flash_drop_last.get(key),
                               now, cooldown):
                self.flash_drop_last[key] = now
                hit = True
        return hit

    def start_drop_flash(self):
        """启动下跌闪烁状态：正常色与下跌红交替 FLASH_DROP_PHASES 个相位。"""
        self.flash_drop_on = True
        self.flash_drop_count = 0

    def drop_flash_tick(self, phases):
        """推进一个下跌闪烁相位。

        返回 (active, on)：
        - 相位用尽 → (False, False)：主窗停表恢复 _trend_color() 正确色；
        - 未用尽 → (True, **本帧相位**)：主窗按该相位显示并重绘。

        v1.2.65 第1轮走查修复：原实现「先翻转再返回」使 start_drop_flash 置亮的
        起始相位在第一个 tick 就被翻成灭。FLASH_DROP_PHASES=6 的设计语义是
        「亮-灭-亮-灭-亮-灭 = 3 个来回」，实际却只闪了 2 次红
        （实测序列 灭→亮→灭→亮→灭→停）。改为「先返回当前相位，再翻转供下一帧」，
        序列恢复为 亮→灭→亮→灭→亮→停，即设计的 3 次红闪。
        """
        self.flash_drop_count += 1
        if self.flash_drop_count >= phases:
            self.flash_drop_on = False
            return False, False
        on = self.flash_drop_on
        self.flash_drop_on = not on
        return True, on

    # ---- 复位（原 apply_stock 切股/退出时的动画状态复位） --------------------
    def reset_flash(self):
        """切股：复位破位白闪与下跌红闪（截止/相位/计数/冷却字典）。"""
        self.price_flash_until = 0.0
        self.price_flash_on = False
        self.flash_drop_on = False
        self.flash_drop_count = 0
        self.flash_drop_last = {}
        self.reset_rainbow()

    # ---- 目标价流动彩虹 ----
    def start_rainbow(self, now, dur=6.0):
        """启动或续期一次目标价命中后的流动彩虹提醒。"""
        self.rainbow_start = float(now)
        # max(..., 0.0)：负时长会把 until 顶到 now 之前，彩虹"启动即结束"，
        # 定时器收不到任何一帧，表现为目标价命中后完全无提醒。
        self.rainbow_until = float(now) + max(float(dur), 0.0)

    def rainbow_active(self, now):
        """彩虹是否仍在播放；到期后由窗口停止定时器并恢复普通行情颜色。"""
        return float(now) < self.rainbow_until

    def rainbow_phase(self, now, cycles=4.0):
        """返回 0~1 的流动相位；默认在 6 秒内完成 4 次右下到左上的色带扫动。"""
        if not self.rainbow_active(now):
            return 0.0
        duration = self.rainbow_until - self.rainbow_start
        if duration <= 0:
            return 0.0
        return ((float(now) - self.rainbow_start) / duration * float(cycles)) % 1.0

    def reset_rainbow(self):
        """停止目标价彩虹提醒，用于切换股票和退出。"""
        self.rainbow_start = 0.0
        self.rainbow_until = 0.0
