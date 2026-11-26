#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Allocate and verify the monotonic release sequence used by update publication."""

import argparse
import fcntl
import json
import os
import plistlib
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "tools" / "release_sequence_ledger.json"
DEFAULT_CONSTANTS = ROOT / "sf_constants.py"
_SEQUENCE_RE = re.compile(r"^(RELEASE_SEQUENCE\s*=\s*)(\d+)\s*$", re.M)
_VERSION_RE = re.compile(r"^APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]\s*$", re.M)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _positive_int(value, label):
    if type(value) is not int or value < 1:
        raise ValueError("%s 必须是正整数，不能用 bool 伪装" % label)
    return value


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError("无法读取账本：%s" % path) from exc
    except json.JSONDecodeError as exc:
        raise ValueError("账本不是有效 JSON：%s" % path) from exc
    if not isinstance(data, dict) or type(data.get("schema")) is not int:
        raise ValueError("账本必须是带整数 schema 的对象")
    releases = data.get("releases")
    if not isinstance(releases, list):
        raise ValueError("账本 releases 必须是数组")
    seen_sequences = set()
    seen_versions = set()
    previous = 0
    for record in releases:
        if not isinstance(record, dict):
            raise ValueError("账本记录必须是对象")
        sequence = _positive_int(record.get("sequence"), "账本 sequence")
        if sequence in seen_sequences:
            raise ValueError("账本 sequence 不得重复")
        if sequence <= previous:
            raise ValueError("账本 sequence 必须严格递增")
        if sequence > previous + 1:
            reason = record.get("skip_reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("账本序号跳号必须有 skip_reason")
        seen_sequences.add(sequence)
        previous = sequence
        if not isinstance(record.get("version"), str) or not record["version"].strip():
            raise ValueError("账本 version 必须是非空字符串")
        if record["version"] in seen_versions:
            raise ValueError("账本 version 不得重复")
        seen_versions.add(record["version"])
    return data


def load_ledger(path):
    path = Path(path)
    if not path.exists():
        return {"schema": 1, "releases": []}
    return _read_json(path)


def _atomic_write(path, text):
    path = Path(path)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_ledger(path, ledger):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")


def mark_published(ledger_path, version, sequence):
    """Promote one allocated identity under the allocator's stable lock."""
    _positive_int(sequence, "release_sequence")
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            ledger = load_ledger(path)
            matches = [record for record in ledger["releases"]
                       if record["version"] == version and record["sequence"] == sequence]
            if len(matches) != 1:
                raise ValueError("发布账本必须有唯一的 version/sequence 身份记录")
            record = matches[0]
            if record.get("status") == "published":
                return record
            if record.get("status") != "allocated":
                raise ValueError("发布账本身份必须处于 allocated 状态")
            record["status"] = "published"
            write_ledger(path, ledger)
            return record
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def source_identity(constants_path):
    source = Path(constants_path).read_text(encoding="utf-8")
    versions = _VERSION_RE.findall(source)
    sequences = _SEQUENCE_RE.findall(source)
    if len(versions) != 1:
        raise ValueError("sf_constants.py 必须恰好有一个 APP_VERSION")
    if len(sequences) != 1:
        raise ValueError("sf_constants.py 必须恰好有一个 RELEASE_SEQUENCE")
    return versions[0], int(sequences[0][1])


def update_source_sequence(constants_path, sequence):
    _positive_int(sequence, "RELEASE_SEQUENCE")
    path = Path(constants_path)
    source = path.read_text(encoding="utf-8")
    matches = list(_SEQUENCE_RE.finditer(source))
    if len(matches) != 1:
        raise ValueError("sf_constants.py 必须恰好有一个 RELEASE_SEQUENCE")
    updated = source[:matches[0].start(2)] + str(sequence) + source[matches[0].end(2):]
    _atomic_write(path, updated)


def _record_for(ledger, version, sequence):
    return next((item for item in ledger["releases"]
                 if item["version"] == version and item["sequence"] == sequence), None)


def allocate(ledger_path, constants_path, version, *, skip=None, reason=None, stage="beta", now=None):
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _allocate_locked(path, constants_path, version, skip=skip, reason=reason,
                                    stage=stage, now=now)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _allocate_locked(ledger_path, constants_path, version, *, skip=None, reason=None, stage="beta", now=None):
    if not isinstance(version, str) or not version.strip():
        raise ValueError("version 必须是非空字符串")
    ledger = load_ledger(ledger_path)
    existing = next((item for item in ledger["releases"] if item["version"] == version), None)
    if existing is not None:
        update_source_sequence(constants_path, existing["sequence"])
        return existing
    highest = max((item["sequence"] for item in ledger["releases"]), default=0)
    next_sequence = highest + 1
    if skip is None:
        sequence = next_sequence
        skip_reason = None
    else:
        sequence = _positive_int(skip, "--skip")
        if sequence <= next_sequence:
            raise ValueError("--skip 必须严格大于下一序号 %d" % next_sequence)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("--skip 必须提供非空 reason")
        skip_reason = reason.strip()
    if stage not in ("beta", "stable"):
        raise ValueError("stage 必须是 beta 或 stable")
    record = {
        "sequence": sequence,
        "version": version,
        "stage": stage,
        "status": "allocated",
        "allocated_at": now or utc_now(),
        "notes": "",
        "skip_reason": skip_reason,
    }
    ledger["releases"].append(record)
    write_ledger(ledger_path, ledger)
    update_source_sequence(constants_path, sequence)
    return record


def _plist_identity(data, label):
    if not isinstance(data, dict):
        raise ValueError("%s Info.plist 必须是对象" % label)
    version = data.get("CFBundleShortVersionString")
    sequence = data.get("SkyrocketReleaseSequence")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("%s 缺少 CFBundleShortVersionString" % label)
    _positive_int(sequence, "%s SkyrocketReleaseSequence" % label)
    return version, sequence


def app_identity(app_path):
    try:
        with (Path(app_path) / "Contents" / "Info.plist").open("rb") as stream:
            return _plist_identity(plistlib.load(stream), "冻结包")
    except OSError as exc:
        raise ValueError("无法读取冻结包 Info.plist") from exc
    except plistlib.InvalidFileException as exc:
        raise ValueError("冻结包 Info.plist 无效") from exc


def zip_identity(archive_path):
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = [name for name in archive.namelist() if name.endswith(".app/Contents/Info.plist")]
            if len(entries) != 1:
                raise ValueError("ZIP 内必须恰好有一个 App Info.plist")
            return _plist_identity(plistlib.loads(archive.read(entries[0])), "ZIP 内 App")
    except OSError as exc:
        raise ValueError("无法读取 ZIP") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError("ZIP 无效") from exc


def manifest_identity(manifest_path):
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError("无法读取签名清单") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("签名清单无效") from exc
    if not isinstance(data, dict):
        raise ValueError("签名清单必须是对象")
    version = data.get("version")
    sequence = data.get("release_sequence")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("签名清单缺少 version")
    _positive_int(sequence, "签名清单 release_sequence")
    return version, sequence


def verify_prearchive(ledger_path, constants_path, app_path):
    ledger = load_ledger(ledger_path)
    source = source_identity(constants_path)
    frozen = app_identity(app_path)
    if frozen != source:
        raise ValueError("源码与冻结包发布序号不一致：源码=%s/%s；冻结包=%s/%s" %
                         (source[0], source[1], frozen[0], frozen[1]))
    if _record_for(ledger, *source) is None:
        raise ValueError("账本没有源码版本与序号的完全匹配记录：%s/%s" % source)
    return {"version": source[0], "release_sequence": source[1]}


def verify(ledger_path, constants_path, app_path, manifest_path, archive_path):
    ledger = load_ledger(ledger_path)
    identities = {
        "源码": source_identity(constants_path),
        "冻结包": app_identity(app_path),
        "签名清单": manifest_identity(manifest_path),
        "ZIP 内 App": zip_identity(archive_path),
    }
    expected = identities["源码"]
    mismatches = ["%s=%s/%s" % (label, identity[0], identity[1])
                  for label, identity in identities.items() if identity != expected]
    if mismatches:
        raise ValueError("四处发布序号不一致：源码=%s/%s；%s" %
                         (expected[0], expected[1], "；".join(mismatches)))
    if _record_for(ledger, *expected) is None:
        raise ValueError("账本没有源码版本与序号的完全匹配记录：%s/%s" % expected)
    return {"version": expected[0], "release_sequence": expected[1]}


def verify_rollback(ledger_path, manifest_path, archive_path):
    """Require the rollback archive to match one previously published identity."""
    ledger = load_ledger(ledger_path)
    expected = manifest_identity(manifest_path)
    bundled = zip_identity(archive_path)
    if bundled != expected:
        raise ValueError("回滚清单与 ZIP 内 App 身份不一致")
    record = _record_for(ledger, *expected)
    if record is None or record.get("status") != "published":
        raise ValueError("回滚必须匹配账本唯一已发布身份")
    return {"version": expected[0], "release_sequence": expected[1]}


def self_test():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = root / "ledger.json"
        constants = root / "sf_constants.py"
        constants.write_text("APP_VERSION = '1.2.3'\nRELEASE_SEQUENCE = 0\n", encoding="utf-8")
        first = allocate(ledger, constants, "1.2.3", now="2026-11-19T00:00:00Z")
        if first["sequence"] != 1 or source_identity(constants)[1] != 1:
            print("FAIL 首次分配没有写入序号 1")
            return 1
        try:
            allocate(ledger, constants, "1.2.4", skip=3)
        except ValueError:
            pass
        else:
            print("FAIL 无理由跳号被放行")
            return 1
    print("self-test 通过：首次分配、源码更新和无理由跳号拒绝")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    subcommands = parser.add_subparsers(dest="command")
    allocate_parser = subcommands.add_parser("allocate")
    allocate_parser.add_argument("--version", required=True)
    allocate_parser.add_argument("--skip", type=int)
    allocate_parser.add_argument("--reason")
    allocate_parser.add_argument("--stage", default="beta")
    verify_parser = subcommands.add_parser("verify")
    verify_parser.add_argument("--app", type=Path, required=True)
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser.add_argument("--archive", type=Path, required=True)
    for command in (allocate_parser, verify_parser):
        command.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
        command.add_argument("--constants", type=Path, default=DEFAULT_CONSTANTS)
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.command == "allocate":
        record = allocate(args.ledger, args.constants, args.version, skip=args.skip,
                          reason=args.reason, stage=args.stage)
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "verify":
        result = verify(args.ledger, args.constants, args.app, args.manifest, args.archive)
        print("四处一致：%s / %s" % (result["version"], result["release_sequence"]))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
