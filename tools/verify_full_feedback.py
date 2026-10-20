#!/usr/bin/env python3
"""Synthetic full-package acceptance. Offline by default; at most one live POST."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sf_constants import APP_VERSION, BUNDLE_ID, FEEDBACK_ENDPOINT
from sf_diagnostics import DiagnosticArchive
from sf_feedback import build_archive, make_envelope, utc_now
from sf_feedback_admin import analyze_isolated
from sf_feedback_bundle import FULL_CONSENT_VERSION, REQUIRED_DIAGNOSTICS, read_diagnostics
from sf_feedback_capture import package_files, read_upload_archive, validate_upload_receipt
from sf_feedback_github import GitHubAPI
from sf_feedback_transport import submit_https


def generate(root, stress=False):
    """Only generated synthetic values; no user files, state collectors or logs."""
    diagnostic = root / "diagnostics.zip"
    archive = DiagnosticArchive(str(diagnostic))
    for name in sorted(REQUIRED_DIAGNOSTICS - {"export.json"}):
        archive.writestr(name, "{}")
    archive.writestr("diagnostics.txt", "SYNTHETIC FULL DIAGNOSTIC TEST ONLY. No real user data.")
    archive.writestr("runtime.json", '{"net_ok":true,"age":1.5,"synthetic":true}')
    for n in range(4):
        body = (base64.b64encode(os.urandom(3 * 5 * 1024 * 1024 // 4)) if stress
                else b"[synthetic] WARNING: controlled test only\n")
        archive.writestr("debug.log" + (f".{n}" if n else ""), body)
    archive.writestr("export.json", json.dumps({"version": APP_VERSION, "issues": [],
        "files": [{"name": item.filename, "size_bytes": item.file_size} for item in archive.infolist()]}))
    archive.finish()
    data = diagnostic.read_bytes()
    read_diagnostics(data)
    now = utc_now()
    state: dict[str, Any] = {"app_version": APP_VERSION, "bundle_id": BUNDLE_ID, "run_id": "abcdef",
        "capture_started_at": now, "capture_completed_at": now, "phase": "user_feedback",
        "health": None, "counts": {}, "summary": {"net_ok": True, "capture_issue_count": 0},
        "omitted": ["camera", "screenshots"], "truncated": []}
    files = package_files(state, "SYNTHETIC TEST ONLY. No real user data.")
    files["diagnostics.zip"] = data
    packet = root / "feedback.zip"
    build_archive(packet, files, app_version=APP_VERSION, bundle_id=BUNDLE_ID, run_id=state["run_id"],
                  omitted=state["omitted"], truncated=[], capture_started_at=now, capture_completed_at=now)
    envelope = make_envelope(archive_path=packet, app_version=APP_VERSION, outbox_id=os.urandom(16).hex(),
                             consent_policy_version=FULL_CONSENT_VERSION)
    (root / "envelope.json").write_text(json.dumps(envelope, indent=2))
    return packet, envelope


def readback(root, envelope, gh):
    read_upload_archive(root / "feedback.zip", envelope)
    try:
        token = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=15, check=True).stdout.strip()
    except subprocess.CalledProcessError:
        raise RuntimeError("Administrator authentication failed; no credential output displayed") from None
    api = GitHubAPI(token)
    release = api.inbox()
    assets = {entry["name"]: entry for entry in api.assets(release["id"])}
    case = "feedback-" + envelope["idempotency_key"]
    receipt = json.loads(api.download(assets[case + ".receipt.json"]["id"], limit=64 * 1024))
    validate_upload_receipt(receipt, envelope)
    remote = api.download(receipt["asset_id"], limit=envelope["size_bytes"])
    if remote != (root / "feedback.zip").read_bytes():
        raise RuntimeError("Remote archive does not match the sealed synthetic packet")
    (root / "remote-feedback.zip").write_bytes(remote)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--stress", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--readback-only", action="store_true")
    parser.add_argument("--gh", default=shutil.which("gh"))
    args = parser.parse_args()
    os.umask(0o077)
    if (args.live or args.readback_only) and (not args.gh or not Path(args.gh).is_absolute()):
        parser.error("An absolute administrator gh path is required")
    root = args.out.resolve()
    marker = root / "live-attempt.json"
    if args.readback_only:
        envelope = json.loads((root / "envelope.json").read_text())
        packet = root / "feedback.zip"
    else:
        if root.exists() and any(root.iterdir()):
            parser.error("Existing acceptance directory: use --readback-only; never resend automatically")
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        packet, envelope = generate(root, args.stress)
    evidence = {"synthetic_only": True, "feedback_post_budget": 1, "feedback_post_calls": 0,
                "live": args.live or args.readback_only, "archive_sha256": envelope["archive_sha256"],
                "size_bytes": envelope["size_bytes"]}
    if args.live or args.readback_only:
        if args.live and not args.readback_only:
            with marker.open("x") as stream:
                json.dump({"idempotency_key": envelope["idempotency_key"],
                    "reserved_feedback_posts": 1, "started_at": utc_now()}, stream)
            evidence["feedback_post_calls"] = 1
            try:
                submit_https(FEEDBACK_ENDPOINT, envelope, packet)
            except Exception as exc:
                evidence["submission_result"] = type(exc).__name__
                if isinstance(exc, HTTPError):
                    evidence["http_status"] = exc.code
                    try:
                        with exc:
                            body = exc.read(4097)
                        response = json.loads(body) if isinstance(body, bytes) and len(body) <= 4096 else {}
                    except (ValueError, OSError):
                        response = {}
                    if isinstance(response, dict) and isinstance(response.get("error"), str) and re.fullmatch(r"[a-z0-9_]{1,80}", response["error"]):
                        evidence["receiver_error"] = response["error"]
        # Even after a lost response, read back by the original identity, not a second POST.
        try:
            evidence["receipt"] = readback(root, envelope, args.gh)
        except Exception as exc:
            evidence["readback_result"] = type(exc).__name__
            (root / "verification.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
            print(json.dumps(evidence, ensure_ascii=False, indent=2))
            return 1
    report = analyze_isolated(packet, root / "reports")
    evidence["analysis_run_id"] = report["analysis_run_id"]
    evidence["report"] = report["markdown_path"]
    evidence["diagnostics_sha256"] = hashlib.sha256((root / "diagnostics.zip").read_bytes()).hexdigest()
    (root / "verification.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
