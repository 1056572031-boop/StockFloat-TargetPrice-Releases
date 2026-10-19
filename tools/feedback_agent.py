#!/usr/bin/env python3
"""Fetch private GitHub feedback and produce local, sandboxed analysis reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sf_feedback_admin import FeedbackAgent
from sf_feedback_github import GitHubAPI
from sf_feedback_notifications import FeedbackNotifications, report_versions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--gh", default=shutil.which("gh"))
    args = parser.parse_args()
    os.umask(0o077)
    if not args.gh or not Path(args.gh).is_absolute():
        parser.error("an absolute administrator gh executable is required")
    notifications = FeedbackNotifications(args.data_dir)
    phase = "authentication"
    try:
        token = subprocess.run([args.gh, "auth", "token"], capture_output=True, text=True,
                               check=True, timeout=15).stdout.strip()
        phase = "processing"
        agent = FeedbackAgent(GitHubAPI(token), args.data_dir)
        result = agent.run_once()
    except Exception:
        notice = notifications.sync({}, "backend:" + phase)
        print(json.dumps({"error": "feedback_backend_unavailable", "phase": phase,
                          "notifications": notice}, ensure_ascii=False))
        return 1
    failure = ("cases:" + hashlib.sha256(json.dumps(sorted(result["failed"])).encode()).hexdigest()
               if result["failed"] else "")
    try:
        versions = report_versions(args.data_dir)
    except Exception:
        result["notifications"] = notifications.sync({}, "backend:notification_index")
        result["notifications"]["failed"].append("report_index_unavailable")
    else:
        result["notifications"] = notifications.sync(versions, failure)
    print(json.dumps(result, ensure_ascii=False))
    return int(bool(result["failed"] or result["notifications"]["failed"]))


if __name__ == "__main__":
    raise SystemExit(main())
