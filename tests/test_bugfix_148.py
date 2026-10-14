# -*- coding: utf-8 -*-
"""实时股价 v1.2.48 BUG 走查修复回归测试（B1/B2/H1/H2/H3/S1/S4/S8）。

覆盖项：
    B1  eastmoney_search_bj 经 sf_network.get_last_http_status() 实时读状态（不再用导入快照）；
    S4  sf_network 提供 getter，sf_quote 不再绑定模块级 _LAST_HTTP_STATUS 快照；
    S1  _tencent_code 对 bj 前缀 4 位数字补 0 到 6 位（bj8307 → bj830700）；
    B2  切股丢弃判断前代码归一（北交所 4 位代码行情不再被误丢弃）；
    H1  _toggle_chart 展开分支异常时恢复 MIN_H，min 不残留 0；
    H2  v1.2.57 呼吸灯改造：确认 _dot_flash_until 已移除，minimized 分支分时数据仍正常更新；
    H3  _icon_worker 区分超时与真实失败，超时跳过应用图标生成；
    S8  hi/lo 文本统一走 _fmt_hl 格式化归一；_anim_decimals 对 "123." 回落 2 位。

运行方式（在 StockFloat 目录）：
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

import stock_float_window as m
import sf_icons
import sf_network
import sf_quote
import sf_ui_render


def _quote(**over):
    q = {"name": "贵州茅台", "code": "600519", "price": "0.00", "pct": "0.00",
         "change": "0.00", "pre_close": "403.50", "high": "403.50", "low": "403.50",
         "volume": "0", "limit_up": "443.85", "limit_down": "363.15"}
    q.update(over)
    return q


class _WinBase(unittest.TestCase):
    """与 test_v130/v132 相同的 patch 模式，构造后停掉后台线程。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", lambda *a, **k: ""),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()
        self.win.timer.stop()
        self.win.top_timer.stop()
        self.win._anim_timer.stop()
        self.win._flash_timer.stop()
        for _t in (getattr(self.win, "_quote_thread", None),
                   getattr(self.win, "_minute_thread", None)):
            if _t is not None:
                _t.join(timeout=1.5)
        self.win.show()

    def tearDown(self):
        self.win.close()
        for p in self._patches:
            p.stop()


# =====================================================================
# B1 + S4：HTTP 状态码实时读取（getter），消除导入时快照死分支
# =====================================================================
class TestB1S4StatusGetter(_WinBase):
    def test_getter_reflects_latest_status(self):
        """getter 必须实时反映最新状态写入，而非旧快照。

        v1.3.68 修正：原本直接给 `sf_network._LAST_HTTP_STATUS` 赋值再断言，
        但 getter 的实现是 `getattr(_STATUS_LOCAL, "value", _LAST_HTTP_STATUS)` ——
        **线程本地值优先**。主线程一旦被前面某个测试写过 `value`（写 `None` 也算），
        进程级变量就永远读不到，于是固定顺序下侥幸绿、随机顺序下红
        （"我刚赋的 429，读回来是 None"）。
        正确做法是走真实写入口 `_set_last_http_status`，并把线程本地值在收尾时清干净，
        既不依赖执行顺序，也真的在测业务契约。
        """
        def _clear_local():
            try:
                del sf_network._STATUS_LOCAL.value
            except AttributeError:
                pass

        _clear_local()
        try:
            sf_network._set_last_http_status(429)
            self.assertEqual(sf_network.get_last_http_status(), 429)
            sf_network._set_last_http_status(200)
            self.assertEqual(sf_network.get_last_http_status(), 200,
                             "getter 必须每次读取最新状态")
        finally:
            _clear_local()
            sf_network._LAST_HTTP_STATUS = None

    def test_quote_module_no_snapshot_binding(self):
        """sf_quote 不得再以 from...import 复制 _LAST_HTTP_STATUS 快照。"""
        self.assertFalse(hasattr(sf_quote, "_LAST_HTTP_STATUS"),
                         "sf_quote 不应再绑定模块级 _LAST_HTTP_STATUS")

    def test_eastmoney_branch_uses_getter_src(self):
        """源码级防回归：eastmoney_search_bj 4xx 判别必须走 getter 而非快照变量。"""
        import inspect
        src = inspect.getsource(sf_quote.eastmoney_search_bj)
        self.assertIn("get_last_http_status()", src)
        # 旧实现"isinstance(_LAST_HTTP_STATUS, int) 恒 False"的判别必须已移除
        self.assertNotIn("isinstance(_LAST_HTTP_STATUS", src)

    def test_eastmoney_4xx_returns_empty(self):
        """功能级：http_get 抛异常且最新状态为 4xx 时返回空列表且不崩溃。"""
        def boom(*a, **k):
            raise RuntimeError("net down")
        with patch.object(sf_quote, "http_get", side_effect=boom), \
             patch.object(sf_network, "_LAST_HTTP_STATUS", 429):
            self.assertEqual(sf_quote.eastmoney_search_bj("8307"), [])


