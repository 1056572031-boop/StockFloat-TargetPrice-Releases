"""本地运行状态（自动化日报、事故保全副本、过程计划稿）必须被忽略，且不在版本控制里。

2026-11-05 之前这条只是口头规则：日报被分批提交了 10 份，含个人配置与日志的事故保全副本
躺在没被忽略的目录里，一次 `git add -A` 就会入库。忽略规则加这条判据取代口头提醒。
"""
import os
from pathlib import Path
import subprocess

from tests._base import SFCase

ROOT = Path(__file__).resolve().parents[1]
LOCAL_STATE_SAMPLES = (
    "outputs/todo-runs/history.jsonl",
    "outputs/todo-runs/2099-01-01.md",
    "outputs/config-incident-preservation-20990101/config.json",
    "docs/superpowers/plans/anything.md",
)
LOCAL_STATE_PATHSPECS = ("outputs/todo-runs", "outputs/config-incident-preservation-*",
                         "docs/superpowers")


def git(*args):
    # 钩子环境会把 GIT_DIR / GIT_INDEX_FILE 指向被提交的那棵树，这里要问的是仓库本身
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(["git", *args], cwd=ROOT, env=env, capture_output=True, text=True)


class LocalStateStaysOutOfVersionControl(SFCase):
    def setUp(self):
        super().setUp()
        inside = git("rev-parse", "--is-inside-work-tree")
        if inside.stdout.strip() != "true":
            self.skipTest("不在 git 工作树里，无法核对忽略规则：" + inside.stderr.strip())

    def test_every_local_state_sample_is_ignored_by_rule(self):
        for path in LOCAL_STATE_SAMPLES:
            with self.subTest(path=path):
                # --no-index：只看规则本身，不因文件已被跟踪而放过
                result = git("check-ignore", "--no-index", "-q", "--", path)
                self.assertEqual(result.returncode, 0, "没有被忽略规则覆盖：" + path)

    def test_nothing_under_those_paths_is_tracked(self):
        # 忽略规则对已跟踪的文件不生效，所以还要单独确认没有文件留在版本控制里
        tracked = git("ls-files", "--", *LOCAL_STATE_PATHSPECS).stdout.split("\n")
        tracked = [name for name in tracked if name]
        self.assertEqual(tracked, [], "本地运行状态不该在版本控制里")
