# -*- coding: utf-8 -*-
"""实时股价 v1.3.52 回归测试：主源缺「涨跌幅」字段时整条行情被丢弃。

BUG（stock_float_window.FloatWindow._apply_quote）：
    主源 sf_quote._fetch_quote_primary 的涨跌幅是**尾部扩展字段**容错读取的
    （"pct": _num(32)），字段缺失时 _num 返回空串 ""。旧代码
    `new_pct = float(data["pct"])` 抛 ValueError → 走 except → `return`
    → **整条行情被丢掉**。现价明明是好的，界面却一直显示上一次的值甚至 "--"，
    用户以为软件卡死。

修复（本文件锁定，防止回退）：
    1. 价格仍严格解析：price 缺失/非法/非有限 → 丢弃整条（不变）。
    2. 涨跌幅宽容解析：pct 可用就用；否则用 pre_close 自算
       (price - pre) / pre * 100（pre 须有限且 > 0）；都不行 → 未知。
    3. 未知时 new_pct 回落 0.0（**必须是数值**：参与 quote_changed /
       abs(new_pct - _disp_pct) / _anim_from_pct 等动画算术，传 None 会
       抛 TypeError），语义由 self._pct_unknown 承载；能算出来时置回 False。
    4. 渲染层 _paint_price：_pct_unknown 为真时涨跌幅显示 "--" 并用中性灰
       FG_DIM，不得渲染成带涨跌色的 "0.00%"。

运行方式（在 StockFloat 目录）：
    QT_QPA_PLATFORM=offscreen ./.venv/bin/python -m unittest \
        discover -s tests -p "test_v1349_pct.py" -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

import stock_float_window as m
import sf_quote


def _quote(**over):
    """构造一条主源行情；默认即「缺涨跌幅」的故障形态（pct 为空串）。"""
    q = {"name": "贵州茅台", "code": "sh600519", "price": "12.34", "pct": "",
         "change": "", "pre_close": "12.00", "high": "", "low": "",
         "volume": "", "limit_up": "", "limit_down": ""}
    q.update(over)
    return q


class _WinBase(unittest.TestCase):
    """公共夹具：离屏主窗与业务状态，但不为本文件启动后台行情 worker。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._runtime_starts = []

        def block_runtime_start(controller, quote_target, minute_target):
            self.assertEqual(controller.threads(), (None, None),
                             "夹具构造前不应已有行情或分时 worker")
            self._runtime_starts.append((controller, quote_target, minute_target))

        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: True),
            patch.object(sf_quote, "http_get", lambda *a, **k: ""),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch.object(m.QuoteRuntimeController, "start", block_runtime_start),
            patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
            # 首屏快照会跨用例泄漏（A 用例写盘 → B 用例新建窗口时读回，
            # _disp_price 被上一只股票的值预置），使「丢弃」类断言失真。
            patch.object(m.FloatWindow, "_seed_first_screen", lambda self: None),
            patch.object(m.FloatWindow, "_maybe_cache_first_screen", lambda self: None),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.win = m.FloatWindow()
        self.assertEqual(len(self._runtime_starts), 1,
                         "窗口构造必须登记两条常驻循环")
        controller, quote_target, minute_target = self._runtime_starts[0]
        self.assertIs(controller, self.win._quote_runtime)  # discipline-ok(R5): 接线断言必须取窗口持有的运行时控制器
        self.assertEqual(quote_target.__name__, "_quote_loop_wrapper")
        self.assertEqual(minute_target.__name__, "_minute_loop_wrapper")
        self.assertEqual(controller.threads(), (None, None),
                         "本文件不测后台抓取，不应真的创建 worker")
        self.win.running = False
        self.win._wake.set()
        for t in ("timer", "top_timer", "_anim_timer", "_flash_timer"):
            try:
                getattr(self.win, t).stop()
            except Exception:
                pass

    def tearDown(self):
        self.win._exit_persisted = True  # discipline-ok(R5): §6.11 规定关窗前必须阻止真实配置落盘
        self.win.close()


class TestPctMissingNotDroppingPrice(_WinBase):
    """核心：缺涨跌幅不得连价格一起丢。"""

    def test_pct_empty_but_pre_close_ok_price_adopted(self):
        """pct="" 但 pre_close 可用 → 自算涨跌幅，价格被采纳（不再整条丢弃）。"""
        data = _quote()
        self.win._apply_quote(data)
        self.assertIsNotNone(self.win._disp_price, "价格必须被采纳（旧行为在此整条丢弃）")
        self.assertAlmostEqual(self.win._disp_price, 12.34, places=2)
        self.assertIs(self.win.quote, data, "quote 应被写入")
        self.assertFalse(self.win._pct_unknown,
                         "pre_close 可自算出涨跌幅，不得标记为未知")
        self.assertAlmostEqual(self.win._disp_pct, (12.34 - 12.00) / 12.00 * 100.0,
                               places=2)

    def test_pct_empty_and_pre_close_zero_marks_unknown(self):
        """pct 缺失且 pre_close=\"0\" → 未知，但价格仍被采纳。"""
        self.win._apply_quote(_quote(pre_close="0"))
        self.assertAlmostEqual(self.win._disp_price, 12.34, places=2)
        self.assertTrue(self.win._pct_unknown, "无法自算涨跌幅时应标记未知")

    def test_pct_empty_and_pre_close_empty_marks_unknown(self):
        """pct 缺失且 pre_close="" → 未知，但价格仍被采纳。"""
        self.win._apply_quote(_quote(pre_close=""))
        self.assertAlmostEqual(self.win._disp_price, 12.34, places=2)
        self.assertTrue(self.win._pct_unknown)

    def test_pct_key_absent_marks_unknown(self):
        """data 里干脆没有 pct 键 → 未知，价格仍被采纳。"""
        data = _quote(pre_close="0")   # 同时去掉自算所需的昨收，才是真·未知
        del data["pct"]
        self.win._apply_quote(data)
        self.assertAlmostEqual(self.win._disp_price, 12.34, places=2)
        self.assertTrue(self.win._pct_unknown)

    def test_unknown_pct_is_numeric_zero(self):
        """未知时 new_pct 必须是数值 0.0 —— 动画算术不接受 None。"""
        self.win._apply_quote(_quote(pre_close="0"))
        self.assertIsInstance(self.win._disp_pct, float)
        self.assertEqual(self.win._disp_pct, 0.0)


