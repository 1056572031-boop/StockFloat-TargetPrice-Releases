# -*- coding: utf-8 -*-
"""实时股价 v1.2.65 回归测试：第 1 轮全面走查修复项。

本轮修复（由此文件锁定，防止回退）：

1. 收起态高度少算 3px —— 高度公式用「纯数字的视觉高(7)」充当「含汉字行的字形
   高(10)」，导致「价格区底 → 底部行1 顶」的实测间隙只有 6px（设计 8px）。
   修复：公式下沉为 sf_ui_render.collapsed_height() 唯一实现，用 BOTTOM_ROW_ASCENT。
2. 收起分时图的过渡动画被吞 —— 收起分支在取动画起点前就 setMaximumHeight(target)，
   Qt 的 min/max 会**同步**钳制几何高度，起点被压成终点（from==to），driver 守卫
   不成立 → 高度瞬间跳变、无过渡，且收口动作永不发出。
3. 16ms 动画定时器永久空转 —— driver.tick 的四个出口只有「价格完成帧」会停表，
   其余空动作路径没有停表分支；行情静止时（夜间/周末/停牌）以约 62Hz 纯空转。
4. 拔掉外接屏后窗口永久消失 —— _clamp_to_screen 用一次性闸门且闸门在 try 前置位，
   屏幕热插拔后无从复位；_keep_on_screen 又只修右/上两个方向。
5. 下跌红闪少闪一次 / 破位白闪首帧被吞 —— 「先翻转再返回」把起始相位吃掉。
   （断言见 tests/test_anim_driver.py）
6. NSLog 把日志原文当格式串 —— 日志含 '%' 时读栈垃圾/乱码/崩溃。

运行方式（在 StockFloat 目录）：
    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_v165 -v
"""
import ast
import os
import re
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_ui_render as R
import stock_float_window as m
import sf_quote
import sf_utils

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fake_http_get(url, **kw):
    """离线行情/分时 mock，避免测试访问网络。"""
    if "hq.sinajs.cn" in url:
        return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
    if "web.ifzq.gtimg.cn" in url:
        return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                '"data":["0930 1510.0 100 1000", "1000 1511.0 200 2000"]}}}}')
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


class _WinCase(unittest.TestCase):
    """创建离屏主窗的公共夹具（与 test_v164 同构）。"""

    @classmethod
    def setUpClass(cls):
        # 单跑本模块时没有 QApplication，QWidget 构造会直接 abort；全量套件里
        # 由其他模块先行创建，此处取已有实例即可（与 test_v164 同构）。
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._saved = []
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", _fake_http_get),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        self.win._expanded_h = 0      # 必须为 int：_toggle_chart 会 max(from_h, _expanded_h)
        self.win._anim_timer.stop()

    def tearDown(self):
        try:
            self.win._anim_timer.stop()
        except Exception:
            pass
        self.win.close()
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        for p in self._patches:
            p.stop()

    # ---- 工具 -------------------------------------------------------
    def _enter_expanded(self):
        """把窗口置于展开态真实几何，返回展开高度。"""
        w = self.win
        w._chart_expanded = True
        h_exp = w._height_expanded()
        w.setMinimumHeight(0)
        w.setMaximumHeight(m.MAX_H)
        w.resize(m.W, h_exp)
        self.assertEqual(w.height(), h_exp, "未进入展开态几何，用例前提不成立")
        return h_exp


