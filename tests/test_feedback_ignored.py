"""已忽略状态：被忽略的案件不再下载、分析、导出，取消忽略后原样恢复。只用合成数据。"""

import fcntl
import io
import json
import sqlite3
import sys
from contextlib import closing
from unittest.mock import patch

from sf_feedback import FeedbackProtocolError
from sf_feedback_admin import IGNORED, FeedbackAgent
from sf_feedback_analyzer import analyze_archive
from sf_feedback_notifications import report_versions
from tests.test_feedback_pipeline import PipelineCase
from tools import feedback_agent
from tools import feedback_desktop_reports as desktop

ABSENT_CASE = "feedback-" + "0" * 32


class IgnoredStatusTests(PipelineCase):
    def prepare(self):
        path, envelope = self.package()
        receipt = self.inbox.accept(envelope, path.read_bytes())
        agent = FeedbackAgent(self.api, self.root / "admin", analyzer=analyze_archive)
        self.assertEqual(len(agent.run_once()["ready"]), 1)
        return agent, receipt["case_id"]

    def status(self, agent, case):
        with closing(sqlite3.connect(agent.database)) as db:
            return db.execute("SELECT status FROM queue WHERE case_id=?", (case,)).fetchone()[0]

    def test_an_ignored_case_is_not_repaired_reanalyzed_or_reset_by_discovery(self):
        agent, case = self.prepare()
        self.assertEqual(agent.set_ignored([case])["changed"], [case])
        archive = agent.root / "objects" / (case + ".zip")
        archive.unlink()
        result = agent.run_once()
        self.assertEqual((result["ready"], result["failed"], result["ignored"]), ([], [], 1))
        self.assertFalse(archive.exists(), "已忽略的案件缺了反馈包也不许再下载")
        self.assertEqual(self.status(agent, case), IGNORED, "回执仍在收件箱里，发现流程也不许把它改回待处理")

    def test_an_ignored_case_is_neither_exported_nor_announced(self):
        agent, case = self.prepare()
        self.assertEqual(desktop.export_reports(agent.root, self.root / "before")["cases"], 1)
        agent.set_ignored([case])
        after = desktop.export_reports(agent.root, self.root / "after")
        self.assertEqual((after["cases"], after["failed"]), (0, []))
        self.assertFalse((self.root / "after" / case).exists())
        self.assertEqual(report_versions(agent.root), {})

    def test_unignore_restores_a_cached_report_without_reanalyzing_it(self):
        agent, case = self.prepare()
        agent.set_ignored([case])
        self.assertEqual(agent.set_ignored([case], ignored=False)["changed"], [case])
        self.assertEqual(self.status(agent, case), "ready")
        self.assertEqual(agent.run_once()["ready"], [], "缓存仍然有效，取消忽略不该触发重新分析")
        self.assertEqual(desktop.export_reports(agent.root, self.root / "again")["cases"], 1)

    def test_unignore_repairs_what_went_missing_while_ignored(self):
        agent, case = self.prepare()
        agent.set_ignored([case])
        (agent.root / "objects" / (case + ".zip")).unlink()
        agent.set_ignored([case], ignored=False)
        self.assertEqual(len(agent.run_once()["ready"]), 1)

    def test_unignore_without_a_report_goes_back_to_pending_and_is_analyzed(self):
        agent, case = self.prepare()
        agent.set_ignored([case])
        with closing(sqlite3.connect(agent.database)) as db, db:
            db.execute("UPDATE queue SET report=NULL WHERE case_id=?", (case,))
        agent.set_ignored([case], ignored=False)
        self.assertEqual(self.status(agent, case), "pending")
        self.assertEqual(len(agent.run_once()["ready"]), 1)

    def test_repeating_and_unknown_ids_are_reported_not_applied(self):
        agent, case = self.prepare()
        first = agent.set_ignored([case, case, ABSENT_CASE])
        self.assertEqual(first, {"changed": [case], "unchanged": [], "missing": [ABSENT_CASE]})
        second = agent.set_ignored([case])
        self.assertEqual(second, {"changed": [], "unchanged": [case], "missing": []})
        never_ignored = agent.set_ignored([ABSENT_CASE], ignored=False)
        self.assertEqual(never_ignored["missing"], [ABSENT_CASE])

    def test_a_malformed_id_rejects_the_whole_batch_before_touching_the_queue(self):
        agent, case = self.prepare()
        for bad in ("../escaped", "feedback-xyz", "", None):
            with self.subTest(bad=bad), self.assertRaises(FeedbackProtocolError):
                agent.set_ignored([case, bad])
            self.assertEqual(self.status(agent, case), "ready")

    def test_changing_the_queue_is_refused_while_the_agent_holds_its_lock(self):
        agent, case = self.prepare()
        with (agent.root / "agent.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(FeedbackProtocolError):
                agent.set_ignored([case])
        self.assertEqual(self.status(agent, case), "ready")
        self.assertEqual(agent.set_ignored([case])["changed"], [case], "锁释放后可以正常标记")

    def run_cli(self, agent, *arguments):
        argv = ["feedback_agent", "--data-dir", str(agent.root), *arguments]
        with patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch.object(feedback_agent.subprocess, "run") as token_call, \
                patch.object(feedback_agent, "GitHubAPI") as github:
            code = feedback_agent.main()
        self.assertFalse(token_call.called, "忽略/取消忽略只改本地队列，不许去取令牌")
        self.assertFalse(github.called, "忽略/取消忽略不许联网")
        return code, json.loads(out.getvalue())

    def test_cli_ignores_and_restores_without_a_token_or_network(self):
        agent, case = self.prepare()
        code, outcome = self.run_cli(agent, "--ignore", case)
        self.assertEqual((code, outcome["changed"]), (0, [case]))
        self.assertEqual(self.status(agent, case), IGNORED)
        code, outcome = self.run_cli(agent, "--unignore", case)
        self.assertEqual((code, outcome["changed"]), (0, [case]))
        self.assertEqual(self.status(agent, case), "ready")

    def test_cli_reports_unknown_ids_with_a_failing_exit_code(self):
        agent, case = self.prepare()
        code, outcome = self.run_cli(agent, "--ignore", case, ABSENT_CASE)
        self.assertEqual((code, outcome["missing"], outcome["changed"]), (1, [ABSENT_CASE], [case]))

    def test_cli_rejects_both_directions_at_once_and_surfaces_a_busy_queue(self):
        agent, case = self.prepare()
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(agent, "--ignore", case, "--unignore", case)
        self.assertEqual(caught.exception.code, 2)
        with (agent.root / "agent.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, outcome = self.run_cli(agent, "--ignore", case)
        self.assertEqual((code, outcome["error"], outcome["error_type"]),
                         (1, "queue_update_failed", "FeedbackProtocolError"))
        self.assertEqual(self.status(agent, case), "ready")
