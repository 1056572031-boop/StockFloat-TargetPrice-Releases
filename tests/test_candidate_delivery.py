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
import types
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
        self.write_archive_fixture()
        self.out = self.root / "report.json"
        self.console = contextlib.redirect_stdout(io.StringIO())
        self.console.__enter__()
        self.addCleanup(self.console.__exit__, None, None, None)
        self.track_patch(mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no processes")))
        self.good = {"started": True, "startup": ["STARTUP vfixture"],
                     "first_frame": ["first-frame painted"], "returncode": -15}

    def write_info(self, info):
        (self.app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))

    def write_archive_fixture(self):
        import zipfile
        with zipfile.ZipFile(self.archive, "w") as archive:
            for path in sorted(self.app.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(self.app.parent).as_posix())

    def test_artifact_token_changes_when_second_xattr_value_changes(self):
        import ctypes

        with tempfile.TemporaryDirectory(prefix="sf_xattr_token_test_") as directory:
            root = Path(directory)
            path = root / "Skyrocket.app"
            path.mkdir()
            payload = path / "payload"
            payload.write_bytes(b"payload")
            try:
                libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
                setter = libc.setxattr
                setter.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
                setter.restype = ctypes.c_int
                names = (b"com.skyrocket.token.one", b"com.skyrocket.token.two")
                for name, value in ((names[0], b"first"), (names[1], b"second")):
                    result = setter(os.fsencode(path), name, value, len(value), 0, 0)
                    if result != 0:
                        error = ctypes.get_errno()
                        self.skipTest("临时文件不支持 xattr: errno %d" % error)
            except (AttributeError, OSError) as exc:
                self.skipTest("本平台不支持真实 xattr: %s" % exc)

            first = delivery.artifact_token(path)
            result = setter(os.fsencode(path), names[1], b"changed", len(b"changed"), 0, 0)
            self.assertEqual(result, 0, "第二个 xattr 必须可修改")
            second = delivery.artifact_token(path)

            self.assertNotEqual(first, second)

    def test_xattrs_reports_library_and_attribute_read_failures(self):
        class FakeLib:
            def __init__(self, list_result=0, value_result=0, read_result=0):
                self.listxattr = lambda *_args: list_result
                self.getxattr = lambda *_args: value_result
                self.read_result = read_result

        path = self.root / "xattrs"
        path.mkdir()
        with mock.patch.object(delivery.ctypes, "CDLL", side_effect=OSError("missing")):
            with self.assertRaisesRegex(RuntimeError, "无法加载扩展属性接口"):
                delivery._xattrs(path)  # discipline-ok(R5): 直接验证 xattr 能力探测失败

        with mock.patch.object(delivery.ctypes, "CDLL", return_value=FakeLib(list_result=-1)), \
                mock.patch.object(delivery.ctypes, "get_errno", return_value=5):
            with self.assertRaisesRegex(RuntimeError, "无法读取候选扩展属性"):
                delivery._xattrs(path)  # discipline-ok(R5): 直接验证 xattr 列表读取失败

        list_failure = FakeLib()
        list_failure.listxattr = lambda _path, buffer, _size, _flags: (
            1 if buffer is None else -1
        )
        with mock.patch.object(delivery.ctypes, "CDLL", return_value=list_failure), \
                mock.patch.object(delivery.ctypes, "get_errno", return_value=6):
            with self.assertRaisesRegex(RuntimeError, "无法读取候选扩展属性"):
                delivery._xattrs(path)  # discipline-ok(R5): 直接验证 xattr 名称列表二次读取失败

        value_failure = FakeLib()

        def write_names(_path, buffer, _size, _flags):
            if buffer is None:
                return 11
            buffer.raw = b"user.token\0"
            return 11

        value_failure.listxattr = write_names
        value_failure.getxattr = lambda _path, _name, _buffer, _size, _position, _flags: (
            -1 if _buffer is None else -1
        )
        with mock.patch.object(delivery.ctypes, "CDLL", return_value=value_failure), \
                mock.patch.object(delivery.ctypes, "create_string_buffer", side_effect=lambda size: types.SimpleNamespace(raw=b"user.token\0")), \
                mock.patch.object(delivery.ctypes, "get_errno", return_value=7):
            with self.assertRaisesRegex(RuntimeError, "无法读取候选扩展属性"):
                delivery._xattrs(path)  # discipline-ok(R5): 直接验证 xattr 值大小读取失败

        value_read_failure = FakeLib()
        value_read_failure.listxattr = write_names
        value_read_failure.getxattr = lambda _path, _name, _buffer, _size, _position, _flags: (
            1 if _buffer is None else -1
        )
        with mock.patch.object(delivery.ctypes, "CDLL", return_value=value_read_failure), \
                mock.patch.object(delivery.ctypes, "create_string_buffer", side_effect=lambda size: types.SimpleNamespace(raw=b"user.token\0")), \
                mock.patch.object(delivery.ctypes, "get_errno", return_value=8):
            with self.assertRaisesRegex(RuntimeError, "无法读取候选扩展属性"):
                delivery._xattrs(path)  # discipline-ok(R5): 直接验证 xattr 值二次读取失败

    def test_artifact_token_rejects_invalid_bundle_shapes(self):
        with self.assertRaisesRegex(ValueError, "候选 APP 缺失"):
            delivery.artifact_token(self.root / "missing.app")  # discipline-ok(R5): 直接验证候选包缺失边界

        symlink = self.root / "alias.app"
        symlink.symlink_to(self.app)
        with self.assertRaisesRegex(ValueError, "候选 APP 缺失"):
            delivery.artifact_token(symlink)  # discipline-ok(R5): 直接验证候选包顶层软链接边界

        inside = self.app / "Contents" / "MacOS" / "inside-link"
        inside.symlink_to("Skyrocket")
        self.assertEqual(delivery.artifact_token(self.app)["kind"], "directory")

        fifo = self.app / "Contents" / "fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "非普通文件"):
            delivery.artifact_token(self.app)  # discipline-ok(R5): 直接验证候选包特殊文件拒绝
        fifo.unlink()

        outside_link = self.app / "Contents" / "MacOS" / "outside-link"
        outside_link.symlink_to(self.root / "outside")
        with self.assertRaisesRegex(ValueError, "候选 APP 软链接越界"):
            delivery.artifact_token(self.app)  # discipline-ok(R5): 直接验证包内软链接越界

    def test_preflight_rejects_archive_artifact_identity_mismatch(self):
        with mock.patch.object(delivery, "run", side_effect=self.archive_utility), \
                mock.patch.object(delivery, "artifact_token", side_effect=[
                    {"sha256": "input", "entries": 1, "kind": "directory"},
                    {"sha256": "archive", "entries": 1, "kind": "directory"},
                ]):
            with self.assertRaisesRegex(ValueError, "artifact 身份不一致"):
                delivery.preflight(self.app, self.archive, self.root)  # discipline-ok(R5): 直接验证 ZIP 身份绑定拒绝

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

    def test_metadata_accepts_timestamped_rollback_bundle_as_old_app(self):
        rollback = self.root / ".Skyrocket.app.rollback-20261112"
        shutil.copytree(self.app, rollback)
        self.assertEqual(delivery.metadata(rollback, require_standard_name=False), self.info)

    def test_utility_and_preflight(self):
        with mock.patch.object(subprocess, "run", autospec=True) as runner:
            delivery.run(["/usr/bin/ditto"])
            runner.assert_called_once_with(["/usr/bin/ditto"], check=True, capture_output=True,
                                           text=True, errors="replace", timeout=180)
        with mock.patch.object(delivery, "run", side_effect=self.archive_utility) as runner:
            report = delivery.preflight(self.app, self.archive, self.root)
        self.assertTrue(report["archive_verified"])
        self.assertEqual(report["archive_top_level"], "Skyrocket.app")
        self.assertTrue(report["app_and_copy_signatures_valid"])
        self.assertTrue(any(call.args[0][:3] == ["/usr/bin/ditto", "-x", "-k"] for call in runner.call_args_list))
        self.assertEqual(len(runner.call_args_list), 5, "ditto, archive extraction and both signature checks are mandatory")
        for archive in (self.root / "missing.zip", self.exe):
            with self.assertRaisesRegex(ValueError, "zip"):
                delivery.preflight(self.app, archive, self.root)

    def test_preflight_verifies_real_zip_top_level_bundle(self):
        import zipfile
        with zipfile.ZipFile(self.archive, "w") as archive:
            for path in sorted(self.app.rglob("*")):
                rel = path.relative_to(self.app.parent)
                if path.is_file():
                    archive.write(path, rel.as_posix())

        with mock.patch.object(delivery, "run", side_effect=self.archive_utility) as runner:
            report = delivery.preflight(self.app, self.archive, self.root)

        self.assertTrue(report["archive_verified"])
        self.assertEqual(report["archive_top_level"], "Skyrocket.app")
        self.assertEqual(len(runner.call_args_list), 5, "ditto, archive extraction and both signature checks are mandatory")

    def test_preflight_rejects_malformed_archive_shapes(self):
        import zipfile

        cases = []
        empty = self.root / "empty.zip"
        with zipfile.ZipFile(empty, "w"):
            pass
        cases.append((empty, "为空"))

        traversal = self.root / "traversal.zip"
        with zipfile.ZipFile(traversal, "w") as archive:
            archive.writestr("../escape.txt", "bad")
        cases.append((traversal, "越界"))

        absolute = self.root / "absolute.zip"
        with zipfile.ZipFile(absolute, "w") as archive:
            archive.writestr("/absolute.txt", "bad")
        cases.append((absolute, "越界"))

        empty_name = self.root / "empty-name.zip"
        with zipfile.ZipFile(empty_name, "w") as archive:
            archive.writestr("", "bad")
        cases.append((empty_name, "非法条目"))

        wrong_root = self.root / "wrong-root.zip"
        with zipfile.ZipFile(wrong_root, "w") as archive:
            archive.writestr("Other.app/Contents/Info.plist", b"bad")
        cases.append((wrong_root, "顶层"))

        bad_zip = self.root / "bad.zip"
        bad_zip.write_bytes(b"not a zip")
        cases.append((bad_zip, "不是有效"))

        for archive, message in cases:
            with self.subTest(archive=archive.name):
                work = self.root / (archive.stem + "-work")
                work.mkdir()
                with mock.patch.object(delivery, "run"), self.assertRaisesRegex(ValueError, message):
                    delivery.preflight(self.app, archive, work)

    def test_preflight_rejects_archive_version_mismatch(self):
        import zipfile
        wrong = self.root / "wrong-version.zip"
        info = dict(self.info, CFBundleShortVersionString="wrong")
        with zipfile.ZipFile(wrong, "w") as archive:
            archive.writestr("Skyrocket.app/Contents/Info.plist", plistlib.dumps(info))
            archive.writestr("Skyrocket.app/Contents/MacOS/Skyrocket", b"x")
        work = self.root / "wrong-version-work"
        work.mkdir()
        with mock.patch.object(delivery, "run", side_effect=self.archive_utility), self.assertRaisesRegex(ValueError, "版本"):
            delivery.preflight(self.app, wrong, work)

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

    def archive_utility(self, cmd):
        if cmd[0] != "/usr/bin/ditto":
            return
        if cmd[:3] == ["/usr/bin/ditto", "-x", "-k"]:
            import zipfile
            with zipfile.ZipFile(cmd[3]) as archive:
                archive.extractall(cmd[4])
        elif "-c" not in cmd:
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

        with mock.patch.object(delivery, "run", side_effect=self.archive_utility), \
                mock.patch.object(subprocess, "Popen", return_value=mock.Mock(pid=12345)), \
                mock.patch.object(subprocess, "run", side_effect=helper), \
                mock.patch.object(delivery, "stop_target") as stop, \
                mock.patch.object(delivery, "readback_config", return_value={"scope": "fixture"}), \
                mock.patch.object(delivery, "smoke", return_value=self.good) as smoke:
            result = delivery.helper_route(self.archive, "candidate", self.old, root, rollback)
            smoke.assert_called_once_with(root / "Skyrocket.app")
            self.assertTrue(stop.called)
            self.assertEqual(result["smoke"]["first_frame"], self.good["first_frame"])
            self.assertTrue(result["sentinel_unchanged"])
            return result

    def test_new_process_config_readback_requires_exact_semantics(self):
        config, content = delivery.sentinel(self.root / "semantic-home")
        payload = json.loads(content)
        observed = {"window": [payload[key] for key in ("code", "x", "y", "h", "bg")],
                    "alerts": payload["alerts"]}
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(stdout=json.dumps(observed))) as runner:
            report = delivery.readback_config(config, content)
        self.assertEqual(report["observed"], observed)
        self.assertEqual(runner.call_args.kwargs["env"]["SF_CONFIG_PATH"], str(config))
        with mock.patch.object(delivery.tempfile, "gettempdir", return_value=str(self.root / "unrelated")), \
                self.assertRaisesRegex(ValueError, "私有临时"):
            delivery.readback_config(config, content)
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(stdout='{"window": [], "alerts": []}')), \
                self.assertRaisesRegex(RuntimeError, "无法完整读回"):
            delivery.readback_config(config, content)
        frozen = dict(observed, bundle_version=self.info["CFBundleShortVersionString"])
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(stdout=json.dumps(frozen))) as runner:
            result = delivery.readback_config(config, content, self.app)
        self.assertEqual(result["scope"], "frozen_bundle_parser_new_process_not_ui")
        self.assertIn("--app", runner.call_args.args[0])
        frozen["bundle_version"] = "wrong"
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(stdout=json.dumps(frozen))), \
                self.assertRaisesRegex(RuntimeError, "包内版本"):
            delivery.readback_config(config, content, self.app)

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
        with mock.patch.object(delivery, "run", side_effect=self.archive_utility), \
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

    def test_out_that_is_a_directory_is_rejected_before_any_verification(self):
        """`--out` 是报告文件。传成目录时，冒烟、首装、升级、回退都先跑完，最后写报告才报
        IsADirectoryError，白跑一整段还留下红的执行凭据；所以必须在做任何事之前拒绝。"""
        directory = self.root / "evidence-dir"
        directory.mkdir()
        for bad in (str(directory), str(self.root / "not-yet-created") + os.sep):
            with self.subTest(out=bad), mock.patch.object(delivery, "verify") as verifier, \
                    contextlib.redirect_stderr(io.StringIO()) as stderr, \
                    self.assertRaises(SystemExit) as raised:
                delivery.main(["--app", str(self.app), "--zip", str(self.archive),
                               "--out", bad, "--old-app", str(self.old)])
            self.assertEqual(raised.exception.code, 2)
            verifier.assert_not_called()
            self.assertIn("--out", stderr.getvalue(), "报错要点名是哪个参数")
        with mock.patch.object(delivery, "verify", return_value=0) as verifier:
            self.assertEqual(delivery.main(["--app", str(self.app), "--zip", str(self.archive),
                                            "--out", str(self.root / "sub" / "report.json"),
                                            "--old-app", str(self.old)]), 0,
                             "文件路径（含尚不存在的子目录）照常放行")
            verifier.assert_called_once()