# =====================================================================
# 1. 收起态高度：价格区底 → 底部行1 顶 的间隙必须是设计值
# =====================================================================
class TestCollapsedHeightGap(_WinCase):
    """纯算术，不经过 QFontMetrics —— 与运行平台无关。"""

    def test_gap_equals_design_constant(self):
        """核心回归：间隙必须等于 PRICE_TO_BOTTOM_GAP(8)，不得是 6。"""
        h = R.collapsed_height()
        price_bottom = R.Y_PRICE + R.H_PRICE
        row1_top = (h - R.BOTTOM_BASE_HIDDEN - R.BOTTOM_ROW_H
                    - R.BOTTOM_ROW_GAP - R.BOTTOM_ROW_ASCENT)
        self.assertEqual(row1_top - price_bottom, R.PRICE_TO_BOTTOM_GAP,
                         "价格区底到行1 顶的间隙应为 %d，实测 %d"
                         % (R.PRICE_TO_BOTTOM_GAP, row1_top - price_bottom))

    def test_uses_ascent_not_cap_height(self):
        """高度公式必须用 BOTTOM_ROW_ASCENT（含汉字行），不能用数字 cap height。

        v1.2.64 的 COLLAPSED_ROW_H=7 就是 cap height：它只适用于纯数字，而底部行1
        内容是「最大涨幅 +1.23%」这类**含汉字**文本，字形顶由 ascent=10 决定。
        """
        self.assertGreater(R.BOTTOM_ROW_ASCENT, 7,
                           "含汉字行的 ascent 应大于纯数字 cap height(7)")
        # 7 → 10 的差额（3px）正是历史亏算量
        self.assertEqual(R.BOTTOM_ROW_ASCENT - 7, 3)

    def test_no_legacy_collapsed_row_h(self):
        """COLLAPSED_ROW_H / COLLAPSED_GAP 两个死常量应已被移除（不得再被赋值）。"""
        with open(os.path.join(ROOT, "stock_float_window.py"), encoding="utf-8") as f:
            src = f.read()
        # 只查「真实引用」不查文字：历史说明（注释与 docstring）里仍会提到它们的
        # 名字，故按 AST 收集标识符，而不是对源码做字符串匹配。
        names = {n.id for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Name)}
        self.assertNotIn("COLLAPSED_ROW_H", names)
        self.assertNotIn("COLLAPSED_GAP", names)

    def test_window_collapsed_height_equals_pure_function(self):
        self.assertEqual(self.win._height_collapsed(), R.collapsed_height())
        self.assertEqual(self.win._height_expanded(),
                         R.collapsed_height() + m.CHART_H)


