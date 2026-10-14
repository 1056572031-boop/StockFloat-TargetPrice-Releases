# -*- coding: utf-8 -*-
"""`tools/shell_cjk_scan.py` 的判据：shell 里 `$NAME` 紧跟中文标点，一律硬零。

为什么这属于"第一道防线"而不是书写洁癖（全部实测，2026-10-13）：
macOS 自带的 `/bin/sh` 与 `/bin/bash` 是同一份 bash 3.2.57，它在 **UTF-8 locale** 下
扫描变量名时，会把紧跟其后的多字节字符的**首字节当成名字的一部分**：

    DEST=/Applications/Skyrocket.app
    echo "旧版本来在 $DEST，但没能移到废纸篓"
    → /bin/sh: DEST\xEF: unbound variable      （set -u 下当场终止，rc 127）

这枚缺陷当时就躺在 `installer/一键安装.command` 的**错误分支**里 —— 也就是
"旧版移不动"（标准用户 / 别人装过的机器）最常见的那一屏。用户看到的不是指引，
而是一行 unbound variable，违反这个脚本自己的 FR-12「失败必须打印下一步」。

**为什么全量回归两年都是绿的**：这台开发机的 shell 没有设 `LANG`，走的是安全一侧；
而用户机器的 AppleLocale 是 zh_CN → 终端给的是 `LANG=zh_CN.UTF-8`，走的是必炸那一侧。
所以这里除了"扫得对不对"，还必须钉住**机制本身还成立**（下面 TestMechanism 那两条），
并且核验器要站在用户那一侧跑（`tools/verify_install_paths.py` 的 locale 判据 + 变异 S10）。

判据四件事：
  1. 全仓零命中（硬零、不留基线 —— 留基线等于承认还可以有下一处，同 §5.2 裸 hex 那条口径）；
  2. 扫描范围不许静默失效（少于下限就拒绝下结论，`test_discipline_scan.min_files` 同族）；
  3. 种错必须被抓到、正当写法必须放行（`$1，` `$#，` 与 `${V}，` 都不该报）；
  4. 机制本身可失败：UTF-8 locale 下 bash 3.2 真炸、`${V}` 写真不炸。
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import shell_cjk_scan as SCAN                     # noqa: E402

try:
    from _base import SFCase                        # noqa: E402
except ImportError:                                 # 以包形式被 discover 导入时
    from tests._base import SFCase                  # noqa: E402

# 用户那一侧的 locale。取不到就跳过机制判据并说清为什么（不假装通过）。
USER_LANG = "zh_CN.UTF-8"


def locale_available(name: str) -> bool:
    p = subprocess.run(["/usr/bin/locale", "-a"], capture_output=True, text=True)
    return name in set(p.stdout.split())


def sh_line(body: str, lang: str) -> tuple[int, str]:
    """在指定 locale 下真跑一行 shell（只跑这里手写的样例，不执行仓库脚本）。"""
    p = subprocess.run(["/usr/bin/env", "LANG=%s" % lang, "LC_ALL=%s" % lang,
                        "/bin/sh", "-c", body],
                       capture_output=True, text=True, errors="replace")
    return p.returncode, (p.stdout + p.stderr)


class TestRepoIsClean(SFCase):
    """这一版交出去的时候，全仓一处都不许有。"""

    def test_全仓零命中(self):
        hits = SCAN.scan()
        self.assertEqual(hits, [], "这些行会让用户终端当场崩在错误分支上：%s" % hits[:6])

    def test_扫描范围真的覆盖到链上脚本(self):
        """硬零若是"什么都没扫到"换来的，就是一道永不失败的关。"""
        files = {p.name for p in SCAN.iter_shell_files()}
        for must in ("build_release.sh", "run_tests.sh", "update_helper.sh"):
            self.assertIn(must, files, "%s 没进扫描范围" % must)
        self.assertGreaterEqual(len(files), SCAN.MIN_SHELL_FILES,
                                "只扫到 %d 个文件，低于下限 %d" % (len(files), SCAN.MIN_SHELL_FILES))

    def test_安装脚本在范围内(self):
        """这轮改的就是它；它哪天脱了账，下一条判据会一起变空转。"""
        names = [p.name for p in SCAN.iter_shell_files()]
        self.assertTrue(any(n.endswith(".command") for n in names), names)


class TestScanRules(SFCase):
    def test_命名变量紧跟全角逗号必须命中(self):
        self.assertEqual([h[1] for h in SCAN.find_hits('echo "旧版在 $DEST，"\n')],
                         ["$DEST，"])

    def test_一行两处都要命中(self):
        self.assertEqual(len(SCAN.find_hits('echo "$A，$B，"\n')), 2)

    def test_花括号写法放行(self):
        self.assertEqual(SCAN.find_hits('echo "旧版在 ${DEST}，"\n'), [])

    def test_位置与特殊变量放行(self):
        """实测它们不吞字节（单字符名字），所以不许报 —— 报了就是判据过宽，
        下一轮人会为了让它绿而给整类写法开豁免。"""
        for body in ('echo "参数是$1，"\n', 'echo "计数=$#，"\n', 'echo "星=$*，"\n'):
            self.assertEqual(SCAN.find_hits(body), [], body)

    def test_注释行放行(self):
        """解释这条坑本身就得写 `$DEST，` 这种样子；不执行的字串不该挡人写文档。"""
        self.assertEqual(SCAN.find_hits('# 坑在于 $DEST， 这种写法\n'), [])

    def test_非注释里的说明也必须走花括号(self):
        """注释判据只认"整行以 # 开头"，行中 # 不算 —— 免得 `say "# $V，"` 蒙过去。"""
        self.assertEqual(len(SCAN.find_hits('say "备注 # $V，"\n')), 1)


