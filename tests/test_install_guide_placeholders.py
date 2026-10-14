# -*- coding: utf-8 -*-
"""v1.4.60（复核 F3）：对外安装说明里不许出现"发给一个不存在的人"这种死路。

起因是实测发出去的 INSTALL.md 里有两处没替换的占位符：
  · `Skyrocket-V%s.dmg`（渲染器少写一步 % 运算）—— 用户对着桌面上真实的
    Skyrocket-V1.4.59.dmg 找不着北；
  · 「请把以下三样发给（发版前把这里换成专用邮箱：SF_CONTACT_EMAIL）」——
    卡住的人照这句话一个人都联系不上。
这个渲染器此前**没有任何用例**（grep render_install_guide tests/ = 0），
所以它把占位符原样发出去时，全量回归一直是绿的。

写法遵守 AGENTS §6：不读源码做字符串断言，判据落在渲染产物与退出码这两件
可观测的事上；临时目录用 addCleanup 收掉。
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

try:
    from _base import SFCase                          # noqa: E402
except ImportError:                                    # pragma: no cover
    from tests._base import SFCase                    # noqa: E402


def _load_guide():
    spec = importlib.util.spec_from_file_location(
        "render_install_guide_under_test",
        os.path.join(ROOT, "tools", "render_install_guide.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _GuideCase(SFCase):
    def setUp(self):
        super().setUp()
        self.g = _load_guide()
        # 不依赖 build/ 里有没有本轮清单：摘要固定给一个，渲染路径才稳定
        self.track_patch(patch.object(self.g, "manifest_sha256",
                                      lambda: "ab" * 32))
        self.tmp = tempfile.mkdtemp(prefix="sf_install_guide_")
        self.out = os.path.join(self.tmp, "docs", "INSTALL.md")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.track_patch(patch.object(self.g, "OUT_DEFAULT",
                                      __import__("pathlib").Path(self.out)))


class RenderedGuideCarriesNoPlaceholders(_GuideCase):
    def test_rendered_text_has_no_literal_percent_s(self):
        with patch.dict(os.environ, {self.g.CONTACT_ENV: "support@example.com"}):
            text = self.g.render()
        self.assertNotIn("%s", text,
                         "文档里留着没替换的 %%s —— 用户会照着一个不存在的文件名去找包")
        # 不判版本号（AGENTS §6 第 1 条）：判"写出去的是一个真实文件名"就够
        self.assertRegex(text, r"Skyrocket-V\d+\.\d+\.\d+\.dmg",
                         "第二种装法必须写出一个真实的 DMG 文件名，而不是占位符")
        self.assertEqual(self.g.lint_placeholders(text), [])

    def test_contact_line_is_the_real_address_when_env_is_set(self):
        with patch.dict(os.environ, {self.g.CONTACT_ENV: "support@example.com"}):
            text = self.g.render()
        self.assertIn("support@example.com", text)
        self.assertNotIn(self.g.CONTACT_ENV, text,
                         "环境变量名不许出现在对外文档里（那是给发版人看的）")


class ContactPlaceholderIsCaughtBeforeItShips(_GuideCase):
    """No private email is required; public Issues is the usable fallback."""

    def _env_without_contact(self):
        env = dict(os.environ)
        env.pop(self.g.CONTACT_ENV, None)
        return patch.dict(os.environ, env, clear=True)

    def test_unset_contact_uses_public_feedback_without_personal_email(self):
        with self._env_without_contact():
            text = self.g.render()
        problems = self.g.lint_placeholders(text)
        self.assertEqual(problems, [])
        self.assertIn("/issues", text)
        self.assertNotIn(self.g.CONTACT_ENV, text)

    def test_explicit_escape_hatch_only_clears_the_contact_item(self):
        with self._env_without_contact():
            text = self.g.render()
        cleared = self.g.lint_placeholders(text, allow_unset_contact=True)
        self.assertFalse(any(self.g.CONTACT_ENV in p for p in cleared))
        # 但占位符本身照样拦：绕联系渠道不等于允许写坏文件名
        self.assertFalse(any("%s" in p for p in cleared))

    def test_main_write_path_refuses_a_leaked_placeholder(self):
        """改坏必红：把 DMG 名那行改回少一步 % 运算的旧写法，写入必须被拒。"""
        broken = "# 安装说明\n1. 双击桌面上的 `Skyrocket-V%s.dmg`；\n"
        with patch.object(self.g, "render", lambda: broken), \
                patch.object(sys, "argv", ["render_install_guide.py"]):
            rc = self.g.main()
        self.assertEqual(rc, 1, "带 %s 的稿子必须拒绝写入")
        self.assertFalse(os.path.exists(self.out), "拒绝写入就不该留下文件")

    def test_check_accepts_the_public_feedback_fallback(self):
        """写入侧允许先出稿（只 WARNING），但 --check 判红 —— 死路发不到用户手上。
        线上可达性一律打桩：用例不许真去 curl GitHub（本仓"测试禁真网络"的约定）。"""
        env = dict(os.environ)
        env.pop(self.g.CONTACT_ENV, None)
        with patch.dict(os.environ, env, clear=True), \
                patch.object(self.g, "probe_asset",
                             lambda url: ("unknown", "用例不联网")):
            with patch.object(sys, "argv", ["render_install_guide.py"]):
                self.assertEqual(self.g.main(), 0, "先出稿这一步不该被拦（否则阻塞发版）")
            self.assertTrue(os.path.exists(self.out))
            with patch.object(sys, "argv", ["render_install_guide.py", "--check"]):
                self.assertEqual(self.g.main(), 0,
                                 "Public feedback is usable without exposing a private email")
            with patch.object(sys, "argv",
                              ["render_install_guide.py", "--check",
                               "--allow-unset-contact"]):
                self.assertEqual(self.g.main(), 0, "显式放行要有出口，与 --skip-tests 同口径")


class SelfTestIsReal(SFCase):
    """链上那条"每个工具都要能自证失败"的用例是**起子进程**跑的，覆盖率看不见它。

    所以这里在同一进程里再跑一遍 self_test()：既把"自检真的会红"钉成判据，
    也避免"为了自检加了几十句代码、账上却变成覆盖率掉了"这种自惩罚形状
    （v1.4.61 实测：加完 self_test 后总数从 84 掉到 83，提交被棘轮拦下）。
    """

    def setUp(self):
        super().setUp()
        self.g = _load_guide()

    def test_self_test_passes_and_is_counted_in_this_process(self):
        self.assertEqual(self.g.self_test(), 0,
                         "渲染器的变异自检不过 —— 链上 6.1 那道门现在不可信")

    def test_self_test_catches_a_blinded_lint(self):
        """把 lint 改瞎（永不告状），自检必须红 —— 否则它是一枚永远不会失败的检查。"""
        with patch.object(self.g, "lint_placeholders", lambda *a, **k: []):
            self.assertEqual(self.g.self_test(), 1,
                             "种了错还报通过 —— 这道自检是说假话的")


class PlantedPlaceholdersAreCaught(SFCase):
    """闸门自己必须能红：种三种错，一种都不能漏。"""

    def setUp(self):
        super().setUp()
        self.g = _load_guide()

    def test_planted_dmg_placeholder(self):
        problems = self.g.lint_placeholders("双击 `Skyrocket-V%s.dmg` 即可")
        self.assertTrue(problems, "字面 %s 漏放 —— 这道门是说谎的")

    def test_planted_contact_placeholder(self):
        text = "请把截图发给（发版前把这里换成专用邮箱：%s）" % self.g.CONTACT_ENV
        self.assertTrue(self.g.lint_placeholders(text),
                        "死路联系方式必须判红，除非显式 --allow-unset-contact")
        self.assertEqual(self.g.lint_placeholders(text, allow_unset_contact=True), [])

    def test_clean_text_passes(self):
        self.assertEqual(self.g.lint_placeholders(
            "双击 Skyrocket-V1.4.60.dmg，发给 support@example.com"), [])


if __name__ == "__main__":
    unittest.main()