class TestPctNormalRegression(_WinBase):
    """涨跌幅正常时的既有行为不回归。"""

    def test_normal_pct_not_unknown(self):
        """pct=\"1.23\" → 未知标记为 False，_disp_pct ≈ 1.23。"""
        self.win._apply_quote(_quote(pct="1.23", change="0.15"))
        self.assertFalse(self.win._pct_unknown)
        self.assertAlmostEqual(self.win._disp_pct, 1.23, places=2)

    def test_unknown_cleared_when_pct_comes_back(self):
        """先未知、后正常：必须置回 False（同一条行情后续又正常了要恢复显示）。"""
        self.win._apply_quote(_quote(pre_close="0"))
        self.assertTrue(self.win._pct_unknown, "前置条件：先进入未知态")
        self.win._apply_quote(_quote(pct="-2.50", change="-0.31"))
        self.assertFalse(self.win._pct_unknown, "涨跌幅恢复后必须清掉未知标记")
        self.assertAlmostEqual(self.win._disp_pct, -2.50, places=2)


class TestPriceStillStrict(_WinBase):
    """价格仍严格：非法价格照旧丢弃整条（这条不能被改坏）。"""

    def test_empty_price_still_dropped(self):
        """price="" → 丢弃，_disp_price 保持 None、quote 不写入。"""
        self.win._apply_quote(_quote(price="", pct="1.23"))
        self.assertIsNone(self.win._disp_price, "非法价格必须丢弃整条")
        self.assertIsNone(self.win.quote)
        self.assertFalse(self.win._pct_unknown, "被丢弃的行情不得污染未知标记")

    def test_nan_price_still_dropped(self):
        """price=\"nan\" → 非有限数，丢弃。"""
        self.win._apply_quote(_quote(price="nan", pct="1.23"))
        self.assertIsNone(self.win._disp_price)

    def test_price_key_absent_still_dropped(self):
        """没有 price 键 → KeyError 走丢弃分支。"""
        data = _quote(pct="1.23")
        del data["price"]
        self.win._apply_quote(data)
        self.assertIsNone(self.win._disp_price)


class TestPctUnknownReset(_WinBase):
    """重置点：不重置会永久卡在 "--"。"""

    def test_init_default_false(self):
        self.assertFalse(self.win._pct_unknown, "__init__ 必须初始化为 False")

    def test_apply_stock_resets(self):
        """切股时重置（apply_stock 里 _disp_pct=None 那处）。"""
        self.win._apply_quote(_quote(pre_close="0"))
        self.assertTrue(self.win._pct_unknown)
        with patch.object(m.FloatWindow, "_force_fetch", lambda self: None):
            self.win.apply_stock("sh600519")
        self.assertFalse(self.win._pct_unknown, "切股必须清掉未知标记")


class TestRenderUnknownPct(_WinBase):
    """渲染冒烟：未知态显示 "--" 且用中性灰，不崩。"""

    def _paint(self, win):
        """离屏抓帧：直接对 QPixmap 起 QPainter 调 _paint_price。

        不用 show()/processEvents() —— 离屏环境下会 SIGSEGV；走 QPixmap
        也不需要 native window，最稳。
        """
        pm = QPixmap(m.W, 220)
        pm.fill(m.Qt.GlobalColor.transparent)
        p = QPainter(pm)
        try:
            win._paint_price(p, m.W)
        finally:
            p.end()

    def test_paint_price_unknown_does_not_raise(self):
        self.win._apply_quote(_quote(pre_close="0"))
        self.assertTrue(self.win._pct_unknown)
        self.win._anim_decimals = 2
        self._paint(self.win)   # 不抛异常即通过

    def test_paint_price_unknown_then_normal(self):
        """同一窗口先未知后正常，两帧都不崩。"""
        self.win._apply_quote(_quote(pre_close="0"))
        self._paint(self.win)
        self.win._apply_quote(_quote(pct="1.23", change="0.15"))
        self._paint(self.win)

    def test_grab_unknown_does_not_crash(self):
        """离屏抓帧：_pct_unknown=True 时 grab() 不崩。"""
        self.win._apply_quote(_quote(pre_close="0"))
        self.win._need_redraw = True
        self.win.update()
        pix = self.win.grab()
        self.assertIsNotNone(pix)
        self.assertFalse(pix.isNull(), "grab() 应返回有效图像")

    def test_render_layer_uses_neutral_gray_source_contract(self):
        """源码契约：未知态涨跌幅必须是中性色 FG_DIM，不得沿用红/绿涨跌色。"""
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "sf_ui_render.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('pct = "--" if _pct_unknown else fmt_pct(self._disp_pct)', src)
        self.assertIn("pct_color = FG_DIM if _pct_unknown else color", src)


if __name__ == "__main__":
    unittest.main()
