"""Read-only startup checks: real Qt sampling, diagnosis export and negative controls."""

import ast
import copy
import json
import plistlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QWidget

import sf_glass
import sf_startup_health as health
import sf_update
from sf_constants import APP_DISPLAY_NAME, APP_VERSION, BUNDLE_ID
from sf_window_state import UpdateFlowState
from tests._base import SFCase, wait_for
import tests.test_coverage_window_diagnostics as diagnostic_fixtures


def healthy_data():
    return {"identity": {"frozen": True, "bundle_id": BUNDLE_ID, "version": APP_VERSION,
                         "executable": APP_DISPLAY_NAME},
            "visibility": {"qt_visible": True, "native_visible": True, "intersects_screen": True},
            "glass": {"present": True, "level": sf_glass._TOP_WINDOW_LEVEL,  # discipline-ok(R5): existing authoritative native level
                      "expected_level": sf_glass._TOP_WINDOW_LEVEL,  # discipline-ok(R5): same runtime level policy
                      "requested": True, "desync_tolerance_pt": sf_glass._Z_DESYNC_TOLERANCE_PT,  # discipline-ok(R5): existing watchdog tolerance
                      "real_state": {"glass_on_top": False, "desync_px": 0.0}},
            "space": {"on_active_space": True},
            "update": {"manifest": {"status": "verified"},
                       "flow": {"checking": False, "downloading": False, "transition_pending": False}}}


