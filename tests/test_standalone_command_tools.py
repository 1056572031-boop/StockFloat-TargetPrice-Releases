# -*- coding: utf-8 -*-
"""两个"双击运行"的自包含 shell 工具：不在 .app 里、不进覆盖率链，此前零判据。

为什么它们值得单独钉一条（2026-10-12 全方位复核 F2，实测缺陷）：
v1.4.55 全局改名 Skyrocket / com.skyrocket.app 之后，这两个工具仍只写旧名 ——

  · `tools/cleanup_temp_copies.command` 在装机版机器上打印「应用程序未安装本程序」
    然后 `exit 0` 什么都不做。而 AGENTS §3.1 恰恰把它列为"没有仓库的机器上唯一手段"，
    用户以为清理过了，旧副本第二天照样被 LaunchServices 拉起并 SIGSEGV。
  · `tools/verify_responsiveness.command` 直接 `exit 1`，iMac 实机复验整条路线跑不起来。

两处都是"改名要两代都认"这条老规矩的漏点（助手包名是第六处、可执行路径是第七处，
这一枚是第八处）。全量回归当时全绿，因为没有任何用例跑过这两个文件。

判据形状说明：这两个工具会动 /Applications 与系统临时目录（真跑会把用户机器上
的旧副本改名），所以不能像 update_helper 那样端到端跑。能钉住的是它们**认识谁**：
名单必须覆盖 sf_constants 里的两代身份，且不许退回"只写一枚"的形状。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sf_constants as C  # noqa: E402

try:
    from _base import SFCase  # noqa: E402
except ImportError:  # 以包形式被 discover 导入时
    from tests._base import SFCase  # noqa: E402

TOOLS = ("tools/cleanup_temp_copies.command", "tools/verify_responsiveness.command")


def shell_words(text: str, var: str) -> list[str]:
    """取 `VAR="a b c"` 这一行的名单（工具里没有数组，空格分隔就是它的清单）。"""
    m = re.search(r'^%s="([^"]*)"' % var, text, re.M)
    return m.group(1).split() if m else []


def wanted_bundle_ids() -> list[str]:
    return [C.BUNDLE_ID, *C.LEGACY_BUNDLE_IDS]


def wanted_app_names() -> list[str]:
    return [C.APP_DISPLAY_NAME + ".app",
            *(n + ".app" for n in C.LEGACY_APP_DISPLAY_NAMES)]


def uncovered(text: str, var: str, wanted: list[str]) -> list[str]:
    """返回"该认却没认"的名字 —— 空表才算过。"""
    have = shell_words(text, var)
    return [n for n in wanted if n not in have]


class BothGenerationsAreRecognized(SFCase):
    """两代身份都要在名单里：漏哪一代，工具就在那一代的机器上说"没装"。"""

    def _texts(self):
        for rel in TOOLS:
            path = ROOT / rel
            self.assertTrue(path.is_file(), "工具文件不见了：%s" % rel)
            yield rel, path.read_text(encoding="utf-8")

    def test_tools_exist_and_parse_as_shell(self):
        """`sh -n` 只读语法：这两个文件没有任何用例跑过，改坏了要在这里红。"""
        for rel in TOOLS:
            cp = subprocess.run(["/bin/sh", "-n", str(ROOT / rel)],
                                capture_output=True, text=True, timeout=30)
            self.assertEqual(cp.returncode, 0,
                             "%s 语法不过：%s" % (rel, cp.stderr.strip()[:200]))

    def test_bundle_id_list_covers_both_generations(self):
        for rel, text in self._texts():
            self.assertEqual(
                uncovered(text, "BUNDLE_IDS", wanted_bundle_ids()), [],
                "%s 的 BUNDLE_IDS 没覆盖全部包标识 —— 没覆盖那一代的机器上"
                "它会说「未安装本程序」" % rel)

    def test_app_name_list_covers_both_generations(self):
        for rel, text in self._texts():
            self.assertEqual(
                uncovered(text, "APP_NAMES", wanted_app_names()), [],
                "%s 的 APP_NAMES 没覆盖全部包名 —— 「应用程序」里那一代找不到，"
                "工具就在那一代上什么都不做" % rel)

    def test_single_generation_assignment_is_gone(self):
        """退回旧形状（`BUNDLE_ID="…"` 单数、只写一枚）必须被这条抓到。"""
        for rel, text in self._texts():
            self.assertIsNone(
                re.search(r'^BUNDLE_ID="', text, re.M),
                "%s 又出现了单数的 BUNDLE_ID —— 两代名单被换回了一枚" % rel)
            self.assertIsNone(re.search(r'^APP_NAME="', text, re.M),
                              "%s 又出现了单数的 APP_NAME" % rel)


class CriteriaCanFail(SFCase):
    """判据本身要能红：把"只认旧名"这处真实漏法种回去，必须被抓到。"""

    REGRESSED = ('#!/bin/sh\n'
                 'BUNDLE_ID="com.stockfloat.targetprice"\n'
                 'APP_NAME="实时股价-目标价版.app"\n'
                 'INSTALLED="/Applications/$APP_NAME"\n')

    def test_planted_legacy_only_tool_is_caught(self):
        gaps = uncovered(self.REGRESSED, "BUNDLE_IDS", wanted_bundle_ids())
        self.assertIn(C.BUNDLE_ID, gaps,
                      "种了「只认旧名」却没报缺当前这一代 → 这条判据是恒真的")

    def test_planted_third_generation_is_caught(self):
        """将来再改名时，只要 sf_constants 多出一代而工具没跟上，这条就要红。"""
        text = ('BUNDLE_IDS="com.skyrocket.app com.stockfloat.targetprice"\n'
                'APP_NAMES="Skyrocket.app 实时股价-目标价版.app"\n')
        self.assertEqual(uncovered(text, "APP_NAMES",
                                   wanted_app_names() + ["NextName.app"]),
                         ["NextName.app"], "多出来的一代没被发现 → 名单核对失真")

    def test_clean_text_passes(self):
        text = ('BUNDLE_IDS="%s"\nAPP_NAMES="%s"\n'
                % (" ".join(wanted_bundle_ids()), " ".join(wanted_app_names())))
        self.assertEqual(uncovered(text, "BUNDLE_IDS", wanted_bundle_ids()), [])
        self.assertEqual(uncovered(text, "APP_NAMES", wanted_app_names()), [])
