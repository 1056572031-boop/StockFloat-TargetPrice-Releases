"""Local cache and filesystem boundaries; synthetic feedback only."""

import json
import sqlite3
from contextlib import closing

from sf_feedback import FeedbackProtocolError
from sf_feedback_admin import FeedbackAgent
from sf_feedback_analyzer import analyze_archive
from tests.test_feedback_pipeline import PipelineCase


class AgentGuardTests(PipelineCase):
    def prepare(self):
        path, envelope = self.package()
        receipt = self.inbox.accept(envelope, path.read_bytes())
        agent = FeedbackAgent(self.api, self.root / "admin", analyzer=analyze_archive)
        self.assertEqual(len(agent.run_once()["ready"]), 1)
        return agent, receipt["case_id"]

    def test_missing_and_damaged_cached_archive_are_repaired(self):
        agent, case = self.prepare()
        archive = agent.root / "objects" / (case + ".zip")
        original = archive.read_bytes()
        for damaged in (None, b"invalid", b"x" * len(original)):
            with self.subTest(damaged=damaged is None):
                if damaged is None:
                    archive.unlink()
                else:
                    archive.write_bytes(damaged)
                self.assertEqual(len(agent.run_once()["ready"]), 1)
                self.assertEqual(archive.read_bytes(), original)
        self.assertEqual(agent.run_once()["ready"], [])

    def test_unbound_and_incomplete_report_caches_are_rebuilt(self):
        agent, case = self.prepare()
        for mutate in (
            lambda r: r.update(archive_sha256="0" * 64),
            lambda r: r.update(artifact_sha256={}),
            lambda r: r.update(json_path=str(self.root / "outside.json")),
            lambda r: r.update(markdown_path=r["json_path"]),
            lambda r: r.pop("json_path"),
        ):
            with closing(sqlite3.connect(agent.database)) as db, db:
                report = json.loads(db.execute("SELECT report FROM queue WHERE case_id=?", (case,)).fetchone()[0])
                mutate(report)
                db.execute("UPDATE queue SET report=? WHERE case_id=?", (json.dumps(report), case))
            self.assertEqual(len(agent.run_once()["ready"]), 1)

    def test_directory_and_object_symlinks_cannot_redirect_writes(self):
        for relative in ("objects", "reports", "case", "archive"):
            with self.subTest(relative=relative):
                path, envelope = self.package()
                receipt = self.inbox.accept(envelope, path.read_bytes())
                case = receipt["case_id"]
                agent = FeedbackAgent(self.api, self.root / relative, analyzer=analyze_archive)
                outside = self.root / (relative + "-outside")
                outside.mkdir()
                sentinel = outside / "sentinel"
                sentinel.write_text("untouched")
                target = agent.root / {"objects": "objects", "reports": "reports",
                                       "case": "reports/" + case,
                                       "archive": "objects/" + case + ".zip"}[relative]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(outside if relative != "archive" else sentinel)
                result = agent.run_once()
                self.assertIn(case, result["failed"])
                self.assertEqual(sentinel.read_text(), "untouched")
                self.assertEqual(list(outside.iterdir()), [sentinel])

    def test_corrupt_case_id_and_receipt_cannot_starve_healthy_feedback(self):
        agent, case = self.prepare()
        with closing(sqlite3.connect(agent.database)) as db, db:
            db.execute("INSERT INTO queue VALUES (?, '{}', 'ready', NULL, '{}')", ("../../escaped",))
            db.execute("INSERT INTO queue VALUES (NULL, '{}', 'ready', NULL, '{}')")
            receipt = json.loads(db.execute("SELECT receipt FROM queue WHERE case_id=?", (case,)).fetchone()[0])
            receipt["case_id"] = "feedback-" + "0" * 32
            db.execute("UPDATE queue SET receipt=? WHERE case_id=?", (json.dumps(receipt), case))
        path, envelope = self.package(text="healthy feedback")
        healthy = self.inbox.accept(envelope, path.read_bytes())
        result = agent.run_once()
        self.assertEqual(set(result["failed"]), {case, "invalid_local_case"})
        self.assertEqual(result["failed"].count("invalid_local_case"), 2)
        with closing(sqlite3.connect(agent.database)) as db:
            self.assertEqual(db.execute("SELECT status,error FROM queue WHERE case_id IS NULL").fetchone(),
                             ("failed", "InvalidCaseIdentity"))
        self.assertEqual(result["ready"][0]["case_id"], healthy["case_id"])
        self.assertFalse((self.root / "escaped").exists())

    def test_symlink_database_lock_and_sidecars_are_rejected(self):
        for name in ("queue.sqlite3", "queue.sqlite3-wal", "queue.sqlite3-shm", "agent.lock"):
            directory = self.root / ("guard-" + name)
            directory.mkdir()
            outside = self.root / ("outside-" + name)
            outside.write_text("untouched")
            (directory / name).symlink_to(outside)
            with self.assertRaises(FeedbackProtocolError):
                FeedbackAgent(self.api, directory).run_once()
            self.assertEqual(outside.read_text(), "untouched")

    def test_explicit_absolute_escape_is_rejected(self):
        agent, case = self.prepare()
        for path in (self.root / "outside", agent.root / ".." / "outside"):
            with closing(sqlite3.connect(agent.database)) as db, db:
                report = json.loads(db.execute("SELECT report FROM queue WHERE case_id=?", (case,)).fetchone()[0])
                report["json_path"] = str(path)
                db.execute("UPDATE queue SET report=? WHERE case_id=?", (json.dumps(report), case))
            self.assertEqual(len(agent.run_once()["ready"]), 1)
            self.assertFalse((self.root / "outside").exists())

    def test_report_symlink_is_not_read(self):
        agent, case = self.prepare()
        with closing(sqlite3.connect(agent.database)) as db:
            report = json.loads(db.execute("SELECT report FROM queue WHERE case_id=?", (case,)).fetchone()[0])
        path = agent.root / "reports" / case / "linked.json"
        outside = self.root / "outside-report.json"
        outside.write_text("private local content")
        path.symlink_to(outside)
        report["json_path"] = str(path)
        with closing(sqlite3.connect(agent.database)) as db, db:
            db.execute("UPDATE queue SET report=? WHERE case_id=?", (json.dumps(report), case))
        self.assertEqual(len(agent.run_once()["ready"]), 1)
        self.assertEqual(outside.read_text(), "private local content")
