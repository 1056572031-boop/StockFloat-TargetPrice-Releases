# -*- coding: utf-8 -*-
"""验证回归运行器会拒绝与正式版并行运行 —— 改名后的**两代**名字都算。

v1.4.55 把程序改名成 Skyrocket（bundle id 一并改）。用户机器上可能同时存在
`/Applications/Skyrocket.app` 与还没被替换掉的旧包，两种都在运行时会把
`~/Library/Application Support/StockFloatTargetPrice/config.json` 写歪，
所以守卫必须两代都认。

为什么真起子进程而不是读源码字符串（AGENTS §6 第 2 条例外）：这条判据的价值就在
"脚本跑起来会不会拒绝"，读源码断言不了 shell 的引号/转义/`||` 续行是否写对 ——
本轮改名时正是这里先红：模式串一改，旧桩不再匹配，脚本就直接往下跑整轮回归。
配合 `SF_GUARD_PROBE=1` 短路，判错会立刻返回而不烧掉一次全量。
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_TESTS = PROJECT_ROOT / "run_tests.sh"

# 假 pgrep：只有脚本传来的模式串里含指定片段时，才回答"这个进程在运行"。
# 于是下面四条用例真正验的是"守卫的模式里两代名字都还在"—— 少一个就红。
_STUB = """if [ "$1" = "{flag}" ]; then
  case "$2" in
    *{needle}*) exit 0 ;;
  esac
fi
exit 1"""


class TestRunTestsProductionGuard(unittest.TestCase):
    def _probe(self, pgrep_body):
        with tempfile.TemporaryDirectory() as tmpdir:
            pgrep = Path(tmpdir) / "pgrep"
            pgrep.write_text("#!/usr/bin/env bash\n" + pgrep_body + "\n", encoding="utf-8")
            pgrep.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = "%s:%s" % (tmpdir, env["PATH"])
            env["SF_GUARD_PROBE"] = "1"          # 只验守卫，不许嵌套跑全量
            return subprocess.run(["bash", str(RUN_TESTS)], cwd=PROJECT_ROOT, env=env,
                                  text=True, capture_output=True, check=False, timeout=60)

    def _assert_blocks(self, flag, needle, label):
        result = self._probe(_STUB.format(flag=flag, needle=needle))
        self.assertEqual(result.returncode, 2,
                         "%s 在运行时守卫必须拦（got %r / %r）"
                         % (label, result.returncode, (result.stdout + result.stderr)[-260:]))
        self.assertIn("请先退出后再运行回归", result.stderr)

    def test_blocks_when_new_app_path_is_running(self):
        self._assert_blocks("-f", "/Applications/Skyrocket.app", "新版 Skyrocket")

    def test_blocks_when_legacy_app_path_is_running(self):
        self._assert_blocks("-f", "/Applications/实时股价-目标价版.app", "改名前的旧包")

    def test_blocks_when_new_executable_name_is_running(self):
        self._assert_blocks("-x", "Skyrocket", "新版可执行文件名")

    def test_blocks_when_legacy_executable_name_is_running(self):
        self._assert_blocks("-x", "StockFloatTargetPrice", "改名前的可执行文件名")

    def test_probe_does_not_run_the_suite_when_nothing_is_running(self):
        """没在运行时必须**快速放行**，而不是把整轮回归再跑一遍。"""
        result = self._probe("exit 1")
        self.assertEqual(result.returncode, 0,
                         "守卫不该拦一个没在运行的程序，也不该嵌套跑全量："
                         + (result.stdout + result.stderr)[-260:])


if __name__ == "__main__":
    unittest.main()
