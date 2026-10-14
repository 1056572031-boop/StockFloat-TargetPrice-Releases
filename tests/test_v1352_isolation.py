# -*- coding: utf-8 -*-
"""v1.3.52：测试隔离必须**自动生效**，不能只靠 run_tests.sh 设环境变量。

背景（血泪）：v1.3.50 只在 run_tests.sh 里 export 了 SF_CONFIG_PATH，
工程师直接敲 `python -m unittest tests.test_xxx` 就绕过去了 —— 一次单测把用户
真实配置的 bg 从 glass 写成 translucent，症状伪装成"毛玻璃消失"，排查方向被
带到渲染/并发上。v1.3.52 起隔离下沉到 sf_constants 常量层：只要检测到正在跑
unittest 就自动重定向，任何调用方式都躲不掉。

本用例锁死这个契约：
  1. 当前进程（unittest 下）配置路径必须已重定向；
  2. **不设任何环境变量**开一个干净子进程，只 import unittest + sf_constants，
     也必须被重定向 —— 这是"自动生效"的核心证据，绕不过去；
  3. 冻结产物（sys.frozen）绝不能被重定向（否则打包后配置会写进临时目录）。
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sf_constants  # noqa: E402  （必须在 sys.path 调整后导入）

REAL_CONFIG = os.path.join(sf_constants.APP_SUPPORT_DIR, "config.json")


class TestAutoIsolation(unittest.TestCase):
    def test_config_path_is_redirected_under_unittest(self):
        """当前进程在 unittest 下，配置路径必须不是用户真实路径。"""
        self.assertNotEqual(sf_constants.CONFIG_PATH, REAL_CONFIG,
                            "跑测试时配置路径必须重定向，否则会写坏用户真实配置")
        self.assertTrue(os.environ.get("SF_CONFIG_PATH"),
                        "SF_CONFIG_PATH 必须被自动设置（run_tests.sh 没设时由常量层兜底）")

    def test_last_quote_path_is_redirected_under_unittest(self):
        self.assertTrue(os.environ.get("SF_LAST_QUOTE_PATH"),
                        "首屏快照路径也必须重定向，否则用例之间会互相预填行情")

    def test_clean_subprocess_is_also_redirected(self):
        """核心：不设任何环境变量的干净子进程，也必须被自动重定向。

        这是"绕不过去"的证据 —— 只 export 在 run_tests.sh 里做不到这一点。
        """
        env = dict(os.environ)
        for key in ("SF_CONFIG_PATH", "SF_LAST_QUOTE_PATH", "SF_SEED_FIRST_SCREEN"):
            env.pop(key, None)
        code = ("import unittest, sys, sf_constants;"
                "print(sf_constants.CONFIG_PATH)")
        out = subprocess.run([sys.executable, "-c", code],
                             cwd=str(ROOT), env=env, capture_output=True,
                             text=True, timeout=120)
        self.assertEqual(out.returncode, 0,
                         "子进程执行失败: %s" % (out.stderr[-800:],))
        got = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
        self.assertTrue(got, "子进程未输出配置路径: %s" % (out.stderr[-800:],))
        self.assertNotEqual(got, REAL_CONFIG,
                            "未设环境变量时也必须自动重定向，否则隔离形同虚设")
        self.assertEqual(got, os.path.join(tempfile.gettempdir(),
                                           "sf_config_test.json"))

    def test_frozen_build_is_never_redirected(self):
        """冻结产物绝不能被重定向：源码里必须有 sys.frozen 这道闸。"""
        src = (ROOT / "sf_constants.py").read_text(encoding="utf-8")
        self.assertIn('getattr(sys, "frozen", False)', src,
                      "常量层隔离必须用 sys.frozen 排除打包产物")
        self.assertIn('"unittest" in sys.modules', src,
                      "常量层隔离必须靠 unittest 在 sys.modules 中判定")


if __name__ == "__main__":
    unittest.main()
