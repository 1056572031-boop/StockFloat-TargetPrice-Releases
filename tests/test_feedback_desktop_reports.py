"""Desktop delivery retains original bytes and never overwrites user changes."""

import hashlib
import io
import json
import os
import runpy
import sqlite3
import sys
import tempfile
from contextlib import closing
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

    def test_reports_are_exported_without_the_original_packet_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _api, case, agent = self.case(root)
            destination = root / "desktop"
            first = desktop.export_reports(agent.root, destination)
            self.assertEqual(first["failed"], [])
            self.assertEqual(first["copied_files"], 2)
            self.assertFalse(first["includes_archive"])
            self.assertFalse((destination / case / (case + ".zip")).exists())
            self.assertEqual(desktop.export_reports(agent.root, destination)["copied_files"], 0)

    def test_explicit_archive_export_preserves_matching_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _api, case, agent = self.case(root)
            destination = root / "desktop"
            result = desktop.export_reports(agent.root, destination, include_archive=True)
            self.assertTrue(result["includes_archive"])
            self.assertEqual((destination / case / (case + ".zip")).read_bytes(),
                             (agent.root / "objects" / (case + ".zip")).read_bytes())

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
            self.assertTrue((destination / second_case).exists())
            self.assertFalse((destination / second_case / (second_case + ".zip")).exists())

    def test_invalid_case_path_and_receipt_cannot_export_unbound_archives(self):
        for fault in ("identity", "path", "receipt", "source_hash", "case_link", "object_link"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _api, case, agent = self.case(root)
                destination = root / "desktop"
                with closing(sqlite3.connect(agent.database)) as db:
                    with db:
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
                result = desktop.export_reports(agent.root, destination, include_archive=True)
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


class SecondPassRedactionTests(SFCase):
    """桌面那份是会被看见、被同步、被截图的地方：必须再过一遍身份与凭据。

    分析器写的那一份存在 0700 的私有对象目录里；复制到桌面时二次脱敏，
    同时**保留来源哈希绑定**（桌面副本可以变短变样，但它来自哪个原始件必须可追）。
    """

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "report.json"
        self.target = self.root / "desktop" / "report.json"
        self.target.parent.mkdir(parents=True)

    def write(self, body):
        self.source.write_bytes(body)
        return hashlib.sha256(body).hexdigest()

    def test_a_secret_and_identity_never_reach_the_desktop_copy(self):
        body = json.dumps({"note": "token=do-not-export 这一段留着",
                           "home": os.path.expanduser("~")}).encode()
        digest = self.write(body)
        outcome = desktop.write_redacted(self.source, self.target, digest)
        text = self.target.read_text(encoding="utf-8")
        self.assertNotIn("do-not-export", text)
        self.assertNotIn(os.path.expanduser("~"), text)
        self.assertIn("<REDACTED>", text)
        self.assertEqual(outcome["changed"], 1, "桌面副本与原始件不同时必须记账为改过")
        self.assertEqual(outcome["sha256"], hashlib.sha256(self.target.read_bytes()).hexdigest())
        self.assertEqual(self.source.read_bytes(), body, "私有目录里那一份必须逐字节不动")

    def test_json_keys_are_scrubbed_and_the_copy_stays_valid_json(self):
        body = json.dumps({os.path.expanduser("~") + "/Library/reports": "反馈编号"}).encode()
        digest = self.write(body)
        desktop.write_redacted(self.source, self.target, digest)
        restored = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(list(restored), ["~/Library/reports"])
        self.assertNotIn(os.path.expanduser("~") + "/Library", self.target.read_text(encoding="utf-8"))

    def test_json_formatting_limit_is_enforced(self):
        body = json.dumps({"data": "fixture"}).encode()
        self.write(body)
        with patch.object(desktop, "MAX_REPORT_BYTES", 1):
            with self.assertRaisesRegex(RuntimeError, "redacted report exceeds"):
                desktop.redacted_body(self.source, body)

    def test_redacted_report_is_checked_by_the_full_feedback_privacy_guard(self):
        body = json.dumps({"note": "fixture"}).encode()
        self.write(body)
        with patch.object(desktop, "check_privacy", side_effect=desktop.PrivacyRejected("late secret")):
            with self.assertRaisesRegex(RuntimeError, "failed privacy validation"):
                desktop.redacted_body(self.source, body)

    def test_markdown_copy_is_checked_by_the_full_feedback_privacy_guard(self):
        body = b"fixture"
        self.source = self.root / "report.md"
        self.write(body)
        with patch.object(desktop, "check_privacy", side_effect=desktop.PrivacyRejected("late secret")):
            with self.assertRaisesRegex(RuntimeError, "failed privacy validation"):
                desktop.redacted_body(self.source, body)

    def test_markdown_formatting_limit_is_enforced(self):
        body = b"markdown body"
        self.source = self.root / "report.md"
        self.write(body)
        with patch.object(desktop, "MAX_REPORT_BYTES", 1):
            with self.assertRaisesRegex(RuntimeError, "redacted report exceeds"):
                desktop.redacted_body(self.source, body)

    def test_read_after_stat_detects_a_source_that_grows(self):
        body = b"{}"
        digest = self.write(body)
        original_read = Path.read_bytes

        def grown():
            data = original_read(self.source)
            return data + b"x" * (desktop.MAX_REPORT_BYTES + 1)

        with patch.object(Path, "read_bytes", side_effect=grown):
            with self.assertRaisesRegex(RuntimeError, "report exceeds its limit"):
                desktop.write_redacted(self.source, self.target, digest)

    def test_source_stat_failure_is_reported_as_unavailable(self):
        digest = self.write(b"{}")
        with patch.object(Path, "stat", side_effect=OSError("stat failed")):
            with self.assertRaisesRegex(RuntimeError, "source artifact unavailable"):
                desktop.write_redacted(self.source, self.target, digest)

    def test_redacted_json_key_collision_is_refused(self):
        body = json.dumps({os.path.expanduser("~") + "/a": 1, "~/a": 2}).encode()
        digest = self.write(body)
        with self.assertRaisesRegex(RuntimeError, "key collision"):
            desktop.write_redacted(self.source, self.target, digest)
        self.assertFalse(self.target.exists())

    def test_redacted_output_size_is_limited_after_formatting(self):
        body = json.dumps({"data": "fixture"}).encode()
        digest = self.write(body)
        oversized = b"x" * (desktop.MAX_REPORT_BYTES + 1)
        with patch.object(desktop, "redacted_body", return_value=oversized):
            with self.assertRaisesRegex(RuntimeError, "redacted report exceeds"):
                desktop.write_redacted(self.source, self.target, digest)
        self.assertFalse(self.target.exists())

    def test_hashes_and_numbers_survive_the_redaction_unchanged(self):
        """不能把 SHA-256 或计数当成凭据抹掉 —— 那会悄悄毁掉与原始件的绑定关系。"""
        digest_value = hashlib.sha256(b"x").hexdigest()
        body = json.dumps({"sha": digest_value, "count": 7, "ok": True, "ratio": 1.5,
                           "list": [digest_value]}).encode()
        source_digest = self.write(body)
        desktop.write_redacted(self.source, self.target, source_digest)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")),
                         json.loads(body.decode("utf-8")))

    def test_a_malformed_json_report_is_refused_not_copied_raw(self):
        digest = self.write(b"{not json")
        with self.assertRaises(RuntimeError) as raised:
            desktop.write_redacted(self.source, self.target, digest)
        self.assertIn("unreadable", str(raised.exception))
        self.assertFalse(self.target.exists())

    def test_unavailable_identity_needles_fail_closed(self):
        digest = self.write(json.dumps({"note": "x"}).encode())
        with patch.object(desktop, "identity_needles", lambda issues: issues.append("no_home")):
            with self.assertRaises(RuntimeError) as raised:
                desktop.write_redacted(self.source, self.target, digest)
        self.assertIn("identity redaction unavailable", str(raised.exception))
        self.assertFalse(self.target.exists(), "取不到脱敏规则时不许写出一份「以为干净」的报告")

    def test_the_source_hash_binding_is_still_enforced(self):
        self.write(b"{}")
        with self.assertRaises(RuntimeError) as raised:
            desktop.write_redacted(self.source, self.target, "0" * 64)
        self.assertIn("source checksum mismatch", str(raised.exception))

    def test_a_modified_desktop_copy_is_refused_and_an_identical_one_is_a_no_op(self):
        digest = self.write(json.dumps({"note": "token=do-not-export"}).encode())
        first = desktop.write_redacted(self.source, self.target, digest)
        self.assertEqual(first["written"], 1)
        self.assertEqual(desktop.write_redacted(self.source, self.target, digest)["written"], 0)
        self.target.write_text("用户自己改过", encoding="utf-8")
        with self.assertRaises(RuntimeError) as raised:
            desktop.write_redacted(self.source, self.target, digest)
        self.assertIn("refusing to overwrite", str(raised.exception))

    def test_an_oversized_report_is_refused_before_reading_it_all(self):
        digest = self.write(b"x" * (desktop.MAX_REPORT_BYTES + 1))
        with self.assertRaises(RuntimeError) as raised:
            desktop.write_redacted(self.source, self.target, digest)
        self.assertIn("exceeds its limit", str(raised.exception))
        self.assertFalse(self.target.exists())

    def test_re_exporting_an_unchanged_archive_is_a_no_op_and_a_modified_one_is_refused(self):
        """原始件那条路线仍然要求证：重复导出不许重写，桌面那份被改过就不许覆盖。

        报告文件现在走二次脱敏的写入器，`copy_verified` 只剩原始 ZIP 这一条路 ——
        它自己的三道守卫不能再指望别的用例顺带跑到。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _api, case, agent = DesktopReportTests().case(root)
            destination = root / "desktop"
            original = (agent.root / "objects" / (case + ".zip")).read_bytes()
            first = desktop.export_reports(agent.root, destination, include_archive=True)
            self.assertEqual(first["failed"], [])
            archived = destination / case / (case + ".zip")
            self.assertEqual(archived.read_bytes(), original)
            self.assertEqual(first["copied_files"], 3, "两份报告加一份原始包")

            again = desktop.export_reports(agent.root, destination, include_archive=True)
            self.assertEqual(again["failed"], [])
            self.assertEqual(again["copied_files"], 0, "内容一致时重复导出必须是空操作")

            archived.write_text("用户自己动过桌面这份", encoding="utf-8")
            third = desktop.export_reports(agent.root, destination, include_archive=True)
            self.assertEqual([item["category"] for item in third["failed"]], ["RuntimeError"])
            self.assertEqual(archived.read_text(encoding="utf-8"), "用户自己动过桌面这份",
                             "被改过的桌面副本不许被悄悄覆盖回原样")
            self.assertEqual((agent.root / "objects" / (case + ".zip")).read_bytes(), original,
                             "私有目录里那一份始终是原始件")

    def test_symlinked_source_or_desktop_target_is_refused_before_any_write(self):
        """两份都是软链接就不许写：桌面目录可能被人换成指向别处的替身。"""
        real = self.root / "real.json"
        digest = self.write(real.read_bytes() if real.exists() else b"{}")
        link = self.root / "link.json"
        link.symlink_to(self.source)
        with self.assertRaises(RuntimeError) as raised:
            desktop.write_redacted(link, self.target, digest)
        self.assertIn("source artifact is a symlink", str(raised.exception))
        self.target.symlink_to(self.source)
        with self.assertRaises(RuntimeError) as raised:
            desktop.write_redacted(self.source, self.target, digest)
        self.assertIn("desktop target is a symlink", str(raised.exception))

    def test_a_desktop_file_that_changes_under_the_write_is_detected(self):
        """链接完之后被改写：读回来核对的是**自己写出的字节**，不是源文件。"""
        digest = self.write(json.dumps({"note": "token=do-not-export"}).encode())
        original_link = os.link

        def corrupt(temporary, destination, **kwargs):
            original_link(temporary, destination, **kwargs)
            self.target.write_bytes(b"someone else")

        with patch.object(desktop.os, "link", side_effect=corrupt):
            with self.assertRaises(RuntimeError) as raised:
                desktop.write_redacted(self.source, self.target, digest)
        self.assertIn("desktop copy mismatch", str(raised.exception))
        self.assertFalse(self.source.is_symlink(), "原始件不该被这次失败的写入动过")

    def test_write_refuses_an_unavailable_destination_directory(self):
        digest = self.write(b"{}")
        with patch.object(desktop.os, "open", side_effect=OSError("no directory")):
            with self.assertRaisesRegex(RuntimeError, "directory unavailable"):
                desktop.write_redacted(self.source, self.target, digest)
        self.assertFalse(self.target.exists())

    def test_write_refuses_when_the_temporary_file_cannot_be_created(self):
        digest = self.write(b"{}")
        real_open = desktop.os.open

        def fail_temporary(name, flags, mode=0o777, *, dir_fd=None):
            if isinstance(name, str) and name.startswith(".feedback-"):
                raise OSError("read-only directory")
            return real_open(name, flags, mode, dir_fd=dir_fd)

        with patch.object(desktop.os, "open", side_effect=fail_temporary):
            with self.assertRaises(OSError):
                desktop.write_redacted(self.source, self.target, digest)
        self.assertFalse(self.target.exists())

    def test_temporary_stream_failure_closes_the_open_file_descriptor(self):
        digest = self.write(b"{}")
        with patch.object(desktop.os, "fdopen", side_effect=OSError("stream failed")):
            with self.assertRaisesRegex(OSError, "stream failed"):
                desktop.write_redacted(self.source, self.target, digest)
        self.assertFalse(any(self.target.parent.glob(".feedback-*.tmp")))

    def test_concurrent_identical_destination_is_idempotent(self):
        digest = self.write(b"{}")
        expected = desktop.redacted_body(self.source, b"{}")

        def already_there(*args, **kwargs):
            self.target.write_bytes(expected)
            raise FileExistsError(self.target)

        with patch.object(desktop.os, "link", side_effect=already_there):
            result = desktop.write_redacted(self.source, self.target, digest)
        self.assertEqual(result["written"], 0)
        self.assertEqual(self.target.read_bytes(), expected)

    def test_concurrent_changed_destination_is_refused(self):
        digest = self.write(b"{}")

        def changed_destination(*args, **kwargs):
            self.target.write_bytes(b"different")
            raise FileExistsError(self.target)

        with patch.object(desktop.os, "link", side_effect=changed_destination):
            with self.assertRaisesRegex(RuntimeError, "changed concurrently"):
                desktop.write_redacted(self.source, self.target, digest)
        self.assertEqual(self.target.read_bytes(), b"different")

    def test_export_reports_counts_every_desktop_file_it_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _api, case, agent = DesktopReportTests().case(root)
            destination = root / "desktop"
            result = desktop.export_reports(agent.root, destination)
            self.assertEqual(result["failed"], [])
            self.assertEqual(result["redaction"]["files"], 2)
            self.assertEqual(len(result["exported_sha256"]), 2)
            for path, recorded in result["exported_sha256"].items():
                self.assertEqual(recorded, hashlib.sha256(Path(path).read_bytes()).hexdigest())
                if Path(path).suffix == ".json":
                    json.loads(Path(path).read_text(encoding="utf-8"))
