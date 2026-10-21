"""Admin transport, deployment snapshots and safe command entry points."""

import io
import plistlib
import subprocess
import tempfile
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from sf_feedback import FeedbackProtocolError
from sf_feedback_github import GitHubAPI, GitHubRequestError, INBOX_TAG
from sf_feedback_admin import FeedbackAgent
from sf_feedback_analyzer import analyze_archive
from tests.test_feedback_pipeline import MemoryGitHub
from tests._base import SFCase
from tools import feedback_agent, feedback_service, install_feedback_agent, verify_feedback_github


class AdminToolTests(SFCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_installed_runtime_is_hashed_and_independent_of_worktree(self):
        result = install_feedback_agent.install(self.root / "agent", "/usr/local/bin/gh")
        runtime = Path(result["runtime"])
        self.assertTrue((runtime / "sf_feedback_admin.py").is_file())
        self.assertTrue((runtime / "tools/feedback_agent.py").is_file())
        plist = plistlib.loads(Path(result["plist"]).read_bytes())
        self.assertEqual(plist["StartInterval"], 30 * 60)
        self.assertIn(str(runtime / "tools/feedback_agent.py"), plist["ProgramArguments"])
        self.assertFalse(any(".venv" in value for value in plist["ProgramArguments"]))
        self.assertNotIn("GITHUB_TOKEN", plist["EnvironmentVariables"])
        self.assertEqual(result["manifest"]["version"], sys.version.split()[0])
        self.assertFalse(result["activated"])

    def test_installed_cli_starts_with_the_base_interpreter_and_only_its_snapshot(self):
        result = install_feedback_agent.install(self.root / "agent", "/usr/local/bin/gh")
        runtime = Path(result["runtime"])
        process = subprocess.run([result["manifest"]["python"], "-B", str(runtime / "tools/feedback_agent.py"), "--help"],
                                 cwd="/", env={"HOME": str(self.root), "PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
                                 capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("--desktop-dir", process.stdout)

    def test_installer_rejects_changed_runtime_without_overwriting_it(self):
        first = install_feedback_agent.install(self.root / "agent", "/usr/local/bin/gh")
        target = Path(first["runtime"]) / "sf_feedback.py"
        target.write_text("unrelated change")
        with self.assertRaises(RuntimeError):
            install_feedback_agent.install(self.root / "agent", "/usr/local/bin/gh")
        self.assertEqual(target.read_text(), "unrelated change")

    def test_activation_uses_launchctl_and_preserves_conflicting_agent(self):
        with patch.object(Path, "home", return_value=self.root / "home"), patch.object(install_feedback_agent.subprocess, "run") as run:
            result = install_feedback_agent.install(self.root / "agent", "/usr/local/bin/gh", activate=True)
            run.assert_called_once()
            self.assertTrue(result["activated"])
            existing = self.root / "home/Library/LaunchAgents/com.skyrocket.feedback-agent.plist"
            existing.write_bytes(b"unrelated existing job")
            with self.assertRaises(RuntimeError):
                install_feedback_agent.install(self.root / "agent", "/usr/local/bin/gh", activate=True)
            self.assertEqual(existing.read_bytes(), b"unrelated existing job")

    def test_bootstrap_only_creates_private_policy_and_draft_inbox(self):
        api = Mock(spec=GitHubAPI)
        api.request.side_effect = [None, GitHubRequestError(409), None, [], {"id": 1}]
        verify_feedback_github.initialize(api)
        api.assert_private.assert_called_once()
        release_call = api.request.call_args
        self.assertEqual(release_call.args[0], "releases")
        self.assertTrue(release_call.kwargs["data"]["draft"])
        self.assertEqual(release_call.kwargs["data"]["tag_name"], INBOX_TAG)

    def test_bootstrap_does_not_confuse_small_repository_with_empty_repository(self):
        api = Mock(spec=GitHubAPI)
        api.request.side_effect = [None, [{"sha": "existing-commit"}], [{"tag_name": INBOX_TAG}]]
        verify_feedback_github.initialize(api)
        self.assertFalse(any(call.args[0] == "contents/README.md" for call in api.request.call_args_list))

    def test_admin_cli_uses_gh_without_printing_credentials(self):
        agent = Mock()
        agent.run_once.return_value = {"ready": [], "failed": [], "discovered": 0}
        with patch("sys.argv", ["feedback_agent", "--data-dir", str(self.root), "--gh", "/absolute/gh"]), \
                patch.object(feedback_agent.subprocess, "run", return_value=SimpleNamespace(stdout="fake-admin-token\n")), \
                patch.object(feedback_agent, "FeedbackAgent", return_value=agent), \
                patch.object(feedback_agent.os, "umask"), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(feedback_agent.main(), 0)
            self.assertNotIn("fake-admin-token", output.getvalue())

    def test_storage_capacity_warning_reuses_the_single_notification_sync(self):
        agent = Mock()
        agent.run_once.return_value = {"ready": [], "failed": [], "discovered": 0,
                                       "capacity": {"near_limit": True}}
        notices = Mock()
        notices.sync.return_value = {"submitted": ["backend"], "failed": []}
        with patch("sys.argv", ["feedback_agent", "--data-dir", str(self.root), "--gh", "/absolute/gh"]), \
                patch.object(feedback_agent.subprocess, "run", return_value=SimpleNamespace(stdout="fake-admin-token\n")), \
                patch.object(feedback_agent, "FeedbackAgent", return_value=agent), \
                patch.object(feedback_agent, "FeedbackNotifications", return_value=notices), \
                patch.object(feedback_agent, "report_versions", return_value={}), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(feedback_agent.main(), 0)
        notices.sync.assert_called_once_with({}, "storage_capacity")

    def test_service_cli_closes_server_after_interrupt(self):
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch("sys.argv", ["feedback_service", "--data-dir", str(self.root), "--gh", "/absolute/gh"]), \
                patch.object(feedback_service.subprocess, "run", return_value=SimpleNamespace(stdout="fake-admin-token")), \
                patch.object(feedback_service, "GitHubInbox"), patch.object(feedback_service, "make_server", return_value=server), \
                patch.object(feedback_service.os, "umask"), \
                patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(KeyboardInterrupt):
                feedback_service.main()
        server.server_close.assert_called_once()

    def test_live_verification_tool_is_reproducible_with_fake_remote_and_real_reports(self):
        api = MemoryGitHub()
        def agent(remote, directory):
            return FeedbackAgent(remote, directory, analyzer=analyze_archive)
        with patch.object(verify_feedback_github, "FeedbackAgent", side_effect=agent):
            result = verify_feedback_github.verify(api, self.root / "evidence")
        self.assertTrue(result["synthetic_only"])
        self.assertFalse(result["public_https_client_verified"])
        self.assertTrue((self.root / "evidence/verification.json").is_file())
        self.assertEqual(len(api.rows), 2)


class GitHubApiTests(SFCase):
    def response(self, payload):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = payload
        return response

    def test_request_binds_admin_auth_to_github_only_and_caps_response(self):
        api = GitHubAPI("fake-admin-token")
        opener = Mock()
        opener.open.return_value = self.response(b'{"private":true}')
        with patch("sf_feedback_github.build_opener", return_value=opener):
            self.assertTrue(api.request("")["private"])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.host, "api.github.com")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-admin-token")
        opener.open.return_value = self.response(b"12345")
        with patch("sf_feedback_github.build_opener", return_value=opener):
            with self.assertRaises(FeedbackProtocolError):
                api.request("x", binary=True, limit=4)

    def test_asset_redirect_drops_auth_and_unknown_hosts_are_refused(self):
        api = GitHubAPI("fake-admin-token")
        opener = Mock()
        redirect = HTTPError("https://api.github.com/x", 302, "redirect",
                             {"Location": "https://release-assets.githubusercontent.com/object"}, None)
        opener.open.side_effect = [redirect, self.response(b"archive")]
        with patch("sf_feedback_github.build_opener", return_value=opener):
            self.assertEqual(api.download(1), b"archive")
        redirected = opener.open.call_args.args[0]
        self.assertIsNone(redirected.get_header("Authorization"))
        opener.open.side_effect = HTTPError("https://api.github.com/x", 302, "redirect",
                                           {"Location": "https://evil.example/object"}, None)
        with patch("sf_feedback_github.build_opener", return_value=opener):
            with self.assertRaises(FeedbackProtocolError):
                api.download(1)

    def test_private_target_and_inbox_pagination(self):
        api = GitHubAPI("fake-admin-token")
        with patch.object(api, "request", return_value={"private": False}):
            with self.assertRaises(FeedbackProtocolError):
                api.assert_private()
        with patch.object(api, "assert_private"), patch.object(api, "request", return_value=[{"tag_name": INBOX_TAG, "id": 7}]):
            self.assertEqual(api.inbox()["id"], 7)
        with patch.object(api, "assert_private"), patch.object(api, "request", return_value=[]):
            with self.assertRaises(FeedbackProtocolError):
                api.inbox()
        with patch.object(api, "request", side_effect=[[{"id": n} for n in range(100)], [{"id": 101}]]):
            self.assertEqual(len(api.assets(1)), 101)
