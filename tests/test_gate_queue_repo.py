"""排队提交的端到端：真 git 仓库、真钩子、真的就绪检查与提交核对，只把领票、等锁和便宜检查换成替身。

单元层（tests/test_gate_queue.py）用假依赖证明编排；这里证明编排接上真函数之后，
真实出过的那类失败（暂存区非空、合并现场被改动、输给另一轮回归）确实在该停的地方停。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_release_wrapup import MergeCase  # noqa: E402
from tools import gate_queue as gq  # noqa: E402


def started(_pid):
    return "started"


def forbidden(*_args, **_kwargs):
    raise AssertionError("this step must not run here")


class RealRepoQueue(MergeCase):
    """MergeCase 给出 main 与 side 两条分叉分支和装好的钩子；这里再接上排队编排。"""

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="sf_gq_repo_tickets_")
        self.addCleanup(temp.cleanup)
        self.tickets = pathlib.Path(temp.name)
        self.said, self.waits = [], []
        self.timeline = self.root / "timeline.json"

    def arrive(self, _root, remaining, ticket=None, say=None):
        self.waits.append(remaining)
        return 0.5

    def deps(self, **over):
        base = {"take_ticket": lambda: gq.take_ticket(str(self.tickets), started), "wait": self.arrive,
                "say": self.said.append,
                "prework": lambda *a, **k: {"predict_status": "skipped", "problems": [], "notes": []}}
        base.update(over)
        return base

    def queue_commit(self, **over):
        return gq.run_commit_queue(self.repo, ["base.txt"], self.msg, None, None, True, 60.0, 60.0, 30.0, True,
                                   self.log, self.timeline, self.deps(**over))

    def queue_merge(self, tree, apply=True, **over):
        return gq.run_merge_queue(self.repo, self.msg, None, self.main, self.side, tree, apply, 60.0, 60.0,
                                  self.log, self.timeline, self.deps(**over))

    def leftover_tickets(self):
        return list((self.tickets / "sf_gate_queue").glob("*"))

    # --- 提交：暂存区非空要在领票之前就被拒绝（真实发生过：排完队才发现，白等一轮） -------------------

    def test_a_staged_index_is_refused_before_a_place_in_line_is_taken(self):
        self.change("other.txt", "staged by someone else\n")
        self.git(self.repo, "add", "--", "other.txt")
        self.change()
        with self.assertRaises(gq.Stop) as caught:
            self.queue_commit(take_ticket=forbidden, wait=forbidden, prework=forbidden)
        self.assertEqual(caught.exception.code, gq.EXIT_USAGE)
        self.assertIn("already holds staged changes", str(caught.exception))
        self.assertEqual((self.staged(), self.head(), self.hook_ran()), (["other.txt"], self.main, False))

    def test_a_file_without_a_pending_change_is_refused_before_the_line_too(self):
        with self.assertRaises(gq.Stop) as caught:
            self.queue_commit(take_ticket=forbidden)
        self.assertIn("no pending change in: base.txt", str(caught.exception))

    def test_a_clean_scene_commits_exactly_the_named_file_through_the_real_hook(self):
        self.change()
        result = self.queue_commit()
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["result"]["commit"], self.head())
        self.assertEqual(self.git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.split(),
                         ["base.txt"])
        self.assertTrue(self.hook_ran())
        self.assertEqual(self.leftover_tickets(), [])

    # --- 合并提交 --------------------------------------------------------------------------------------

    def test_the_reviewed_merge_is_committed_through_the_real_hook(self):
        tree = self.start_merge()
        result = self.queue_merge(tree)
        new = self.head()
        self.assertEqual((result["status"], result["result"]["commit"]), ("committed", new))
        self.assertEqual(self.git(self.repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()[1:],
                         [self.main, self.side])
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD^{tree}").stdout.strip(), tree)
        self.assertEqual((w_heads(self.repo), self.hook_ran(), self.leftover_tickets()), ([], True, []))
        events = [item["event"] for item in result["timeline"]]
        self.assertEqual(events, ["frozen", "prework", "ticket", "turn", "commit_started", "committed"])

    def test_a_merge_dry_run_changes_nothing_and_queues_nothing(self):
        tree = self.start_merge()
        result = self.queue_merge(tree, apply=False, take_ticket=forbidden, wait=forbidden, prework=forbidden)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual((self.head(), w_heads(self.repo), self.hook_ran()), (self.main, [self.side], False))

    def test_a_working_file_edited_while_waiting_is_never_committed(self):
        tree = self.start_merge()

        def edit_then_arrive(_root, remaining, ticket=None, say=None):
            self.change("main.txt", "edited while queued\n")
            return 1.0

        with self.assertRaises(gq.Stop) as caught:
            self.queue_merge(tree, wait=edit_then_arrive)
        self.assertEqual(caught.exception.code, gq.EXIT_CHANGED)
        self.assertEqual((self.head(), self.hook_ran(), self.leftover_tickets()), (self.main, False, []))
        self.assertEqual(w_heads(self.repo), [self.side], "the merge stays exactly as it was")

    def test_losing_the_lock_race_keeps_the_merge_and_commits_on_the_second_try(self):
        tree = self.start_merge()
        self.write_hook("#!/bin/sh\nif [ ! -e .git/hook-first ]; then touch .git/hook-first\n"
                        "  echo 已有另一轮完整回归 >&2\n  exit 1\nfi\necho ran >> .git/hook-ran\nexit 0\n")
        result = self.queue_merge(tree)
        self.assertEqual(result["result"]["commit"], self.head())
        self.assertEqual(len(self.waits), 2, "one wait per attempt")
        self.assertIn("刚好输给另一轮完整回归，保留输入不动，重新排队", self.said)

    def test_a_real_gate_failure_is_reported_once_and_not_retried(self):
        tree = self.start_merge()
        self.write_hook("#!/bin/sh\necho 静态闸门红 >&2\nexit 1\n")
        with self.assertRaises(gq.Stop) as caught:
            self.queue_merge(tree)
        self.assertEqual(caught.exception.code, gq.EXIT_FAILED)
        self.assertIn("静态闸门红", str(caught.exception))
        self.assertEqual((len(self.waits), self.head(), w_heads(self.repo)), (1, self.main, [self.side]))

    def test_an_unresolved_conflict_is_refused_before_the_line(self):
        self.start_conflict()
        source = w_heads(self.repo)[0]
        with self.assertRaises(gq.Stop) as caught:
            gq.run_merge_queue(self.repo, self.msg, None, self.head(), source, "", True, 60.0, 60.0, self.log,
                               self.timeline, self.deps(take_ticket=forbidden, prework=forbidden))
        self.assertEqual(caught.exception.code, gq.EXIT_USAGE)
        self.assertIn("unresolved conflicts: base.txt", str(caught.exception))

    def test_a_different_tree_than_the_reviewed_one_is_refused_before_the_line(self):
        self.start_merge()
        with self.assertRaises(gq.Stop) as caught:
            self.queue_merge("0" * 40, take_ticket=forbidden, prework=forbidden)
        self.assertEqual(caught.exception.code, gq.EXIT_CHANGED)
        self.assertIn("tree", str(caught.exception))


def w_heads(repo):
    return gq.wrapup.merge_heads(repo)
