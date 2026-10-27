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
import uuid
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sf_diag import identity_needles, scrub_text
from sf_feedback import MAX_FULL_ARCHIVE_BYTES
from sf_feedback_bundle import PrivacyRejected, check_privacy
from sf_privacy import scrub_secrets

MAX_REPORT_BYTES = 4 * 1024 * 1024


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


def redact(text):
    """桌面副本的第二层脱敏，用的就是诊断包那两套现成规则，不在这里另算一份标准。

    桌面目录是**会被看见、被同步、被截图**的地方：分析器那一份存在 0700 的私有对象目录里，
    复制到桌面时必须再过一遍身份与凭据。需要"身份 needles"本身取不到时，宁可整条案件失败
    并把原因写进 failed，也不放一份「以为脱过敏」的报告出去。
    """
    issues: list[str] = []
    needles = identity_needles(issues)
    if issues:
        raise RuntimeError("identity redaction unavailable")
    return scrub_text(scrub_secrets(text), needles)


def _scrub(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            clean_key = redact(key) if isinstance(key, str) else key
            if clean_key in result:
                raise RuntimeError("redaction key collision")
            result[clean_key] = _scrub(item)
        return result
    return value


def redacted_body(source, body):
    """JSON 报告按结构逐字段脱敏（并保证出来还是合法 JSON），Markdown 整篇过一遍。"""
    text = body.decode("utf-8")
    if source.suffix == ".json":
        try:
            data = _scrub(json.loads(text))
            output = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            check_privacy(output.decode("utf-8"))
            if len(output) > MAX_REPORT_BYTES:
                raise RuntimeError("redacted report exceeds its limit")
            return output
        except PrivacyRejected as exc:
            raise RuntimeError("redacted report failed privacy validation") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("report json is unreadable") from exc
    output = redact(text).encode("utf-8")
    try:
        check_privacy(output.decode("utf-8"))
    except PrivacyRejected as exc:
        raise RuntimeError("redacted report failed privacy validation") from exc
    if len(output) > MAX_REPORT_BYTES:
        raise RuntimeError("redacted report exceeds its limit")
    return output


def write_redacted(source, target, expected):
    """校验来源哈希 → 二次脱敏 → 原子落盘 → 读回核对**自己写出的字节**。

    来源哈希仍然必须等于队列账本记录的那一份：桌面副本可以变短、变样，但它到底来自
    哪个原始件必须可追。写完之后按写出内容的 SHA-256 记账，不再按源文件哈希记账 ——
    那会让人以为桌面那份与分析器那份逐字节相同。
    """
    if source.is_symlink():
        raise RuntimeError("source artifact is a symlink")
    try:
        if source.stat().st_size > MAX_REPORT_BYTES:
            raise RuntimeError("report exceeds its limit")
    except OSError as exc:
        raise RuntimeError("source artifact unavailable") from exc
    body = source.read_bytes()
    if len(body) > MAX_REPORT_BYTES:
        raise RuntimeError("report exceeds its limit")
    if hashlib.sha256(body).hexdigest() != expected:
        raise RuntimeError("source checksum mismatch")
    data = redacted_body(source, body)
    if len(data) > MAX_REPORT_BYTES:
        raise RuntimeError("redacted report exceeds its limit")
    if target.is_symlink():
        raise RuntimeError("desktop target is a symlink")
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RuntimeError("desktop target directory unavailable") from exc
    try:
        os.fchmod(directory, 0o700)
        target_name = target.name
        try:
            target_fd = os.open(target_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
        except FileNotFoundError:
            target_fd = None
        if target_fd is not None:
            try:
                existing = os.read(target_fd, MAX_REPORT_BYTES + 1)
            finally:
                os.close(target_fd)
            if existing != data:
                raise RuntimeError("refusing to overwrite a modified desktop artifact")
            os.chmod(target, 0o600, follow_symlinks=False)
            created = False
        else:
            created = True
            temporary_name = ".feedback-%s.tmp" % uuid.uuid4().hex
            temporary_fd = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                   | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
            try:
                with os.fdopen(temporary_fd, "wb") as stream:
                    temporary_fd = -1
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary_name, target_name, src_dir_fd=directory, dst_dir_fd=directory,
                            follow_symlinks=False)
                except FileExistsError:
                    check_fd = os.open(target_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
                    try:
                        if os.read(check_fd, MAX_REPORT_BYTES + 1) != data:
                            raise RuntimeError("desktop artifact changed concurrently")
                    finally:
                        os.close(check_fd)
                    created = False
            finally:
                if temporary_fd >= 0:
                    os.close(temporary_fd)
                os.unlink(temporary_name, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)
    if hashlib.sha256(target.read_bytes()).hexdigest() != hashlib.sha256(data).hexdigest():
        raise RuntimeError("desktop copy mismatch")
    return {"written": int(created), "changed": int(data != body), "sha256": hashlib.sha256(data).hexdigest()}


def export_reports(data, destination, *, include_archive=False):
    data, destination = Path(data), Path(destination)
    if data.is_symlink() or destination.is_symlink():
        raise RuntimeError("data or desktop directory is a symlink")
    data, destination = data.resolve(), destination.resolve()
    if any((data / name).is_symlink() for name in ("queue.sqlite3", "queue.sqlite3-wal", "queue.sqlite3-shm")):
        raise RuntimeError("queue database path is a symlink")
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    destination.chmod(0o700)
    copied, failed = 0, []
    redaction = {"files": 0, "changed": 0}
    exported: dict[str, str] = {}
    with closing(sqlite3.connect((data / "queue.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
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
                outcome = write_redacted(source, target / source.name, report["artifact_sha256"][str(source)])
                copied += outcome["written"]
                redaction["files"] += 1
                redaction["changed"] += outcome["changed"]
                exported[str(target / source.name)] = outcome["sha256"]
            if include_archive:
                source = data / "objects" / (case + ".zip")
                if source.is_symlink() or (data / "objects").is_symlink():
                    raise RuntimeError("invalid archive path")
                if (receipt["case_id"] != case or receipt["archive_sha256"] != report["archive_sha256"]
                        or source.stat().st_size != receipt["size_bytes"]):
                    raise RuntimeError("archive receipt binding mismatch")
                copied += copy_verified(source, target / source.name, receipt["archive_sha256"])
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            failed.append({"case_id": identity, "category": type(exc).__name__})
    return {"desktop_reports": str(destination), "cases": len(rows), "copied_files": copied,
            "redaction": redaction, "exported_sha256": exported, "includes_archive": bool(include_archive),
            "failed": failed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--destination", type=Path, default=Path.home() / "Desktop/Skyrocket 用户反馈报告")
    parser.add_argument("--include-archive", action="store_true",
                        help="同时导出原始诊断 ZIP；默认只导出脱敏分析报告")
    args = parser.parse_args()
    os.umask(0o077)
    result = export_reports(args.data_dir, args.destination, include_archive=args.include_archive)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return int(bool(result["failed"]))


if __name__ == "__main__":
    raise SystemExit(main())