class StartupEvaluationTests(SFCase):
    def test_invalid_alignment_evidence_remains_unverified(self):
        for value in (float("nan"), float("inf"), -1.0):
            with self.subTest(value=value):
                data = healthy_data()
                data["glass"]["real_state"]["desync_px"] = value
                self.assertEqual(health.evaluate(data)["checks"]["glass"]["status"], "unverified")

    def contract(self, evaluate):
        self.assertEqual(evaluate(healthy_data())["status"], "passed")
        for section, key, value in (("identity", "bundle_id", "wrong"),
                                    ("visibility", "native_visible", False),
                                    ("visibility", "intersects_screen", False),
                                    ("space", "on_active_space", False),
                                    ("glass", "present", False)):
            data = healthy_data()
            data[section][key] = value
            self.assertEqual(evaluate(data)["checks"][section]["status"], "failed")
        data = healthy_data()
        data["glass"]["level"] = 0
        self.assertEqual(evaluate(data)["checks"]["window_level"]["status"], "failed")
        for key, value in (("glass_on_top", True), ("desync_px", 5)):
            data = healthy_data()
            data["glass"]["real_state"][key] = value
            self.assertEqual(evaluate(data)["checks"]["glass"]["status"], "failed")
        self.assertEqual(evaluate({})["status"], "unverified")
        for section in ("glass", "space"):
            data = healthy_data()
            data[section] = {}
            self.assertEqual(evaluate(data)["checks"][section]["status"], "unverified")

    def test_positive_and_negative_contract(self):
        self.contract(health.evaluate)

    def test_source_identity_and_incomplete_native_answers_never_pass(self):
        data = healthy_data()
        data["identity"] = {"frozen": False}
        data["glass"]["real_state"]["desync_px"] = None
        data["space"]["on_active_space"] = None
        result = health.evaluate(data)
        for name in ("identity", "glass", "space"):
            self.assertEqual(result["checks"][name]["status"], "unverified")

    def test_minimizing_is_not_a_window_failure(self):
        data = healthy_data()
        data["visibility"].update(qt_visible=False, intentionally_hidden=True)
        result = health.evaluate(data)
        for name in ("visibility", "glass", "space"):
            self.assertEqual(result["checks"][name]["status"], "not_applicable")

    def test_user_selected_translucent_does_not_masquerade_as_broken_glass(self):
        data = healthy_data()
        data["glass"].update(requested=False, present=False, real_state=None)
        result = health.evaluate(data)
        self.assertEqual(result["checks"]["glass"]["status"], "not_applicable")
        self.assertEqual(result["checks"]["window_level"]["status"], "passed")

    def test_ota_failure_and_not_checked_are_distinct(self):
        for status, expected in (("not_checked", "unverified"), ("checking", "unverified"),
                                 ("failed", "failed"), ("verified", "passed")):
            data = healthy_data()
            data["update"]["manifest"]["status"] = status
            self.assertEqual(health.evaluate(data)["checks"]["ota_manifest"]["status"], expected)
        data["update"]["last_outcome"] = "error"
        self.assertEqual(health.evaluate(data)["checks"]["update_state"]["status"], "failed")

    def test_handoff_readiness_does_not_wait_for_the_later_update_check(self):
        """启动采样早于15秒更新首检，不能由 ota_manifest 卡住 OTA 回执。"""
        data = healthy_data()
        data["update"]["manifest"]["status"] = "not_checked"
        sample = health.evaluate(data)
        self.assertEqual(sample["status"], "unverified")
        self.assertTrue(health.ready_for_update_handoff(sample))
        data["identity"]["version"] = "wrong"
        self.assertFalse(health.ready_for_update_handoff(health.evaluate(data)))

    def test_handoff_readiness_requires_a_visible_window_but_allows_disabled_glass(self):
        """最小化/隐藏不是可用的新版本，只有用户明确关掉玻璃可不适用。"""
        hidden = healthy_data()
        hidden["visibility"] = {"intentionally_hidden": True}
        hidden_sample = health.evaluate(hidden)
        self.assertEqual(hidden_sample["checks"]["visibility"]["status"], "not_applicable")
        self.assertFalse(health.ready_for_update_handoff(hidden_sample))
        no_glass = healthy_data()
        no_glass["glass"].update(requested=False, present=False, real_state=None)
        no_glass_sample = health.evaluate(no_glass)
        self.assertEqual(no_glass_sample["checks"]["glass"]["status"], "not_applicable")
        self.assertTrue(health.ready_for_update_handoff(no_glass_sample))

    def test_handoff_readiness_refuses_samples_it_cannot_read(self):
        """读不出来的采样一律不回执：不是字典、没有分项、分项缺失或状态未知。"""
        for sample in (None, "passed", [], {}, {"status": "unverified"},
                       {"status": "unverified", "checks": "x"}):
            with self.subTest(sample=sample):
                self.assertFalse(health.ready_for_update_handoff(sample))
        sample = health.evaluate(healthy_data())
        del sample["checks"]["space"]
        sample["status"] = "unverified"
        self.assertFalse(health.ready_for_update_handoff(sample))
        sample = health.evaluate(healthy_data())
        sample["status"] = "unverified"
        sample["checks"]["glass"]["status"] = "unverified"
        self.assertFalse(health.ready_for_update_handoff(sample))
        sample = health.evaluate(healthy_data())
        sample["checks"]["identity"]["status"] = "failed"
        self.assertFalse(health.ready_for_update_handoff(sample))
        # 玻璃项整个缺失，和"玻璃状态未知"是两种读不出来的形状
        sample = health.evaluate(healthy_data())
        del sample["checks"]["glass"]
        self.assertFalse(health.ready_for_update_handoff(sample))
        # 玻璃"不适用"必须是用户主动关闭才算数，别的原因不能借这个状态放行
        sample = health.evaluate(healthy_data())
        sample["checks"]["glass"] = {"status": "not_applicable", "data": None,
                                     "reason": "user_hidden_or_quitting"}
        self.assertFalse(health.ready_for_update_handoff(sample))
        for name in ("visibility", "space", "window_level"):
            with self.subTest(core_signal=name):
                sample = health.evaluate(healthy_data())
                sample["checks"][name]["status"] = "not_applicable"
                self.assertFalse(health.ready_for_update_handoff(sample))

    def test_six_in_memory_source_mutants_are_killed_by_the_contract(self):
        # Compile and execute changed logic, not a source-string shape assertion.
        tree = ast.parse(Path(health.__file__).read_text())
        replacements = (("failed", "passed"), ("unverified", "passed"),
                        ("bundle_id", "version"), ("on_active_space", "absent_space"),
                        ("glass_on_top", "absent_order"), ("intersects_screen", "absent_screen"))
        for original, replacement in replacements:
            with self.subTest(mutant=original):
                class Mutate(ast.NodeTransformer):
                    def visit_Constant(self, node):
                        if node.value == original:
                            node.value = replacement
                        return node
                mutant = Mutate().visit(copy.deepcopy(tree))
                namespace = {"__name__": "startup_health_mutant"}
                exec(compile(mutant, health.__file__, "exec"), namespace)
                with self.assertRaises(AssertionError):
                    self.contract(namespace["evaluate"])


