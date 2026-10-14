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
from datetime import datetime, timezone
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

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
        }))
        (root / "release_notes.txt").write_text(
            "V%s 发布说明:\n修复更新链。\n" % version, encoding="utf-8")
        key = SigningKey.generate()
        key_dir = root / ".private_tools"
        key_dir.mkdir()
        (key_dir / "update_signing_key.txt").write_text(
            base64.b64encode(bytes(key)).decode("ascii") + "\n", encoding="ascii")
        return app, version, key.verify_key

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
            self.assertEqual(manifest["archive"]["size"], len(b"fake zip bytes"))
            self.assertEqual(manifest["signature"]["algorithm"], "ed25519")
            signature = base64.b64decode(manifest["signature"]["value"])
            self.assertEqual(verify_key.verify(mua.canonical(manifest), signature),
                             mua.canonical(manifest))
            self.assertEqual(manifest["archive"]["sha256"], mua.sha256(archive))
            self.assertEqual(manifest["notes"], "修复更新链。")
            self.assertEqual(manifest["published_at"], "2026-10-13T08:00:00Z")
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertEqual((output / "Skyrocket-update.json").stat().st_mode & 0o777, 0o600)


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
