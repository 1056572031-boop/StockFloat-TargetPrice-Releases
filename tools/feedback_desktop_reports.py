#!/usr/bin/env python3
"""Run the private agent and publish verified reports plus original ZIPs locally."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sf_feedback import MAX_FULL_ARCHIVE_BYTES


def copy_verified(source, target, expected):
    if source.is_symlink():
        raise RuntimeError("source artifact is a symlink")
    with source.open("rb") as stream:
        body = stream.read(MAX_FULL_ARCHIVE_BYTES + 1)
    if len(body) > MAX_FULL_ARCHIVE_BYTES:
        raise RuntimeError("local artifact exceeds its limit")
    if hashlib.sha256(body).hexdigest() != expected:
        raise RuntimeError("source checksum mismatch")
    if target.is_symlink():
        raise RuntimeError("desktop target is a symlink")
    if target.exists():
        if target.stat().st_size != len(body) or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise RuntimeError("refusing to overwrite a modified desktop artifact")
        return 0
    descriptor, temporary = tempfile.mkstemp(prefix=".feedback-", dir=target.parent)
    created = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
            created = True
        except FileExistsError:
            if target.is_symlink() or target.stat().st_size != len(body) or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise RuntimeError("desktop artifact changed concurrently") from None
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
    with target.open("rb") as stream:
        copied = stream.read(MAX_FULL_ARCHIVE_BYTES + 1)
    if hashlib.sha256(copied).hexdigest() != expected:
        raise RuntimeError("desktop copy checksum mismatch")
    return int(created)


def export_reports(data, destination):
    data, destination = Path(data), Path(destination)
    if data.is_symlink() or destination.is_symlink():
        raise RuntimeError("data or desktop directory is a symlink")
    data, destination = data.resolve(), destination.resolve()
    if any((data / name).is_symlink() for name in ("queue.sqlite3", "queue.sqlite3-wal", "queue.sqlite3-shm")):
        raise RuntimeError("queue database path is a symlink")
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    destination.chmod(0o700)
    copied, failed = 0, []
    with sqlite3.connect((data / "queue.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT case_id,report,receipt FROM queue WHERE status='ready'").fetchall()
    for case, raw_report, raw_receipt in rows:
        identity = case if isinstance(case, str) and re.fullmatch(r"feedback-[a-f0-9]{32}", case) else "invalid_local_case"
        try:
            if identity == "invalid_local_case":
                raise RuntimeError("invalid case identity")
            report, receipt = json.loads(raw_report), json.loads(raw_receipt)
            target = destination / case
            if target.is_symlink():
                raise RuntimeError("desktop case directory is a symlink")
            target.mkdir(mode=0o700, exist_ok=True)
            for key in ("json_path", "markdown_path"):
                source = Path(report[key])
                parent = data / "reports" / case
                if source.parent != parent or source.is_symlink() or parent.is_symlink() or (data / "reports").is_symlink():
                    raise RuntimeError("invalid report path")
                copied += copy_verified(source, target / source.name, report["artifact_sha256"][str(source)])
            source = data / "objects" / (case + ".zip")
            if source.is_symlink() or (data / "objects").is_symlink():
                raise RuntimeError("invalid archive path")
            if (receipt["case_id"] != case or receipt["archive_sha256"] != report["archive_sha256"]
                    or source.stat().st_size != receipt["size_bytes"]):
                raise RuntimeError("archive receipt binding mismatch")
            copied += copy_verified(source, target / source.name, receipt["archive_sha256"])
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            failed.append({"case_id": identity, "category": type(exc).__name__})
    return {"desktop_reports": str(destination), "cases": len(rows), "copied_files": copied, "failed": failed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--destination", type=Path, default=Path.home() / "Desktop/Skyrocket 用户反馈报告")
    args = parser.parse_args()
    os.umask(0o077)
    result = export_reports(args.data_dir, args.destination)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return int(bool(result["failed"]))


if __name__ == "__main__":
    raise SystemExit(main())
