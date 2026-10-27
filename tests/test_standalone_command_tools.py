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


class RunningProcessPatternFollowsTheLists(SFCase):
    """找「正在运行的程序」的 pgrep 模式必须由两张名单拼出来，不能另写一份字面量。

    复验工具里名单和 pgrep 模式原先各写一遍：将来名单多一代，模式漏跟，
    工具就会在那一代的机器上说「未在运行」而去让人先启动程序。
    """

    TOOL = "tools/verify_responsiveness.command"

    def _built_pattern(self):
        text = (ROOT / self.TOOL).read_text(encoding="utf-8")
        start = text.index('PGREP_PATTERN=""')
        end = text.index('PID="$(pgrep', start)
        script = "\n".join([
            'BUNDLE_IDS="%s"' % " ".join(shell_words(text, "BUNDLE_IDS")),
            'APP_NAMES="%s"' % " ".join(shell_words(text, "APP_NAMES")),
            text[start:end],
            'printf "%s" "$PGREP_PATTERN"',
        ])
        cp = subprocess.run(["/bin/sh", "-c", script], capture_output=True,
                            text=True, timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout

    def test_pattern_contains_every_wanted_identity(self):
        parts = self._built_pattern().split("|")
        for name in wanted_bundle_ids() + wanted_app_names():
            self.assertIn(name, parts, "pgrep 模式漏了 %s —— 那一代的程序会被说成「未在运行」" % name)

    def test_pattern_has_no_empty_alternative(self):
        """空的备选项会让 pgrep -f 匹配到任何进程。"""
        self.assertNotIn("", self._built_pattern().split("|"))

    def _run_with_fake_pgrep(self, bundle_ids, app_names, files=()):
        """真跑「拼模式 + 调 pgrep」那一段；pgrep 换成只记参数的桩，返回它收到的参数。"""
        import os
        import tempfile
        text = (ROOT / self.TOOL).read_text(encoding="utf-8")
        start = text.index('PGREP_PATTERN=""')
        end = text.index('\n[ -n "$PID" ]', start)
        with tempfile.TemporaryDirectory(prefix="sf_pgrep_stub_") as directory:
            work = pathlib.Path(directory)
            for name in files:
                (work / name).write_text("", encoding="utf-8")
            fake_bin = work / "bin"
            fake_bin.mkdir()
            record = work / "pgrep.args"
            stub = fake_bin / "pgrep"
            stub.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$PGREP_RECORD"\n', encoding="utf-8")
            stub.chmod(0o755)
            script = "BUNDLE_IDS='%s'\nAPP_NAMES='%s'\n%s\n" % (bundle_ids, app_names, text[start:end])
            env = dict(os.environ, PGREP_RECORD=str(record),
                       PATH=str(fake_bin) + ":" + os.environ.get("PATH", ""))
            cp = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True,
                                timeout=30, cwd=str(work), env=env)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            return record.read_text(encoding="utf-8").splitlines()

    def test_pgrep_really_receives_the_built_pattern(self):
        """拼出来的模式必须真的交给 pgrep -f，而不是只拼不用。"""
        text = (ROOT / self.TOOL).read_text(encoding="utf-8")
        ids, names = shell_words(text, "BUNDLE_IDS"), shell_words(text, "APP_NAMES")
        got = self._run_with_fake_pgrep(" ".join(ids), " ".join(names))
        self.assertEqual(got, ["-f", "|".join(ids + names)])

    def test_wildcards_in_the_lists_are_not_expanded_into_file_names(self):
        """名单项里的 * 不许被展开成当前目录的文件名（否则会拼出一串无关备选项）。"""
        got = self._run_with_fake_pgrep("com.example.app", "My*.app",
                                        files=("MyFile.app", "Other.txt"))
        self.assertEqual(got, ["-f", "com.example.app|My*.app"])


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