# =====================================================================
# 2. 收起动画不得被吞（起点必须早于 min/max 钳制前捕获）
# =====================================================================
class TestCollapseAnimationNotSwallowed(_WinCase):

    def test_collapse_has_nonzero_anim_delta(self):
        """核心回归：收起时 from != to，动画真的会跑（而非瞬间跳变）。"""
        h_exp = self._enter_expanded()
        self.win._toggle_chart()
        self.assertFalse(self.win._chart_expanded)
        self.assertEqual(self.win._layout_anim_from, h_exp,
                         "动画起点应为收起前的真实高度（不得被 min/max 钳制污染）")
        self.assertEqual(self.win._layout_anim_to, self.win._height_collapsed())
        self.assertNotEqual(self.win._layout_anim_from, self.win._layout_anim_to,
                            "from==to 说明动画被吞（v1.2.64 及以前的缺陷）")

    def test_max_opened_during_collapse_anim(self):
        """动画期间上限必须放开，否则中间帧（>target）会被 max 钳住，过渡卡住。"""
        self._enter_expanded()
        self.win._toggle_chart()
        self.assertEqual(self.win.maximumHeight(), m.MAX_H,
                         "收起动画期间 max 应放开到 MAX_H，完成帧再锁回")

    def test_collapse_finish_locks_max(self):
        """完成帧必须把 max 锁回收起高度（v1.2.36/37 防系统缩放把手复活）。"""
        h_exp = self._enter_expanded()
        self.win._toggle_chart()
        # 手动把动画推到已完成（起始时刻回拨到动画时长之外）
        self.win._anim_driver.layout_anim_start = time.monotonic() - 10.0
        self.win._anim_tick()
        self.assertEqual(self.win.height(), self.win._height_collapsed(),
                         "完成帧应把几何落到收起高度（曾卡在 %d）" % h_exp)
        self.assertEqual(self.win.maximumHeight(), self.win._height_collapsed(),
                         "完成帧应把 max 锁回收起高度")
        self.assertEqual(self.win._layout_anim_from, 0)
        self.assertEqual(self.win._layout_anim_to, 0)

    def test_expand_still_animates(self):
        """展开分支不能被这次改动带坏。"""
        self._enter_expanded()
        self.win._toggle_chart()                      # → 收起
        self.win._anim_tick()
        h_col = self.win.height()
        self.win._toggle_chart()                      # → 展开
        self.assertTrue(self.win._chart_expanded)
        self.assertEqual(self.win._layout_anim_from, h_col)
        self.assertGreater(self.win._layout_anim_to, h_col)

    def test_immediate_reverse_before_first_frame_locks_collapsed_height(self):
        """连续快点：展开后不等首帧立刻收起，最终仍必须锁定收起高度。

        旧逻辑：展开刚登记、实际高度尚未变化时立即反向，收起得到 from==to。
        driver 不会发出 layout_minmax 收尾，max 留在 MAX_H；窗口看似收起了，
        但高度锁失效，连续点击后会出现可异常拉高/状态错乱。
        """
        w = self.win
        ch = w._height_collapsed()
        w.setMinimumHeight(ch)
        w.setMaximumHeight(ch)
        w.resize(m.W, ch)
        w._chart_expanded = False
        w._toggle_chart()             # 逻辑展开，尚未跑动画帧，几何仍为 ch
        self.assertTrue(w._chart_expanded)
        w._toggle_chart()             # 立即反向：旧实现产生 from==to
        self.assertFalse(w._chart_expanded)
        w._anim_tick_inner()
        self.assertEqual(w.height(), ch)
        self.assertEqual(w.maximumHeight(), ch,
                         "快速反向点击后收起态必须重新锁定高度")

    def test_repeated_reverse_ends_in_consistent_geometry(self):
        """连续多次切换后，以最后一次意图为准，几何与状态必须一致。"""
        w = self.win
        ch = w._height_collapsed()
        w.setMinimumHeight(ch)
        w.setMaximumHeight(ch)
        w.resize(m.W, ch)
        w._chart_expanded = False
        # 连续 4 次（偶数）点击，最终回到收起态；中间刻意不给动画帧。
        for _ in range(4):
            w._toggle_chart()
        w._anim_tick_inner()
        self.assertFalse(w._chart_expanded)
        self.assertEqual(w.height(), ch)
        self.assertEqual(w.maximumHeight(), ch)
        self.assertEqual(w._minute_interval, m.CHART_COLLAPSED_MINUTE_INTERVAL)

    def test_reverse_mid_animation_uses_current_height_and_finishes(self):
        """动画跑到一半再连续反向，最终高度、锁定和刷新频率仍与末次点击一致。"""
        w = self.win
        h_exp = self._enter_expanded()
        ch = w._height_collapsed()
        # 第一次收起，推进到中间高度。
        w._toggle_chart()
        w._anim_driver.layout_anim_start = time.monotonic() - (m.LAYOUT_ANIM_DUR / 2)
        w._anim_tick_inner()
        mid = w.height()
        self.assertGreater(mid, ch)
        self.assertLess(mid, h_exp)
        # 中途反向展开，再推进一帧；应从当前中间高度继续，而非跳回旧起点。
        w._toggle_chart()
        self.assertTrue(w._chart_expanded)
        self.assertEqual(w._layout_anim_from, mid)
        w._anim_driver.layout_anim_start = time.monotonic() - 10
        w._anim_tick_inner()
        self.assertEqual(w.height(), h_exp)
        self.assertEqual(w.maximumHeight(), m.MAX_H)
        # 再收起完成，收起态必须重新锁死。
        w._toggle_chart()
        w._anim_driver.layout_anim_start = time.monotonic() - 10
        w._anim_tick_inner()
        self.assertFalse(w._chart_expanded)
        self.assertEqual(w.height(), ch)
        self.assertEqual(w.maximumHeight(), ch)
        self.assertEqual(w._minute_interval, m.CHART_COLLAPSED_MINUTE_INTERVAL)