class StartupSamplingTests(SFCase):
    def setUp(self):
        super().setUp()
        self.app = self.ensure_qapp()
        self.window = self.track_widget(QWidget())
        self.window.resize(180, 160)
        self.window.show()
        self.window.move(self.app.primaryScreen().geometry().topLeft())
        # The legacy private state port is the only input used by the production sampler.
        self.window._update_state = UpdateFlowState()  # discipline-ok(R5): host port fixture

    def test_offscreen_never_resolves_native_pointer_or_starts_transport(self):
        with mock.patch.object(sf_glass, "native_cocoa_active", return_value=False), \
             mock.patch.object(sf_glass, "content_ns_window", side_effect=AssertionError) as native, \
             mock.patch.object(sf_update, "fetch_manifest", side_effect=AssertionError) as fetch:
            result = health.capture(self.window)
            health.diagnostics(self.window)
        native.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(result["checks"]["space"]["status"], "unverified")
        self.assertIn("native", result["errors"])
        self.assertTrue(self.window.isVisible())

    def test_native_read_port_is_sampled_without_mutating_window(self):
        nsw = SimpleNamespace(isVisible=lambda: True, isOnActiveSpace=lambda: True,
                              collectionBehavior=lambda: 257, level=lambda: sf_glass._TOP_WINDOW_LEVEL)  # discipline-ok(R5): native read-port fixture uses the runtime policy
        surface = {"glass_present": True, "surface": {"backend": "legacy"},
                   "real_state": {"glass_on_top": False, "desync_px": 0}}
        self.window.glass_diagnostics = lambda: surface
        self.window._bg_cfg = "glass"  # discipline-ok(R5): user's existing material preference port
        with mock.patch.object(sf_glass, "native_cocoa_active", return_value=True), \
             mock.patch.object(sf_glass, "content_ns_window", return_value=nsw), \
             mock.patch.object(health, "identity_snapshot", return_value=healthy_data()["identity"]):
            result = health.capture(self.window)
        for name in ("identity", "visibility", "space", "glass"):
            self.assertEqual(result["checks"][name]["status"], "passed")
        self.assertTrue(self.window.isVisible())

    def test_native_failure_is_bounded_and_does_not_hide_other_sections(self):
        with mock.patch.object(sf_glass, "native_cocoa_active", return_value=True), \
             mock.patch.object(sf_glass, "content_ns_window", return_value=None):
            result = health.capture(self.window)
        self.assertIn("space", result["errors"])
        self.assertIn("identity", result["checks"])
        self.assertEqual(result["checks"]["space"]["status"], "unverified")
        self.assertTrue(self.window.isVisible())

    def test_timer_is_single_owned_idempotent_and_source_process_skips(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            health.schedule(self.window)
        self.assertEqual(self.window.findChildren(QTimer), [])
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sf_glass, "native_cocoa_active", return_value=True), \
             mock.patch.object(health, "STARTUP_DELAY_MS", 0), \
             mock.patch.object(health, "capture", return_value={"status": "sampled"}) as capture:
            health.schedule(self.window)
            health.schedule(self.window)
            timers = self.window.findChildren(QTimer)
            self.assertEqual(len(timers), 1)
            self.assertTrue(timers[0].isSingleShot())
            self.assertTrue(wait_for(lambda: self.pump_and_sampled()))
            QApplication.processEvents()
            capture.assert_called_once_with(self.window)
            self.assertFalse(timers[0].isActive())

    def pump_and_sampled(self):
        QApplication.processEvents()
        return health.diagnostics(self.window)["startup"]["status"] == "sampled"

    def test_failure_in_timer_still_yields_an_explicit_unknown(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sf_glass, "native_cocoa_active", return_value=True), \
             mock.patch.object(health, "STARTUP_DELAY_MS", 0), \
             mock.patch.object(health, "capture", side_effect=RuntimeError("fixture failure")), \
             mock.patch("sf_logging.log_swallow") as log:
            health.schedule(self.window)
            QApplication.processEvents()
            self.assertEqual(health.diagnostics(self.window)["startup"]["reason"], "RuntimeError")
            log.assert_called_once()

    def test_quit_before_timer_never_queries_a_tearing_down_native_window(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sf_glass, "native_cocoa_active", return_value=True), \
             mock.patch.object(health, "STARTUP_DELAY_MS", 0), \
             mock.patch.object(health, "capture", side_effect=AssertionError) as capture:
            health.schedule(self.window)
            self.window._quitting = True  # discipline-ok(R5): lifecycle host port
            QApplication.processEvents()
            self.assertEqual(health.diagnostics(self.window)["startup"]["reason"], "quit_before_sample")
            capture.assert_not_called()

    def test_parent_destruction_cancels_the_pending_callback(self):
        from PyQt6 import sip
        window = QWidget()
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sf_glass, "native_cocoa_active", return_value=True), \
             mock.patch.object(health, "STARTUP_DELAY_MS", 0), \
             mock.patch.object(health, "capture", side_effect=AssertionError) as capture:
            health.schedule(window)
            sip.delete(window)
            QApplication.processEvents()
            capture.assert_not_called()

    def test_identity_reads_bundle_metadata_but_never_claims_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            contents = Path(directory) / "Fixture.app" / "Contents"
            contents.mkdir(parents=True)
            (contents / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": BUNDLE_ID, "CFBundleShortVersionString": APP_VERSION,
                "CFBundleExecutable": APP_DISPLAY_NAME}))
            with mock.patch.object(sys, "frozen", True, create=True), \
                 mock.patch.object(sys, "executable", str(contents / "MacOS" / APP_DISPLAY_NAME)):
                result = health.identity_snapshot()
                self.assertEqual(result["bundle_id"], BUNDLE_ID)
                self.assertIn("unverified", result["signature_status"])
                (contents / "Info.plist").write_bytes(b"x" * (64 * 1024 + 1))
                with self.assertRaises(ValueError):
                    health.identity_snapshot()


