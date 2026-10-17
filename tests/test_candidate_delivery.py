"""Pure temporary fixtures and mocked process boundaries; never launch real apps."""
import contextlib
import io
import json
import os
import plistlib
import runpy
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from tests._base import SFCase
from tools import verify_candidate_delivery as delivery


class CandidateDeliveryCase(SFCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="sf_candidate_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = self.root / "Skyrocket.app"
        self.exe = self.app / "Contents" / "MacOS" / "Skyrocket"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b"never execute")
        self.info = {"CFBundleExecutable": "Skyrocket", "CFBundleIdentifier": "com.skyrocket.app",
                     "CFBundleShortVersionString": "candidate"}
        self.write_info(self.info)
        self.old = self.root / "old" / "Skyrocket.app"
        shutil.copytree(self.app, self.old)
        (self.old / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
            dict(self.info, CFBundleShortVersionString="old")))
        self.archive = self.root / "candidate.zip"
        self.archive.write_bytes(b"fixture")
        self.out = self.root / "report.json"
        self.console = contextlib.redirect_stdout(io.StringIO())
        self.console.__enter__()
        self.addCleanup(self.console.__exit__, None, None, None)
        self.track_patch(mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no processes")))
        self.good = {"started": True, "startup": ["STARTUP vfixture"],
                     "first_frame": ["first-frame painted"], "returncode": -15}

    def write_info(self, info):
        (self.app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))

    def test_metadata_rejects_bad_identity_and_escape(self):
        self.assertEqual(delivery.metadata(self.app), self.info)
        with self.assertRaisesRegex(ValueError, "Skyrocket.app"):
            delivery.metadata(self.root)
        for value in ([], dict(self.info, CFBundleExecutable="../escape"),
                      dict(self.info, CFBundleExecutable=".."), dict(self.info, CFBundleExecutable=1),
                      dict(self.info, CFBundleExecutable=""), dict(self.info, CFBundleExecutable="absent"),
                      dict(self.info, CFBundleIdentifier="wrong"),
                      dict(self.info, CFBundleShortVersionString="")):
            self.write_info(value)
            with self.assertRaises(ValueError):
                delivery.metadata(self.app)
        self.write_info(self.info)
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        self.exe.unlink()
        self.exe.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "逃出"):
            delivery.metadata(self.app)

    def test_utility_and_preflight(self):
        with mock.patch.object(subprocess, "run", autospec=True) as runner:
            delivery.run(["/usr/bin/ditto"])
            runner.assert_called_once_with(["/usr/bin/ditto"], check=True, capture_output=True,
                                           text=True, errors="replace", timeout=180)
        with mock.patch.object(delivery, "run") as runner:
            report = delivery.preflight(self.app, self.archive, self.root)
        self.assertFalse(report["archive_verified"])
        self.assertTrue(report["app_and_copy_signatures_valid"])
        self.assertEqual(report["version"], self.info["CFBundleShortVersionString"])
        self.assertEqual(len(runner.call_args_list), 3, "ditto plus both signature checks are mandatory")
        for archive in (self.root / "missing.zip", self.exe):
            with self.assertRaisesRegex(ValueError, "zip"):
                delivery.preflight(self.app, archive, self.root)

    def test_smoke_uses_observed_report_and_rejects_missing_markers(self):
        import importlib
        import sys
        sys.path.insert(0, str(Path(delivery.__file__).parent))
        smoke_frozen_app = importlib.import_module("smoke_frozen_app")
        with mock.patch.object(smoke_frozen_app, "run_smoke", return_value=(0, self.good)) as runner:
            self.assertEqual(delivery.smoke(self.app), self.good)
            runner.assert_called_once_with(self.app, platform="offscreen", timeout=35, keep_dir=True)
        for rc, report in ((1, self.good), (0, {}), (0, dict(self.good, startup=[])),
                           (0, dict(self.good, first_frame=[])), (0, dict(self.good, returncode=None))):
            with mock.patch.object(smoke_frozen_app, "run_smoke", return_value=(rc, report)), \
                    self.assertRaisesRegex(RuntimeError, "冒烟"):
                delivery.smoke(self.app)

    def test_helper_capability_and_real_negative_selftest(self):
        helper = self.root / "helper"
        helper.write_text('--verification-home) validate_verification_home launch_app '
                          '/usr/bin/open -n -g --env "SF_CONFIG_PATH= --env SF_ALLOW_TEMP_LAUNCH=1')
        delivery.require_helper(helper)
        self.assertEqual(delivery.self_test(), 0, "pure negative self-test must pass")
        with mock.patch.object(delivery, "check_sentinel"), mock.patch.object(delivery, "metadata"), \
                mock.patch.object(delivery, "require_helper"):
            self.assertEqual(delivery.self_test(), 1, "a broken judge must fail self-test")

    def test_process_rows_and_exact_cleanup(self):
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(stdout=" 12 /tmp/a b\nbad\n")):
            self.assertEqual(delivery.process_rows(), [(12, "/tmp/a b")])
        exact = str(self.exe)
        rows = [(12345, exact), (12346, exact + "-other"), (os.getpid(), exact)]
        with mock.patch.object(delivery, "process_rows", side_effect=[rows, rows, []]), \
                mock.patch.object(delivery.os, "kill") as kill:
            delivery.stop_target(self.app, self.exe.name)
            kill.assert_called_once_with(12345, delivery.signal.SIGTERM)
        with mock.patch.object(delivery, "process_rows", side_effect=[rows, [], []]), \
                mock.patch.object(delivery.os, "kill") as kill:
            delivery.stop_target(self.app, self.exe.name)
            kill.assert_not_called()
        with mock.patch.object(delivery, "process_rows", side_effect=[rows, rows, []]), \
                mock.patch.object(delivery.os, "kill", side_effect=ProcessLookupError):
            delivery.stop_target(self.app, self.exe.name)
        for path in (Path("/Applications/Skyrocket.app"), self.root):
            with self.assertRaisesRegex(ValueError, "非临时"):
                delivery.stop_target(path, "Skyrocket")
        with mock.patch.object(delivery, "process_rows", return_value=[(12345, exact)]), \
                mock.patch.object(delivery.os, "kill"), \
                mock.patch.object(delivery.time, "monotonic", side_effect=[100, 101, 111]), \
                mock.patch.object(delivery.time, "sleep") as sleep, \
                self.assertRaisesRegex(RuntimeError, "仍在运行"):
            delivery.stop_target(self.app, self.exe.name)
        sleep.assert_called_once_with(0.1)

    def test_sentinel_detects_symlink_and_tampering(self):
        config, content = delivery.sentinel(self.root / "home")
        delivery.check_sentinel(config, content)
        config.unlink()
        config.symlink_to(self.archive)
        with self.assertRaisesRegex(RuntimeError, "sentinel"):
            delivery.check_sentinel(config, content)

    def fake_utility(self, cmd):
        if cmd[0] == "/usr/bin/ditto" and "-c" not in cmd:
            shutil.copytree(cmd[1], cmd[2], dirs_exist_ok=True)

    def exercise_route(self, rollback=False, outcome=None, rc=None, mutate=None):
        root = self.root / ("rollback" if rollback else "upgrade")
        expected = {"status": "rolled_back", "message": "restart_new_app_failed"} if rollback else {
            "status": "installed", "message": "ok"}

        def helper(cmd, **kwargs):
            self.assertEqual(cmd[0:2], ["/bin/sh", str(delivery.HELPER)])
            home = Path(cmd[cmd.index("--verification-home") + 1])
            target = Path(cmd[cmd.index("--target") + 1])
            self.assertEqual(home.parent, target.parent)
            self.assertFalse(any(k.startswith("SF_") for k in kwargs["env"]))
            Path(cmd[cmd.index("--result") + 1]).write_text(json.dumps(outcome or expected))
            if not rollback:
                (target / "Contents" / "Info.plist").write_bytes(plistlib.dumps(self.info))
            if mutate:
                mutate(target, home)
            return mock.Mock(returncode=(1 if rollback else 0) if rc is None else rc)

        with mock.patch.object(delivery, "run", side_effect=self.fake_utility), \
                mock.patch.object(subprocess, "Popen", return_value=mock.Mock(pid=12345)), \
                mock.patch.object(subprocess, "run", side_effect=helper), \
                mock.patch.object(delivery, "stop_target") as stop, \
                mock.patch.object(delivery, "smoke", return_value=self.good) as smoke:
            result = delivery.helper_route(self.archive, "candidate", self.old, root, rollback)
            smoke.assert_called_once_with(root / "Skyrocket.app")
            self.assertTrue(stop.called)
            self.assertEqual(result["smoke"]["first_frame"], self.good["first_frame"])
            self.assertTrue(result["sentinel_unchanged"])
            return result

    def test_upgrade_and_real_old_rollback_orchestration(self):
        self.exercise_route()
        self.exercise_route(rollback=True)

    def test_route_rejects_wrong_helper_result_and_cleans(self):
        with self.assertRaisesRegex(RuntimeError, "结果不符"):
            self.exercise_route(outcome={"status": "failed"})

    def test_rollback_rejects_changed_old_executable(self):
        with self.assertRaisesRegex(RuntimeError, "可执行内容"):
            self.exercise_route(rollback=True, mutate=lambda target, home:
                                (target / "Contents/MacOS/Skyrocket").write_bytes(b"changed"))

    def test_route_rejects_changed_sentinel(self):
        with self.assertRaisesRegex(RuntimeError, "sentinel"):
            self.exercise_route(mutate=lambda target, home: (home / "config.json").write_bytes(b"bad"))

    def test_wrong_upgrade_version(self):
        with self.assertRaisesRegex(RuntimeError, "版本"):
            delivery.metadata_archive_expected("wrong", self.info)

    def test_fault_archive_keeps_top_level_and_signs_short_lived_exe(self):
        with mock.patch.object(delivery, "run") as runner:
            archive = delivery.fault_archive(self.root, "fault")
        app = self.root / "fault" / "Skyrocket.app"
        info = delivery.metadata(app)
        exe = app / "Contents/MacOS" / info["CFBundleExecutable"]
        self.assertEqual(exe.read_text(), "#!/bin/sh\nexit 0\n")
        self.assertTrue(exe.stat().st_mode & 0o111)
        self.assertEqual(runner.call_args_list, [
            mock.call(["/usr/bin/codesign", "--force", "--sign", "-", str(app)]),
            mock.call(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)]),
            mock.call(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive)])])

    def test_verify_success_and_stage_failures_are_honest(self):
        with mock.patch.object(delivery, "run", side_effect=self.fake_utility), \
                mock.patch.object(delivery, "require_helper"), \
                mock.patch.object(delivery, "smoke", return_value=self.good), \
                mock.patch.object(delivery, "helper_route", return_value={"passed": True}) as route, \
                mock.patch.object(delivery, "fault_archive", return_value=self.archive):
            result = delivery.verify(self.app, self.archive, self.out, self.old)
            self.assertEqual(result, 0, "all observed stages must pass")
            report = json.loads(self.out.read_text())
            self.assertTrue(report["verified"])
            self.assertTrue(report["preflight"]["archive_verified"])
            self.assertEqual(route.call_count, 2, "upgrade and OTA rollback both required")
        for error in (OSError("missing"), ValueError("bad"), RuntimeError("blocked"),
                      subprocess.TimeoutExpired("helper", 150)):
            with mock.patch.object(delivery, "preflight", side_effect=error):
                result = delivery.verify(self.app, self.archive, self.out, self.old)
            self.assertEqual(result, 1, "errors must fail closed")
            self.assertFalse(json.loads(self.out.read_text())["verified"])
        with mock.patch.object(delivery, "preflight", return_value={}), \
                mock.patch.object(delivery, "require_helper"), \
                mock.patch.object(delivery, "smoke") as smoke:
            delivery.verify(self.app, self.archive, self.out)
            self.assertIn("--old-app", json.loads(self.out.read_text())["error"])
            delivery.verify(self.app, self.archive, self.out, self.app)
            self.assertIn("版本必须不同", json.loads(self.out.read_text())["error"])
            smoke.assert_not_called()

    def test_cli_and_script_entry(self):
        for args in ([], ["--unknown"], ["--app", str(self.app)]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                delivery.main(args)
            self.assertEqual(raised.exception.code, 2, "unknown/incomplete CLI must reject")
        with mock.patch.object(delivery, "self_test", return_value=0):
            self.assertEqual(delivery.main(["--self-test"]), 0, "self-test dispatch")
        with mock.patch.object(delivery, "verify", return_value=1) as verifier:
            result = delivery.main(["--app", str(self.app), "--zip", str(self.archive),
                                    "--out", str(self.out), "--old-app", str(self.old)])
            self.assertEqual(result, 1, "preserve verifier failure")
            verifier.assert_called_once_with(self.app, self.archive, self.out, self.old)
        with mock.patch.object(delivery.sys, "argv", [str(Path(delivery.__file__).resolve())]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            runpy.run_path(delivery.__file__, run_name="__main__")
        self.assertEqual(raised.exception.code, 2, "script entry rejects missing inputs")
