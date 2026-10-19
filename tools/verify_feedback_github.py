#!/usr/bin/env python3
"""Explicit live synthetic GitHub upload/readback/download/isolated-analysis receipt."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sf_feedback import build_archive, make_envelope, utc_now
from sf_feedback_admin import FeedbackAgent
from sf_feedback_capture import CONSENT_VERSION, package_files
from sf_feedback_github import GitHubAPI, GitHubInbox, GitHubRequestError, INBOX_TAG, REPOSITORY


def initialize(api):
    api.assert_private()
    api.request("", method="PATCH", data={"description":
        "Private opt-in scrubbed diagnostic archives and user descriptions as Release assets. No payloads in Git history. No Actions."})
    try:
        commits = api.request("commits?per_page=1")
        empty = not commits
    except GitHubRequestError as exc:
        if exc.status != 409:
            raise
        empty = True
    if empty:
        readme = ("# Skyrocket Private Feedback\n\n"
                  "Owner authorized private storage of scrubbed diagnostic archives and explicit user descriptions on 2026-10-19.\n"
                  "Payloads are private Release assets, never Git commits. No Actions workflows.\n"
                  "Client applications must not contain administrator GitHub credentials.\n"
                  "Retention: manual deletion by maintainer; no automatic expiry or immediate backup-erasure promise.\n")
        api.request("contents/README.md", method="PUT", data={
            "message": "Initialize private feedback storage policy",
            "content": base64.b64encode(readme.encode()).decode(), "branch": "main"})
    releases = api.request("releases?per_page=100", limit=2 * 1024 * 1024)
    if not any(row["tag_name"] == INBOX_TAG for row in releases):
        api.request("releases", method="POST", data={
            "tag_name": INBOX_TAG, "name": "Private diagnostic feedback inbox",
            "body": "Private receiver storage. Assets contain explicit opt-in feedback. Not an application release.",
            "draft": True, "prerelease": False})


def verify(api, output):
    root = Path(output)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    now = utc_now()
    state: dict[str, Any] = {"app_version": "1.4.96", "bundle_id": "com.skyrocket.app", "run_id": "abcdef",
             "phase": "user_feedback", "capture_started_at": now, "capture_completed_at": now,
             "health": None, "counts": {"synthetic_error_count": 1},
             "summary": {"capture_issue_count": 0, "loaded_modules": 1},
             "omitted": ["all_real_user_data"], "truncated": []}
    archive = root / "synthetic-feedback.zip"
    build_archive(archive, package_files(state, "SYNTHETIC TEST ONLY. No real user diagnostic data."),
                  app_version=state["app_version"], bundle_id=state["bundle_id"], run_id=state["run_id"],
                  omitted=state["omitted"], truncated=state["truncated"],
                  capture_started_at=now, capture_completed_at=now)
    envelope = make_envelope(archive_path=archive, app_version=state["app_version"],
                             outbox_id=uuid.uuid4().hex, consent_policy_version=CONSENT_VERSION)
    inbox = GitHubInbox(api)
    receipt = inbox.accept(envelope, archive.read_bytes())
    repeated = inbox.accept(envelope, archive.read_bytes())
    if receipt != repeated:
        raise RuntimeError("same-key retry did not return the same receipt")
    agent = FeedbackAgent(api, root / "agent")
    result = agent.run_once()
    matching = [r for r in result["ready"] if r["case_id"] == receipt["case_id"]]
    if result["failed"] or len(matching) != 1:
        raise RuntimeError("live download/isolated analysis did not produce the expected report")
    evidence = {"synthetic_only": True, "repository": REPOSITORY, "receipt": receipt,
                "idempotency_readback": True, "analysis": matching[0], "verified_at": utc_now(),
                "public_https_client_verified": False, "frozen_app_verified": False,
                "real_user_data_uploaded": False}
    (root / "verification.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--gh", required=True)
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    token = subprocess.run([args.gh, "auth", "token"], capture_output=True, text=True,
                           check=True, timeout=15).stdout.strip()
    api = GitHubAPI(token)
    if args.initialize:
        initialize(api)
    print(json.dumps(verify(api, args.out), ensure_ascii=False))


if __name__ == "__main__":
    main()