# =====================================================================
# S1：_tencent_code 对 bj 4 位补 0 到 6 位
# =====================================================================
class TestS1TencentCode(unittest.TestCase):
    def test_bj_4digit_padded(self):
        self.assertEqual(sf_quote._tencent_code("bj8307"), "bj830700")

    def test_bj_full_6digit_unchanged(self):
        self.assertEqual(sf_quote._tencent_code("bj830700"), "bj830700")

    def test_bj_non_digit_unchanged(self):
        self.assertEqual(sf_quote._tencent_code("bjabc"), "bjabc")

    def test_sh_sz_unchanged(self):
        self.assertEqual(sf_quote._tencent_code("sh600519"), "sh600519")
        self.assertEqual(sf_quote._tencent_code("sz000001"), "sz000001")

    def test_bare_code_prefix(self):
        self.assertEqual(sf_quote._tencent_code("600519"), "sh600519")
        self.assertEqual(sf_quote._tencent_code("000001"), "sz000001")

    def test_empty_input(self):
        self.assertEqual(sf_quote._tencent_code(""), "sz")
        self.assertEqual(sf_quote._tencent_code(None), "sz")


# =====================================================================
# B2：切股丢弃判断前代码归一（北交所 4 位代码不被误丢弃）
# =====================================================================
class TestB2NormalizeCode(_WinBase):
    def test_normalize_6(self):
        self.assertEqual(m._normalize_code_6("bj8307"), "830700")
        self.assertEqual(m._normalize_code_6("bj830700"), "830700")
        self.assertEqual(m._normalize_code_6("sh600519"), "600519")
        self.assertEqual(m._normalize_code_6("830700"), "830700")
        self.assertIsNone(m._normalize_code_6("abc"))
        self.assertIsNone(m._normalize_code_6(None))

    def test_bj_4digit_quote_not_dropped(self):
        """自选 bj8307，行情回包补 0 的 830700 不得被误判为旧股票丢弃。"""
        self.win.code = "bj8307"
        with patch.object(m, "_is_trading_time", return_value=True), \
             patch.object(m, "market_status", return_value=None):
            self.win._apply_quote(_quote(code="830700", price="10.50", pct="1.00"))
        self.assertEqual(self.win._disp_price, 10.50,
                         "北交所 4 位自选代码行情必须正常更新价格")

    def test_wrong_stock_still_dropped(self):
        """不同股票（600519 vs 000001）仍应被丢弃。"""
        self.win.code = "sh600519"
        with patch.object(m, "_is_trading_time", return_value=True), \
             patch.object(m, "market_status", return_value=None):
            self.win._apply_quote(_quote(code="000001", price="9.00", pct="1.00"))
        self.assertIsNone(self.win._disp_price, "不同股票行情仍应被丢弃")


# =====================================================================
# H1：_toggle_chart 展开分支异常恢复 MIN_H（min 不残留 0）
# =====================================================================
class TestH1ToggleChart(_WinBase):
    def test_source_lowered_flag_and_recovery(self):
        """源码级防回归：展开分支先算 target 再下探 min，except 恢复 MIN_H/MAX_H。"""
        import inspect
        src = inspect.getsource(m.FloatWindow._toggle_chart)
        self.assertIn("lowered_min = True", src)
        self.assertIn("setMinimumHeight(0)", src)
        self.assertIn("if self._chart_expanded and lowered_min:", src)
        self.assertIn("setMinimumHeight(MIN_H)", src)

    def test_expand_layout_error_restores_min(self):
        """功能级：begin_layout 抛异常后 min 必须恢复为 MIN_H（不残留 0）。"""
        self.win._chart_expanded = False
        with patch.object(m, "feature_enabled", return_value=True), \
             patch.object(self.win._anim_driver, "begin_layout",
                          side_effect=RuntimeError("boom")):
            self.win._toggle_chart()
        self.assertEqual(self.win.minimumHeight(), m.MIN_H,
                         "展开动画异常后 min 必须恢复 MIN_H，不得残留 0")