class TestScopeRedLine(SFCase):
    def test_空目录拒绝下结论(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SCAN.ScopeBroken):
                SCAN.scan(Path(tmp), min_files=1)

    def test_工具在无命中时退零有命中时退一(self):
        """退出码判据走子进程：主进程直调会把这块账记错，也与发版链的调用形状不同。"""
        py = str(ROOT / ".venv" / "bin" / "python")
        tool = str(ROOT / "tools" / "shell_cjk_scan.py")
        rc = subprocess.run([py, tool], capture_output=True, text=True).returncode
        self.assertEqual(rc, 0, "全仓应当零命中：%s" % rc)

    def test_未知参数退二且不落到默认动作(self):
        py = str(ROOT / ".venv" / "bin" / "python")
        tool = str(ROOT / "tools" / "shell_cjk_scan.py")
        p = subprocess.run([py, tool, "--definitely-not-a-flag"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)

    def test_自检必须过(self):
        p = subprocess.run([str(ROOT / ".venv" / "bin" / "python"),
                            str(ROOT / "tools" / "shell_cjk_scan.py"), "--self-test"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


@unittest.skipUnless(locale_available(USER_LANG),
                     "本机没有 %s 这个 locale，机制判据跑不了（不是通过）" % USER_LANG)
class TestMechanism(SFCase):
    """判据的**理由**也要有判据：这条坑还在，且花括号确实修得好。

    没有这一组，下一轮升 macOS / 换 shell 之后，"为什么硬零"就只剩文档在说。
    """

    def test_utf8_locale_下_bash32_真的炸(self):
        rc, out = sh_line('set -u\nV=x\necho "旧版在 $V，但"\n', USER_LANG)
        self.assertNotEqual(rc, 0, "机制变了：这一族缺陷不再成立，判据要重新谈")
        self.assertIn("unbound variable", out)
        self.assertIn("V", out)

    def test_花括号写法在同一_locale_下正常(self):
        rc, out = sh_line('set -u\nV=Sky\necho "旧版在 ${V}，但"\n', USER_LANG)
        self.assertEqual(rc, 0, out)
        self.assertIn("旧版在 Sky，但", out)

    def test_不设置_set_u_时是值消失而不是报错(self):
        """更阴的那一半：静默把变量值吞掉。所以"我们的脚本没开 set -u"不是免死金牌。"""
        rc, out = sh_line('V=Sky\necho "名字：$V（完）"\n', USER_LANG)
        self.assertEqual(rc, 0)
        self.assertNotIn("Sky", out, "值没被吞掉：判据的理由需要更新")


if __name__ == "__main__":
    unittest.main()
