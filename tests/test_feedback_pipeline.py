"""Fault-oriented tests for the private GitHub feedback chain; no real accounts."""

import copy
import hashlib
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sf_feedback import FeedbackProtocolError, build_archive, make_envelope
from sf_feedback_admin import FeedbackAgent, analyze_isolated
from sf_feedback_analyzer import analyze_archive
from sf_feedback_capture import CONSENT_VERSION, capture_snapshot, clean_description, package_files
from sf_feedback_github import GitHubAPI, GitHubInbox, REPOSITORY
from sf_feedback_outbox import FeedbackOutbox
from sf_feedback_transport import endpoint_url, submit_https, submit_pending
from tools.feedback_service import Admission, make_server
from tests._base import SFCase


def snapshot():
    return {"app_version": "1.4.96", "bundle_id": "com.skyrocket.app", "run_id": "abcdef",
            "phase": "user_feedback", "capture_started_at": "2026-10-19T00:00:00Z",
            "capture_completed_at": "2026-10-19T00:00:00Z", "health": None,
            "counts": {"synthetic_error_count": 1},
            "summary": {"capture_issue_count": 0, "loaded_modules": 1},
            "omitted": ["raw_logs"], "truncated": []}


class MemoryGitHub:
    def __init__(self):
        self.rows = {}
        self.bodies = {}
        self.private = True
        self.lost_upload = False

    def inbox(self):
        if not self.private:
            raise FeedbackProtocolError("repository is public")
        return {"id": 1}

    def assets(self, release_id):
        return copy.deepcopy(list(self.rows.values()))

    def upload(self, release_id, name, data):
        if any(row["name"] == name for row in self.rows.values()):
            raise FeedbackProtocolError("asset exists")
        asset_id = max(self.rows, default=0) + 1
        row = {"id": asset_id, "name": name, "size": len(data)}
        self.rows[asset_id] = row
        self.bodies[asset_id] = data
        if self.lost_upload:
            self.lost_upload = False
            raise TimeoutError("response lost after GitHub stored asset")
        return copy.deepcopy(row)

    def download(self, asset_id, *, limit=256 * 1024):
        body = self.bodies[asset_id]
        if len(body) > limit:
            raise FeedbackProtocolError("too large")
        return body