# =====================================================================
# 3. 16ms 定时器不得在无事可做时空转
# =====================================================================
class TestAnimTimerStopsWhenIdle(_WinCase):

    def _make_idle(self):
        w = self.win
        w._resizing = False
        w._resize_target_h = None
        w._anim_driver.layout_anim_from = 0
        w._anim_driver.layout_anim_to = 0
        w._disp_price = 0.0
        w._disp_pct = 0.0
        w._anim_from_price = w._anim_to_price = 0.0
        w._anim_from_pct = w._anim_to_pct = 0.0

    def test_idle_tick_stops_timer(self):
        """核心回归：无任何进行中动画的一帧必须停表。"""
        self._make_idle()
        self.win._anim_timer.start()
        self.assertTrue(self.win._anim_timer.isActive())
        self.win._anim_tick_inner()
        self.assertFalse(self.win._anim_timer.isActive(),
                         "空闲帧未停表 —— 行情静止时会以约 62Hz 永久空转耗电")

    def test_stops_after_layout_anim_completes(self):
        """布局动画跑完后，下一帧应停表（覆盖「收起动画被吞」的永久空转）。"""
        self._enter_expanded()
        self.win._toggle_chart()
        self.win._anim_driver.layout_anim_start = time.monotonic() - 10.0
        self.win._anim_tick()          # 完成帧：resize + 收口
        self._make_idle()              # 清掉价格动画残留，只留布局已归零的状态
        self.win._anim_timer.start()
        self.win._anim_tick_inner()    # 空闲帧
        self.assertFalse(self.win._anim_timer.isActive())

    def test_active_price_anim_keeps_timer(self):
        """有进行中价格动画时绝不能停表（防止反向误伤）。"""
        w = self.win
        self._make_idle()
        w._anim_from_price = 10.0
        w._anim_to_price = 20.0
        w._anim_start = time.monotonic()
        w._anim_dur = 0.2
        w._anim_timer.start()
        w._anim_tick_inner()
        self.assertTrue(w._anim_timer.isActive(), "价格动画进行中不应停表")

    def test_roll_tail_keeps_timer_after_price_finish(self):
        """价格插值结束但逐位滚动未结束时，定时器必须继续驱动重绘。"""
        w = self.win
        self._make_idle()
        w._roll_anim = {"price": {0: ["1", "2", time.monotonic(), 0.18]}}
        w._anim_timer.start()
        updates = []
        w.update = lambda *args: updates.append(args)
        w._anim_tick_inner()
        self.assertTrue(w._anim_timer.isActive(), "逐位滚动尾帧期间不应提前停表")
        self.assertTrue(updates, "逐位滚动尾帧应请求重绘")
        w._roll_anim = {}
        w._anim_tick_inner()
        self.assertFalse(w._anim_timer.isActive(), "逐位滚动结束后应停表")


