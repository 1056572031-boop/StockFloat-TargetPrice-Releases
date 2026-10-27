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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sf_feedback_admin import FeedbackAgent
from sf_feedback_github import GitHubAPI
from sf_feedback_notifications import MAX_FAILURE_IDENTITY, FeedbackNotifications, report_versions
from feedback_desktop_reports import export_reports


def failure_identity(failed, export_failures, near_limit):
    """本轮失败的指纹，通知器靠它判断“失败变了没有”。

    三类失败各带一段 64 位哈希时，直接拼接会超过通知器的长度上限（实测 142 字符），
    整次同步因此报 state_unavailable，所有通知都发不出去。没超上限时保持原样（升级后不会
    对已经通知过的状态重复提醒）；超了就整体取哈希，仍然随任何一类失败的变化而变化。
    """
    failure = ("cases:" + hashlib.sha256(json.dumps(sorted(failed)).encode()).hexdigest()
               if failed else "")
    if export_failures:
        failure += "desktop:" + hashlib.sha256(json.dumps(export_failures, sort_keys=True).encode()).hexdigest()
    if near_limit:
        failure += "storage_capacity"
    if len(failure) > MAX_FAILURE_IDENTITY:
        failure = "combined:" + hashlib.sha256(failure.encode()).hexdigest()
    return failure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--gh", default=shutil.which("gh"))
    parser.add_argument("--desktop-dir", type=Path)
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
        if args.desktop_dir is not None:
            phase = "desktop_export"
            result["desktop_export"] = export_reports(args.data_dir, args.desktop_dir)
    except Exception as exc:
        notice = notifications.sync({}, "backend:" + phase)
        # 只留异常类名：异常正文可能带令牌、请求路径或用户反馈内容。
        # 此前整段吞掉，日志里只剩 phase，出了问题无法判断是鉴权、拉取还是分析失败。
        print(json.dumps({"error": "feedback_backend_unavailable", "phase": phase,
                          "error_type": type(exc).__name__,
                          "notifications": notice}, ensure_ascii=False))
        return 1
    export_failures = result.get("desktop_export", {}).get("failed", [])
    failure = failure_identity(result["failed"], export_failures, result.get("capacity", {}).get("near_limit"))
    try:
        versions = report_versions(args.data_dir)
    except Exception:
        result["notifications"] = notifications.sync({}, "backend:notification_index")
        result["notifications"]["failed"].append("report_index_unavailable")
    else:
        result["notifications"] = notifications.sync(versions, failure)
    print(json.dumps(result, ensure_ascii=False))
    return int(bool(result["failed"] or result["notifications"]["failed"] or export_failures))


if __name__ == "__main__":
    raise SystemExit(main())
