"""Exercise the acceptance harness without requesting real camera permission."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools import verify_camera_auth_upgrade as P

try:
    from _base import SFCase
except ImportError:
    from tests._base import SFCase


class CameraUpgradeHarnessTests(SFCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="sf_camera_harness_")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.policy = {"bundle_id": P.PROBE_ID, "certificate_sha256": "A" * 64}

    def result(self, name, status):
        path = self.root / name
        path.write_text(json.dumps({"status": status, "version": "1"}))
        return path

    def test_reads_native_result_and_refuses_missing_result(self):
        path = self.result("result.json", "authorized_without_request")
        self.assertEqual(P.wait_result(path)["status"], "authorized_without_request")
        with self.assertRaisesRegex(ValueError, "No native"):
            P.wait_result(self.root / "missing", timeout=0)

    def test_launch_request_is_allowed_only_when_explicit(self):
        with mock.patch.object(P.signing, "run") as command:
            P.launch(self.root / "test.app", self.root / "result.json")
            self.assertIn("SF_CAMERA_PROBE_ALLOW_REQUEST=0", command.call_args.args[0])
            P.launch(self.root / "test.app", self.root / "baseline.json", may_request=True)
            self.assertIn("SF_CAMERA_PROBE_ALLOW_REQUEST=1", command.call_args.args[0])

    def test_existing_evidence_cannot_be_overwritten(self):
        path = self.result("existing.json", "authorized_without_request")
        with self.assertRaisesRegex(ValueError, "already exists"):
            P.launch(self.root / "test.app", path)
        with self.assertRaisesRegex(ValueError, "already exists"):
            P.prepare(self.root, self.policy, self.root)

    def test_running_probe_is_never_replaced(self):
        with mock.patch.object(P.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            with mock.patch.object(P.time, "monotonic", side_effect=[0, 0, 11]):
                with mock.patch.object(P.time, "sleep"):
                    with self.assertRaisesRegex(ValueError, "still running"):
                        P.wait_exit(self.root / "test.app")

    def test_process_detection_failure_is_not_treated_as_exit(self):
        for code in (1, 2):
            with self.subTest(code=code):
                with mock.patch.object(P.subprocess, "run", return_value=subprocess.CompletedProcess([], code)):
                    if code == 1:
                        P.wait_exit(self.root / "test.app")
                    else:
                        with self.assertRaisesRegex(ValueError, "Cannot determine"):
                            P.wait_exit(self.root / "test.app")

    def test_unanswered_or_denied_baseline_cannot_pass(self):
        for status in ("undetermined", "denied_by_user", "restricted"):
            with self.subTest(status=status):
                self.result("baseline-result.json", status)
                with self.assertRaisesRegex(ValueError, "not authorized"):
                    P.upgrade(self.root, self.policy)

    def test_changed_binary_upgrade_keeps_exact_path_and_records_observed_result(self):
        for status, expected in (("authorized_without_request", True), ("undetermined", False)):
            with self.subTest(status=status), tempfile.TemporaryDirectory(dir=self.root) as directory:
                out = Path(directory)
                (out / "baseline-result.json").write_text(json.dumps({"status": "granted_by_user"}))
                live = out / "Installed/CameraProbe.app"
                live.mkdir(parents=True)
                (live / "old").write_text("old")
                candidate = out / "candidate.app"
                candidate.mkdir()
                (candidate / "new").write_text("new")
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(P, "wait_exit"))
                    stack.enter_context(mock.patch.object(P.signing, "verify", side_effect=[
                        {"version": "1"}, {"version": "2"}]))
                    stack.enter_context(mock.patch.object(P.signing, "run", side_effect=[
                        "CDHash=old", "CDHash=new"]))
                    launch = stack.enter_context(mock.patch.object(P, "launch"))
                    stack.enter_context(mock.patch.object(P, "wait_result", return_value={"status": status}))
                    report = P.upgrade(out, self.policy)
                self.assertEqual(report["passed"], expected)
                self.assertTrue((live / "new").exists())
                self.assertTrue((out / "previous.app.stale/old").exists())
                self.assertEqual(launch.call_args.args[0], live)
                self.assertEqual(json.loads((out / "result.json").read_text()), report)

    def test_same_version_and_same_hash_cannot_fake_an_upgrade(self):
        self.result("baseline-result.json", "granted_by_user")
        for versions in (("1", "1"), ("1", "2")):
            with self.subTest(versions=versions), mock.patch.object(P, "wait_exit"):
                with mock.patch.object(P.signing, "verify", side_effect=[
                    {"version": versions[0]}, {"version": versions[1]}]):
                    with mock.patch.object(P.signing, "run", return_value="CDHash=unchanged"):
                        with self.assertRaises(ValueError):
                            P.upgrade(self.root, self.policy)

    def test_partial_copy_failure_restores_baseline_and_preserves_failed_candidate(self):
        self.result("baseline-result.json", "granted_by_user")
        live = self.root / "Installed/CameraProbe.app"
        live.mkdir(parents=True)
        (live / "old").write_text("old")

        def fail_copy(source, destination, **kwargs):
            destination.mkdir()
            (destination / "partial").write_text("incomplete")
            raise OSError("copy failed")

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(P, "wait_exit"))
            stack.enter_context(mock.patch.object(P.signing, "verify", side_effect=[
                {"version": "1"}, {"version": "2"}]))
            stack.enter_context(mock.patch.object(P.signing, "run", side_effect=["CDHash=old", "CDHash=new"]))
            stack.enter_context(mock.patch.object(P.shutil, "copytree", side_effect=fail_copy))
            with self.assertRaisesRegex(OSError, "copy failed"):
                P.upgrade(self.root, self.policy)
        self.assertTrue((live / "old").is_file())
        self.assertTrue((self.root / "failed-candidate.app.stale/partial").is_file())

    def test_baseline_builds_different_variants_and_launches_one_permission_request(self):
        out = self.root / "fresh"
        commands = []

        def compile_only(command):
            commands.append(command)
            target = Path(command[command.index("-o") + 1])
            target.write_bytes(b"fixture binary")

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(P.signing, "check_material"))
            stack.enter_context(mock.patch.object(P.signing, "run", side_effect=compile_only))
            sign = stack.enter_context(mock.patch.object(P.signing, "sign"))
            launch = stack.enter_context(mock.patch.object(P, "launch"))
            P.prepare(out, self.policy, self.root)
        self.assertEqual(sign.call_count, 2)
        self.assertNotIn("UPGRADE_RELEASE", commands[0])
        self.assertIn("UPGRADE_RELEASE", commands[1])
        self.assertEqual(launch.call_count, 1)
        self.assertTrue(launch.call_args.kwargs["may_request"])
        self.assertTrue((out / "Installed/CameraProbe.app/Contents/Info.plist").is_file())

    def test_main_does_not_call_pending_baseline_a_pass(self):
        with mock.patch.object(P.signing, "load_policy", return_value=self.policy):
            with mock.patch.object(P, "prepare"), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(P.main(["baseline", "--out", str(self.root)]), 0)
        self.assertIn("NOT a pass", output.getvalue())

    def test_main_upgrade_and_restart_report_actual_failure(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(P.signing, "load_policy", return_value=self.policy))
            stack.enter_context(mock.patch.object(P, "upgrade", return_value={"passed": False}))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(P.main(["upgrade", "--out", str(self.root)]), 1)
        (self.root / "result.json").write_text(json.dumps({"passed": True}))
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(P.signing, "load_policy", return_value=self.policy))
            stack.enter_context(mock.patch.object(P.signing, "verify"))
            stack.enter_context(mock.patch.object(P, "wait_exit"))
            launch = stack.enter_context(mock.patch.object(P, "launch"))
            stack.enter_context(mock.patch.object(P, "wait_result", return_value={"status": "undetermined"}))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(P.main(["restart", "--out", str(self.root)]), 1)
        self.assertNotIn("may_request", launch.call_args.kwargs)
        self.assertFalse(json.loads((self.root / "result.json").read_text())["passed"])

    def test_main_missing_baseline_is_a_failure(self):
        with mock.patch.object(P.signing, "load_policy", return_value=self.policy):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(P.main(["upgrade", "--out", str(self.root)]), 2)


if __name__ == "__main__":
    unittest.main()