# =====================================================================
# 4. 窗口被甩出屏幕后必须能拉回来
# =====================================================================
class TestOffscreenRecovery(_WinCase):

    def _screen_geom(self):
        from PyQt6.QtGui import QGuiApplication
        return QGuiApplication.primaryScreen().availableGeometry()

    def test_gate_blocks_without_force(self):
        """闸门已消耗时，普通调用应直接 return（保持既有行为）。"""
        w = self.win
        ag = self._screen_geom()
        w._clamped = True
        w.move(ag.right() + 400, ag.top())
        w._clamp_to_screen()
        self.assertEqual(w.x(), ag.right() + 400, "无 force 时闸门应生效")

    def test_force_recovers_window(self):
        """核心回归：force=True 必须绕过一次性闸门把窗口拉回屏幕内。"""
        w = self.win
        ag = self._screen_geom()
        w._clamped = True                 # 模拟「启动时已消耗闸门」
        w.move(ag.right() + 400, ag.top())
        self.assertFalse(ag.intersects(w.frameGeometry()), "用例前提：窗口应在屏幕外")
        w._clamp_to_screen(force=True)
        self.assertTrue(ag.intersects(w.frameGeometry()),
                        "force=True 未把窗口拉回屏幕内 —— 拔屏后窗口会永久消失")

    def test_keep_on_screen_fixes_left_edge(self):
        w = self.win
        ag = self._screen_geom()
        w.move(ag.left() - 300, ag.top() + 50)
        w._keep_on_screen()
        self.assertGreaterEqual(w.x(), ag.left(), "左边缘越界未被修回")

    def test_keep_on_screen_fixes_bottom_edge(self):
        """下边缘是 v1.2.64 及以前完全没修的方向。"""
        w = self.win
        ag = self._screen_geom()
        w.move(ag.left() + 50, ag.bottom() + 300)
        w._keep_on_screen()
        self.assertLessEqual(w.y() + w.height(), ag.bottom() + 1,
                             "下边缘越界未被修回 —— 拔掉外接屏后窗口会从下方消失")


