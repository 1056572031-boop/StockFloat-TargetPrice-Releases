# -*- coding: utf-8 -*-
"""v1.4.5 启动位置守卫回归：冻结产物从临时目录误启动时必须"提示 + 正常退出"。

背景：2026-09-22 崩溃报告（v1.3.97）显示进程从 /private/tmp/*/实时股价-目标价版.app
启动，在导入 PyQt6 阶段 SIGSEGV，用户看到"程序意外退出"。守卫是第二道防线
（第一道是 build_release.sh 移走旧产物时把 .app 改名中性化）。
本文件全部是行为断言：只观察"是否退出、是否弹提示"，不读源码做字符串匹配。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_bootstrap


class _Calls:
    """记录 osascript 调用，替代真弹窗。"""

    def __init__(self):
        self.argvs = []

    def run(self, argv, **kwargs):
        self.argvs.append(list(argv))
        return None


class TestTemporaryLocation(unittest.TestCase):
    def test_system_temp_dirs_are_flagged(self):
        with tempfile.TemporaryDirectory(prefix="sf_guard_") as folder:
            exe = os.path.join(folder, "实时股价-目标价版.app",
                               "Contents", "MacOS", "StockFloatTargetPrice")
            self.assertTrue(sf_bootstrap.is_temporary_location(exe), folder)

    def test_applications_dir_is_not_flagged(self):
        exe = ("/Applications/实时股价-目标价版.app/Contents/MacOS/"
               "StockFloatTargetPrice")
        self.assertFalse(sf_bootstrap.is_temporary_location(exe))


class TestGuardTemporaryLaunch(unittest.TestCase):
    def _run_guard(self, frozen, exe, env=None):
        calls = _Calls()
        patches = [
            patch.object(sf_bootstrap.subprocess, "run", calls.run),
            patch.object(sf_bootstrap.sys, "frozen", frozen, create=True),
            patch.object(sf_bootstrap.sys, "executable", exe),
            patch.dict(os.environ, {"SF_ALLOW_TEMP_LAUNCH": env or ""}),
        ]
        for p in patches:
            p.start()
        try:
            sf_bootstrap.guard_temporary_launch()
            exited = None
        except SystemExit as exc:
            exited = exc.code
        finally:
            for p in reversed(patches):
                p.stop()
        return exited, calls

    def test_frozen_copy_in_temp_exits_cleanly_with_alert(self):
        with tempfile.TemporaryDirectory(prefix="sf_guard_") as folder:
            exe = os.path.join(folder, "实时股价-目标价版.app",
                               "Contents", "MacOS", "StockFloatTargetPrice")
            exited, calls = self._run_guard(True, exe)
        self.assertEqual(exited, 0, "必须正常退出，不能进入 Qt 初始化")
        self.assertEqual(len(calls.argvs), 1)
        self.assertEqual(calls.argvs[0][:2], ["/usr/bin/osascript", "-e"])
        self.assertIn("应用程序", calls.argvs[0][2], "提示须告知去「应用程序」打开正式版")

    def test_allow_env_bypasses_guard_for_verification_scripts(self):
        with tempfile.TemporaryDirectory(prefix="sf_guard_") as folder:
            exe = os.path.join(folder, "实时股价-目标价版.app",
                               "Contents", "MacOS", "StockFloatTargetPrice")
            exited, calls = self._run_guard(True, exe, env="1")
        self.assertIsNone(exited)
        self.assertEqual(calls.argvs, [])

    def test_installed_app_launches_untouched(self):
        exe = ("/Applications/实时股价-目标价版.app/Contents/MacOS/"
               "StockFloatTargetPrice")
        exited, calls = self._run_guard(True, exe)
        self.assertIsNone(exited)
        self.assertEqual(calls.argvs, [])

    def test_source_run_is_always_untouched(self):
        # 源码/测试运行没有 sys.frozen，即使解释器位于临时目录也不拦截。
        with tempfile.TemporaryDirectory(prefix="sf_guard_") as folder:
            exe = os.path.join(folder, "python")
            exited, calls = self._run_guard(False, exe)
        self.assertIsNone(exited)
        self.assertEqual(calls.argvs, [])


if __name__ == "__main__":
    unittest.main()
