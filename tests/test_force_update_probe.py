# -*- coding: utf-8 -*-
"""Task 7: forced-update diagnostics and isolated probe contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sf_update import UpdateInfo
from sf_update_cache import UpdateCache
from sf_update_controller import UpdateController
from sf_update_flow import UpdateFlowController, update_flow_diagnostics
from sf_update_policy import PolicyState
from tests._base import SFCase

ROOT = Path(__file__).resolve().parents[1]

import importlib.util

_PROBE_SPEC = importlib.util.spec_from_file_location("probe_force_update", ROOT / "tools" / "probe_force_update.py")
_PROBE = importlib.util.module_from_spec(_PROBE_SPEC)
assert _PROBE_SPEC.loader is not None
_PROBE_SPEC.loader.exec_module(_PROBE)


class _Cache:
    def __init__(self):
        self.ready = None

    def validate_cached_archive(self, update):
        return self.ready

    def accept_manifest_identity(self, update):
        return True

    def write_lock(self):
        from contextlib import nullcontext
        return nullcontext()

    def diagnostics(self):
        return {
            "schema": 1,
            "identity": None,
            "startup_verification": None,
            "cache_file": {"name": "update-cache.json", "present": True},
        }


class _Port:
    def __init__(self):
        self.downloads = []
        self.prompts = []
        self.installs = []

    def get_local_version(self):
        return "1.0.0"

    def get_local_sequence(self):
        return 10

    def start_download(self, update, **options):
        self.downloads.append((update, options))

    def cancel_download(self, update=None, *, token=None):
        pass

    def prompt_update(self, update, *, kind, generation=None, identity=None):
        self.prompts.append(kind)

    def show_update_message(self, title, body):
        pass

    def install_update(self, update, archive):
        self.installs.append((update, archive))

    def request_check(self, manual):
        pass


class ForcedUpdateDiagnosticsContractTest(SFCase):
    def _update(self, gap=8):
        return UpdateInfo(
            version="2.%d.0" % gap,
            notes="1. 用户可感知的更新",
            archive_url="https://github.com/example/repo/releases/download/v2/pkg.zip?token=secret",
            archive_size=4096,
            archive_sha256="a" * 64,
            stage="stable",
            release_sequence=10 + gap,
            published_at="2026-11-19T00:00:00Z",
        )

    def test_flow_snapshot_exposes_sanitized_forced_update_evidence(self):
        port = _Port()
        flow = UpdateFlowController(port=port, cache=_Cache())
        update = self._update()
        flow.on_manifest_verified(update, automatic=True)
        snapshot = flow.diagnostics()
        encoded = json.dumps(snapshot, ensure_ascii=False)

        self.assertEqual(snapshot["state"], PolicyState.FORCE_PENDING.value)
        self.assertEqual(snapshot["update"]["version"], update.version)
        self.assertEqual(snapshot["update"]["release_sequence"], update.release_sequence)
        self.assertEqual(snapshot["package"]["size"], update.archive_size)
        self.assertEqual(snapshot["package"]["sha256"], update.archive_sha256)
        self.assertEqual(snapshot["manifest"]["host"], "github.com")
        self.assertNotIn("token=secret", encoded)
        self.assertNotIn("/Users/", encoded)
        self.assertIn("policy", snapshot)
        self.assertIn("errors", snapshot)
        self.assertIn("install", snapshot)

    def test_error_evidence_redacts_complete_url_paths_and_credentials(self):
        port = _Port()
        flow = UpdateFlowController(port=port, cache=_Cache())
        update = self._update()
        flow.on_manifest_verified(update, automatic=False)
        flow._record_error(  # discipline-ok(R5): exercising public diagnostic error capture seam
            "download",
            RuntimeError("GET https://github.com/private/repo.zip?token=secret /Users/me/file.zip"),
        )
        encoded = json.dumps(flow.diagnostics(), ensure_ascii=False)
        self.assertNotIn("private/repo.zip", encoded)
        self.assertNotIn("token=secret", encoded)
        self.assertNotIn("/Users/me", encoded)

    def test_diagnostic_update_entry_reads_public_flow_and_cache_ports(self):
        flow = SimpleNamespace(diagnostics=lambda: {"state": "force_pending"})
        cache = SimpleNamespace(diagnostics=lambda: {"schema": 1, "identity": None})
        controller = SimpleNamespace(diagnostics=lambda: {"stopped": False, "workers": 0})
        host = SimpleNamespace(
            _update_port=SimpleNamespace(_flow=flow, _cache=cache),
            _update_controller=controller,
            _update_downloading=False,
            _update_transition_pending=False,
            _update_manual_wait=False,
            _update_last_check_at=0.0,
            _update_stale_resets=0,
            _update_last_result=None,
            _update_last_download=None,
        )
        fields = []

        def field(key, getter, default):
            fields.append(key)
            return getter()

        value = update_flow_diagnostics(host, field, lambda: None)
        self.assertEqual(value["flow"], {"state": "force_pending"})
        self.assertEqual(value["cache"], {"schema": 1, "identity": None})
        self.assertEqual(value["controller"], {"stopped": False, "workers": 0})
        self.assertIn("update.flow", fields)

    def test_flow_diagnostics_is_read_only_and_returns_detached_data(self):
        flow = UpdateFlowController(port=_Port(), cache=_Cache())
        flow.on_manifest_verified(self._update(), automatic=False)
        before_threads = {thread.ident for thread in threading.enumerate()}
        before_files = set(Path(tempfile.gettempdir()).glob("sf_task7_*"))
        with patch("urllib.request.urlopen") as network:
            first = flow.diagnostics()
            second = flow.diagnostics()
        self.assertFalse(network.called)
        self.assertEqual(before_threads, {thread.ident for thread in threading.enumerate()})
        self.assertEqual(before_files, set(Path(tempfile.gettempdir()).glob("sf_task7_*")))
        self.assertEqual(first, second)
        first["update"]["version"] = "mutated"
        self.assertEqual(first["update"]["version"], "mutated")
        self.assertEqual(flow.diagnostics()["update"]["version"], "2.8.0")  # discipline-ok(R1): fixed probe fixture


    def test_probe_run_without_native_uses_temporary_only_contract(self):
        evidence = _PROBE.run(native=False)
        self.assertIsNone(evidence["temporary_only"])
        self.assertIsNone(evidence["network_disabled"])
        self.assertEqual(evidence["native_alert"], {"status": "not_run"})
        self.assertIsNone(evidence["scenarios"]["rollback"])
        self.assertIsNone(evidence["scenarios"]["install_failure_rollback"])

    def test_cache_diagnostics_reports_no_local_archive(self):
        cache = _PROBE._Cache()  # discipline-ok(R5): isolated probe cache boundary
        self.assertEqual(cache.diagnostics(), {"schema": 1, "identity": None,
                                               "cache_file": {"name": "update-cache.json", "present": False}})

    def test_probe_main_distinguishes_valid_core_scenarios_and_native_failure(self):
        import copy
        baseline = _PROBE.run(native=False)
        with patch.object(_PROBE, "run", return_value=baseline):
            self.assertEqual(_PROBE.main(["--self-test"]), 0)
        broken = copy.deepcopy(baseline)
        broken["scenarios"]["force_ready"] = False
        with patch.object(_PROBE, "run", return_value=broken):
            self.assertEqual(_PROBE.main(["--self-test"]), 1)
        native = copy.deepcopy(baseline)
        native["native_alert"] = {"status": "failed"}
        with patch.object(_PROBE, "run", return_value=native):
            self.assertEqual(_PROBE.main(["--native"]), 1)
        native["native_alert"] = {"status": "passed"}
        with patch.object(_PROBE, "run", return_value=native):
            self.assertEqual(_PROBE.main(["--native"]), 0)

    def test_probe_main_rejects_failed_observations(self):
        bad = {"gap_checks": {}, "same_identity_check": {}, "scenarios": {},
               "native_alert": {"status": "failed"}}
        with patch.object(_PROBE, "run", return_value=bad):
            self.assertNotEqual(_PROBE.main(["--self-test"]), 0)

    def test_native_alert_maps_unavailable_child_and_failed_child(self):
        with patch.object(_PROBE.subprocess, "run", side_effect=OSError("missing")):
            self.assertEqual(_PROBE._native_alert_result()["status"], "skipped")  # discipline-ok(R5): exercise isolated native probe boundary
        with patch.object(_PROBE.subprocess, "run", return_value=SimpleNamespace(returncode=4)):
            self.assertEqual(_PROBE._native_alert_result()["reason"], "native AppKit unavailable")  # discipline-ok(R5): exercise isolated native probe boundary
        with patch.object(_PROBE.subprocess, "run", return_value=SimpleNamespace(returncode=9)):
            result = _PROBE._native_alert_result()  # discipline-ok(R5): exercise isolated native probe boundary
        self.assertEqual(result, {"status": "failed", "returncode": 9})

    def test_probe_ports_are_complete_and_native_probe_skips_missing_child(self):
        port = _PROBE._Port()  # discipline-ok(R5): exercise isolated probe port boundary
        self.assertEqual(port.cancel_download("update"), "update")  # discipline-ok(R5): exercise isolated probe port boundary
        self.assertIsNone(port.show_update_message("title", "body"))  # discipline-ok(R5): exercise isolated probe port boundary
        self.assertIsNone(port.request_check(True))  # discipline-ok(R5): exercise isolated probe port boundary
        with patch.object(_PROBE, "ROOT", Path(tempfile.mkdtemp(prefix="sf-probe-root-"))):
            result = _PROBE._native_alert_result()  # discipline-ok(R5): isolated native child boundary
        self.assertEqual(result["status"], "skipped")

    def test_probe_main_rejects_missing_mode_and_dispatches_native(self):
        with self.assertRaises(SystemExit):
            _PROBE.main([])
        with patch.object(_PROBE, "run", return_value={"probe": "force_update"}) as run:
            self.assertNotEqual(_PROBE.main(["--native"]), 0)
        run.assert_called_once_with(native=True)


class UpdateControllerDiagnosticsContractTest(SFCase):
    def test_diagnostics_reports_read_only_lifecycle_counters(self):
        controller = UpdateController(lambda _message: None, lambda: "1.0.0")
        try:
            snapshot = controller.diagnostics()
        finally:
            controller.stop()
        self.assertEqual(snapshot["workers"], 0)
        self.assertIn("checks_started", snapshot)
        self.assertIn("downloads_started", snapshot)
        self.assertIn("downloads_completed", snapshot)
        self.assertIn("downloads_failed", snapshot)
        self.assertIn("pending_download", snapshot)


class ArchitectureAndProbeContractTest(SFCase):
    def test_architecture_contract_registers_update_policy_and_cache_state(self):
        contract = json.loads((ROOT / "docs/agents/architecture_contract.json").read_text())
        names = {entry["name"]: entry for entry in contract["state_owners"]}
        for name, owner, port in (
            ("update_policy_state", "sf_update_flow.py:_ForcedUpdateFlowController", "sf_update_flow.py:diagnostics"),
            ("update_cache_state", "sf_update_cache.py:UpdateCache", "sf_update_cache.py:diagnostics"),
        ):
            self.assertIn(name, names)
            self.assertEqual(names[name]["owner"], owner)
            self.assertEqual(names[name]["diagnostic_port"], port)
            self.assertTrue(names[name]["shutdown"])

    def test_probe_script_entry_returns_success_without_native_ui(self):
        import runpy
        with patch.object(sys, "argv", [str(ROOT / "tools" / "probe_force_update.py"), "--self-test"]):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(ROOT / "tools" / "probe_force_update.py"), run_name="__main__")
        self.assertEqual(result.exception.code, 0)

    def test_probe_self_test_is_available_and_emits_bounded_json(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools/probe_force_update.py"), "--self-test"],
            cwd=ROOT,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence["probe"], "force_update")
        self.assertIsNone(evidence["temporary_only"])
        self.assertIsNone(evidence["network_disabled"])
        self.assertEqual(set(evidence["gaps"]), {0, 1, 2})
        self.assertIn("rollback", evidence["scenarios"])
        self.assertTrue(evidence["scenarios"]["prefetch"])
        self.assertIsNone(evidence["scenarios"]["exit_without_install"])
        self.assertNotIn("/Users/", result.stdout)


if __name__ == "__main__":
    unittest.main()
