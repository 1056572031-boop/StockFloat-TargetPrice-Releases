# -*- coding: utf-8 -*-
"""TargetBubble 旧文本入口与分组入口的可观测等价性，不构造主窗或启动 worker。"""
from __future__ import annotations

import unittest

import sf_ui_render
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage

from stock_float_window import TargetBubble
from tests._base import SFCase


_QAPP = None


class TargetBubbleTextCase(SFCase):
    def setUp(self):
        global _QAPP
        super().setUp()
        # 必须持有 QApplication；这里只建气泡，不调用 show_below 或泵全局事件。
        _QAPP = SFCase.ensure_qapp()
        sf_ui_render.load_bundled_fonts()
        self.bubble = self.track_widget(TargetBubble())
        self.grouped = self.track_widget(TargetBubble())

    @staticmethod
    def image(widget):
        image = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        widget.render(image)
        return image

    def assert_matches_groups(self, text, pending, hit):
        self.bubble.setText(text)
        self.grouped.setGroups(pending, hit)
        self.assertEqual(self.bubble.size(), self.grouped.size(),
                         "文本入口必须使用与分组入口一致的布局")
        self.assertEqual(self.image(self.bubble), self.image(self.grouped),
                         "分区顺序、文字、颜色与分隔线必须与显式分组一致")

    def test_混合前缀与空行按组展示并保留组内顺序(self):
        pending = ["待触发 ≥ 18.20", "普通目标 ≤ 9.50", "未触发 ≥ 30.00",
                   "前缀已触发但不是行首", " 已触发（前导空格保留）"]
        hit = ["已触发 ≤ 10.00", "已触发 ≥ 22.50"]
        text = "\n已触发 ≤ 10.00\n待触发 ≥ 18.20\n\n普通目标 ≤ 9.50\r\n已触发 ≥ 22.50\n未触发 ≥ 30.00\n前缀已触发但不是行首\n 已触发（前导空格保留）\n"
        self.assert_matches_groups(text, pending, hit)

    def test_单组与空白行不产生虚假分区(self):
        cases = (
            ("\n待触发 ≥ 18.20\n\n", ["待触发 ≥ 18.20"], []),
            ("\n已触发 ≥ 22.50\r\n\n", [], ["已触发 ≥ 22.50"]),
            ("\n\r\n", [], []),
            # 只有真正空行被忽略，空格不是空行；不擅自 strip 用户文本。
            (" \n\n", [" "], []),
        )
        for text, pending, hit in cases:
            with self.subTest(text=repr(text)):
                self.assert_matches_groups(text, pending, hit)

    def test_再次置空清除旧文字与分隔线且可重新填充(self):
        for empty in ("", None, "\n\r\n"):
            with self.subTest(empty=repr(empty)):
                self.bubble.setText("待触发 ≥ 123456.78\n已触发 ≤ 87654.32")
                filled_size = self.bubble.size()
                filled_image = self.image(self.bubble)
                self.assert_matches_groups(empty, [], [])
                self.assertLess(self.bubble.height(), filled_size.height(),
                                "置空必须收回旧行与分隔线占位")
                self.assertNotEqual(self.image(self.bubble), filled_image)
                self.assert_matches_groups("已触发 ≤ 1.00", [], ["已触发 ≤ 1.00"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
