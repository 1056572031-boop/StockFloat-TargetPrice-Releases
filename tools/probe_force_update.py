#!/usr/bin/env python3
"""Offline forced-update probe with an optional isolated AppKit alert check."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _Cache:
    def __init__(self, archive=None):
        self.archive = archive

    def validate_cached_archive(self, _update):
        return self.archive

    def accept_manifest_identity(self, _update):
        return True

    def write_lock(self):
        from contextlib import nullcontext
        return nullcontext()

    def diagnostics(self):
        return {"schema": 1, "identity": None,
                "cache_file": {"name": "update-cache.json", "present": False}}


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
        self.downloads.append((update.version, options))

    def cancel_download(self, update=None, *, token=None):
        return update

    def prompt_update(self, update, *, kind, generation=None, identity=None):
        self.prompts.append((update.version, kind, generation, identity))

    def show_update_message(self, _title, _body):
        pass

    def install_update(self, update, archive):
        self.installs.append((update.version, str(archive)))

    def request_check(self, _manual):
        pass


def _update(gap):
    from sf_update import UpdateInfo
    return UpdateInfo(
        version="2.%d.0" % gap,
        notes="1. 用户可感知的更新",
        archive_url="https://github.com/example/repo/releases/download/v/pkg.zip",
        archive_size=128,
        archive_sha256=("%02x" % gap) * 32,
        stage="stable",
        release_sequence=10 + gap,
        published_at="2026-11-19T00:00:00Z",
    )


def _native_alert_result():
    child = ROOT / "tests" / "_alert_child.py"
    if not child.is_file():
        return {"status": "skipped", "reason": "alert child unavailable"}
    env = {key: value for key, value in os.environ.items() if not key.startswith("SF_")}
    env["PYTHONPATH"] = str(ROOT)
    try:
        result = subprocess.run(
            [sys.executable, str(child), "force"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "skipped", "reason": type(exc).__name__}
    if result.returncode == 4:
        return {"status": "skipped", "reason": "native AppKit unavailable"}
    return {"status": "passed" if result.returncode == 0 else "failed",
            "returncode": result.returncode}


def run(*, native=False):
    from sf_update_flow import UpdateFlowController
    from sf_update_policy import PolicyState

    with tempfile.TemporaryDirectory(prefix="sf_force_update_probe_") as root:
        temp = Path(root)
        key = secrets.token_hex(16)
        manifest = temp / "manifest.json"
        cache_state = temp / "update-cache.json"
        manifest.write_text(json.dumps({"token": key, "gaps": [0, 1, 2]}), encoding="utf-8")
        cache_state.write_text("{}", encoding="utf-8")

        gap_checks = {}
        for gap in (0, 1, 2, 4):
            port = _Port()
            flow = UpdateFlowController(port=port, cache=_Cache())
            update = _update(gap)
            decision = flow.on_manifest_verified(update, automatic=True)
            gap_checks[gap] = {"state": decision.state.value,
                               "downloads": len(port.downloads)}

        same_port = _Port()
        same_update = _update(0)
        from dataclasses import replace
        same_update = replace(same_update, version=same_port.get_local_version())
        same_decision = UpdateFlowController(port=same_port, cache=_Cache()).on_manifest_verified(
            same_update, automatic=True)
        same_identity_check = {"state": same_decision.state.value,
                               "downloads": len(same_port.downloads),
                               "prompts": len(same_port.prompts)}

        forced_port = _Port()
        forced_cache = _Cache()
        forced_flow = UpdateFlowController(port=forced_port, cache=forced_cache)
        forced_update = _update(8)
        pending = forced_flow.on_manifest_verified(forced_update, automatic=False)
        forced_cache.archive = temp / "package.zip"
        forced_flow.on_download_ready(forced_update, temp / "package.zip")
        ready = forced_flow.diagnostics()
        install_requested = forced_flow.install_ready()

        evidence = {
            "probe": "force_update",
            "temporary_only": None,
            "network_disabled": None,
            "install_location": "none",
            "gaps": [0, 1, 2],
            "gap_states": {gap: gap_checks[gap]["state"] for gap in (0, 1, 2)},
            "gap_checks": gap_checks,
            "same_identity_check": same_identity_check,
            "scenarios": {
                "prefetch": gap_checks[4]["state"] == "prepare" and gap_checks[4]["downloads"] == 1,
                "force_pending": pending.state == PolicyState.FORCE_PENDING,
                "force_ready": ready["state"] == PolicyState.FORCE_READY.value,
                "exit_without_install": None,
                "install_requested": bool(install_requested and forced_port.installs),
                "install_success": None,
                "install_failure_rollback": None,
                "rollback": None,
                "offline_start": None,
            },
            "native_alert": _native_alert_result() if native else {"status": "not_run"},
        }
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--native", action="store_true")
    args = parser.parse_args(argv)
    if not args.self_test and not args.native:
        parser.error("use --self-test or --native")
    evidence = run(native=args.native)
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    expected_gaps = {
        0: {"state": "replace_offer", "downloads": 0},
        1: {"state": "offer", "downloads": 0},
        2: {"state": "offer", "downloads": 0},
        4: {"state": "prepare", "downloads": 1},
    }
    if (evidence.get("gap_checks") != expected_gaps or
            evidence.get("same_identity_check") !=
            {"state": "up_to_date", "downloads": 0, "prompts": 0}):
        return 1
    scenarios = evidence.get("scenarios", {})
    if not all(scenarios.get(name) is True for name in
               ("prefetch", "force_pending", "force_ready", "install_requested")):
        return 1
    if args.native and evidence.get("native_alert", {}).get("status") != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