# =====================================================================
# H2：v1.2.57 呼吸灯改造 —— 确认 _dot_flash_until 已移除，
#     minimized 分支分时数据仍正常更新
# =====================================================================
class TestH2BreathingDot(_WinBase):
    def test_minimized_minute_data_still_updates(self):
        """Dock 收起期间分时数据正常更新（呼吸灯由渲染层内置周期驱动，无需门控变量）。"""
        self.win.running = True
        self.win._minimized = True
        self.win.minute_points = [("0930", "10.00")]
        with patch.object(self.win, "_apply_icons", lambda: None):
            self.win.q.put(("minute", ("sh600519", [("0931", "10.50")])))
            self.win._poll()
        self.assertEqual(self.win.minute_points, [("0931", "10.50")],
                         "minimized 分支分时数据应正常更新")
        self.assertFalse(hasattr(self.win, '_dot_flash_until'),
                         "_dot_flash_until 应已移除（v1.2.57 呼吸灯改造）")

    def test_no_dot_flash_until_attribute(self):
        """确认 FloatWindow 实例不再包含 _dot_flash_until 属性。"""
        self.assertFalse(hasattr(self.win, '_dot_flash_until'),
                         "_dot_flash_until 应已移除（v1.2.57 呼吸灯改造）")


# =====================================================================
# H3：_icon_worker 区分超时与真实失败（超时跳过应用图标生成）
# =====================================================================
class TestH3IconWorker(_WinBase):
    def test_worker_checks_app_ready(self):
        """源码级防回归：_app_ready.wait 结果必须被判断，超时不无条件 make_app_icon。"""
        import inspect
        src = inspect.getsource(sf_icons._icon_worker)
        self.assertIn("if _app_ready.wait(timeout=10):", src,
                      "超时必须跳过应用图标生成，不得无条件 make_app_icon")
        self.assertNotIn("_app_ready.wait(timeout=10)\n", src.replace("    if ", "    "))


# =====================================================================
# S8：hi/lo 格式化归一 + _anim_decimals "123." 回落 2 位
# =====================================================================
class TestS8Format(_WinBase):
    def test_fmt_hl_trailing_zero(self):
        """最高/最低文本去除多余尾 0，避免 "1234.5000" 原样显示。"""
        self.assertEqual(sf_ui_render._fmt_hl("1234.5000"), "1234.5")
        self.assertEqual(sf_ui_render._fmt_hl("12.30"), "12.3")
        self.assertEqual(sf_ui_render._fmt_hl("100"), "100")
        self.assertEqual(sf_ui_render._fmt_hl("12.345"), "12.345")

    def test_fmt_hl_invalid(self):
        self.assertEqual(sf_ui_render._fmt_hl("abc"), "--")
        self.assertEqual(sf_ui_render._fmt_hl(""), "--")
        self.assertEqual(sf_ui_render._fmt_hl(None), "--")

    def test_anim_decimals_dot_only_falls_back(self):
        """price 形如 "123." 时 _anim_decimals 应回落 2 位而非 0。"""
        self.win.code = "sh600519"
        with patch.object(m, "_is_trading_time", return_value=True), \
             patch.object(m, "market_status", return_value=None):
            self.win._apply_quote(_quote(code="600519", price="123.", pct="1.00"))
        self.assertEqual(self.win._anim_decimals, 2, '"123." 小数位应回落 2 而非 0')

    def test_anim_decimals_normal(self):
        """正常小数位保持原样（1 位 / 2 位）。"""
        self.win.code = "sh600519"
        with patch.object(m, "_is_trading_time", return_value=True), \
             patch.object(m, "market_status", return_value=None):
            self.win._apply_quote(_quote(code="600519", price="123.5", pct="1.00"))
        self.assertEqual(self.win._anim_decimals, 1)
        self.win._apply_quote(_quote(code="600519", price="123.45", pct="1.00"))
        self.assertEqual(self.win._anim_decimals, 2)


if __name__ == "__main__":
    unittest.main()
