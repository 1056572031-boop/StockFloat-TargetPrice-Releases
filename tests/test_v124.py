# -*- coding: utf-8 -*-
"""实时股价 v1.2.24 新功能回归测试。

覆盖 2 项显示层优化：
1. 字体内置打包：D-DIN 数字字体随包分发（fonts/ 资源 + spec datas +
   QFontDatabase.addApplicationFont 启动加载），任意 Mac 数字等宽效果一致
2. 价格/涨跌幅逐位滚动：在现有插值动画基础上，位值变化时旧字滑出/新字滑入
   （老虎机式逐位翻滚），红涨绿跌/等宽对齐/fmt_pct/字号自适应不回归

运行方式（在 StockFloat 目录）：
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

import stock_float_window as m
import sf_quote
from sf_constants import APP_VERSION, VERSION_TEXT
from sf_ui_render import (load_bundled_fonts, _bundled_font_path, NUM_FONT,
                          BUNDLED_FONT_FILES, ROLL_DUR)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fake_http_get(url, **kw):
    """离线行情 mock：构造窗口时避免真实网络请求。"""
    if "hq.sinajs.cn" in url:
        return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
    if "/appstock/app/minute/query?" in url:
        return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                '"data":["0930 1510.0 100 1000", "1000 1511.0 200 2000"]}}}}')
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


_QUOTE = {"name": "贵州茅台", "code": "600519", "price": "410.00", "pct": "1.61",
          "change": "6.50", "pre_close": "403.50", "high": "412.00", "low": "400.00",
          "volume": "12345", "limit_up": "443.85", "limit_down": "363.15"}


class TestVersion(unittest.TestCase):
    """v1.2.24 版本号唯一来源 sf_constants.APP_VERSION，spec 与测试同步。"""

        

    def test_spec_uses_app_version(self):
        with open(os.path.join(REPO, "StockFloat.spec"), encoding="utf-8") as f:
            spec = f.read()
        # spec 从 sf_constants 读 APP_VERSION（版本唯一来源，不硬编码）
        self.assertIn("APP_VERSION", spec)
        self.assertIn("sf_constants", spec)
        self.assertNotIn("'1.2.23'", spec)

    def test_spec_embeds_fonts_datas(self):
        with open(os.path.join(REPO, "StockFloat.spec"), encoding="utf-8") as f:
            spec = f.read()
        self.assertIn("fonts/*.otf", spec, "spec 应把字体作为 datas 打进包")


class TestBundledFonts(unittest.TestCase):
    """需求1：字体随包分发（fonts/ 资源存在 + 启动加载可注册）。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_font_files_in_repo(self):
        for fn in BUNDLED_FONT_FILES:
            self.assertTrue(
                os.path.isfile(os.path.join(REPO, "fonts", fn)),
                "字体资源缺失: fonts/%s" % fn)

    def test_bundled_path_resolves(self):
        d = _bundled_font_path()
        self.assertIsNotNone(d, "内置字体目录应可解析（源码模式取工程 fonts/）")
        for fn in BUNDLED_FONT_FILES:
            self.assertTrue(os.path.isfile(os.path.join(d, fn)))

    def test_load_registers_bundled_font(self):
        fams = load_bundled_fonts()
        self.assertIn("D-DIN", fams,
                      "addApplicationFont 应注册内置数字字体，实际: %r" % (fams,))

    def test_qfont_family_available(self):
        fams = load_bundled_fonts()
        self.assertIn("D-DIN", fams)
        # 注册后可构造 QFont 且族名被 Qt 认可
        f = QFont("D-DIN")
        self.assertEqual(f.family().lower(), "d-din")

    def test_bold_weight_resolvable(self):
        # Bold 变体可显式构造（Regular/Bold 双文件注册）
        f = QFont("D-DIN")
        f.setWeight(QFont.Weight.Bold)
        self.assertEqual(f.weight(), QFont.Weight.Bold)


