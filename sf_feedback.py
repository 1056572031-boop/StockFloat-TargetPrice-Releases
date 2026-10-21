"""Local diagnostic feedback package protocol.

This module deliberately has no Qt, network, or application-state imports.  It
is the small, testable boundary between a frozen diagnostic snapshot and the
future feedback transport.  The existing user-visible diagnostic export is not
changed by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
import uuid
import zipfile
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast

SCHEMA_VERSION = 1
DIAGNOSTIC_PAYLOAD_VERSION = 1
DEFAULT_CONSENT_POLICY_VERSION = "feedback-consent-v1"
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_FULL_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_MEMBER_COUNT = 64
MAX_MANIFEST_BYTES = 64 * 1024
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# This is an allow-list, not a secret-name blacklist.  Nested values are
# intentionally bounded to simple JSON values and the listed diagnostic keys.
ALLOWED_FIELDS = frozenset({
    "app_version", "bundle_id", "run_id", "capture_started_at",
    "capture_completed_at", "error_category", "phase", "counts", "health",
    "summary", "omitted", "truncated",
})


class FeedbackProtocolError(ValueError):
    """Raised when a local feedback artifact violates the frozen protocol."""


class PrivacyRejected(FeedbackProtocolError):
    """Raised when a snapshot contains a field outside the upload allow-list."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FeedbackProtocolError("JSON value is not finite or serializable") from exc


