# -*- coding: utf-8 -*-
"""回归运行器的"不许弹真实窗口"关卡（tools/test_runner.py ⑤）。

由来：用户桌面"时不时"冒出一个「软件更新 / 正文 / 好」系统窗口。查清来源不是
App —— 产品文案里没有"正文"，它是 tests/test_v146_systemalert.py 一条漏打桩的
用例真的 Popen 了 osascript display dialog。回归跑在用户机器上（pre-commit、
发版前全量），于是每次提交都可能往桌面砸窗口。

本文件守的是关卡本身：漏打桩时必须**被拦截 + 记违规**，且不能误伤确实需要
真实子进程的用例（sys.executable 隔离子进程、bash run_tests.sh、git）。
"""

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load_runner():
    """tools/ 不是包，按文件路径加载（与 tests/test_verify_bundle_tools.py 一致）。"""
    spec = importlib.util.spec_from_file_location(
        "sf_test_runner_under_test", os.path.join(ROOT, "tools", "test_runner.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestDescribeUiCommand(unittest.TestCase):
    def setUp(self):
        self.runner = _load_runner()

    def test_real_dialog_command_shape_is_detected(self):
        """关卡必须认得 sf_system_alert 降级通道的真实调用形态。"""
        import sf_system_alert
        launch = sf_system_alert.build_launch("软件更新", "正文")
        summary = self.runner.describe_ui_command(
            ["/usr/bin/osascript", "-e"] + launch)
        self.assertIsNotNone(summary, "osascript display dialog 必须被识别为弹窗")
        self.assertIn("软件更新", summary)
        self.assertIn("正文", summary)

    def test_notification_and_alert_variants_detected(self):
        for marker in ("display notification", "display alert"):
            cmd = ["osascript", "-e", '%s "x"' % marker]
            self.assertIsNotNone(self.runner.describe_ui_command(cmd), marker)

    def test_non_dialog_commands_pass_through(self):
        """放行的口径：不是"osascript + display dialog/notification/alert"就不拦。"""
        for cmd in (
                [sys.executable, "-c", "print(1)"],
                ["bash", os.path.join(ROOT, "run_tests.sh")],
                ["git", "status", "--short"],
                ["/usr/bin/osascript", "-e", 'id of app "Finder"'],
                ["hdiutil", "create", "-volname", "StockFloat"],
                None, 42, object()):
            self.assertIsNone(self.runner.describe_ui_command(cmd), repr(cmd)[:60])


class TestInstallGuard(unittest.TestCase):
    def setUp(self):
        self.runner = _load_runner()
        self.runner.UI_DIALOG_VIOLATIONS.clear()
        self.addCleanup(self.runner.UI_DIALOG_VIOLATIONS.clear)
        self._orig_popen = subprocess.Popen
        self._orig_run = subprocess.run
        self.addCleanup(self._restore)
        self.through = []

        def recorder(cmd, *args, **kwargs):
            self.through.append(cmd)
            return "PASSED_THROUGH"

        subprocess.Popen = recorder
        subprocess.run = recorder
        self.runner.install_ui_dialog_guard()

    def _restore(self):
        subprocess.Popen = self._orig_popen
        subprocess.run = self._orig_run

    def test_dialog_popen_is_blocked_without_executing(self):
        with self.assertRaises(self.runner.UiDialogBlocked):
            subprocess.Popen(["/usr/bin/osascript", "-e", "display dialog \"正文\""])
        self.assertEqual(self.through, [], "拦截必须在真实子进程之前，不能先跑再报")
        self.assertEqual(len(self.runner.UI_DIALOG_VIOLATIONS), 1)

    def test_blocked_error_is_oserror_so_callers_degrade(self):
        """继承 OSError：sf_system_alert / sf_notify 既有降级路径吞的就是它。"""
        self.assertTrue(issubclass(self.runner.UiDialogBlocked, OSError))

    def test_run_path_returns_failure_instead_of_popping(self):
        result = subprocess.run(
            ["osascript", "-e", 'display notification "x" with title "y"'],
            check=False, capture_output=True)
        self.assertEqual(self.through, [])
        self.assertEqual(result.returncode, 1, "降级方按 osascript 失败 处理")
        self.assertEqual(len(self.runner.UI_DIALOG_VIOLATIONS), 1)

    def test_ordinary_subprocess_still_passes_through(self):
        self.assertEqual(
            subprocess.Popen([sys.executable, "-c", "print(1)"]), "PASSED_THROUGH")
        self.assertEqual(len(self.through), 1)
        self.assertEqual(self.runner.UI_DIALOG_VIOLATIONS, [])

    def test_violation_names_the_case_that_caused_it(self):
        self.runner._current_case[0] = "test_something.TestX.test_y"
        with contextlib.suppress(self.runner.UiDialogBlocked):
            subprocess.Popen(["/usr/bin/osascript", "-e", "display dialog \"正文\""])
        case, _summary = self.runner.UI_DIALOG_VIOLATIONS[0]
        self.assertEqual(case, "test_something.TestX.test_y",
                         "报红必须指到用例，否则又要人当场猜是哪条")


class TestReporting(unittest.TestCase):
    def setUp(self):
        self.runner = _load_runner()
        self.runner.UI_DIALOG_VIOLATIONS.clear()
        self.addCleanup(self.runner.UI_DIALOG_VIOLATIONS.clear)

    def test_clean_run_reports_no_violation(self):
        self.assertFalse(self.runner._report_ui_dialog_violations())

    def test_report_lists_each_offending_case_once(self):
        self.runner.UI_DIALOG_VIOLATIONS.extend([
            ("a.test_1", "osascript … display dialog"),
            ("a.test_1", "osascript … display dialog"),
            ("b.test_2", "osascript … display notification"),
        ])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertTrue(self.runner._report_ui_dialog_violations())
        out = buf.getvalue()
        self.assertIn("a.test_1", out)
        self.assertIn("b.test_2", out)
        self.assertEqual(out.count("osascript … display dialog"), 1,
                         "同一用例重复弹窗只报一条，避免刷屏")


if __name__ == "__main__":
    unittest.main()
