# -*- coding: utf-8 -*-
"""把下载入口（services/download-analytics）的 node 测试接进提交关卡。

为什么需要：那组测试守着下载页的真实下载链接、版本与大小注入、安全策略、素材登记、
隐私说明和构建脚本，但 `run_tests.sh`、提交钩子、云端回归都只跑 Python 用例 ——
它们写在 `services/` 里，没人调用就等于不存在，页面一被改坏，关卡照样全绿。
这里起一个 `node --test` 子进程，退出码非零就红，输出尾部带进失败信息。

边界：
  - 只在本机/云端有 node 时跑；没有就跳过并说明原因（不假装通过）。
  - 子进程不联网、不写仓库：构建测试把产物写进临时目录，图标导出用假程序顶替。
  - 不判断页面好不好看，只判断那组断言是否全绿。
"""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from _base import SFCase                        # noqa: E402
except ImportError:                                 # 以包形式被 discover 导入时
    from tests._base import SFCase                  # noqa: E402

SERVICE = ROOT / "services" / "download-analytics"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "本机没有 node，跳过下载入口的 node 测试")
class DownloadEntryNodeSuite(SFCase):
    def test_service_node_tests_all_pass(self):
        files = sorted(str(p.relative_to(SERVICE)) for p in (SERVICE / "tests").glob("*.test.mjs"))
        self.assertGreaterEqual(len(files), 2, "测试文件少于预期，说明目录被挪动或清空，不能当作通过")
        result = subprocess.run([NODE, "--test", *files], cwd=SERVICE, capture_output=True,
                                text=True, errors="replace", timeout=120)
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-25:])
        self.assertEqual(result.returncode, 0, "下载入口的 node 测试没有全绿：\n" + tail)
        self.assertRegex(result.stdout, r"(?m)^ℹ fail 0$", "输出里没有‘fail 0’，不能当作通过")
        self.assertNotRegex(result.stdout, r"(?m)^ℹ tests 0$", "一条用例都没跑，不能当作通过")


if __name__ == "__main__":
    unittest.main()