def _check_json_shape(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise PrivacyRejected("diagnostic JSON nesting exceeds the protocol limit")
    if isinstance(value, dict):
        if len(value) > 64:
            raise PrivacyRejected("diagnostic object has too many fields")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 96:
                raise PrivacyRejected("diagnostic field name is invalid")
            _check_json_shape(item, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 128:
            raise PrivacyRejected("diagnostic list has too many items")
        for item in value:
            _check_json_shape(item, depth=depth + 1)
    elif not isinstance(value, (str, int, float, bool)) and value is not None:
        raise PrivacyRejected("diagnostic value has an unsupported type")
    elif isinstance(value, str) and len(value) > 4096:
        raise PrivacyRejected("diagnostic string exceeds the protocol limit")


def project_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return the upload projection, rejecting unknown fields instead of dropping them."""
    if not isinstance(snapshot, Mapping):
        raise PrivacyRejected("diagnostic snapshot must be an object")
    unknown = sorted(set(snapshot) - ALLOWED_FIELDS)
    if unknown:
        raise PrivacyRejected("diagnostic fields are not allow-listed: " + ", ".join(unknown))
    result = dict(snapshot)
    _check_json_shape(result)
    return result


def _safe_member_name(name: str) -> str:
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 240:
        raise FeedbackProtocolError("invalid archive member name")
    normalized = unicodedata.normalize("NFC", name)
    if normalized != name or "\\" in name or "//" in name or name.startswith(("/", "~")):
        raise FeedbackProtocolError("archive member uses an unsafe path separator")
    path = Path(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise FeedbackProtocolError("archive member escapes its root")
    if not _SAFE_NAME.fullmatch(name):
        raise FeedbackProtocolError("archive member contains unsupported characters")
    return name


def sha256_file(path: os.PathLike[str] | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _ZipMetadata(Protocol):
    _EndRecData: Callable[[BinaryIO], list[Any] | None]
    _ECD_ENTRIES_TOTAL: int
    _ECD_SIZE: int


def check_zip_directory(stream, maximum_members):
    # Bound ZipInfo allocation before the pinned stdlib constructs entry objects.
    metadata = cast(_ZipMetadata, zipfile)
    end = metadata._EndRecData(stream)
    if end is None:
        raise FeedbackProtocolError("archive is not a valid ZIP")
    if end[metadata._ECD_ENTRIES_TOTAL] > maximum_members:
        raise FeedbackProtocolError("archive contains too many members")
    if end[metadata._ECD_SIZE] > 256 * 1024:
        raise FeedbackProtocolError("archive central directory exceeds the size limit")


def _atomic_replace(path: Path, writer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            writer(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            # Replacement is already visible, but its directory entry has not
            # been confirmed durable. Do not report a durable success to callers;
            # keep the valid target so an explicit retry can verify/reuse it.
            raise FeedbackProtocolError(
                "archive replaced but directory durability could not be confirmed"
            ) from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError as exc:
            del exc


def build_archive(path: os.PathLike[str] | str, files: Mapping[str, bytes | str], *,
                  app_version: str, bundle_id: str, run_id: str,
                  omitted: list[str] | None = None,
                  truncated: list[str] | None = None,
                  capture_started_at: str | None = None,
                  capture_completed_at: str | None = None) -> dict[str, Any]:
    """Write a self-contained feedback ZIP and return its package metadata."""
    if "manifest.json" in files:
        raise FeedbackProtocolError("manifest.json is generated and cannot be supplied")
    if not files or len(files) > MAX_MEMBER_COUNT - 1:
        raise FeedbackProtocolError(f"archive must contain 1 to {MAX_MEMBER_COUNT - 1} data members")
    prepared: dict[str, bytes] = {}
    for raw_name, raw_data in files.items():
        name = _safe_member_name(raw_name)
        if name in prepared or name.casefold() in {key.casefold() for key in prepared}:
            raise FeedbackProtocolError("archive member name collision")
        data = raw_data.encode("utf-8") if isinstance(raw_data, str) else bytes(raw_data)
        limit = MAX_FULL_ARCHIVE_BYTES if name == "diagnostics.zip" else MAX_MEMBER_BYTES
        if len(data) > limit:
            raise FeedbackProtocolError("archive member exceeds the size limit: " + name)
        prepared[name] = data
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "diagnostic_payload_version": 2 if "diagnostics.zip" in prepared else DIAGNOSTIC_PAYLOAD_VERSION,
        "app_version": str(app_version),
        "bundle_id": str(bundle_id),
        "run_id": str(run_id),
        "capture_started_at": capture_started_at or utc_now(),
        "capture_completed_at": capture_completed_at or utc_now(),
        "files": [{"name": name, "size_bytes": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in prepared.items()],
        "omitted": list(omitted or []),
        "truncated": list(truncated or []),
    }
    manifest_bytes = _json_bytes(manifest)
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise FeedbackProtocolError("manifest exceeds the size limit")

    def write(stream):
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for name, data in prepared.items():
                archive.writestr(name, data, compress_type=(zipfile.ZIP_STORED if name == "diagnostics.zip"
                                                          else zipfile.ZIP_DEFLATED))
            archive.writestr("manifest.json", manifest_bytes)

    target = Path(path)
    _atomic_replace(target, write)
    metadata = validate_archive(target)
    return {"path": str(target), "archive_sha256": sha256_file(target),
            "size_bytes": target.stat().st_size, "manifest": metadata}


def validate_archive(path: os.PathLike[str] | str) -> dict[str, Any]:
    target = Path(path)
    size = target.stat().st_size
    if size < 1 or size > MAX_FULL_ARCHIVE_BYTES:
        raise FeedbackProtocolError("archive exceeds the size limit")
    with target.open("rb") as stream:
        check_zip_directory(stream, MAX_MEMBER_COUNT)
    try:
        with zipfile.ZipFile(target) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBER_COUNT:
                raise FeedbackProtocolError("archive contains too many members")
            names: list[str] = []
            for info in infos:
                name = _safe_member_name(info.orig_filename)
                if name in names or name.casefold() in {item.casefold() for item in names}:
                    raise FeedbackProtocolError("archive member name collision")
                names.append(name)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode) or info.flag_bits & 0x1:
                    raise FeedbackProtocolError("archive contains a link or encrypted member")
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise FeedbackProtocolError("archive contains an unsupported compression method")
                limit = MAX_FULL_ARCHIVE_BYTES if name == "diagnostics.zip" else MAX_MEMBER_BYTES
                if info.file_size > limit:
                    raise FeedbackProtocolError("archive member exceeds the size limit")
            if "manifest.json" not in names:
                raise FeedbackProtocolError("archive manifest is missing")
            manifest_bytes = archive.read("manifest.json")
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise FeedbackProtocolError("manifest exceeds the size limit")
            try:
                manifest = json.loads(manifest_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FeedbackProtocolError("manifest is not valid UTF-8 JSON") from exc
            if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION \
                    or manifest.get("diagnostic_payload_version") not in (1, 2):
                raise FeedbackProtocolError("unsupported manifest version")
            if manifest["diagnostic_payload_version"] == 1 and size > MAX_ARCHIVE_BYTES:
                raise FeedbackProtocolError("legacy archive exceeds the size limit")
            if manifest["diagnostic_payload_version"] == 2 and set(names) != {
                    "manifest.json", "state.json", "problem.txt", "diagnostics.zip"}:
                raise FeedbackProtocolError("full archive members are incomplete")
            declared = manifest.get("files")
            if not isinstance(declared, list) or len(declared) != len(names) - 1:
                raise FeedbackProtocolError("manifest file list does not match the archive")
            expected: dict[str, dict[str, Any]] = {}
            for entry in declared:
                if not isinstance(entry, dict):
                    raise FeedbackProtocolError("manifest file entry is invalid")
                raw_name = entry.get("name")
                if not isinstance(raw_name, str):
                    raise FeedbackProtocolError("manifest file name is invalid")
                name = _safe_member_name(raw_name)
                if name == "manifest.json" or name in expected or name.casefold() in {key.casefold() for key in expected}:
                    raise FeedbackProtocolError("manifest file list contains a collision")
                if not isinstance(entry.get("size_bytes"), int) or not isinstance(entry.get("sha256"), str) \
                        or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
                    raise FeedbackProtocolError("manifest file hash entry is invalid")
                expected[name] = entry
            if set(expected) != set(names) - {"manifest.json"}:
                raise FeedbackProtocolError("manifest file list is incomplete")
            for name, entry in expected.items():
                data = archive.read(name)
                if len(data) != entry["size_bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise FeedbackProtocolError("manifest hash mismatch: " + name)
            return manifest
    except zipfile.BadZipFile as exc:
        raise FeedbackProtocolError("archive is not a valid ZIP") from exc


def make_envelope(*, archive_path: os.PathLike[str] | str, app_version: str,
                  outbox_id: str, consent_policy_version: str = DEFAULT_CONSENT_POLICY_VERSION,
                  idempotency_key: str | None = None) -> dict[str, Any]:
    manifest = validate_archive(archive_path)
    target = Path(archive_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "diagnostic_payload_version": manifest["diagnostic_payload_version"],
        "idempotency_key": idempotency_key or uuid.uuid4().hex,
        "archive_sha256": sha256_file(target),
        "size_bytes": target.stat().st_size,
        "consent_policy_version": str(consent_policy_version),
        "app_version": str(app_version),
        "outbox_id": str(outbox_id),
    }


def validate_receipt(receipt: Mapping[str, Any], envelope: Mapping[str, Any]) -> dict[str, Any]:
    required = ("case_id", "archive_sha256", "size_bytes", "received_at", "storage_status",
                "validation_status")
    if not isinstance(receipt, Mapping) or any(not receipt.get(key) for key in required):
        raise FeedbackProtocolError("receipt is incomplete")
    if "retention_until" not in receipt or (
            not receipt["retention_until"] and receipt.get("retention_policy") != "manual_deletion"):
        raise FeedbackProtocolError("receipt retention policy is incomplete")
    if receipt["archive_sha256"] != envelope["archive_sha256"] or receipt["size_bytes"] != envelope["size_bytes"]:
        raise FeedbackProtocolError("receipt does not match the submitted archive")
    if not isinstance(receipt["case_id"], str) or len(receipt["case_id"]) > 128:
        raise FeedbackProtocolError("receipt case_id is invalid")
    return dict(receipt)
