# -*- coding: utf-8 -*-
"""覆盖率补强：把已由子进程验证的边界在主进程留下可计账的证据。

这组用例不碰真实摄像头、签名私钥、/Applications 或 OTA 网络；它只覆盖：
  * 发版工具的 CLI 分发、纯判定层和“环境不存在时如实跳过”路径；
  * 配置/日历/网络模块中可在临时目录和假响应下验证的失败分支。

端到端路线仍由原有发版链测试负责，两层证据不能互相替代。
"""
from __future__ import annotations

import base64
import io
import json
import pathlib
import plistlib
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import datetime, timezone
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from pathlib import Path

from nacl.signing import SigningKey

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

try:
    from _base import SFCase
except ImportError:  # pragma: no cover
    from tests._base import SFCase

import sf_calendar
import sf_config
import sf_network
import sf_utils
import bundle_closure_check as bcc
import make_update_assets as mua
import shell_cjk_scan as cjk
import release_sequence as rs
import publish_update_release as publish
import probe_force_update as force_probe
import verify_face_presence as face

load_month_cache = sf_calendar._load_month_cache  # discipline-ok(R5): disk-cache loader is the tested unit
FIXED_PUBLISHED_AT = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)


class FixedPublicationClock:
    @staticmethod
    def now(tz):
        return FIXED_PUBLISHED_AT.astimezone(tz)


