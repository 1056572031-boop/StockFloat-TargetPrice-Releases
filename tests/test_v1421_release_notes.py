# -*- coding: utf-8 -*-
"""v1.4.21 回归：更新弹窗只展示**本次版本**的说明。

现象（用户 2026-09-23 截图）：v1.4.20 的"发现新版本"弹窗里，V1.4.20 / V1.4.19 /
V1.4.18 / V1.4.17 四段说明一起堆在一屏，划不到底。根因是 release_notes.txt 按版本
往上堆，而清单 notes 直接读了整份文件。

两道修复，任一生效都能让弹窗变干净（另一道兜住"线上已有清单是全文"的旧客户端）：
  · 发版侧 tools/make_update_assets.py 只把本次版本那一段写进并签进清单；
  · 显示侧 sf_update.notes_for_version() 在弹窗前再裁一次。

显示侧必须**宽容**：取不到本次版本那一段时原样返回，宁可文字多，也不能把更新
说明显示成空白 —— 用户看不到"这次改了什么"比看到旧格式更糟。
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_system_alert
import sf_update

STACKED = """V1.4.21 发布说明（Beta）:
1. 本次的第一条。
2. 本次的第二条。

V1.4.20 发布说明（Beta）:
1. 上一版的第一条。

V1.4.19 发布说明:
1. 更早的一条。
"""


class TestNotesForVersion(unittest.TestCase):
    """纯函数：sf_update.notes_for_version。"""

    def test_only_the_requested_block_survives(self):
        got = sf_update.notes_for_version(STACKED, "1.4.20")
        self.assertEqual(got, "1. 上一版的第一条。")

    def test_heading_line_itself_is_dropped(self):
        """标题行不进正文：弹窗主文案已经有"发现新版本 vX"了。"""
        got = sf_update.notes_for_version(STACKED, "1.4.21")
        self.assertNotIn("发布说明", got)
        self.assertIn("本次的第二条", got)

    def test_last_block_has_no_following_heading(self):
        got = sf_update.notes_for_version(STACKED, "1.4.19")
        self.assertEqual(got, "1. 更早的一条。")

    def test_beta_suffix_in_heading_still_matches(self):
        """标题写成"V1.4.21 发布说明（Beta）:"也必须认（真实文件就是这样）。"""
        self.assertIn("本次的第一条",
                      sf_update.notes_for_version(STACKED, "1.4.21"))

    def test_unknown_version_falls_back_to_full_text(self):
        """取不到本次那一段时原样返回：宁可多，不能空。"""
        self.assertEqual(sf_update.notes_for_version(STACKED, "9.9.9"),
                         STACKED.strip())

    def test_free_form_notes_pass_through(self):
        self.assertEqual(sf_update.notes_for_version("就修了几个问题", "1.4.21"),
                         "就修了几个问题")

    def test_empty_block_does_not_yield_blank(self):
        raw = "V1.4.21 发布说明:\n\nV1.4.20 发布说明:\n1. 甲"
        self.assertEqual(sf_update.notes_for_version(raw, "1.4.21"), raw.strip())

    def test_empty_input(self):
        self.assertEqual(sf_update.notes_for_version("", "1.4.21"), "")
        self.assertEqual(sf_update.notes_for_version(None, "1.4.21"), "")

    def test_already_sliced_text_is_left_alone(self):
        """发版侧已裁过时，显示侧再裁一次必须幂等。"""
        once = sf_update.notes_for_version(STACKED, "1.4.20")
        self.assertEqual(sf_update.notes_for_version(once, "1.4.20"), once)

    def test_real_release_notes_split_by_version(self):
        """工程里真实的 release_notes.txt：本次版本那块必须是有界的单版本块。

        不断言"别版正文一行都不许出现"—— 各版末尾那句"不变：行情拉取节奏…"本来就
        是重复的通用条款，那样断言会误伤。守的是边界：块里不能混进别的版本标题，
        且明显短于整份文件（整份文件随版本只增不减）。
        """
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        raw = (root / "release_notes.txt").read_text(encoding="utf-8").strip()
        latest = sf_update.APP_VERSION
        mine = sf_update.notes_for_version(raw, latest)
        self.assertTrue(mine, "当前版本 %s 在 release_notes.txt 里必须有说明" % latest)
        self.assertIsNone(
            sf_update._NOTES_HEADING_RE.search(mine),
            "本次说明里混进了其它版本的段落标题")
        self.assertLess(len(mine), len(raw),
                        "裁出来的还是整份文件：弹窗又会一屏堆好几个版本")


class TestPromptUpdateShowsCurrentVersionOnly(unittest.TestCase):
    """行为断言：观察 show_alert 实际收到的文案，不读源码。"""

    def _capture(self, notes, version="1.4.21"):
        import queue
        captured = {}

        def fake_show_alert(**kwargs):
            captured.update(kwargs)
            return object()

        update = sf_update.UpdateInfo(version, notes,
                                      "https://github.com/a/b/releases/download/v/a.zip",
                                      8, "a" * 64)
        fake = type("FakeWindow", (), {
            "_quitting": False,
            "_update_prompt_dialog": None,
            "_ready_update_archive": lambda self, value: None,
            "_dispose_update_dialog": lambda self, dialog: None,
            # v1.4.38：弹窗前先登记「弹在哪块屏」；宿主不是 QWidget，真方法会自己跳过
            "_anchor_update_dialog_screen": lambda self: None,
            "q": queue.Queue(),
        })()
        import stock_float_window
        with patch.object(sf_system_alert, "show_alert", fake_show_alert), \
             patch.object(sf_system_alert, "bundled_icon_path", return_value=None):
            stock_float_window.FloatWindow._prompt_update(fake, update)
        return captured

    def test_stacked_manifest_only_shows_this_version(self):
        """线上旧清单是整份历史：弹窗正文必须只剩本次那一段。"""
        got = self._capture(STACKED)
        self.assertIn("本次的第一条", got["info"])
        self.assertNotIn("上一版的第一条", got["info"])
        self.assertNotIn("更早的一条", got["info"])
        self.assertNotIn("V1.4.20", got["text"])
        self.assertNotIn("发布说明", got["text"])

    def test_sliced_manifest_still_shows_normally(self):
        got = self._capture(sf_update.notes_for_version(STACKED, "1.4.21"))
        self.assertIn("本次的第二条", got["info"])

    def test_empty_notes_keeps_the_fallback_copy(self):
        """裁完是空的也不能显示空白，退回占位文案。"""
        got = self._capture("V1.4.21 发布说明:\n\nV1.4.20 发布说明:\n1. 甲")
        self.assertTrue(got["info"].strip(), "副文案不能为空")

    def test_headline_still_carries_the_version(self):
        got = self._capture(STACKED)
        self.assertIn("v1.4.21", got["message"])


if __name__ == "__main__":
    unittest.main()
