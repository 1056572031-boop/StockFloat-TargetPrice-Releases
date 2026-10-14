# -*- coding: utf-8 -*-
"""v1.4.6 系统弹窗桥接回归：osascript 脚本构造、结果解析与收口行为。

背景：更新提示改用 macOS 真·系统弹窗（sf_system_alert），样式即系统默认；
本文件全部是行为断言 —— 观察生成的脚本参数与子进程交互，不读实现源码。
"""

import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_system_alert
try:
    from _base import wait_for
except ImportError:  # 兼容两种运行目录（与既有测试同法）
    from tests._base import wait_for


class _FakeProc:
    """假 osascript 子进程：communicate 可注入输出，terminate 可观察。"""

    def __init__(self, out="button returned:立即更新, gave up:false", gate=None):
        self._out = out
        self._gate = gate
        self.terminated = False

    def communicate(self):
        if self._gate is not None:
            self._gate.wait(timeout=5)
        return (self._out, "")

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


class TestBuildLaunch(unittest.TestCase):
    def test_dynamic_text_never_enters_script_source(self):
        """安全核心：外部文本只落在 argv 参数位，脚本源码恒为常量（防注入）。"""
        evil = 'x" & (do shell script "rm -rf /") & "'
        launch = sf_system_alert.build_launch(
            "软件更新", evil, buttons=("暂不更新", "立即更新"),
            default="立即更新", cancel="暂不更新")
        script = launch[0]
        self.assertEqual(script, sf_system_alert.FALLBACK_SCRIPT)
        self.assertNotIn(evil, script, "外部文本绝不能拼进脚本源码")
        self.assertNotIn("do shell script", script)
        self.assertIn(evil, launch, "外部文本应原样作为参数传递")

    def test_argument_layout(self):
        with tempfile.NamedTemporaryFile(suffix=".icns") as handle:
            launch = sf_system_alert.build_launch(
                "软件更新", "发现新版本 v9.9.9。", buttons=("暂不更新", "立即更新"),
                default="立即更新", cancel="暂不更新", icon_path=handle.name)
        # [script, title, text, default, cancel, icon, count, *buttons]
        self.assertEqual(launch[1], "软件更新")
        self.assertEqual(launch[2], "发现新版本 v9.9.9。")
        self.assertEqual(launch[3], "立即更新")
        self.assertEqual(launch[4], "暂不更新")
        self.assertEqual(launch[5], handle.name)
        self.assertEqual(launch[6], "2")
        self.assertEqual(launch[7:], ["暂不更新", "立即更新"])

    def test_blank_title_defaults_and_empty_optional(self):
        launch = sf_system_alert.build_launch(None, "正文")
        self.assertEqual(launch[1], "提示")          # 标题兜底
        self.assertEqual(launch[3], "好")            # 默认按钮=唯一按钮
        self.assertEqual(launch[4], "")              # 无取消
        self.assertEqual(launch[5], "")              # 无图标


class TestParseButton(unittest.TestCase):
    def test_extracts_button_name(self):
        self.assertEqual(sf_system_alert.parse_button("button returned:立即更新, gave up:false"),
                         "立即更新")

    def test_missing_or_garbage_returns_none(self):
        self.assertIsNone(sf_system_alert.parse_button("gave up:true"))
        self.assertIsNone(sf_system_alert.parse_button(""))
        self.assertIsNone(sf_system_alert.parse_button(None))


class TestShowAlert(unittest.TestCase):
    def test_default_button_falls_back_to_last(self):
        captured = {}
        original = sf_system_alert.build_launch

        def spy(*args, **kwargs):
            result = original(*args, **kwargs)
            captured["launch"] = result
            return result

        with patch.object(sf_system_alert, "native_available", lambda: False), \
             patch.object(sf_system_alert, "build_launch", spy), \
             patch.object(sf_system_alert, "SystemDialog", lambda launch, on_result=None: None):
            sf_system_alert.show_alert("t", "x", buttons=("暂不更新", "立即更新"))
        # build_launch 内 default 缺省取末位按钮
        self.assertEqual(captured["launch"][3], "立即更新")