class ManifestSamplingTests(SFCase):
    def setUp(self):
        super().setUp()
        # Preserve the process-wide readback so fixtures cannot contaminate diagnostics tests.
        self.saved = sf_update.manifest_diagnostics()
        self.addCleanup(self.restore)

    def restore(self):
        with sf_update._MANIFEST_LOCK:  # discipline-ok(R5): restore the only module-owned snapshot
            sf_update._MANIFEST_STATUS.clear()  # discipline-ok(R5): same fixture boundary
            sf_update._MANIFEST_STATUS.update(self.saved)  # discipline-ok(R5): same fixture boundary

    def test_verified_latest_retains_remote_version_without_an_extra_request(self):
        info = sf_update.UpdateInfo(APP_VERSION, "notes", "unused", 10, "a" * 64)
        with mock.patch.object(sf_update, "fetch_manifest", return_value=b"signed fixture") as fetch, \
             mock.patch.object(sf_update, "verify_manifest", return_value=info):
            update = sf_update.check_for_update(APP_VERSION)
            self.assertIsNone(update)
            result = sf_update.manifest_diagnostics()
        fetch.assert_called_once()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["version"], info.version)
        self.assertFalse(result["newer"])
        result["status"] = "tampered"
        self.assertEqual(sf_update.manifest_diagnostics()["status"], "verified")

    def test_transport_and_signature_failure_are_not_reported_as_latest(self):
        for point in ("fetch_manifest", "verify_manifest"):
            with self.subTest(point=point), \
                 mock.patch.object(sf_update, "fetch_manifest", return_value=b"bad fixture"), \
                 mock.patch.object(sf_update, point, side_effect=sf_update.UpdateError("secret URL")):
                with self.assertRaises(sf_update.UpdateError):
                    sf_update.check_for_update(attempts=1)
                result = sf_update.manifest_diagnostics()
                self.assertEqual(result["status"], "failed")
                self.assertNotIn("secret URL", json.dumps(result))
                self.assertNotIn("version", result)


class StartupArchiveTests(SFCase):
    def test_real_diagnostic_zip_contains_startup_and_current_update_without_transport(self):
        case = diagnostic_fixtures.DiagnosticsBranchCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        # Seed a retained startup sample, then export through the real archive path.
        case.win._startup_health = health.evaluate(healthy_data())  # discipline-ok(R5): retained sampler output
        with mock.patch.object(sf_update, "fetch_manifest", side_effect=AssertionError) as fetch, \
             mock.patch("subprocess.Popen"):
            body = json.loads(case._export()["startup_health.json"])  # discipline-ok(R5): isolated real export helper
        self.assertEqual(body["startup"]["status"], "passed")
        self.assertIn("manifest", body["current_update"])
        fetch.assert_not_called()

    def test_diagnostic_provider_failure_is_visible_in_export_issues(self):
        case = diagnostic_fixtures.DiagnosticsBranchCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        with mock.patch.object(health, "diagnostics", side_effect=RuntimeError("fixture")), \
             mock.patch("subprocess.Popen"):
            files = case._export()  # discipline-ok(R5): isolated real export helper
        self.assertIn("startup_health", files["export.json"])
