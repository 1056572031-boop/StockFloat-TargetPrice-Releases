"""The acceptance tool cannot silently spend production requests or user data."""

import json
import io
import runpy
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from sf_feedback_github import GitHubInbox
from tests._base import SFCase
from tests.test_feedback_pipeline import MemoryGitHub
from tools import verify_full_feedback as tool


class FullFeedbackAcceptanceToolTests(SFCase):
    def test_generated_small_and_stress_archives_contain_only_synthetic_files(self):
        for stress in (False, True):
            with self.subTest(stress=stress), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch.object(tool.os, "urandom", side_effect=lambda size: b"x" * size):
                    packet, envelope = tool.generate(root, stress)
                self.assertEqual(envelope["size_bytes"], packet.stat().st_size)
                self.assertTrue((root / "diagnostics.zip").is_file())
                self.assertEqual(tool.read_upload_archive(packet, envelope)["files"]["problem.txt"],
                                 "SYNTHETIC TEST ONLY. No real user data.")

    def test_live_timeout_reads_back_the_original_case_and_never_posts_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = MemoryGitHub()
            inbox = GitHubInbox(remote)

            def lost_response(_url, envelope, path):
                inbox.accept(envelope, Path(path).read_bytes())
                raise TimeoutError("synthetic response lost after storage")

            args = ["verify_full_feedback", "--out", str(root), "--live", "--gh", "/synthetic/gh"]
            with patch.object(sys, "argv", args), patch.object(tool, "submit_https", side_effect=lost_response) as send, \
                    patch.object(tool, "GitHubAPI", return_value=remote), \
                    patch.object(tool.subprocess, "run", return_value=SimpleNamespace(stdout="synthetic-token")), \
                    patch.object(tool, "analyze_isolated", return_value={"analysis_run_id": "synthetic", "markdown_path": "synthetic.md"}):
                self.assertEqual(tool.main(), 0)
                self.assertEqual(send.call_count, 1)
                with self.assertRaises(SystemExit):
                    tool.main()
                self.assertEqual(send.call_count, 1)
                with patch.object(sys, "argv", ["verify_full_feedback", "--out", str(root), "--readback-only", "--gh", "/synthetic/gh"]):
                    self.assertEqual(tool.main(), 0)
                self.assertEqual(send.call_count, 1)

    def test_failed_readback_records_uncertainty_without_a_second_post(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ["verify_full_feedback", "--out", directory, "--live", "--gh", "/synthetic/gh"]
            with patch.object(sys, "argv", args), patch.object(tool, "submit_https", side_effect=TimeoutError) as send, \
                    patch.object(tool, "readback", side_effect=OSError):
                self.assertEqual(tool.main(), 1)
                self.assertEqual(send.call_count, 1)
            evidence = json.loads((Path(directory) / "verification.json").read_text())
            self.assertEqual(evidence["readback_result"], "OSError")
            self.assertTrue((Path(directory) / "live-attempt.json").is_file())

    def test_http_rejection_records_only_bounded_fixed_error_codes(self):
        for body in (b'{"error":"daily_budget"}', b"not-json"):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                error = HTTPError("https://receiver.example", 429, "limited", {}, io.BytesIO(body))
                args = ["verify_full_feedback", "--out", directory, "--live", "--gh", "/synthetic/gh"]
                with patch.object(sys, "argv", args), patch.object(tool, "submit_https", side_effect=error) as send, \
                        patch.object(tool, "readback", side_effect=OSError):
                    self.assertEqual(tool.main(), 1)
                    self.assertEqual(send.call_count, 1)
                evidence = json.loads((Path(directory) / "verification.json").read_text())
                self.assertEqual(evidence["http_status"], 429)
                if body.startswith(b"{"):
                    self.assertEqual(evidence["receiver_error"], "daily_budget")
                else:
                    self.assertNotIn("receiver_error", evidence)

    def test_missing_admin_path_and_bad_auth_never_expose_secret_output(self):
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _packet, envelope = tool.generate(root)
            error = subprocess.CalledProcessError(1, ["gh"], output="synthetic-secret", stderr="synthetic-secret")
            with patch.object(tool.subprocess, "run", side_effect=error), self.assertRaises(RuntimeError) as caught:
                tool.readback(root, envelope, "/synthetic/gh")
            self.assertNotIn("synthetic-secret", str(caught.exception))
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, "argv", [
                "verify_full_feedback", "--out", directory, "--live", "--gh", "relative-gh"]):
            with self.assertRaises(SystemExit):
                tool.main()

    def test_remote_content_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet, envelope = tool.generate(root)
            remote = MemoryGitHub()
            receipt = GitHubInbox(remote).accept(envelope, packet.read_bytes())
            remote.bodies[receipt["asset_id"]] = b"synthetic corruption"
            with patch.object(tool, "GitHubAPI", return_value=remote), \
                    patch.object(tool.subprocess, "run", return_value=SimpleNamespace(stdout="synthetic-token")), \
                    self.assertRaises(RuntimeError):
                tool.readback(root, envelope, "/synthetic/gh")

    def test_offline_entry_point_creates_a_real_isolated_report_without_network(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, "argv", [
                "verify_full_feedback", "--out", directory]), \
                patch("sf_feedback_transport.build_opener") as opener:
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("tools.verify_full_feedback", run_name="__main__")
            self.assertEqual(caught.exception.code, 0)
            opener.assert_not_called()
            evidence = json.loads((Path(directory) / "verification.json").read_text())
            self.assertEqual(evidence["feedback_post_calls"], 0)
            self.assertTrue(Path(evidence["report"]).is_file())
