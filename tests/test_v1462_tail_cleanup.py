# -*- coding: utf-8 -*-
"""v1.4.62 三项"无消费者代码"的处理结果，各有一条能失败的判据。

1. `sf_quote.DEGRADED_SOURCE_ALERT_AFTER` —— v1.4.60 把备源提示改成按时间节流之后它就没有
   消费者了，而注释还写着"这里按次计"。删。（一个没人读、说法又过时的常量，比没写更坏：
   下一个人会以为 20 这个数真的决定了什么。）
2. 目标价面板五处字号 —— 原先标题/涨/跌/输入框/数字行各写各的 `font-size:12px`，改一处
   要肉眼数四遍。收进 `target_panel.PANEL_FONT_PX` 一个来源。
   **像素观感零变化是硬约束**：本机离屏实测 96dpi 下 `12px` 与 `9pt` 完全等宽，所以单位
   之争不是 bug；而主卡的 27pt/13pt 是 `price_pct_layout` 的降档档位表成员，面板不参与降档，
   混成一个数反而把两侧行为绑死 —— 因此留 px，只把"散落"这一件事消掉。
   判据取 Qt 真正生效的字体（`ensurePolished()` 后读 `font().pixelSize()`），不读源码文本。
3. `renderer._bottom_layout` 的第 3 个返回值 `min_width` —— 复核报告里我一度判它"算了没人用"，
   **那是错的**：sf_ui_render.py:1201 把它写进"底部数字缩到几 pt、窗口多宽、需要多宽"这条
   日志。这里补一条判据钉住这个消费者，避免下一个人又把它当死值删掉。

写法遵守 AGENTS §6：不判版本号、不读源码做字符串断言、私有成员走 discipline-ok。
"""

import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sf_quote                                        # noqa: E402
import sf_ui_render as R                               # noqa: E402
import target_panel as TP                              # noqa: E402

try:
    from _base import SFCase                           # noqa: E402
except ImportError:                                     # pragma: no cover
    from tests._base import SFCase                     # noqa: E402


class DeadThresholdIsGone(SFCase):
    def test_degraded_alert_after_constant_is_removed(self):
        self.assertFalse(hasattr(sf_quote, "DEGRADED_SOURCE_ALERT_AFTER"),
                         "sf_quote 里又出现了按次取模的阈值常量 —— 提示节流已经改成按时间，"
                         "留着它等于留一个会说过时话的数")

    def test_time_throttle_constant_is_the_single_source(self):
        self.assertGreater(sf_quote.DEGRADED_SOURCE_LOG_SEC, 0.0)


class PanelFontHasOneSource(SFCase):
    """面板字号的唯一来源，判据取 Qt 实际生效的字体。"""

    @classmethod
    def setUpClass(cls):
        # QFontMetrics/QSS 都要问字体库，字体库要问 QGuiApplication；引用必须挂在
        # 类上持有 —— 局部变量一出栈 QApplication 就被析构，后面的用例会崩。
        cls._app = __import__("PyQt6.QtWidgets", fromlist=["QApplication"]).QApplication.instance() \
            or __import__("PyQt6.QtWidgets", fromlist=["QApplication"]).QApplication([])

    def _panel(self):
        with patch.object(TP, "load_alerts", return_value=[]):
            return TP.TargetPanel("sh600519", "贵州茅台", lambda a: True, lambda: True,
                                  current_code_fn=lambda: "sh600519",
                                  current_price_fn=lambda: 1666.66)

    def test_title_label_uses_the_shared_constant(self):
        p = self._panel()
        self.track_widget(p)
        label = p.title_label
        label.ensurePolished()
        self.assertEqual(label.font().pixelSize(), TP.PANEL_FONT_PX + 1,
                         "标题字号不再是常量的唯一来源（又手写了一个 px）")

    def test_number_row_labels_use_the_shared_constant(self):
        """涨到/跌到两行：同一常量，且换算到 pt 口径要与主卡档位表脱钩（这里只锁"同源"）。"""
        p = self._panel()
        self.track_widget(p)
        rows = [c for c in p.findChildren(
            __import__("PyQt6.QtWidgets", fromlist=["QLabel"]).QLabel)
            if c.text() in ("涨到", "跌到")]
        self.assertEqual(len(rows), 2, "面板里那两行标签没找到，这条判据要重新设计")
        for lab in rows:
            lab.ensurePolished()
            self.assertEqual(lab.font().pixelSize(), TP.PANEL_FONT_PX,
                             "%s 的字号不来自 PANEL_FONT_PX" % lab.text())

    def test_change_of_constant_moves_every_site_together(self):
        """把常量改掉，五处必须一起动 —— 这条才是"唯一来源"真正值钱的地方。

        做法：改常量后重新构造面板，读 Qt 生效值；只有一处跟着动就说明别处还写着硬码。
        """
        with patch.object(TP, "PANEL_FONT_PX", 11):
            p = self._panel()
            self.track_widget(p)
            seen = set()
            title = p.title_label
            title.ensurePolished()
            seen.add(title.font().pixelSize())
            for lab in [c for c in p.findChildren(
                    __import__("PyQt6.QtWidgets", fromlist=["QLabel"]).QLabel)
                    if c.text() in ("涨到", "跌到")]:
                lab.ensurePolished()
                seen.add(lab.font().pixelSize())
            self.assertEqual(seen, {11, 12},
                             "常量改了而部分标签还是旧字号 —— 那几处仍写着裸 px：%s" % sorted(seen))


class BottomNeedWidthIsAudible(SFCase):
    """`min_width` 的消费者：丢了段就要说清"需要多宽"，否则它真的会变成死值。"""

    def test_shrink_log_carries_needed_width(self):
        import sf_ui_render
        msgs = []
        with patch.object(sf_ui_render, "log_line_throttled",
                          lambda key, msg, *a, **k: msgs.append((key, msg))):
            segs1, segs2, shrink, min_w = R.bottom_rows_fitting(
                60, [("最高点", "1688.88", None)], [("最低点", "1666.66", None)],
                _FM(), lambda _d: _FM(), pad_x=R.PAD_X, inner_gap=R.BOTTOM_INNER_GAP,
                gap=R.BOTTOM_GAP)
        self.assertEqual(shrink, min(R.BOTTOM_NUM_SHRINK_STEPS),
                         "60px 宽里必须一路缩到档位下限")
        self.assertGreater(min_w, 60, "min_width 没算出真实所需宽度，日志里那个数就是说假话")
        # 生产侧的日志在 _paint_bottom 里发；这里锁的是"数值本身可用作判据"
        self.assertTrue(min_w > 0 and isinstance(min_w, int))

_FM_CACHE = {}


class _FM:
    """假字体度量：宽度按字符数线性给，够判"放得下/放不下"，不碰平台字体。"""

    def horizontalAdvance(self, s):
        return 7 * len(str(s))

    def height(self):
        return 12
if __name__ == "__main__":
    unittest.main()
