"""Desktop delivery retains original bytes and never overwrites user changes."""

import hashlib
import io
import json
import os
import runpy
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sf_feedback_admin import FeedbackAgent
from sf_feedback_analyzer import analyze_archive
from sf_feedback_github import GitHubInbox
from tests._base import SFCase
from tests.test_feedback_full_bundle import full_packet
from tests.test_feedback_pipeline import MemoryGitHub
from tools import feedback_agent
from tools import feedback_desktop_reports as desktop


class DesktopReportTests(SFCase):
    def case(self, root, api=None):
        api = MemoryGitHub() if api is None else api
        path, envelope, _content = full_packet(root)
        receipt = GitHubInbox(api).accept(envelope, path.read_bytes())
        agent = FeedbackAgent(api, root / "data", analyzer=analyze_archive)
        result = agent.run_once()
        self.assertEqual(result["failed"], [])
        self.assertTrue(result["ready"])
        return api, receipt["case_id"], agent

    def test_reports_and_original_packet_are_exported_once_with_matching_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _api, case, agent = self.case(root)
            destination = root / "desktop"
            first = desktop.export_reports(agent.root, destination)
            self.assertEqual(first["failed"], [])
            self.assertEqual(first["copied_files"], 3)
            self.assertEqual((destination / case / (case + ".zip")).read_bytes(),
                             (agent.root / "objects" / (case + ".zip")).read_bytes())
            self.assertEqual(desktop.export_reports(agent.root, destination)["copied_files"], 0)

    def test_modified_desktop_report_is_not_overwritten_and_other_cases_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            api, first_case, agent = self.case(root)
            destination = root / "desktop"
            desktop.export_reports(agent.root, destination)
            report = next((destination / first_case).glob("*.md"))
            report.write_text("owner notes")
            _api, second_case, _agent = self.case(root, api)
            result = desktop.export_reports(agent.root, destination)
            self.assertEqual(result["failed"][0]["case_id"], first_case)
            self.assertEqual(report.read_text(), "owner notes")
            self.assertTrue((destination / second_case / (second_case + ".zip")).exists())

    def test_invalid_case_path_and_receipt_cannot_export_unbound_archives(self):
        for fault in ("identity", "path", "receipt", "source_hash", "case_link", "object_link"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _api, case, agent = self.case(root)
                destination = root / "desktop"
                with sqlite3.connect(agent.database) as db:
                    raw_report, raw_receipt = db.execute("SELECT report,receipt FROM queue WHERE case_id=?", (case,)).fetchone()
                    report, receipt = json.loads(raw_report), json.loads(raw_receipt)
                    if fault == "identity":
                        db.execute("UPDATE queue SET case_id='../unsafe'")
                    elif fault == "path":
                        report["json_path"] = str(root / "outside.json")
                        db.execute("UPDATE queue SET report=?", (json.dumps(report),))
                    elif fault == "receipt":
                        receipt["archive_sha256"] = "0" * 64
                        db.execute("UPDATE queue SET receipt=?", (json.dumps(receipt),))
                    elif fault == "source_hash":
                        Path(report["json_path"]).write_text("modified source")
                if fault == "case_link":
                    destination.mkdir()
                    (destination / case).symlink_to(root, target_is_directory=True)
                if fault == "object_link":
                    archive = agent.root / "objects" / (case + ".zip")
                    body = archive.read_bytes()
                    archive.unlink()
                    elsewhere = root / "elsewhere.zip"
                    elsewhere.write_bytes(body)
                    archive.symlink_to(elsewhere)
                result = desktop.export_reports(agent.root, destination)
                self.assertEqual(len(result["failed"]), 1)

    def test_symlink_roots_database_and_targets_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _api, _case, agent = self.case(root)
            alias = root / "alias"
            alias.symlink_to(agent.root, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                desktop.export_reports(alias, root / "desktop")
            with self.assertRaises(RuntimeError):
                desktop.export_reports(agent.root, alias)
            bad = root / "bad-data"
            bad.mkdir()
            (bad / "queue.sqlite3").symlink_to(agent.database)
            with self.assertRaises(RuntimeError):
                desktop.export_reports(bad, root / "desktop")

    def test_copy_limits_hashes_symlinks_and_concurrent_changes_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.write_bytes(b"synthetic body")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with self.assertRaises(RuntimeError):
                desktop.copy_verified(source, target, "0" * 64)
            with patch.object(desktop, "MAX_FULL_ARCHIVE_BYTES", 1), self.assertRaises(RuntimeError):
                desktop.copy_verified(source, target, digest)
            link = root / "link"
            link.symlink_to(source)
            with self.assertRaises(RuntimeError):
                desktop.copy_verified(link, target, digest)
            target.symlink_to(source)
            with self.assertRaises(RuntimeError):
                desktop.copy_verified(source, target, digest)
            target.unlink()
            for matching in (True, False):
                def raced(_temporary, destination):
                    Path(destination).write_bytes(source.read_bytes() if matching else b"owner change")
                    raise FileExistsError
                with patch.object(desktop.os, "link", side_effect=raced):
                    if matching:
                        self.assertEqual(desktop.copy_verified(source, target, digest), 0)  # discipline-ok(R6): copied-file count, not desktop geometry
                    else:
                        with self.assertRaises(RuntimeError):
                            desktop.copy_verified(source, target, digest)
                target.unlink()
            original_link = os.link
            def corrupt(temporary, destination):
                original_link(temporary, destination)
                Path(destination).write_bytes(b"corrupt")
            with patch.object(desktop.os, "link", side_effect=corrupt), self.assertRaises(RuntimeError):
                desktop.copy_verified(source, target, digest)

    def test_agent_combines_desktop_errors_with_its_single_notification_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            api, _case, agent = self.case(root)
            notice = Mock()
            notice.sync.return_value = {"failed": []}
            arguments = ["feedback_agent", "--data-dir", str(agent.root), "--gh", "/synthetic/gh", "--desktop-dir", str(root / "desktop")]
            with patch.object(sys, "argv", arguments), patch.object(feedback_agent, "GitHubAPI", return_value=api), \
                    patch.object(feedback_agent.subprocess, "run", return_value=SimpleNamespace(stdout="synthetic-token")), \
                    patch.object(feedback_agent, "FeedbackNotifications", return_value=notice), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(feedback_agent.main(), 0)
                notice.sync.assert_called_once()
                with patch.object(feedback_agent, "export_reports", return_value={"failed": [{"case_id": "synthetic", "category": "RuntimeError"}]}):
                    self.assertEqual(feedback_agent.main(), 1)
                self.assertIn("desktop:", notice.sync.call_args.args[1])

    def test_export_cli_runs_without_authentication_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _api, _case, agent = self.case(root)
            args = ["feedback_desktop_reports", "--data-dir", str(agent.root), "--destination", str(root / "desktop")]
            with patch.object(sys, "argv", args), patch("sys.stdout", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as caught:
                    runpy.run_module("tools.feedback_desktop_reports", run_name="__main__")
            self.assertEqual(caught.exception.code, 0)