class TestRollBase(unittest.TestCase):
    """FloatWindow 逐位滚动状态机测试基类（不依赖像素，直接断言状态）。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", _fake_http_get),
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
        self.win.show()
        self.win._apply_quote(dict(_QUOTE))
        self.win._anim_timer.stop()   # 防止插值定时器覆盖手动 _disp_*
        self._render(410.00, 1.61)    # 建立基线帧

    def tearDown(self):
        self.win.close()
        QApplication.processEvents()
        for p in self._patches:
            p.stop()

    def _render(self, price, pct):
        self.win._disp_price = price
        self.win._disp_pct = pct
        self.win._need_redraw = True
        self.win.update()
        QApplication.processEvents()
        return self.win.grab().toImage()

    def _anim(self, key):
        return self.win._roll_anim.get(key, {})


class TestRollAnimation(TestRollBase):
    """需求2：价格/涨跌幅逐位滚动（状态机行为）。"""

    def test_roll_duration_constant(self):
        self.assertEqual(ROLL_DUR, 0.18)

    def test_baseline_frame_no_roll(self):
        """基线帧：记录 prev，不产生滚动位"""
        self.assertEqual(self.win._roll_prev.get("price"), "410.00")
        self.assertEqual(self.win._roll_prev.get("pct"), "+1.61%")
        self.assertEqual(self._anim("price"), {})
        self.assertEqual(self._anim("pct"), {})

    def test_unchanged_frame_no_new_roll(self):
        """同值连续渲染：不新增滚动位"""
        self._render(410.00, 1.61)
        self.assertEqual(self._anim("price"), {})
        self.assertEqual(self._anim("pct"), {})

    def test_change_triggers_only_changed_digits(self):
        """410.00→410.15：价格仅第 5、6 位（索引4、5）滚动；其余位不动"""
        self._render(410.15, 1.64)
        pa = self._anim("price")
        self.assertEqual(sorted(pa.keys()), [4, 5],
                         "价格应仅索引 4/5 滚动, got %s" % sorted(pa.keys()))
        self.assertEqual((pa[4][0], pa[4][1]), ("0", "1"))
        self.assertEqual((pa[5][0], pa[5][1]), ("0", "5"))

    def test_pct_change_triggers_only_changed_digits(self):
        """+1.61%→+1.64%：涨跌幅仅索引 4 滚动；+ 与 % 位不动"""
        self._render(410.00, 1.64)
        pp = self._anim("pct")
        self.assertEqual(sorted(pp.keys()), [4],
                         "pct 应仅索引 4 滚动, got %s" % sorted(pp.keys()))
        self.assertEqual((pp[4][0], pp[4][1]), ("1", "4"))

    def test_roll_direction_up_when_value_up(self):
        """数字增大（0→1）：滚动方向为增大（新字自下滚入）"""
        self._render(410.15, 1.61)
        pa = self._anim("price")
        for i in (4, 5):
            self.assertGreater(pa[i][1], pa[i][0])

    def test_roll_direction_down_when_value_down(self):
        """数字减小（9→0）：滚动方向为减小（新字自上滚入）"""
        self._render(419.00, 1.61)    # 建立新基线：第 3 位（索引2）为 9
        self._render(410.00, 1.61)    # 索引2: 9→0 数字减小
        pa = self._anim("price")
        self.assertIn(2, pa)
        self.assertEqual((pa[2][0], pa[2][1]), ("9", "0"))
        self.assertLess(pa[2][1], pa[2][0])

    def test_roll_clears_after_duration(self):
        """滚动结束后滚动位被清理（后续帧静态绘制）"""
        self._render(410.15, 1.61)
        pa = self._anim("price")
        self.assertTrue(pa)
        for entry in pa.values():
            entry[2] -= 10   # 伪造时间流逝，超出 ROLL_DUR
        self._render(410.15, 1.61)
        self.assertEqual(self._anim("price"), {}, "滚动结束后应清空")

    def test_length_change_clears_and_no_roll(self):
        """切股/首帧长度变化：清空进行中滚动，仅更新基线（不整串乱滚）"""
        self._render(410.15, 1.61)          # 先触发滚动
        self.assertTrue(self._anim("price"))
        self._render(1234.56, 1.61)          # 长度变化
        self.assertEqual(self._anim("price"), {}, "长度变化应清空滚动")
        self.assertEqual(self.win._roll_prev.get("price"), "1234.56")

    def test_price_pct_isolated(self):
        """价格与涨跌幅滚动状态互不串扰"""
        # 只变价格：pct 无滚动位
        self._render(410.15, 1.61)
        self.assertEqual(sorted(self._anim("price").keys()), [4, 5])
        self.assertEqual(self._anim("pct"), {})
        # 继续只变 pct：price 未过期的滚动位不被覆盖
        self._render(410.15, 1.64)
        self.assertEqual(sorted(self._anim("pct").keys()), [4])
        self.assertEqual(sorted(self._anim("price").keys()), [4, 5],
                         "price 滚动位应保持，不被 pct 串扰")

    def test_roll_width_equals_horizontal_advance(self):
        """逐字符滚动绘制总宽度 == 布局侧量出的宽度 → 对齐不回归。

        口径变更（走查 S8）：旧断言写的是"== 整串 horizontalAdvance"，而自然 advance
        正是抖动之源（`1`=13px、`8`=19px，价格一跳右侧整行横移）。现在数字位占统一
        格宽，绘制与量宽都以 tabular_advance 为唯一口径 —— 守的东西没变（两侧必须相等），
        变的是那个"唯一口径"的定义。
        """
        f = self.win._font(NUM_FONT, 27, QFont.Weight.Bold)
        fm = self.win._fm(f)
        s = "410.00"
        img = QImage(400, 100, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        try:
            w = self.win._draw_roll_text(p, QRectF(0, 0, 300, 60), s, f,
                                         QColor("white"), roll_key="wtest")
        finally:
            p.end()
        from sf_ui_render import digit_cell_width, tabular_advance
        expect = tabular_advance(fm, s, digit_cell_width(fm))
        self.assertNotEqual(expect, fm.horizontalAdvance(s),
                            "定宽口径没生效（与自然宽相同），这条断言就成了空的")
        self.assertAlmostEqual(w, expect, delta=0.6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