class TestSystemDialog(unittest.TestCase):
    def test_user_click_is_reported(self):
        results = []
        proc = _FakeProc()
        with patch.object(sf_system_alert.subprocess, "Popen", return_value=proc):
            dialog = sf_system_alert.SystemDialog(
                sf_system_alert.build_launch("软件更新", "正文"), on_result=results.append)
            # 等待必须在 patch 生效期内完成，否则后台线程会调用真实 osascript。
            ok = wait_for(lambda: len(results) == 1, timeout=5.0)
        self.assertTrue(ok, "点击结果应回调一次")
        self.assertEqual(results[0], "立即更新")

    def test_close_suppresses_result_and_terminates_process(self):
        gate = threading.Event()
        proc = _FakeProc(gate=gate)
        results = []
        popen_started = threading.Event()
        real_popen_calls = []

        def fake_popen(*args, **kwargs):
            real_popen_calls.append(args)
            popen_started.set()
            return proc

        with patch.object(sf_system_alert.subprocess, "Popen", fake_popen):
            dialog = sf_system_alert.SystemDialog(
                sf_system_alert.build_launch("软件更新", "正文"), on_result=results.append)
            self.assertTrue(wait_for(lambda: bool(real_popen_calls)), "子进程必须被拉起")
            dialog.close()
            self.assertTrue(proc.terminated, "close 必须 terminate 弹窗进程")
            gate.set()
        self.assertTrue(wait_for(lambda: not dialog._thread.is_alive(), timeout=5.0),
                        "后台线程必须随收口结束")
        self.assertEqual(results, [], "程序主动收口不得伪装成用户选择")

    def test_close_is_safe_before_and_after_run(self):
        """close() 幂等，且 osascript 缺失时不抛 —— 全程不得拉起真实弹窗。

        血泪教训（2026-09-22）：本用例原先只给后半段打了桩，前半段
        `SystemDialog(launch)` 在后台线程里真的 Popen 了
        `osascript -e '… display dialog "正文" … with title "软件更新"'`。
        于是每次跑回归（含 pre-commit 关卡、发版前全量）用户桌面都会凭空弹出一个
        「软件更新 / 正文 / 好」系统窗口 —— close() 与子进程启动有竞态，
        terminate 未必赶得上，窗口就留在那儿等人点掉。
        现在整段都在 patch 覆盖内，并显式断言真实 Popen 一次都没被调用。
        """
        launch = sf_system_alert.build_launch("软件更新", "正文")
        started = []

        def fake_popen(*args, **kwargs):
            started.append(args)
            return _FakeProc()

        real_popen = sf_system_alert.subprocess.Popen
        with patch.object(sf_system_alert.subprocess, "Popen", fake_popen):
            dialog = sf_system_alert.SystemDialog(launch)
            dialog.close()
            dialog.close()
            self.assertTrue(wait_for(lambda: not dialog._thread.is_alive(), timeout=5.0),
                            "收口后后台线程必须自行结束")
            with patch.object(sf_system_alert.subprocess, "Popen",
                              side_effect=OSError("no osascript")):
                late = sf_system_alert.SystemDialog(launch)
                late._run()  # 直接跑一次：OSError 被吞，不抛出即通过
        self.assertIs(sf_system_alert.subprocess.Popen, real_popen,
                      "patch 退出后必须还原，不给后续用例留真实弹窗通道")


class TestBundledIcon(unittest.TestCase):
    def test_source_tree_icon_is_found(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not os.path.isfile(os.path.join(root, "icon.icns")):
            self.skipTest("工程根目录无 icon.icns")
        self.assertTrue(sf_system_alert.bundled_icon_path().endswith("icon.icns"))


if __name__ == "__main__":
    unittest.main()