def call(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = fn(*args, **kwargs)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
            if not isinstance(exc.code, int):
                err.write(str(exc.code) + "\n")
    return rc, out.getvalue() + err.getvalue()


class ReleaseSequenceInProcess(SFCase):
    def test_allocate_and_verify_identity_paths_are_covered(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            ledger = root / "ledger.json"
            constants = root / "sf_constants.py"
            constants.write_text("APP_VERSION = '9.9.9'\nRELEASE_SEQUENCE = 1\n", encoding="utf-8")
            record = rs.allocate(ledger, constants, "9.9.9", now="2026-11-20T00:00:00Z")
            self.assertEqual(record["sequence"], 1)
            self.assertEqual(rs.source_identity(constants), ("9.9.9", 1))  # discipline-ok(R1): fixed release identity fixture

            app = root / "Skyrocket.app" / "Contents"
            app.mkdir(parents=True)
            (app / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleShortVersionString": "9.9.9",
                "SkyrocketReleaseSequence": 1,
            }))
            self.assertEqual(rs.verify_prearchive(ledger, constants, app.parent),  # discipline-ok(R1): fixed release identity fixture
                             {"version": "9.9.9", "release_sequence": 1})  # discipline-ok(R1): fixed release identity fixture

    def test_release_sequence_identity_readers_reject_invalid_shapes(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            bad = root / "bad.json"
            bad.write_text("not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                rs.load_ledger(bad)
            constants = root / "sf_constants.py"
            constants.write_text("APP_VERSION='1.0.0'\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                rs.source_identity(constants)

    def test_release_sequence_rejects_invalid_ledger_records_and_source_shapes(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            ledger = root / "ledger.json"
            for payload in (
                {"schema": "bad", "releases": []},
                {"schema": 1, "releases": "bad"},
                {"schema": 1, "releases": ["bad"]},
                {"schema": 1, "releases": [{"sequence": 1}]},
            ):
                ledger.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    rs.load_ledger(ledger)
            constants = root / "sf_constants.py"
            constants.write_text("APP_VERSION='1.0.0'\nRELEASE_SEQUENCE=1\nRELEASE_SEQUENCE=2\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                rs.source_identity(constants)

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            ledger = root / "ledger.json"
            constants = root / "sf_constants.py"
            constants.write_text("APP_VERSION='9.9.9'\nRELEASE_SEQUENCE=2\n", encoding="utf-8")
            ledger.write_text(json.dumps({"schema": 1, "releases": [
                {"version": "9.9.9", "sequence": 2, "stage": "stable",
                 "skip_reason": "historical reservation"}]}), encoding="utf-8")
            app = root / "Skyrocket.app" / "Contents"
            app.mkdir(parents=True)
            info = {"CFBundleShortVersionString": "9.9.9", "SkyrocketReleaseSequence": 2}
            (app / "Info.plist").write_bytes(plistlib.dumps(info))
            archive = root / "archive.zip"
            import zipfile
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("Skyrocket.app/Contents/Info.plist", plistlib.dumps(info))
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": "9.9.9", "release_sequence": 2}), encoding="utf-8")
            self.assertEqual(rs.verify(ledger, constants, app.parent, manifest, archive),  # discipline-ok(R1): fixed release identity fixture
                             {"version": "9.9.9", "release_sequence": 2})  # discipline-ok(R1): fixed release identity fixture

    def test_release_sequence_error_and_cli_paths_are_observable(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            missing = root / "missing"
            self.assertEqual(rs.load_ledger(missing), {"schema": 1, "releases": []})
            constants = root / "sf_constants.py"
            constants.write_text("APP_VERSION='1.0.0'\nRELEASE_SEQUENCE=0\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "必须是正整数"):
                rs.update_source_sequence(constants, 0)
            with self.assertRaisesRegex(ValueError, "必须是正整数"):
                rs.update_source_sequence(constants, True)

            app = root / "Skyrocket.app" / "Contents"
            app.mkdir(parents=True)
            (app / "Info.plist").write_bytes(b"not plist")
            with self.assertRaisesRegex(ValueError, "Info.plist 无效"):
                rs.app_identity(app.parent)
            archive = root / "bad.zip"
            archive.write_bytes(b"not zip")
            with self.assertRaisesRegex(ValueError, "ZIP 无效"):
                rs.zip_identity(archive)
            manifest = root / "manifest.json"
            manifest.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "必须是对象"):
                rs.manifest_identity(manifest)

            self.assertEqual(rs.main(["--self-test"]), 0)
            self.assertEqual(rs.main([]), 2)
            self.assertEqual(rs.main(["allocate", "--version", "1.0.0", "--ledger", str(root / "cli-ledger.json"),  # discipline-ok(R1): fixed release identity fixture
                                      "--constants", str(constants)]), 0)

    def test_release_sequence_remaining_error_and_cli_verify_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            bad_dir = root / "ledger-dir"
            bad_dir.mkdir()
            with self.assertRaisesRegex(ValueError, "无法读取账本"):
                rs.load_ledger(bad_dir)
            constants = root / "sf_constants.py"
            constants.write_text("RELEASE_SEQUENCE=1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                rs.source_identity(constants)
            constants.write_text("APP_VERSION='1.0.0'\nRELEASE_SEQUENCE=1\nRELEASE_SEQUENCE=2\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                rs.update_source_sequence(constants, 3)
            with self.assertRaisesRegex(ValueError, "非空字符串"):
                rs.allocate(root / "ledger.json", constants, "")
            with self.assertRaisesRegex(ValueError, "严格大于"):
                rs.allocate(root / "ledger.json", constants, "1.0.0", skip=1)
            with self.assertRaisesRegex(ValueError, "stage"):
                rs.allocate(root / "ledger.json", root / "single.py", "1.0.0", stage="canary")

            app = root / "Skyrocket.app" / "Contents"
            app.mkdir(parents=True)
            (app / "Info.plist").write_bytes(plistlib.dumps([]))
            with self.assertRaisesRegex(ValueError, "Info.plist 必须是对象"):
                rs.app_identity(app.parent)
            with self.assertRaisesRegex(ValueError, "无法读取冻结包"):
                rs.app_identity(root / "missing.app")
            with self.assertRaisesRegex(ValueError, "无法读取 ZIP"):
                rs.zip_identity(root / "missing.zip")
            with self.assertRaisesRegex(ValueError, "无法读取签名清单"):
                rs.manifest_identity(root / "missing.json")

            constants.write_text("APP_VERSION='1.0.0'\nRELEASE_SEQUENCE=1\n", encoding="utf-8")
            ledger = root / "ledger.json"
            ledger.write_text(json.dumps({"schema": 1, "releases": []}), encoding="utf-8")
            info = {"CFBundleShortVersionString": "1.0.1", "SkyrocketReleaseSequence": 2}
            (app / "Info.plist").write_bytes(plistlib.dumps(info))
            with self.assertRaisesRegex(ValueError, "不一致"):
                rs.verify_prearchive(ledger, constants, app.parent)
            (app / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleShortVersionString": "1.0.0", "SkyrocketReleaseSequence": 1}))
            with self.assertRaisesRegex(ValueError, "没有源码版本"):
                rs.verify_prearchive(ledger, constants, app.parent)

            archive = root / "archive.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("Skyrocket.app/Contents/Info.plist", plistlib.dumps({
                    "CFBundleShortVersionString": "1.0.0", "SkyrocketReleaseSequence": 1}))
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": "1.0.0", "release_sequence": 1}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "账本没有源码版本"):
                rs.verify(ledger, constants, app.parent, manifest, archive)

            ledger.write_text(json.dumps({"schema": 1, "releases": [{
                "version": "1.0.0", "sequence": 1}]}), encoding="utf-8")
            self.assertEqual(rs.main(["verify", "--app", str(app.parent), "--manifest", str(manifest),
                                      "--archive", str(archive), "--ledger", str(ledger),
                                      "--constants", str(constants)]), 0)


        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            app = root / "Skyrocket.app" / "Contents"
            app.mkdir(parents=True)
            (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "1.0.0"}))
            with self.assertRaisesRegex(ValueError, "SkyrocketReleaseSequence"):
                rs.app_identity(app.parent)
            archive = root / "multiple.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                payload = plistlib.dumps({"CFBundleShortVersionString": "1.0.0", "SkyrocketReleaseSequence": 1})
                zf.writestr("one.app/Contents/Info.plist", payload)
                zf.writestr("two.app/Contents/Info.plist", payload)
            with self.assertRaisesRegex(ValueError, "恰好有一个"):
                rs.zip_identity(archive)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "release_sequence"):
                rs.manifest_identity(manifest)


    def test_release_sequence_duplicate_allocation_and_first_skip_reason_are_guarded(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            ledger = root / "ledger.json"
            constants = root / "sf_constants.py"
            constants.write_text("APP_VERSION='1.0.0'\nRELEASE_SEQUENCE=9\n", encoding="utf-8")
            ledger.write_text(json.dumps({"schema": 1, "releases": [{
                "version": "1.0.0", "sequence": 3, "stage": "beta", "status": "allocated",
                "skip_reason": "reserved"}]}), encoding="utf-8")
            record = rs.allocate(ledger, constants, "1.0.0")
            self.assertEqual(record["sequence"], 3)
            self.assertEqual(rs.source_identity(constants), ("1.0.0", 3))  # discipline-ok(R1): fixed release identity fixture
            ledger.write_text(json.dumps({"schema": 1, "releases": [{
                "version": "1.0.0", "sequence": 3, "stage": "beta", "status": "allocated"}]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "skip_reason"):
                rs.load_ledger(ledger)

    def test_release_sequence_manifest_and_cli_selftest_negative_paths_are_covered(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": "1.0.0", "release_sequence": 0}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "正整数"):
                rs.manifest_identity(manifest)
            constants = root / "sf_constants.py"
            constants.write_text("APP_VERSION='1.0.0'\nRELEASE_SEQUENCE=1\n", encoding="utf-8")
            app = root / "App.app" / "Contents"
            app.mkdir(parents=True)
            (app / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleShortVersionString": "1.0.0", "SkyrocketReleaseSequence": 1}))
            with self.assertRaisesRegex(ValueError, "没有源码版本"):
                rs.verify_prearchive(root / "missing.json", constants, app.parent)  # discipline-ok(R1): fixed release identity fixture


        with self.assertRaises(SystemExit):
            publish.ensure_release_sequence({}, {}, resume=False)
        with self.assertRaises(SystemExit):
            publish.ensure_release_sequence({"release_sequence": 0}, {}, resume=False)
        with self.assertRaises(SystemExit):
            publish.ensure_release_sequence({"release_sequence": 2}, {"release_sequence": 3}, resume=False)
        with self.assertRaises(SystemExit):
            publish.ensure_release_sequence({"release_sequence": 3}, {"release_sequence": "bad"}, resume=False)
        with self.assertRaises(SystemExit):
            publish.ensure_release_sequence(
                {"version": "1.0.0", "release_sequence": 3, "archive": {"sha256": "a"}},
                {"version": "2.0.0", "release_sequence": 3, "archive": {"sha256": "a"}},
                resume=False)
        with self.assertRaises(SystemExit):
            publish.ensure_release_sequence(
                {"version": "1.0.0", "release_sequence": 3, "archive": {"sha256": "a"}},
                {"version": "1.0.0", "stage": "stable", "release_sequence": 3,
                 "archive": {"sha256": "b"}}, resume=False)
        with self.assertRaises(SystemExit):
            publish.prepare_rollback({"version": "1.0.0"}, {}, {}, "1.0.0", "", now="2026-11-20T00:00:00Z")
        with self.assertRaises(SystemExit):
            publish.rollback_ledger_path(Path("override"), Path("/tmp/source"))

    def test_publish_timestamp_and_manifest_guards_are_observable(self):
        live = {"published_at": "2026-11-20T00:00:00Z"}
        with self.assertRaises(SystemExit):
            publish.ensure_published_at_forward({"published_at": "2026-11-19T00:00:00Z"}, live)
        with self.assertRaises(SystemExit):
            publish._utc_timestamp("not-a-time")  # discipline-ok(R5): exercise private timestamp parser boundary

        with self.assertRaises(SystemExit):
            publish._utc_timestamp("2026-11-20T00:00:00")  # discipline-ok(R5): exercise private timestamp parser boundary

        key = SigningKey.generate()
        stamped = publish.resign_manifest({"version": "1.0.0", "signature": "old"}, key,
                                           "2026-11-20T00:00:00Z")
        self.assertEqual(stamped["signature"]["algorithm"], "ed25519")
        with mock.patch.object(publish, "read_contents_main", return_value={"content": "!!!"}):
            with self.assertRaises(SystemExit):
                publish.live_manifest_metadata("token")
        payload = base64.b64encode(json.dumps([]).encode()).decode()
        with mock.patch.object(publish, "read_contents_main", return_value={"content": payload}):
            with self.assertRaises(SystemExit):
                publish.live_manifest_metadata("token")
    def test_publish_readback_and_forward_time_edges_are_reported(self):
        live_at = "2026-11-20T00:00:00Z"
        self.assertGreater(publish.newer_utc_now(live_at), live_at)
        self.assertGreater(publish.newer_utc_now("2999-01-01T00:00:00Z"), "2999-01-01T00:00:00Z")
        with self.assertRaises(SystemExit):
            publish.ensure_forward_version("1.0.0", "1.0.0", resume=False)
        with self.assertRaises(SystemExit):
            publish.ensure_published_at_forward({"published_at": live_at}, {"published_at": live_at})

        expected = {
            "version": "1.0.0", "release_sequence": 2, "published_at": "2026-11-20T00:00:01Z",
            "archive": {"sha256": "expected", "size": 10},
        }
        mismatched = {
            "version": "1.0.0", "release_sequence": 1, "published_at": "old",
            "archive": {"sha256": "actual", "size": 11},
        }
        ok, lines = publish.readback_verdict(mismatched, "1.0.0", expected=expected)
        self.assertFalse(ok)
        self.assertTrue(any("release_sequence" in line for line in lines))
        self.assertTrue(any("published_at" in line for line in lines))
        self.assertTrue(any("archive.sha256" in line for line in lines))
        self.assertTrue(any("archive.size" in line for line in lines))
        ok, lines = publish.readback_verdict({"version": "1.0.0"}, "1.0.0", expected=expected)
        self.assertFalse(ok)
        self.assertTrue(any("archive 不完整" in line for line in lines))


class ShellScanInProcess(SFCase):
    def test_undecodable_shell_cannot_be_silently_omitted(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            broken = root / "broken.sh"
            broken.write_bytes(b'echo "$DEST\xff"\n')
            with self.assertRaisesRegex(cjk.ScopeBroken, "broken.sh"):
                cjk.scan(root, min_files=1)

    def test_unreadable_shell_cannot_be_silently_omitted(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "blocked.sh").write_text("echo hi\n", encoding="utf-8")
            with mock.patch.object(pathlib.Path, "read_text", side_effect=PermissionError("blocked")):
                with self.assertRaisesRegex(cjk.ScopeBroken, "blocked.sh"):
                    cjk.scan(root, min_files=1)

    def test_main_reports_clean_scope_and_hits_scope(self):
        files = [ROOT / "run_tests.sh", ROOT / "build_release.sh"]
        with mock.patch.object(cjk, "scan", return_value=[]), \
             mock.patch.object(cjk, "iter_shell_files", return_value=files):
            rc, out = call(cjk.main, ["shell_cjk_scan.py", "--report"])
        self.assertEqual(rc, 0, out)
        self.assertIn("零命中", out)
        self.assertIn("run_tests.sh", out)

        hits = [("x.sh", 4, "$DEST，", 'echo "$DEST，"')]
        with mock.patch.object(cjk, "scan", return_value=hits), \
             mock.patch.object(cjk, "iter_shell_files", return_value=files):
            rc, out = call(cjk.main, ["shell_cjk_scan.py", "--report"])
        self.assertEqual(rc, 1, out)
        self.assertIn("共 1 处", out)
        self.assertIn("${NAME}", out)

    def test_main_handles_scope_failure_and_unknown_argument(self):
        with mock.patch.object(cjk, "scan", side_effect=cjk.ScopeBroken("范围坏了")):
            rc, out = call(cjk.main, ["shell_cjk_scan.py"])
        self.assertEqual(rc, 2)
        self.assertIn("范围坏了", out)
        rc, out = call(cjk.main, ["shell_cjk_scan.py", "--typo"])
        self.assertEqual(rc, 2)
        self.assertIn("不认识参数", out)

    def test_main_self_test_is_dispatched_in_process(self):
        with mock.patch.object(cjk, "self_test", return_value=17) as run_self:
            rc, _out = call(cjk.main, ["shell_cjk_scan.py", "--self-test"])
        self.assertEqual(rc, 17)
        run_self.assert_called_once_with()


class FaceProbeDegradedRoutes(SFCase):
    def setUp(self):
        super().setUp()
        self.frameworks = mock.patch.object(
            face.sf_presence, "_load_frameworks", return_value=(None, "桥接不可用"))
        self.frameworks.start()
        self.addCleanup(self.frameworks.stop)

    def test_image_and_curve_skip_without_framework(self):
        for fn in (face.run_image, face.run_curve, face.run_edge,
                   face.run_near, face.run_lowlight):
            rc, out = call(fn, "/does/not/exist.png")
            self.assertEqual(rc, 2, fn.__name__)
            self.assertIn("跳过", out, fn.__name__)

    def test_camera_route_reports_start_failure_without_waiting(self):
        monitor = mock.Mock()
        monitor.start.return_value = face.sf_presence.STATE_DENIED
        monitor.state = face.sf_presence.STATE_DENIED
        monitor.last_error = "没有权限"
        with mock.patch.object(face.sf_presence, "FacePresenceMonitor", return_value=monitor), \
             mock.patch.object(face.sf_presence, "camera_access_status", return_value="denied"):
            rc, out = call(face.run, 0)
        self.assertEqual(rc, 1, out)
        self.assertIn("没能起来", out)

    def test_main_dispatches_each_non_camera_route(self):
        for option, name in (("--self-test", "self_test"), ("--curve", "run_curve"),
                             ("--edge", "run_edge"), ("--near", "run_near"),
                             ("--lowlight", "run_lowlight"), ("--image", "run_image")):
            with mock.patch.object(face, name, return_value=23) as fn:
                rc, _out = call(face.main, [option])
            self.assertEqual(rc, 23, option)
            fn.assert_called_once()

    def test_main_default_dispatches_camera_route(self):
        with mock.patch.object(face, "run", return_value=19) as fn:
            rc, _out = call(face.main, ["--seconds", "0"])
        self.assertEqual(rc, 19)
        fn.assert_called_once_with(0.0)


class BundleClosurePureRoutes(SFCase):
    def test_collect_imports_covers_static_relative_and_dynamic_shapes(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "good.py").write_text(
                "import json, package.sub\n"
                "from alpha.beta import thing\n"
                "from .local import value\n"
                "import importlib\n"
                "def f(name):\n"
                "    importlib.import_module(name)\n"
                "    __import__('literal.mod')\n", encoding="utf-8")
            (root / "bad.py").write_text("def broken(:\n", encoding="utf-8")
            tops, dynamic = bcc.collect_imports(root)
        self.assertIn("json", tops)
        self.assertIn("package", tops)
        self.assertIn("alpha", tops)
        self.assertIn("literal", tops)
        self.assertTrue(any("解析失败" in item for item in dynamic))
        self.assertTrue(any("运行时算出来" in item for item in dynamic))

    def test_judge_missing_distinguishes_allowed_present_stdlib_and_missing(self):
        ok, missing = bcc.judge_missing(
            {"json", "present", "allowed", "missing"},
            {"present"}, {"json"}, {"allowed"})
        self.assertEqual(ok, ["present"])
        self.assertEqual(missing, ["missing"])

    def test_bundle_available_includes_archives_frameworks_and_extensions(self):
        class FakeBundle:
            def __init__(self, _app):
                pass

            def entries(self):
                return ["PyQt6/QtCore.so", "pkg.mod.py", "plain"]

            def dispose(self):
                pass

        with tempfile.TemporaryDirectory() as td:
            app = pathlib.Path(td) / "Fake.app"
            for sub in ("Frameworks", "Resources", "MacOS"):
                (app / "Contents" / sub).mkdir(parents=True, exist_ok=True)
            (app / "Contents" / "Frameworks" / "_cffi_backend.cpython.so").write_bytes(b"")
            (app / "Contents" / "Resources" / "Theme.bundle").mkdir()
            with mock.patch.object(bcc.VD, "Bundle", FakeBundle):
                avail = bcc.bundle_available(app)
        self.assertIn("PyQt6", avail)
        self.assertIn("_cffi_backend", avail)
        self.assertIn("Theme", avail)
        self.assertIn("plain", avail)
        self.assertIn("sys", avail)

    def test_self_test_and_main_json_paths_are_counted(self):
        rc, out = call(bcc.self_test)
        self.assertEqual(rc, 0, out)
        with tempfile.TemporaryDirectory() as td:
            app = pathlib.Path(td) / "Fake.app"
            app.mkdir()
            result = {"checked": 1, "present": ["json"], "missing": [],
                      "dynamic_sites": [], "allow_missing": []}
            with mock.patch.object(bcc, "check", return_value=result):
                rc, out = call(bcc.main, [str(app), "--json"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out)["checked"], 1)

    def test_main_prints_dynamic_and_missing_modules(self):
        with tempfile.TemporaryDirectory() as td:
            app = pathlib.Path(td) / "Fake.app"
            app.mkdir()
            result = {"checked": 2, "present": [], "missing": ["lost"],
                      "dynamic_sites": ["x.py:2 运行时算出来"], "allow_missing": []}
            with mock.patch.object(bcc, "check", return_value=result):
                rc, out = call(bcc.main, [str(app)])
        self.assertEqual(rc, 1)
        self.assertIn("运行时算出来", out)
        self.assertIn("lost", out)

    def test_json_output_does_not_turn_missing_modules_into_success(self):
        with tempfile.TemporaryDirectory() as td:
            app = pathlib.Path(td) / "Fake.app"
            app.mkdir()
            result = {"checked": 1, "present": [], "missing": ["lost"],
                      "dynamic_sites": [], "allow_missing": []}
            with mock.patch.object(bcc, "check", return_value=result):
                rc, out = call(bcc.main, [str(app), "--json"])
        self.assertEqual(json.loads(out)["missing"], ["lost"])
        self.assertEqual(rc, 1, "JSON output must preserve the release gate's failure exit code")


class MakeUpdateAssetsPositiveRoute(SFCase):
    def _fixture(self, root: pathlib.Path):
        version = "9.9.91"
        app = root / mua.APP_NAME
        (app / "Contents" / "MacOS").mkdir(parents=True)
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": mua.BUNDLE_ID,
            "CFBundleShortVersionString": version,
            "SkyrocketReleaseSequence": 10,
        }))
        (root / "release_notes.txt").write_text(
            "V%s 发布说明:\n1. 修复更新链。\n" % version, encoding="utf-8")
        (root / "sf_constants.py").write_text(
            "APP_VERSION = '%s'\nRELEASE_SEQUENCE = 10\n" % version, encoding="utf-8")
        (root / "tools").mkdir()
        (root / "tools" / "release_sequence_ledger.json").write_text(json.dumps({
            "schema": 1,
            "releases": [{"version": version, "sequence": 10, "status": "allocated",
                          "stage": "beta", "skip_reason": "historical sequence reservation"}],
        }), encoding="utf-8")
        key = SigningKey.generate()
        key_dir = root / ".private_tools"
        key_dir.mkdir()
        (key_dir / "update_signing_key.txt").write_text(
            base64.b64encode(bytes(key)).decode("ascii") + "\n", encoding="ascii")
        return app, version, key.verify_key

    def test_prearchive_identity_mismatch_rejects_before_signing_or_creating_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            app, version, _ = self._fixture(root)
            output = root / "out"
            constants = root / "sf_constants.py"
            constants.write_text(
                "APP_VERSION = '%s'\nRELEASE_SEQUENCE = 11\n" % version, encoding="utf-8")
            argv = ["make_update_assets.py", str(app), "--output", str(output)]
            with mock.patch.object(mua, "__file__", str(root / "tools" / "make_update_assets.py")), \
                    mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(mua.subprocess, "run") as ditto:
                rc, out = call(mua.main)
            self.assertNotEqual(rc, 0)
            self.assertIn("拒绝生成 OTA 资产", out)
            self.assertFalse(output.exists())
            ditto.assert_not_called()

    def test_inconsistent_prearchive_verdict_stops_before_any_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            app, version, _ = self._fixture(root)
            output = root / "out"
            argv = ["make_update_assets.py", str(app), "--output", str(output)]
            with mock.patch.object(mua, "__file__", str(root / "tools" / "make_update_assets.py")), \
                    mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(mua, "verify_prearchive", return_value={"version": version, "release_sequence": 11}), \
                    mock.patch.object(mua.subprocess, "run") as archive:
                rc, out = call(mua.main)
            self.assertNotEqual(rc, 0)
            self.assertIn("Info.plist 发布序号", out)
            self.assertFalse(output.exists())
            archive.assert_not_called()

    def test_mismatched_signing_key_stops_before_archive_or_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            app, _, _ = self._fixture(root)
            output = root / "out"
            wrong_public = base64.b64encode(bytes(SigningKey.generate().verify_key)).decode()
            with mock.patch.object(mua, "__file__", str(root / "tools" / "make_update_assets.py")), \
                    mock.patch.object(mua.subprocess, "run") as archive, \
                    mock.patch.object(sys, "argv", ["make_update_assets.py", str(app), "--output", str(output)]), \
                    mock.patch("sf_constants.UPDATE_PUBLIC_KEY_B64", wrong_public):
                with self.assertRaisesRegex(SystemExit, "公钥"):
                    mua.main()
            archive.assert_not_called()
            self.assertFalse(output.exists())

    def test_main_writes_signed_manifest_and_rotates_previous_archive(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            app, version, verify_key = self._fixture(root)
            output = root / "out"

            def fake_ditto(cmd, check):
                self.assertTrue(check)
                pathlib.Path(cmd[-1]).write_bytes(b"fake zip bytes")

            argv = ["make_update_assets.py", str(app), "--output", str(output)]
            with mock.patch.object(mua, "__file__", str(root / "tools" / "make_update_assets.py")), \
                 mock.patch.object(mua.subprocess, "run", side_effect=fake_ditto), \
                 mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(mua, "datetime", FixedPublicationClock), \
                 mock.patch("sf_constants.UPDATE_PUBLIC_KEY_B64", base64.b64encode(bytes(verify_key)).decode()), \
                 mock.patch("sf_constants.RELEASE_STAGE", "beta"):
                rc, out = call(mua.main)
                self.assertIsNone(rc, out)
                archive = output / ("Skyrocket-%s-macos.zip" % version)
                self.assertTrue(archive.is_file())
                rc, out = call(mua.main)

            self.assertIsNone(rc, out)
            self.assertTrue((output / (archive.name + ".previous")).is_file())
            manifest = json.loads((output / "Skyrocket-update.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["stage"], "beta")
            self.assertEqual(manifest["release_sequence"], 10)
            self.assertEqual(manifest["archive"]["size"], len(b"fake zip bytes"))
            self.assertEqual(manifest["signature"]["algorithm"], "ed25519")
            signature = base64.b64decode(manifest["signature"]["value"])
            self.assertEqual(verify_key.verify(mua.canonical(manifest), signature),
                             mua.canonical(manifest))
            self.assertEqual(manifest["archive"]["sha256"], mua.sha256(archive))
            self.assertEqual(manifest["notes"], "1. 修复更新链。")
            self.assertEqual(manifest["published_at"], "2026-10-13T08:00:00Z")
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertEqual((output / "Skyrocket-update.json").stat().st_mode & 0o777, 0o600)

    def test_self_test_rejects_broken_positive_controls(self):
        """自检里的正向对照若失效，必须把自检本身判红并留下覆盖证据。"""
        original = mua.require_own_section
        calls = {"long": 0}

        def broken_positive_controls(raw_notes, version):
            if version == "1.4.45":
                calls["long"] += 1
                if calls["long"] == 1:
                    raise SystemExit("forced positive-control rejection")
                return None
            return original(raw_notes, version)

        with mock.patch.object(mua, "require_own_section", side_effect=broken_positive_controls):
            rc, out = call(mua.self_test)

        self.assertEqual(rc, 1, out)
        self.assertIn("扩大上限后的正向对照未通过", out)
        self.assertIn("300 字的说明被放行", out)

    def test_self_test_exercises_exact_note_length_boundaries(self):
        """200 字被误拒与 201 字被放行两条自检分支也要计入主进程覆盖率。"""
        original = mua.require_own_section

        def broken_boundaries(raw_notes, version):
            count = raw_notes.count("甲")
            if count == 186:
                raise SystemExit("forced at-limit rejection")
            if count == 187:
                return None
            return original(raw_notes, version)

        with mock.patch.object(mua, "require_own_section", side_effect=broken_boundaries):
            rc, out = call(mua.self_test)

        self.assertEqual(rc, 1, out)
        self.assertIn("刚好 200 字的说明被误拒", out)
        self.assertIn("201 字的说明被放行", out)


class ProductSmallEdgeBranches(SFCase):
    def test_utils_handles_market_and_format_edges(self):
        self.assertEqual(sf_utils.fmt_pct(0.004), "0.00%")
        self.assertEqual(sf_utils.fmt_pct(-0.004), "0.00%")
        self.assertEqual(sf_utils.fmt_pct(1001), "+1.00e+03%")
        self.assertEqual(sf_utils.market_prefix("110001"), "sh")
        self.assertEqual(sf_utils.market_prefix("159915"), "sz")
        self.assertEqual(sf_utils.market_prefix("920001"), "bj")
        self.assertEqual(sf_utils.market_prefix("abc123"), "")

    def test_calendar_loads_valid_disk_cache_and_rejects_empty_cache(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(sf_calendar, "_CACHE_DIR", td), \
             mock.patch.object(sf_calendar, "_month_cache", {}), \
             mock.patch.object(sf_calendar, "_fetched_months", set()), \
             mock.patch.object(sf_calendar, "_month_sync_status", {}):
            path = pathlib.Path(td) / "calendar_2099-01.json"
            path.write_text(json.dumps({"holidays": ["2099-01-01"]}), encoding="utf-8")
            self.assertEqual(load_month_cache("2099-01"), {"2099-01-01"})
            path.unlink()
            self.assertEqual(load_month_cache("2099-01"), {"2099-01-01"},
                             "confirmed memory cache must survive disk removal")
            (path.parent / "calendar_2099-02.json").write_text(
                json.dumps({"holidays": []}), encoding="utf-8")
            self.assertEqual(load_month_cache("2099-02"), set())

    def test_network_non_retryable_http_error_is_one_attempt(self):
        error = urllib.error.HTTPError("https://example.test", 403, "forbidden", {}, io.BytesIO(b""))
        self.addCleanup(error.close)
        calls = []

        def fail(_request, timeout=None):
            calls.append(timeout)
            raise error

        with mock.patch.object(sf_network, "feature_enabled", return_value=False), \
             mock.patch.object(sf_network, "safe_urlopen", side_effect=fail), \
             mock.patch.object(sf_network, "_interruptible_sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                sf_network.http_get("https://example.test", retries=3)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_config_future_schema_refuses_alert_save(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = pathlib.Path(td) / "config.json"
            bak = pathlib.Path(td) / "config.json.bak"
            cfg.write_text(json.dumps({"version": sf_config.CONFIG_SCHEMA_VERSION + 1,
                                       "alerts": []}), encoding="utf-8")
            before = cfg.read_bytes()
            with mock.patch.object(sf_config, "CONFIG_PATH", str(cfg)), \
                 mock.patch.object(sf_config, "CONFIG_BACKUP_PATH", str(bak)), \
                 mock.patch.object(sf_config, "log_line") as log:
                self.assertFalse(sf_config.save_alerts([]))
            self.assertEqual(cfg.read_bytes(), before)
            self.assertTrue(any("future schema" in args[0]
                                for args, _kwargs in log.call_args_list))


if __name__ == "__main__":
    unittest.main(verbosity=2)
