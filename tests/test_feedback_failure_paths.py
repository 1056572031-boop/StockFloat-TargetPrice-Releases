"""Public failure results, durable recovery and safe command entry points."""

import contextlib
import fcntl
import io
import json
import os
import runpy
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import http.client
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QApplication

import sf_feedback_admin as admin
import sf_feedback_capture as capture
import sf_feedback_transport as transport
import sf_feedback_ui as ui
import stock_float_window as window
from sf_feedback import FeedbackProtocolError, build_archive, validate_receipt
from sf_feedback_analyzer import analyze_archive
from sf_feedback_capture import CONSENT_VERSION
from sf_feedback_github import GitHubAPI
from sf_feedback_outbox import FeedbackOutbox
from tests._base import SFCase
from tests.test_feedback_pipeline import MemoryGitHub, PipelineCase, snapshot
from tests import test_coverage_window_diagnostics as diagnostic_cases
from tools import feedback_agent, install_feedback_agent, verify_feedback_github
from tools.feedback_service import Admission, make_server

ROOT = Path(__file__).resolve().parent.parent


class ProjectionFailures(PipelineCase):
    def test_identity_failure_is_visible_instead_of_silent_upload(self):
        def unavailable(issues):
            issues.append("synthetic identity lookup failure")
            return ()
        with patch.object(capture.sf_diag, "identity_needles", side_effect=unavailable):
            with self.assertRaises(FeedbackProtocolError):
                capture.clean_description("text")

    def test_redaction_expansion_is_rejected_before_sealing_instead_of_truncated(self):
        text = "token=x " * 500
        self.assertLessEqual(len(text), capture.MAX_DESCRIPTION)
        with self.assertRaises(FeedbackProtocolError):
            capture.clean_description(text)

    def test_counter_limits_are_explicit_and_do_not_round_large_integers(self):
        raw = {"modules": {"sf_probe": {**{f"c_{n}_count": n for n in range(70)},
                                       "huge_count": 2 ** 60}}, "objects": {},
               "truncated": [], "issues": [], "meta": {"loaded_product_modules": 1}}
        with patch.object(capture.sf_diag, "collect", return_value=raw):
            result = capture.capture_snapshot(None, app_version="1.4.96", bundle_id="com.skyrocket.app", run_id="abcdef")
        self.assertEqual(len(result["counts"]), 63)
        self.assertIn("counters", result["truncated"])
        self.assertGreater(result["summary"]["capture_issue_count"], 0)
        raw["modules"] = {"sf_probe": {"huge_count": 2 ** 60}}
        with patch.object(capture.sf_diag, "collect", return_value=raw):
            result = capture.capture_snapshot(None, app_version="1.4.96", bundle_id="com.skyrocket.app", run_id="abcdef")
        self.assertEqual(result["counts"], {})

    def test_receiver_rejects_unknown_schema_and_incomplete_timestamps(self):
        changes = [
            ("health", True), ("run_id", "private user"), ("capture_started_at", "2026-10-19T00:00:00"),
            ("counts", {"path/to/user": 1}), ("counts", {"count": 2 ** 60}),
            ("summary", {"private_path": "secret"}), ("omitted", ["unreviewed"]),
            ("truncated", ["unreviewed"]),
        ]
        for name, value in changes:
            state = snapshot()
            state[name] = value
            with self.subTest(name=name, value=value):
                with self.assertRaises(FeedbackProtocolError):
                    capture.validate_uploaded_snapshot(state)
        for raw in ('{"x":1,"x":2}', '{"x":NaN}'):
            with self.assertRaises(FeedbackProtocolError):
                capture.parse_uploaded_json(raw)

    def test_manual_retention_requires_an_explicit_policy(self):
        path, envelope = self.package()
        from sf_feedback_github import GitHubInbox
        receipt = GitHubInbox(self.api).accept(envelope, path.read_bytes())
        receipt.pop("retention_policy")
        with self.assertRaises(FeedbackProtocolError):
            validate_receipt(receipt, envelope)


