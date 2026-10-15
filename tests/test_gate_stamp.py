# -*- coding: utf-8 -*-
"""关卡时间戳（tools/gate_stamp.py）的行为用例。

守的是什么
----------
v1.4.59 之前，build_release.sh 全程没有调用过 run_tests.sh：DMG 覆盖桌面、
/Applications 被替换、线上 OTA 指针被切换，这三件事都发生在一轮回归都没跑过的代码上，
而且**不会有任何报错**。30 轮走查把它记成 S7-②。时间戳是这条链上唯一的凭据，
所以它自己不能是一条「永远通过」或「永远红」的关：

  · 正向：跑完回归必须算数（否则每次发版都白跑两分钟，人很快就会习惯加 --skip-tests）；
  · 反向：回归之后改了任何会进包的东西，必须判过期；
  · 边界：只改文档、只提交（字节没变）不许判过期 —— 这一条是它能不能被长期沿用的关键。

口径：全部在临时 git 仓库里跑，绝不碰真实工作区（真实树里改一个字节就会把本轮
时间戳刷掉，表现为「下一轮不知道为什么又跑了一遍回归」）。
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import gate_stamp as G                   # noqa: E402

G.GIT_ENV = G.git_env()

try:
    from _base import SFCase                    # noqa: E402
except ImportError:
    from tests._base import SFCase              # noqa: E402


class _Repo(SFCase):
    """带一次初始提交的临时仓库：a.py 进包，AGENTS.md 是纯文档。"""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="sf_gate_stamp_test_")
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        self.git("init", "-q")
        self.git("config", "user.email", "gate@example.invalid")
        self.git("config", "user.name", "gate")
        self.write("a.py", "X = 1\n")
        self.write("AGENTS.md", "# doc\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")

    def git(self, *args):
        # env 必须洗掉 GIT_*：本用例在 pre-commit 钩子里也会跑，钩子环境把
        # GIT_DIR 指到真仓库，临时仓库里的 git 会全打错地方（实测同一次提交里
        # 4 条用例因这个红）。判据本身不变，变的是它跑在哪儿。
        return subprocess.run(["git", *args], cwd=self.repo,
                              capture_output=True, text=True,
                              env=G.GIT_ENV)

    def write(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def ok(self):
        """当前时间戳是否可用（顺带把理由留在断言消息里，失败时一眼看懂差在哪）。"""
        usable, why = G.check(self.repo)
        return usable, why

    def cli(self, *argv):
        # 用**当前解释器**而不是仓库里的 .venv：本用例只验函数行为，
        # 但命令行面也要跑得到 —— 关局的判定最终是由 shell 侧的 main() 决定的。
        return subprocess.run([sys.executable, str(ROOT / "tools" / "gate_stamp.py"),
                               *argv], cwd=self.repo, capture_output=True, text=True,
                              env=G.GIT_ENV)


class TestStampLifecycle(_Repo):
    def test_no_stamp_blocks_release(self):
        """从没跑过回归 → 必须拦住。这是本关存在的全部理由。"""
        usable, why = self.ok()
        self.assertFalse(usable, "没有时间戳却判为可用：%s" % why)
        self.assertIn("没有关卡时间戳", why)

    def test_write_then_usable(self):
        """正向对照：刚跑完必须算数，否则它是一道每轮必红、很快被人绕过的关。"""
        G.write_stamp(self.repo)
        usable, why = self.ok()
        self.assertTrue(usable, "刚写的时间戳竟然不可用：%s" % why)

    def test_stamp_records_head_and_file_count(self):
        G.write_stamp(self.repo)
        stamp = G.read_stamp(self.repo)
        self.assertNotEqual(stamp["head"], G.NO_GIT, "临时仓库里 HEAD 应当取得到")
        # 构建输入只收 a.py；AGENTS.md 是文档，build/.gate_stamp 不是输入
        self.assertEqual(stamp["files"], 1, "构建输入清单应当只含 a.py：%r" % stamp)

    def test_command_line_write_and_check(self):
        """命令行面：write 退 0、check 在没写之前退 1（发版脚本只看退出码做判断）。"""
        bad = self.cli("check")
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
        good = self.cli("write")
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        again = self.cli("check")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.cli("--self-test")
        # 未知子命令要给用法而不是静默通过
        self.assertEqual(self.cli("wat").returncode, 2)


class TestFingerprintScope(_Repo):
    """判据范围：会进包的东西都算，纯文字交付物都不算。"""

    def setUp(self):
        super().setUp()
        G.write_stamp(self.repo)

    def test_source_edit_invalidates(self):
        self.write("a.py", "X = 2\n")
        usable, why = self.ok()
        self.assertFalse(usable, "改了进包的代码还判可用：%s" % why)

    def test_new_untracked_source_invalidates(self):
        """未提交的改动同样进包 —— 只看索引的话这一整类改动会隐身。"""
        self.write("new_tool.py", "Y = 1\n")
        usable, why = self.ok()
        self.assertFalse(usable, "新增未跟踪 .py 没算进指纹：%s" % why)

    def test_chmod_only_invalidates(self):
        """只改权限不改字节：内容哈希看不见，靠把权限编进 token 抓到。"""
        os.chmod(os.path.join(self.repo, "a.py"), 0o755)
        usable, why = self.ok()
        self.assertFalse(usable, "chmod 后仍判可用：%s" % why)

    def test_doc_edit_does_not_invalidate(self):
        """文档不进包、用例也不读它：算进来的话每次更新交付单都白跑一轮回归。"""
        self.write("AGENTS.md", "# doc changed\n")
        usable, why = self.ok()
        self.assertTrue(usable, "只改文档就判过期（会逼人加 --skip-tests）：%s" % why)

    def test_bundled_notice_or_dependency_lock_invalidates_stamp(self):
        for name in ("NOTICE.md", "requirements.lock", "tools/signing_policy.json",
                     "tools/camera_auth_probe.swift"):
            with self.subTest(name=name):
                self.write(name, "changed build input\n")
                usable, why = self.ok()
                self.assertFalse(usable, "随包许可或依赖锁变化必须重新验证：%s" % why)

    def test_commit_only_does_not_invalidate(self):
        """HEAD 不是判据：提交本身没改变任何进包字节时，不必重跑回归。"""
        self.git("add", "-A")
        self.git("commit", "-qm", "docs")
        usable, why = self.ok()
        self.assertTrue(usable, "只提交就判过期（HEAD 不该参与判定）：%s" % why)
        self.assertEqual(G.current_info(self.repo)["head"],
                         self.git("rev-parse", "HEAD").stdout.strip(),
                         "HEAD 应当作为留档信息刷新到当前提交")

    def test_ignored_paths_excluded(self):
        """.venv / build / dist 这类被忽略的目录不该影响时间戳。"""
        self.write(".gitignore", "build/\n.venv/\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "ignore")
        before = G.content_fingerprint(self.repo)
        self.write("build/dmg_root/app.py", "print(1)\n")
        self.write(".venv/lib/site.py", "print(2)\n")
        self.assertEqual(G.content_fingerprint(self.repo), before,
                         "被忽略目录里的文件不该算进构建输入指纹")

    def test_release_notes_excluded(self):
        """发布说明是每轮都要改的文字交付物，算进来等于每轮必然重跑回归。"""
        self.write("release_notes.txt", "本版说明\n")
        usable, why = self.ok()
        self.assertTrue(usable, "release_notes.txt 被算进了指纹：%s" % why)


class TestStampCannotLie(_Repo):
    """坏时间戳一律不算数（宁拦不误放）；非 git 目录也不许自洽通过。"""

    def test_corrupt_stamp(self):
        G.write_stamp(self.repo)
        self.write("build/.gate_stamp", "{broken")
        usable, why = self.ok()
        self.assertFalse(usable, "半截 JSON 时间戳竟然可用：%s" % why)

    def test_stamp_without_fingerprint(self):
        G.write_stamp(self.repo)
        self.write("build/.gate_stamp", '{"head": "abc"}')
        usable, why = self.ok()
        self.assertFalse(usable, "缺 fingerprint 的时间戳竟然可用：%s" % why)

    def test_empty_tree_not_usable(self):
        """取不到任何构建输入时，空指纹会和空时间戳自洽通过 —— 必须显式不放行。"""
        outside = os.path.join(self.repo, "empty_dir")
        os.makedirs(outside, exist_ok=True)
        G.write_stamp(outside)          # 先写一枚「看起来对得上」的时间戳
        usable, why = G.check(outside)
        self.assertFalse(usable, "构建输入为 0 却判可用：%s" % why)
        self.assertEqual(G.current_info(outside)["files"], 0)

    def test_self_test_command_passes(self):
        """闸门自带的变异自检必须真的全过（红了就说明它可能是一条永不失败的检查）。"""
        out = self.cli("--self-test")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("0 项失败", out.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