class PipelineCase(SFCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.api = MemoryGitHub()
        self.inbox = GitHubInbox(self.api)

    def package(self, state=None, text="synthetic user report"):
        state = snapshot() if state is None else state
        path = self.root / (uuid.uuid4().hex + ".zip")
        build_archive(path, package_files(state, text), app_version=state["app_version"],
                      bundle_id=state["bundle_id"], run_id=state["run_id"])
        envelope = make_envelope(archive_path=path, app_version=state["app_version"],
                                 outbox_id=uuid.uuid4().hex, consent_policy_version=CONSENT_VERSION)
        return path, envelope


class CaptureTests(PipelineCase):
    def test_auto_projection_excludes_strings_prices_configuration_and_raw_issues(self):
        raw = {"modules": {"sf_probe": {"_error_count": 2, "_last_error": "password=secret",
                "_price": 123, "_phone_count": "123", "_other_count": {"user": 1}}},
               "objects": {}, "truncated": ["/Users/private/secret"],
               "issues": ["private exception"], "meta": {"loaded_product_modules": 1}}
        with patch("sf_feedback_capture.sf_diag.collect", return_value=raw):
            result = capture_snapshot(SimpleNamespace(), app_version="1.4.96", bundle_id="com.skyrocket.app", run_id="abcdef")
        self.assertEqual(result["counts"], {"sf_probe._error_count": 2})
        self.assertEqual(result["summary"]["capture_issue_count"], 1)
        self.assertEqual(result["truncated"], ["automatic_collection"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))

    def test_curated_runtime_flags_do_not_include_quote_or_target_values(self):
        owner = SimpleNamespace(_quote_presentation=SimpleNamespace(state=SimpleNamespace(
            net_ok=False, minute_stale=True, quote_fail=3, quote={"price": 999.99}, target=888.88)))
        result = capture_snapshot(owner, app_version="1.4.96", bundle_id="com.skyrocket.app", run_id="abcdef")
        self.assertFalse(result["summary"]["net_ok"])
        self.assertTrue(result["summary"]["minute_stale"])
        self.assertEqual(result["counts"]["quote_fail"], 3)
        self.assertNotIn("999.99", json.dumps(result))

    def test_description_is_explicit_bounded_and_scrubbed(self):
        self.assertEqual(clean_description(""), "")
        self.assertNotIn("hunter2", clean_description("password=hunter2"))
        self.assertNotIn("nobody", clean_description("/Users/nobody/Documents/report"))
        with self.assertRaises(FeedbackProtocolError):
            clean_description("x" * 4001)


class GitHubStorageTests(PipelineCase):
    def test_remote_readback_receipt_idempotency_and_conflicting_hash(self):
        path, envelope = self.package()
        receipt = self.inbox.accept(envelope, path.read_bytes())
        self.assertEqual(receipt["repository"], REPOSITORY)
        self.assertEqual(len(self.api.rows), 2)
        self.assertEqual(receipt, self.inbox.accept(envelope, path.read_bytes()))
        different, other_envelope = self.package(text="another description")
        other_envelope["idempotency_key"] = envelope["idempotency_key"]
        with self.assertRaises(FeedbackProtocolError):
            self.inbox.accept(other_envelope, different.read_bytes())
        self.assertEqual(len(self.api.rows), 2)

    def test_github_store_succeeded_but_response_lost_retry_uses_same_asset(self):
        path, envelope = self.package()
        self.api.lost_upload = True
        with self.assertRaises(TimeoutError):
            self.inbox.accept(envelope, path.read_bytes())
        self.assertEqual(len(self.api.rows), 1)
        receipt = self.inbox.accept(envelope, path.read_bytes())
        self.assertEqual(len(self.api.rows), 2)
        self.assertEqual(receipt["archive_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_deleted_archive_never_returns_previous_success_receipt(self):
        path, envelope = self.package()
        receipt = self.inbox.accept(envelope, path.read_bytes())
        del self.api.rows[receipt["asset_id"]]
        with self.assertRaises(FeedbackProtocolError):
            self.inbox.accept(envelope, path.read_bytes())

    def test_public_repository_and_quota_fail_closed(self):
        path, envelope = self.package()
        self.api.private = False
        with self.assertRaises(FeedbackProtocolError):
            self.inbox.accept(envelope, path.read_bytes())
        self.api.private = True
        self.inbox.max_cases = 0
        with self.assertRaises(FeedbackProtocolError):
            self.inbox.accept(envelope, path.read_bytes())
        self.assertEqual(self.api.rows, {})

    def test_unapproved_members_and_consents_are_refused(self):
        path, envelope = self.package()
        envelope["consent_policy_version"] = "not-agreed"
        with self.assertRaises(FeedbackProtocolError):
            self.inbox.accept(envelope, path.read_bytes())
        build_archive(path, {"state.json": "{}", "config.json": '{"target":999}'},
                      app_version="1.4.96", bundle_id="com.skyrocket.app", run_id="abcdef")
        envelope = make_envelope(archive_path=path, app_version="1.4.96", outbox_id="x",
                                 consent_policy_version=CONSENT_VERSION)
        with self.assertRaises(FeedbackProtocolError):
            self.inbox.accept(envelope, path.read_bytes())

    def test_repository_target_is_pinned_and_credentials_required(self):
        with self.assertRaises(FeedbackProtocolError):
            GitHubAPI("token", repository="someone/public")
        with self.assertRaises(FeedbackProtocolError):
            GitHubAPI("")


class ClientRecoveryTests(PipelineCase):
    def test_lost_response_restart_and_explicit_retry_preserve_identity(self):
        path, envelope = self.package()
        outbox = FeedbackOutbox(self.root / "outbox.db", self.root / "objects")
        row = outbox.enqueue(path, app_version="1.4.96", consent_policy_version=CONSENT_VERSION)

        def lost(url, env, archive):
            self.inbox.accept(env, Path(archive).read_bytes())
            raise TimeoutError()

        with self.assertRaises(TimeoutError):
            submit_pending(outbox, row["outbox_id"], "https://receiver.example/v1/feedback", sender=lost)
        self.assertEqual(outbox.get(row["outbox_id"])["status"], "response_unknown")
        reopened = FeedbackOutbox(self.root / "outbox.db", self.root / "objects")
        receipt = submit_pending(reopened, row["outbox_id"], "https://receiver.example/v1/feedback",
                                 sender=lambda url, env, archive: self.inbox.accept(env, Path(archive).read_bytes()))
        self.assertEqual(reopened.get(row["outbox_id"])["status"], "receipt_persisted")
        self.assertEqual(len(self.api.rows), 2)
        self.assertEqual(receipt["archive_sha256"], row["archive_sha256"])

    def test_process_killed_during_submission_is_recoverable_without_network(self):
        path, _ = self.package()
        outbox = FeedbackOutbox(self.root / "outbox.db", self.root / "objects")
        row = outbox.enqueue(path, app_version="1.4.96", consent_policy_version=CONSENT_VERSION)
        outbox.transition(row["outbox_id"], "submitting")
        reopened = FeedbackOutbox(self.root / "outbox.db", self.root / "objects")
        with patch.object(socket.socket, "connect", side_effect=AssertionError("no network")):
            self.assertEqual(reopened.recover_interrupted(), 1)
            self.assertEqual(reopened.get(row["outbox_id"])["status"], "response_unknown")

    def test_unknown_status_or_corruption_cannot_advance_to_success(self):
        path, _ = self.package()
        outbox = FeedbackOutbox(self.root / "outbox.db", self.root / "objects")
        row = outbox.enqueue(path, app_version="1.4.96", consent_policy_version=CONSENT_VERSION)
        Path(row["archive_path"]).write_bytes(b"corrupt")
        with patch("sf_feedback_transport.submit_https") as sender:
            with self.assertRaises(FeedbackProtocolError):
                submit_pending(outbox, row["outbox_id"], "https://receiver.example/v1/feedback", sender=sender)
            sender.assert_not_called()

    def test_client_only_accepts_https_without_credentials_or_redirect_parameters(self):
        for url in ("", "http://example.com", "https://u:p@example.com", "https://example.com?q=token"):
            with self.assertRaises(FeedbackProtocolError):
                endpoint_url(url)
        self.assertEqual(endpoint_url("https://example.com/v1/feedback"), "https://example.com/v1/feedback")


class HttpAndAgentTests(PipelineCase):
    def test_cross_process_style_wire_to_github_to_local_report(self):
        path, envelope = self.package()
        admission = Admission(self.root / "limits.db")
        server = make_server(self.inbox, admission)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            receipt = submit_https("http://127.0.0.1:%d/v1/feedback" % server.server_port,
                                    envelope, path, allow_loopback=True)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(5)
        agent = FeedbackAgent(self.api, self.root / "admin", analyzer=analyze_archive)
        result = agent.run_once()
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["ready"][0]["case_id"], receipt["case_id"])
        self.assertEqual(result["ready"][0]["conclusion"], "suspected_problem")
        self.assertTrue(Path(result["ready"][0]["report"]).is_file())
        self.assertEqual(FeedbackAgent(self.api, self.root / "admin", analyzer=analyze_archive).run_once()["ready"], [])

    def test_download_failure_retries_after_agent_restart(self):
        path, envelope = self.package()
        receipt = self.inbox.accept(envelope, path.read_bytes())
        self.api.bodies[receipt["asset_id"]] = b"corrupt"
        agent = FeedbackAgent(self.api, self.root / "admin", analyzer=analyze_archive)
        first = agent.run_once()
        self.assertEqual(first["failed"], [receipt["case_id"]])
        self.api.bodies[receipt["asset_id"]] = path.read_bytes()
        second = FeedbackAgent(self.api, self.root / "admin", analyzer=analyze_archive).run_once()
        self.assertEqual(len(second["ready"]), 1)
        self.assertEqual(second["failed"], [])

    def test_corrupt_receipt_does_not_block_healthy_cases_and_recovers_after_restart(self):
        path, envelope = self.package()
        first = self.inbox.accept(envelope, path.read_bytes())
        path, envelope = self.package(text="healthy second case")
        second = self.inbox.accept(envelope, path.read_bytes())
        receipt_asset = next(a for a in self.api.rows.values() if a["name"] == first["case_id"] + ".receipt.json")
        original = self.api.bodies[receipt_asset["id"]]
        self.api.bodies[receipt_asset["id"]] = b"invalid receipt"
        agent = FeedbackAgent(self.api, self.root / "admin", analyzer=analyze_archive)
        result = agent.run_once()
        self.assertEqual(result["failed"], [first["case_id"]])
        self.assertEqual(result["ready"][0]["case_id"], second["case_id"])
        self.api.bodies[receipt_asset["id"]] = original
        resumed = FeedbackAgent(self.api, self.root / "admin", analyzer=analyze_archive).run_once()
        self.assertEqual(resumed["failed"], [])
        self.assertEqual(resumed["ready"][0]["case_id"], first["case_id"])

    def test_rule_changes_reanalyze_valid_old_reports_instead_of_reusing_them(self):
        path, envelope = self.package()
        self.inbox.accept(envelope, path.read_bytes())
        agent = FeedbackAgent(self.api, self.root / "admin", analyzer=analyze_archive)
        first = agent.run_once()
        with patch("sf_feedback_admin._canonical_hash", return_value="new-rule-hash"):
            second = agent.run_once()
        self.assertEqual(len(second["ready"]), 1)
        self.assertNotEqual(first["ready"][0]["report"], second["ready"][0]["report"])

    def _cached_corruption_does_not_starve(self, column):
        path, envelope = self.package()
        first = self.inbox.accept(envelope, path.read_bytes())
        agent = FeedbackAgent(self.api, self.root / "admin", analyzer=analyze_archive)
        agent.run_once()
        db = sqlite3.connect(agent.database)
        try:
            statement = "UPDATE queue SET receipt='invalid JSON' WHERE case_id=?" if column == "receipt" else "UPDATE queue SET report='invalid JSON' WHERE case_id=?"
            db.execute(statement, (first["case_id"],))
            db.commit()
        finally:
            db.close()
        path, envelope = self.package(text="healthy later case")
        later = self.inbox.accept(envelope, path.read_bytes())
        result = agent.run_once()
        self.assertEqual(result["failed"], [first["case_id"]])
        self.assertEqual(result["ready"][0]["case_id"], later["case_id"])
        recovered = FeedbackAgent(self.api, self.root / "admin", analyzer=analyze_archive).run_once()
        self.assertEqual(recovered["failed"], [])
        self.assertEqual(recovered["ready"][0]["case_id"], first["case_id"])

    def test_corrupt_local_receipt_recovers_without_blocking_other_cases(self):
        self._cached_corruption_does_not_starve("receipt")

    def test_corrupt_local_report_cache_recovers_without_blocking_other_cases(self):
        self._cached_corruption_does_not_starve("report")

    def test_non_object_state_cannot_claim_no_known_issue(self):
        path = self.root / "invalid-shape.zip"
        build_archive(path, {"state.json": "[]"}, app_version="1.4.96", bundle_id="com.skyrocket.app", run_id="abcdef")
        report = analyze_archive(path, self.root / "reports")
        self.assertEqual(report["conclusion"], "insufficient_evidence")

    def test_missing_report_is_regenerated_but_not_claimed_ready_until_written(self):
        path, envelope = self.package()
        self.inbox.accept(envelope, path.read_bytes())
        agent = FeedbackAgent(self.api, self.root / "admin", analyzer=analyze_archive)
        first = agent.run_once()
        Path(first["ready"][0]["report"]).unlink()
        self.assertEqual(len(agent.run_once()["ready"]), 1)

    def test_admission_limit_persists_across_restarts(self):
        admission = Admission(self.root / "limit.db", daily_limit=1)
        self.assertTrue(admission.admit())
        self.assertFalse(Admission(self.root / "limit.db", daily_limit=1).admit())

    @unittest.skipUnless(Path("/usr/bin/sandbox-exec").exists(), "requires macOS sandbox-exec")
    def test_real_sandbox_produces_bound_reports_without_admin_credentials(self):
        path, _ = self.package(text="<script>alert('untrusted')</script>")
        report = analyze_isolated(path, self.root / "sandbox-reports")
        self.assertEqual(report["archive_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(report["conclusion"], "suspected_problem")
        self.assertIn("&lt;script&gt;", Path(report["markdown_path"]).read_text())

    def test_unknown_health_degrades_to_insufficient_not_no_problem(self):
        state = snapshot()
        state["counts"] = {}
        path, _ = self.package(state)
        report = analyze_archive(path, self.root / "reports")
        self.assertEqual(report["conclusion"], "insufficient_evidence")
