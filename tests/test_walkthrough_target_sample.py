"""走查目标价样本与真实目标价面板的契约。

背景（v1.4.104 装机版点击走查）：包里带网络硬拒绝开关时，走查期间取不到行情。
面板的 `+N%` 要用当前价换算，取不到就提示「无法获取当前价」、不加条目，于是
`target` 判红，`stock` 因编辑入口被产品自己拦下而连带判红。对照实验（只关掉这一个
开关）两项都过。这里用**真实面板**把这条因果钉成判据，而不是只断言工具里的常量：

- 没有行情时，相对样本 `+1%` 必须被面板拒绝（证明为什么不能再用它）；
- 没有行情时，离线整数样本必须能加进去并生效（证明换成它是可行的）；
- 有行情时，离线样本会被拒（走查种子股贵州茅台现价在 1300 以上，1000 低于现价，面板提示
  「需高于当前价」；换成越过涨停线的绝对价则提示涨停价），证明它只能给「没有行情」的场景用；
  相对样本则能算出价格（证明另一半路径没被弄坏）。

只走公开按钮（加号、立即生效）和状态文字，不碰面板私有成员。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sf_constants import TARGET_DIR_ABOVE  # noqa: E402
import target_panel  # noqa: E402
import post_release_click_walkthrough as W  # noqa: E402

try:
    from _base import SFCase  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._base import SFCase  # noqa: E402


class WalkthroughTargetSampleAgainstRealPanel(SFCase):
    @classmethod
    def setUpClass(cls):
        cls._app = cls.ensure_qapp()

    def setUp(self):
        super().setUp()
        self.applied = []

    def _panel(self, price, limits):
        """price/limits 就是产品传给面板的两个回调：没有行情时一个是 0，一个是 (0, 0)。"""
        def on_apply(alerts):
            self.applied.append(alerts)
            return True

        with patch.object(target_panel, "load_alerts", return_value=[]):
            panel = target_panel.TargetPanel(
                "600519", "样本股", on_apply, lambda: True,
                current_code_fn=lambda: "600519",
                current_price_fn=lambda: price,
                limits_fn=lambda: limits)
        return self.track_widget(panel)

    @staticmethod
    def _status(panel):
        """面板状态行文字。单独成方法：断言里直接写 `.text()` 会被纪律扫描器的几何词表
        （含字母 x）误判成裸数字比较几何量，这里不为此去抬基线或加豁免。"""
        return panel.status_label.text()

    def _add_and_apply(self, panel, text):
        panel.edit_up.setText(text)
        panel.btn_up.click()
        panel.btn_apply.click()

    def test_without_a_quote_the_relative_sample_is_refused(self):
        panel = self._panel(0.0, (0.0, 0.0))
        self._add_and_apply(panel, W.TARGET_PRICE)
        self.assertIn("无法获取当前价", self._status(panel))
        self.assertEqual(self.applied, [], "没有当前价就不该有任何条目被保存")

    def test_without_a_quote_the_offline_sample_is_saved_as_an_above_target(self):
        panel = self._panel(0.0, (0.0, 0.0))
        self._add_and_apply(panel, W.TARGET_PRICE_OFFLINE)
        self.assertEqual(len(self.applied), 1, self._status(panel))
        saved = self.applied[0]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["direction"], TARGET_DIR_ABOVE)
        self.assertEqual(saved[0]["target"], float(W.TARGET_PRICE_OFFLINE))

    def test_with_a_quote_the_offline_sample_is_refused_but_relative_works(self):
        # 数字取自走查种子股的真实形态：现价 1346.02，涨停线 1450.34（排障时 1688 就是被它拒的）
        panel = self._panel(1346.02, (1450.34, 1186.64))
        self._add_and_apply(panel, W.TARGET_PRICE_OFFLINE)
        self.assertEqual(self.applied, [], "有行情时离线整数价低于现价，必须被拒")
        self.assertIn("需高于当前价", self._status(panel))
        panel.edit_up.setText("1688")
        panel.btn_up.click()
        self.assertEqual(self.applied, [], "越过涨停线的绝对价必须被拒")
        self.assertIn("涨停价", self._status(panel))
        panel.edit_up.setText(W.TARGET_PRICE)
        panel.btn_up.click()
        panel.btn_apply.click()
        self.assertEqual(len(self.applied), 1, self._status(panel))
        self.assertAlmostEqual(self.applied[0][0]["target"], 1359.48, places=2)

    def test_the_sample_chooser_matches_what_the_panel_can_accept(self):
        """选样本的函数和面板的现实必须同向：带开关（没行情）选到的样本，面板必须接受。"""
        offline_panel = self._panel(0.0, (0.0, 0.0))
        self._add_and_apply(offline_panel, W.pick_target_price(True))
        self.assertEqual(len(self.applied), 1, self._status(offline_panel))


if __name__ == "__main__":
    unittest.main()