# =====================================================================
# 5. 交易所判定：沪市 ETF 不得被误判成深市（用户可见：永久 "--"）
#
# 实测（2026-09-02 对腾讯行情接口）：
#   sh510300 → 正常返回「沪深300ETF华泰柏瑞」
#   sz510300 → 返回 v_pv_none_match="1"（空）→ 界面永久 "--"
#   sh588000 → 正常   sz588000 → 空
#   sz159915 → 正常   sh159915 → 空
# =====================================================================
class TestMarketPrefixRouting(unittest.TestCase):
    """纯函数，不访问网络。"""

    def test_shanghai_etf_is_sh(self):
        """核心回归：5 开头的沪市 ETF 必须判为 sh（旧兜底一律给 sz）。"""
        for code in ("510300", "588000", "512880", "510050"):
            self.assertEqual(sf_utils.market_prefix(code), "sh", code)
            self.assertEqual(sf_quote._tencent_code(code), "sh" + code, code)
            self.assertEqual(sf_utils.normalize_code(code), "sh" + code, code)

    def test_shenzhen_etf_is_sz(self):
        for code in ("159915", "159941", "161725"):
            self.assertEqual(sf_utils.market_prefix(code), "sz", code)
            self.assertEqual(sf_quote._tencent_code(code), "sz" + code, code)

    def test_stocks_unchanged(self):
        """既有股票代码判定不得被这次改动带坏。"""
        self.assertEqual(sf_quote._tencent_code("600519"), "sh600519")
        self.assertEqual(sf_quote._tencent_code("688981"), "sh688981")
        self.assertEqual(sf_quote._tencent_code("000001"), "sz000001")
        self.assertEqual(sf_quote._tencent_code("300750"), "sz300750")

    def test_bj_920_is_bj(self):
        """920xxx 北交所新段必须早于 9→sh 判定（顺序一变即错）。"""
        self.assertEqual(sf_utils.market_prefix("920001"), "bj")
        self.assertEqual(sf_quote._tencent_code("920001"), "bj920001")

    def test_empty_and_none_fallback_preserved(self):
        """无法判定时仍回退 sz —— 旧行为，含空串与 None 入参。"""
        self.assertEqual(sf_quote._tencent_code(""), "sz")
        self.assertEqual(sf_quote._tencent_code(None), "sz")

    def test_non_a_share_untouched(self):
        """非 A 股形式必须原样返回，不能被硬加 sz 前缀。"""
        for raw in ("usAAPL", "hk00700", "xx123456", ""):
            self.assertEqual(sf_utils.normalize_code(raw), raw.strip().lower(), raw)

    def test_bj_4digit_padding_preserved(self):
        self.assertEqual(sf_quote._tencent_code("bj8307"), "bj830700")
        self.assertEqual(sf_quote._tencent_code("bj830700"), "bj830700")
        self.assertEqual(sf_quote._tencent_code("bjabc"), "bjabc")

    def test_single_implementation(self):
        """交易所判定只能有一份实现（防止再次双份漂移）。"""
        with open(os.path.join(ROOT, "sf_quote.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("market_prefix", src,
                      "sf_quote 应复用 sf_utils.market_prefix，不得自己判一遍")
        # 兜底表达式 `sh if c[:1] in ("6","9") else sz` 不应再出现
        self.assertNotIn('"sh" if c[:1] in', src.replace("'", '"'))


# =====================================================================
# 7. 退出收尾：非按钮路径（Cmd+Q / Dock 退出 / 系统关闭）也必须写盘
#
# macOS 上 Cmd+Q 走 QApplication::quit()，它**不投递 closeEvent**（窗口没被关，
# 只是事件循环退出），所以两条钩子缺一不可。
# =====================================================================
class TestLimitPriceFallback(unittest.TestCase):
    def test_public_limit_prices_uses_official_fields(self):
        q = {"code": "sh600519", "pre_close": "100", "limit_up": "111", "limit_down": "89"}
        self.assertEqual(sf_quote.limit_prices(q), (111.0, 89.0))

    def test_public_limit_prices_estimates_missing_fields(self):
        q = {"code": "sh600519", "name": "贵州茅台", "pre_close": "100",
             "limit_up": "", "limit_down": ""}
        self.assertEqual(sf_quote.limit_prices(q), (110.0, 90.0))

    def test_target_guard_only_accepts_official_limits(self):
        """输入目标价不能把板块估算当硬限制，避免新股等特殊规则被误拦。"""
        q = {"code": "sh600519", "name": "贵州茅台", "pre_close": "100",
             "limit_up": "", "limit_down": ""}
        self.assertEqual(sf_quote.official_limit_prices(q), (0.0, 0.0))
        self.assertEqual(sf_quote.limit_prices(q), (110.0, 90.0),
                         "状态展示仍保留板块估算兜底")

    def test_missing_one_limit_field_is_estimated_without_overwriting_official_one(self):
        q = {"code": "sh600519", "name": "贵州茅台", "pre_close": "100",
             "limit_up": "111", "limit_down": ""}
        self.assertEqual(sf_quote.limit_prices(q), (111.0, 90.0))

    def test_etf_estimate_respects_three_decimal_precision(self):
        q = {"code": "sh510300", "name": "ETF", "pre_close": "4.621",
             "limit_up": "", "limit_down": ""}
        self.assertEqual(sf_quote.limit_prices(q), (5.083, 4.159))


class TestExitPersistence(_WinCase):

    def test_close_event_persists_geometry(self):
        w = self.win
        saved = []
        with patch.object(m, "save_config", lambda *a: saved.append(a) or True):
            w.close()
        self.assertEqual(len(saved), 1, "closeEvent 必须写盘一次（含位置与高度）")
        self.assertEqual(saved[0][0], w.code)
        self.assertEqual(saved[0][1], w.x())
        self.assertEqual(saved[0][2], w.y())

    def test_persist_is_idempotent(self):
        """幂等：closeEvent 与 quit() 内部都会走到，绝不能重复写盘。"""
        w = self.win
        saved = []
        with patch.object(m, "save_config", lambda *a: saved.append(a) or True):
            w._persist_on_exit()
            w._persist_on_exit()
            w._persist_on_exit()
        self.assertEqual(len(saved), 1, "重复收尾只应写盘一次")

    def test_about_to_quit_hook_connected(self):
        """aboutToQuit 必须接上收尾钩子 —— 这是 Cmd+Q 唯一的通知点。"""
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        w = self.win
        try:
            app.aboutToQuit.disconnect(w._persist_on_exit)
        except (TypeError, RuntimeError):
            self.fail("aboutToQuit 未连接 _persist_on_exit —— Cmd+Q 时窗口位置会丢失")
        else:
            # 断开成功即证明已连接，立即恢复，避免影响后续用例
            try:
                app.aboutToQuit.connect(w._persist_on_exit)
            except Exception:
                pass

    def test_quit_after_threads_calls_quit_directly(self):
        """后台搜索线程结束后必须直接触发 QApplication.quit，不依赖工作线程定时器。"""
        w = self.win
        class FakeThread:
            def wait(self, _ms):
                return None
        with patch.object(m.QApplication, "quit") as quit_mock:
            w._quit_after_threads([FakeThread()])
        quit_mock.assert_called_once()

    def test_nap_token_released_on_exit(self):
        """App Nap 令牌此前全项目零调用（RuntimeState.shutdown 是死代码）。"""
        w = self.win
        calls = []
        with patch.object(w._runtime, "shutdown", lambda: calls.append(1) or True):
            w._persist_on_exit()
        self.assertEqual(len(calls), 1, "退出时应回收 App Nap 令牌")

    def test_glass_window_ordered_out_not_closed_on_any_exit_path(self):
        """第6+7轮：closeEvent 也会走轻量收尾，毛玻璃子窗不得残留在桌面。

        v1.2.95 修正断言方向：对原生 NSWindow **只能 orderOut_，绝不能 close()** ——
        AppKit 释放后再被 Python 包装对象 release 即 EXC_BAD_ACCESS（本项目已 4 次
        此类 objc_release SIGSEGV）。旧断言 [("orderOut", None), ("close",)] 反向
        锁死了这个崩溃源，此处改为断言 close 绝不被调用。
        """
        class _Glass:
            def __init__(self):
                self.calls = []
            def orderOut_(self, arg):
                self.calls.append(("orderOut", arg))
            def close(self):
                self.calls.append(("close",))
        w = self.win
        gw = _Glass()
        w._glass_window = gw
        w._bg_effect = True
        w._persist_on_exit()
        self.assertIn(("orderOut", None), gw.calls, "退出必须把毛玻璃子窗下屏")
        self.assertNotIn(("close",), gw.calls, "退出绝不能 close() 原生 NSWindow")
        self.assertIsNone(w._glass_window)
        self.assertFalse(w._bg_effect, "退出必须复位 _bg_effect")


# =====================================================================
# 8. 主备行情源的价格小数位必须一致（否则切源时字号抖动）
#
# 实测（2026-09-02，同一只标的两个源）：
#   sh600519  腾讯 "1297.50"   新浪 "1297.500"     ← 2 位 vs 3 位
#   bj920001  腾讯 "12.10"    新浪 "12.100"
#   sh510300  腾讯 "4.620"    新浪 "4.620"        ← ETF 两边都是 3 位
# =====================================================================
class TestPriceDecimalsNormalized(unittest.TestCase):
    """纯函数，不访问网络。"""

    def test_stock_is_two_decimals(self):
        self.assertEqual(sf_quote.price_decimals("sh600519"), 2)
        self.assertEqual(sf_quote.price_decimals("sz300750"), 2)
        self.assertEqual(sf_quote.price_decimals("bj920001"), 2)

    def test_fund_etf_is_three_decimals(self):
        self.assertEqual(sf_quote.price_decimals("sh510300"), 3)
        self.assertEqual(sf_quote.price_decimals("sz159915"), 3)

    def test_two_sources_agree(self):
        """核心回归：同一只标的两个源返回值，归一后必须完全同宽。"""
        for sina_val, tencent_val, code in (
                ("1297.500", "1297.50", "sh600519"),
                ("12.100", "12.10", "bj920001"),
                ("4.620", "4.620", "sh510300"),
        ):
            self.assertEqual(sf_quote._norm_price(code, sina_val),
                             sf_quote._norm_price(code, tencent_val),
                             "%s 主备源归一后应一致（%r vs %r）"
                             % (code, sina_val, tencent_val))

    def test_value_not_altered(self):
        """只改字符串形态，数值不变。"""
        for s in ("1297.500", "4.620", "12.10"):
            self.assertAlmostEqual(float(sf_quote._norm_price("sh600519", s)),
                                   float(s), places=6)

    def test_non_numeric_passthrough(self):
        self.assertEqual(sf_quote._norm_price("sh600519", "abc"), "abc")
        self.assertEqual(sf_quote._norm_price("sh600519", ""), "")


# =====================================================================
# 6. NSLog 格式串（纯源码契约，不触碰 ctypes）
# =====================================================================
class TestNSLogFormatContract(unittest.TestCase):

    def test_nslog_passes_message_as_argument(self):
        with open(os.path.join(ROOT, "sf_logging.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('sel_registerName(b"stringWithUTF8String:")', src,
                      "ctypes 回退路径应构造 NSString 格式串")
        self.assertIn('b"%@"', src,
                      "日志原文必须经 %@ 作为实参传入，不得直接当格式串")
        # 必须是一次带两个参数的调用：NSLog(fmt, s)
        self.assertRegex(src, r"libf\.NSLog\(fmt,\s*s\)",
                         "NSLog 应以 (格式串, 日志内容) 两参数调用")


# =====================================================================
# 9. 改动1：QuoteDataError 必须计入熔断（_quote_fail++ 且 _on_poll_result(False)）
#
# 回归点：此前腾讯主源抛 QuoteDataError、新浪备用源也失败时，_fetch_quote 只记
# 日志不推进熔断状态机，轮询保持 0.1s 正常间隔持续请求坏数据，形成请求风暴。
# 修复后 QuoteDataError 与普通 Exception 走同一条熔断路径，连续失败进入 DEGRADED。
# =====================================================================
class TestQuoteDataErrorCircuitBreaker(_WinCase):
    """改动1 回归：QuoteDataError 必须触发降频，而非保持正常高频。"""

    def _arm(self):
        """让底层 fetch_quote 抛 QuoteDataError，并 spy _on_poll_result 同时保留真实状态机。"""
        # poll_backoff 必须开启，状态机才会推进；交易时段 interval 才反映状态
        self._patches.append(patch.object(m, "feature_enabled", lambda name: True))
        self._patches.append(patch.object(self.win, "_is_trading_time",
                                          lambda now=None: True))
        self._patches.append(patch.object(m, "fetch_quote",
                                          side_effect=m.QuoteDataError("bad format")))
        for p in self._patches[-3:]:
            p.start()
        self._poll_calls = []
        orig = self.win._on_poll_result

        def spy(ok):
            self._poll_calls.append(ok)
            return orig(ok)
        self.win._on_poll_result = spy

    def test_quote_data_error_increments_fail_and_signals(self):
        """核心：一次 QuoteDataError 必须使 _quote_fail +1 且调用 _on_poll_result(False)。"""
        self._arm()
        before = self.win._quote_fail
        self.win._fetch_quote()
        self.assertEqual(self.win._quote_fail, before + 1,
                         "QuoteDataError 未计入 _quote_fail —— 坏数据会持续高频请求")
        self.assertIn(False, self._poll_calls,
                      "QuoteDataError 未调用 _on_poll_result(False) —— 熔断不推进")

    def test_quote_data_error_enters_degraded(self):
        """连续 QuoteDataError 必须进入 DEGRADED（间隔变大），而非恒为 NORMAL。"""
        self._arm()
        for _ in range(3):
            self.win._fetch_quote()
        self.assertEqual(self.win._poll_state, m.POLL_DEGRADED,
                         "连续 QuoteDataError 未使熔断进入 DEGRADED")
        self.assertGreater(self.win._poll_interval(), m.POLL_INTERVAL_NORMAL,
                           "DEGRADED 态间隔应大于正常态（修复前恒为 0.1s 请求风暴）")
        self.assertEqual(self.win._poll_interval(), m.POLL_INTERVAL_DEGRADED,
                         "DEGRADED 态间隔应等于 POLL_INTERVAL_DEGRADED")


if __name__ == "__main__":
    unittest.main()