class StorageFailures(PipelineCase):
    def test_api_non_redirect_failure_and_pagination_exhaustion_are_errors(self):
        from urllib.error import HTTPError
        api = GitHubAPI("fake-token")
        opener = Mock()
        opener.open.side_effect = HTTPError("https://api.github.com/x", 403, "denied", {}, None)
        with patch("sf_feedback_github.build_opener", return_value=opener):
            with self.assertRaises(FeedbackProtocolError):
                api.request("")
        with patch.object(api, "assert_private"), patch.object(api, "request", return_value=[{}] * 100):
            with self.assertRaises(FeedbackProtocolError):
                api.inbox()
            with self.assertRaises(FeedbackProtocolError):
                api.assets(1)
        with patch.object(api, "request", return_value={"id": 1}) as request:
            self.assertEqual(api.upload(1, "feedback.zip", b"synthetic"), {"id": 1})
            self.assertTrue(request.call_args.kwargs["upload"])

    def test_manifest_and_remote_receipt_are_independently_verified(self):
        path, envelope = self.package()
        from sf_feedback_github import GitHubInbox, validate_submission
        inbox = GitHubInbox(self.api)
        receipt = inbox.accept(envelope, path.read_bytes())
        receipt_asset = next(a for a in self.api.rows.values() if a["name"].endswith(".receipt.json"))
        for mutation in ({"case_id": "wrong-case"}, {"validation_status": "pending"}):
            changed = {**receipt, **mutation}
            self.api.bodies[receipt_asset["id"]] = json.dumps(changed).encode()
            with self.assertRaises(FeedbackProtocolError):
                inbox.accept(envelope, path.read_bytes())
        bad = dict(envelope, idempotency_key="invalid")
        with self.assertRaises(FeedbackProtocolError):
            validate_submission(bad, path.read_bytes())
        with self.assertRaises(FeedbackProtocolError):
            validate_submission(envelope, b"corrupt")
        with patch("sf_feedback_capture.parse_uploaded_json", return_value={"unexpected": "field"}):
            with self.assertRaises(FeedbackProtocolError):
                validate_submission(envelope, path.read_bytes())
        import zipfile
        with zipfile.ZipFile(path) as archive:
            state = json.loads(archive.read("state.json"))
        build_archive(path, {"state.json": json.dumps(state), "problem.txt": "x" * 4001},
                      app_version=state["app_version"], bundle_id=state["bundle_id"], run_id=state["run_id"])
        from sf_feedback import make_envelope
        oversized_text = make_envelope(archive_path=path, app_version=state["app_version"],
                                      outbox_id="x", consent_policy_version=CONSENT_VERSION)
        with self.assertRaises(FeedbackProtocolError):
            validate_submission(oversized_text, path.read_bytes())

    def test_admin_limiter_and_sandbox_errors_never_fall_back_to_unsafe_execution(self):
        with patch.object(admin.resource, "setrlimit") as limit:
            admin._limits()  # discipline-ok(R5): the subprocess resource limiter itself is under test
            self.assertEqual(limit.call_count, 3)
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(FeedbackProtocolError):
                admin.analyze_isolated(self.root / "input", self.root / "reports")
        failed = SimpleNamespace(returncode=1, stdout="")
        with patch.object(admin.subprocess, "run", return_value=failed):
            with self.assertRaises(FeedbackProtocolError):
                admin.analyze_isolated(self.root / "input", self.root / "reports")
        wrong = SimpleNamespace(returncode=0, stdout=json.dumps({"json_path": str(self.root / "not-a-report")}))
        with patch.object(admin.subprocess, "run", return_value=wrong):
            with self.assertRaises(FeedbackProtocolError):
                admin.analyze_isolated(self.root / "input", self.root / "reports")

    def test_second_agent_is_blocked_and_unknown_assets_do_not_become_cases(self):
        agent = admin.FeedbackAgent(self.api, self.root / "admin", analyzer=analyze_archive)
        with (self.root / "admin/agent.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(FeedbackProtocolError):
                agent.run_once()
        self.api.upload(1, "notes.txt", b"not a receipt")
        self.api.upload(1, "feedback-invalid.receipt.json", b"{}")
        self.assertEqual(agent.run_once()["discovered"], 0)

    def test_wrong_report_binding_and_nonexistent_outputs_remain_failed(self):
        path, envelope = self.package()
        from sf_feedback_github import GitHubInbox
        GitHubInbox(self.api).accept(envelope, path.read_bytes())
        for index, result in enumerate((
            {"archive_sha256": "wrong"},
            {"archive_sha256": envelope["archive_sha256"], "json_path": str(self.root / "absent.json"),
             "markdown_path": str(self.root / "absent.md")},
        )):
            agent = admin.FeedbackAgent(self.api, self.root / f"agent-{index}", analyzer=lambda *args, value=result: value)
            report = agent.run_once()
            self.assertEqual(report["ready"], [])
            self.assertEqual(len(report["failed"]), 1)

    def test_mismatched_receipt_and_missing_remote_asset_stay_failed(self):
        path, envelope = self.package()
        from sf_feedback_github import GitHubInbox
        receipt = GitHubInbox(self.api).accept(envelope, path.read_bytes())
        receipt_asset = next(a for a in self.api.rows.values() if a["name"].endswith(".receipt.json"))
        original = self.api.bodies[receipt_asset["id"]]
        self.api.bodies[receipt_asset["id"]] = json.dumps({**receipt, "case_id": "wrong"}).encode()
        first = admin.FeedbackAgent(self.api, self.root / "wrong", analyzer=analyze_archive).run_once()
        self.assertEqual(first["ready"], [])
        self.assertEqual(first["failed"], [receipt["case_id"]])
        self.api.bodies[receipt_asset["id"]] = original
        del self.api.rows[receipt["asset_id"]]
        second = admin.FeedbackAgent(self.api, self.root / "missing", analyzer=analyze_archive).run_once()
        self.assertEqual(second["ready"], [])
        self.assertEqual(second["failed"], [receipt["case_id"]])

    def test_runtime_warnings_and_missing_context_degrade_conclusions(self):
        state = snapshot()
        state["summary"]["net_ok"] = False
        path, _ = self.package(state)
        report = analyze_archive(path, self.root / "reports")
        self.assertIn("runtime_warning", {r["code"] for r in report["findings"]})
        del state["run_id"]
        build_archive(path, {"state.json": json.dumps(state)}, app_version="1.4.96", bundle_id="com.skyrocket.app", run_id="abcdef")
        self.assertEqual(analyze_archive(path, self.root / "reports")["conclusion"], "insufficient_evidence")


class ClientFailureResults(PipelineCase):
    def response(self, data, status=200):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = status
        response.read.return_value = data
        return response

    def test_redirect_size_and_unvalidated_receipts_are_not_success(self):
        self.assertIsNone(transport.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere"))
        path, envelope = self.package()
        big = self.root / "big.zip"
        build_archive(big, {"blob.bin": os.urandom(300000)}, app_version="1.4.96", bundle_id="com.skyrocket.app", run_id="abcdef")
        with self.assertRaises(FeedbackProtocolError):
            transport.submit_https("https://receiver.example", envelope, big)
        with self.assertRaises(FeedbackProtocolError):
            transport.submit_https("https://receiver.example", {**envelope, "extra": "x" * 70000}, path)
        from sf_feedback_github import GitHubInbox
        receipt = GitHubInbox(self.api).accept(envelope, path.read_bytes())
        pending = {**receipt, "validation_status": "pending"}
        opener = Mock()
        for response in (self.response(b"{}", 202), self.response(b"x" * 70000), self.response(json.dumps(pending).encode())):
            opener.open.return_value = response
            with patch.object(transport, "build_opener", return_value=opener):
                with self.assertRaises(FeedbackProtocolError):
                    transport.submit_https("https://receiver.example", envelope, path)
                sent = opener.open.call_args.args[0]
                self.assertEqual(sent.get_header("User-agent"), "Skyrocket-Feedback/1")
                self.assertEqual(sent.get_header("Accept"), "application/json")
                self.assertIsNone(sent.get_header("Authorization"))

    def test_pending_status_disk_corruption_and_receipt_commit_failure(self):
        path, _ = self.package()
        outbox = FeedbackOutbox(self.root / "outbox.db", self.root / "objects")
        row = outbox.enqueue(path, app_version="1.4.96", consent_policy_version=CONSENT_VERSION)
        fake = Mock()
        fake.get.return_value = {"status": "created"}
        with self.assertRaises(FeedbackProtocolError):
            transport.submit_pending(fake, "id", "https://receiver.example")
        other, _ = self.package(text="different bytes, valid ZIP")
        Path(row["archive_path"]).write_bytes(other.read_bytes())
        with self.assertRaises(FeedbackProtocolError):
            transport.submit_pending(outbox, row["outbox_id"], "https://receiver.example")
        Path(row["archive_path"]).write_bytes(path.read_bytes())
        from sf_feedback_github import GitHubInbox
        receipt = GitHubInbox(self.api).accept(row["envelope"], path.read_bytes())
        with self.assertRaises(FeedbackProtocolError):
            transport.submit_pending(outbox, row["outbox_id"], "https://receiver.example", sender=lambda *args: {**receipt, "validation_status": "pending"})
        with patch.object(outbox, "persist_receipt", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                transport.submit_pending(outbox, row["outbox_id"], "https://receiver.example", sender=lambda *args: receipt)
        self.assertEqual(outbox.get(row["outbox_id"])["status"], "response_unknown")
        transport.submit_pending(outbox, row["outbox_id"], "https://receiver.example", sender=lambda *args: receipt)
        unused = Mock()
        self.assertEqual(transport.submit_pending(outbox, row["outbox_id"], "https://receiver.example", sender=unused), receipt)
        unused.assert_not_called()


class DialogFailureResults(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dialog = self.track_widget(ui.FeedbackDialog(snapshot=snapshot(), storage_dir=self.temp.name,
            endpoint="https://receiver.example", capture_again=lambda: (_ for _ in ()).throw(OSError())))

    def test_private_state_is_not_exported_and_capture_failure_disables_upload(self):
        self.dialog._upload()  # discipline-ok(R5): disabled-click boundary under test
        self.assertIsNone(self.dialog.worker)
        owner = SimpleNamespace(_feedback_dialog=self.dialog)
        self.assertEqual(ui.feedback_diagnostics(SimpleNamespace()), {"opened": False})
        self.assertEqual(ui.feedback_diagnostics(owner)["stage"], "draft")
        self.dialog.stage = "received"
        self.dialog._select_pending()  # discipline-ok(R5): explicit new-case recapture failure under test
        self.assertEqual(self.dialog.last_error, "capture_failed")
        self.assertIn("重新采集失败", self.dialog.status.text())
        self.dialog.capture_again = None
        self.dialog.endpoint = ""
        self.dialog._select_pending()  # discipline-ok(R5): configuration failure on reopening a persistent submission
        self.assertEqual(self.dialog.last_error, "configuration_unavailable")
        self.dialog.outbox = None
        self.dialog._select_pending()  # discipline-ok(R5): failed storage initialization cannot load a queued package

    def test_source_menu_capture_failure_has_visible_feedback(self):
        host = SimpleNamespace(width=lambda: 100, mapToGlobal=lambda point: point)
        with patch("sf_feedback_capture.capture_snapshot", side_effect=OSError), patch.object(window.QToolTip, "showText") as shown:
            window.FloatWindow._open_feedback(host)  # discipline-ok(R5): source menu handler under test, no app construction
        shown.assert_called_once()
        self.assertFalse(hasattr(host, "_feedback_dialog"))


class FeedbackExitLifecycle(SFCase):
    def test_exit_closes_owned_feedback_dialog(self):
        fixture = diagnostic_cases.DiagnosticsBranchCase()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        dialog = Mock()
        fixture.win._feedback_dialog = dialog  # discipline-ok(R5): existing fully isolated window fixture, never a user process
        fixture.win._persist_on_exit()  # discipline-ok(R5): exercise owned-dialog cleanup without terminating the shared QApplication
        dialog.close.assert_called_once()


class HttpFailureResults(PipelineCase):
    def start_server(self, admission=None, inbox=None):
        server = make_server(inbox or self.inbox, admission or Admission(self.root / "limits.db"))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def stop():
            server.shutdown()
            server.server_close()
            worker.join(5)
        self.addCleanup(stop)
        return server

    def request(self, server, *, body=b"invalid", path="/v1/feedback", headers=None):
        headers = headers or {"Content-Type": "application/vnd.skyrocket.feedback-v1"}
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            client.request("POST", path, body, headers)
            result = client.getresponse()
            data = result.read()
            return result.status, data
        finally:
            client.close()

    def test_http_rejects_wrong_routes_headers_lengths_frames_and_unknown_json(self):
        server = self.start_server()
        self.assertEqual(self.request(server, path="/not-found")[0], 404)
        for headers in ({"Content-Type": "wrong"},
                        {"Content-Type": "application/vnd.skyrocket.feedback-v1", "Content-Length": "invalid"},
                        {"Content-Type": "application/vnd.skyrocket.feedback-v1", "Transfer-Encoding": "chunked"}):
            self.assertEqual(self.request(server, headers=headers)[0], 400)
        headers = {"Content-Type": "application/vnd.skyrocket.feedback-v1", "Content-Length": "99999999"}
        self.assertEqual(self.request(server, headers=headers)[0], 413)
        self.assertEqual(self.request(server, body=struct.pack("!I", 0) + b"payload")[0], 422)
        self.assertEqual(self.request(server, body=struct.pack("!I", 3) + b"bad" + b"archive")[0], 422)

    def test_short_body_and_daily_quota_are_explicit_errors(self):
        server = self.start_server()
        client = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        with client:
            client.sendall(b"POST /v1/feedback HTTP/1.0\r\nContent-Type: application/vnd.skyrocket.feedback-v1\r\nContent-Length: 100\r\n\r\nshort")
            client.shutdown(socket.SHUT_WR)
            self.assertIn(b"422", client.recv(1024))
        limited = self.start_server(admission=Admission(self.root / "zero.db", daily_limit=0))
        self.assertEqual(self.request(limited)[0], 429)

    def test_concurrency_saturates_before_third_request_reads_a_payload(self):
        entered = threading.Barrier(3)
        release = threading.Event()
        slow = Mock()
        def hold(*args):
            entered.wait(timeout=5)
            release.wait(timeout=5)
            return {"synthetic": True}
        slow.accept.side_effect = hold
        server = self.start_server(inbox=slow)
        body = struct.pack("!I", 2) + b"{}" + b"archive"
        results = []
        workers = [threading.Thread(target=lambda: results.append(self.request(server, body=body)[0])) for _ in range(2)]
        for worker in workers:
            worker.start()
        try:
            entered.wait(timeout=5)
            self.assertEqual(self.request(server, body=body)[0], 503)
        finally:
            release.set()
            for worker in workers:
                worker.join(5)
        self.assertEqual(results, [200, 200])


class ToolEntryResults(SFCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        previous_path = list(sys.path)
        self.addCleanup(lambda: setattr(sys, "path", previous_path))

    def test_agent_cli_rejects_relative_executable_and_main_entry_is_safe(self):
        with patch.object(sys, "argv", ["agent", "--data-dir", str(self.root), "--gh", "relative"]), patch("os.umask"):
            with self.assertRaises(SystemExit) as error:
                feedback_agent.main()
            self.assertEqual(error.exception.code, 2)
        agent = Mock()
        agent.run_once.return_value = {"ready": [], "failed": [], "discovered": 0}
        with patch.object(sys, "argv", ["agent", "--data-dir", str(self.root), "--gh", "/absolute/gh"]), \
                patch("subprocess.run", return_value=SimpleNamespace(stdout="fake-token")), \
                patch("sf_feedback_admin.FeedbackAgent", return_value=agent), patch("os.umask"), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                runpy.run_path(str(ROOT / "tools/feedback_agent.py"), run_name="__main__")
            self.assertEqual(error.exception.code, 0)

    def test_installer_cli_version_guard_and_main_entry(self):
        argv = ["install", "--destination", str(self.root / "install"), "--gh", "/absolute/gh"]
        with patch.object(sys, "argv", argv), patch.object(sys, "version_info", (3, 14, 6)), patch("os.umask"):
            with self.assertRaises(SystemExit) as error:
                install_feedback_agent.main()
            self.assertEqual(error.exception.code, 2)
        with patch.object(sys, "argv", argv), patch("os.umask"), contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(str(ROOT / "tools/install_feedback_agent.py"), run_name="__main__")
        self.assertTrue((self.root / "install/com.skyrocket.feedback-agent.plist").is_file())

    def test_verifier_negative_results_and_main_entry(self):
        api = MemoryGitHub()
        fake = Mock()
        fake.run_once.return_value = {"ready": [], "failed": ["synthetic"]}
        with patch.object(verify_feedback_github, "FeedbackAgent", return_value=fake):
            with self.assertRaises(RuntimeError):
                verify_feedback_github.verify(api, self.root / "failed")
        inconsistent = Mock()
        inconsistent.accept.side_effect = [{"synthetic": 1}, {"synthetic": 2}]
        with patch.object(verify_feedback_github, "GitHubInbox", return_value=inconsistent):
            with self.assertRaises(RuntimeError):
                verify_feedback_github.verify(api, self.root / "inconsistent")
        with patch.object(verify_feedback_github, "initialize") as initialize, \
                patch.object(verify_feedback_github, "verify", return_value={"synthetic": True}), \
                patch("subprocess.run", return_value=SimpleNamespace(stdout="fake-token")), \
                patch.object(sys, "argv", ["verify", "--out", str(self.root / "init"), "--gh", "/absolute/gh", "--initialize"]), \
                patch("os.umask"), contextlib.redirect_stdout(io.StringIO()):
            verify_feedback_github.main()
            initialize.assert_called_once()
        original = subprocess.run
        def gh_only(command, *args, **kwargs):
            if command[:2] == ["/absolute/gh", "auth"]:
                return SimpleNamespace(stdout="fake-token")
            return original(command, *args, **kwargs)
        with patch.object(sys, "argv", ["verify", "--out", str(self.root / "verified"), "--gh", "/absolute/gh"]), \
                patch("subprocess.run", side_effect=gh_only), patch("sf_feedback_github.GitHubAPI", return_value=MemoryGitHub()), \
                patch("os.umask"), contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(str(ROOT / "tools/verify_feedback_github.py"), run_name="__main__")
        self.assertTrue((self.root / "verified/verification.json").is_file())

    def test_nonempty_or_unavailable_repo_is_not_blindly_reinitialized(self):
        from sf_feedback_github import GitHubRequestError
        api = Mock(spec=GitHubAPI)
        api.request.side_effect = [None, GitHubRequestError(403)]
        with self.assertRaises(FeedbackProtocolError):
            verify_feedback_github.initialize(api)

    def test_service_main_entry_closes_its_server_without_real_network(self):
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(sys, "argv", ["service", "--data-dir", str(self.root), "--gh", "/absolute/gh"]), \
                patch("subprocess.run", return_value=SimpleNamespace(stdout="fake-token")), \
                patch("sf_feedback_github.GitHubInbox"), patch("http.server.ThreadingHTTPServer", return_value=server), \
                patch("os.umask"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                runpy.run_path(str(ROOT / "tools/feedback_service.py"), run_name="__main__")
        server.server_close.assert_called_once()
