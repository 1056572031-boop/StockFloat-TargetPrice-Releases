"""Upload-specific, allow-listed diagnostic projection. Never exports raw logs."""

from __future__ import annotations

import json
import hashlib
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import sf_diag
from sf_feedback import PrivacyRejected, project_snapshot, utc_now, validate_archive, validate_receipt
from sf_privacy import scrub_secrets
from sf_feedback_bundle import FULL_CONSENT_VERSION, MAX_DIAGNOSTIC_BYTES, check_privacy, read_diagnostics

MAX_DESCRIPTION = 4000
CONSENT_VERSION = "github-private-feedback-v1"
FEEDBACK_REPOSITORY = "1056572031-boop/Skyrocket-Private-Feedback"
CAPTURE_CONTEXT_FIELDS = ("capture_started_at", "capture_completed_at", "omitted", "truncated")
_COUNTER = re.compile(r"(?:^|_)(?:count|counts|fail|fails|failures|retries|dropped|errors)$", re.IGNORECASE)


def validate_capture_context(manifest, state):
    if any(manifest[key] != state[key] for key in CAPTURE_CONTEXT_FIELDS):
        raise PrivacyRejected("反馈清单与状态声明不一致，禁止发送")


def clean_description(text: str) -> str:
    if len(text) > MAX_DESCRIPTION:
        raise PrivacyRejected("问题描述不能超过 4000 字")
    issues: list[str] = []
    needles = sf_diag.identity_needles(issues)
    if issues:
        raise PrivacyRejected("无法完成身份脱敏，请保留本地诊断包")
    result = sf_diag.scrub_text(scrub_secrets(text), needles).strip()
    if len(result) > MAX_DESCRIPTION:
        raise PrivacyRejected("脱敏后的问题描述超过 4000 字，请缩短后重试")
    return result


def capture_snapshot(owner, *, app_version: str, bundle_id: str, run_id: str, index_only=False) -> dict:
    """Called on the UI thread. Copy only bounded numeric counters and flags."""
    started_at = utc_now()
    raw: dict[str, Any] = ({"modules": {}, "objects": {}, "issues": [], "truncated": [],
            "meta": {"loaded_product_modules": 0}} if index_only else sf_diag.collect(owner))
    counts: dict[str, int | bool] = {}
    invalid_counters = 0
    omitted = ["raw_logs", "config", "quotes", "targets", "camera", "screenshots"]
    truncated = ["automatic_collection"] if raw["truncated"] else []
    # Only code-owned names and integers/bools can cross this boundary. String
    # values, dynamic dict keys, prices, paths and raw exception text never do.
    for group in ("modules", "objects"):
        for scope, fields in raw[group].items():
            for name, value in fields.items():
                if not _COUNTER.search(name) or type(value) not in (int, bool):
                    continue
                key = scope + "." + name
                if len(key) > 96 or len(counts) >= 63 or not -(2 ** 53 - 1) <= value <= 2 ** 53 - 1:
                    invalid_counters += 1
                    if "counters" not in truncated:
                        truncated.append("counters")
                    continue
                counts[key] = value
    summary = {"capture_issue_count": len(raw["issues"]) + invalid_counters,
               "loaded_modules": raw["meta"]["loaded_product_modules"]}
    if index_only:
        summary.clear()
    presentation = vars(owner).get("_quote_presentation") if owner is not None else None
    state = vars(presentation).get("state") if presentation is not None else None
    if state is not None:
        values = vars(state)
        for name in ("net_ok", "minute_stale", "price_is_fallback", "pct_unknown"):
            if type(values.get(name)) is bool:
                summary[name] = values[name]
        if type(values.get("quote_fail")) is int:
            counts["quote_fail"] = values["quote_fail"]
    now = utc_now()
    return project_snapshot({
        "app_version": app_version, "bundle_id": bundle_id, "run_id": run_id,
        "capture_started_at": started_at, "capture_completed_at": now,
        "phase": "user_feedback", "health": None, "counts": counts,
        "summary": summary,
        "omitted": omitted, "truncated": truncated,
    })


def package_files(snapshot: dict, description: str) -> dict[str, str]:
    checked = validate_uploaded_snapshot(snapshot)
    return {"state.json": json.dumps(checked, ensure_ascii=False, allow_nan=False),
            "problem.txt": clean_description(description)}


def validate_uploaded_snapshot(state: dict) -> dict:
    """Independent receiver check of the automatic projection's exact schema."""
    state = project_snapshot(state)
    required = {"app_version", "bundle_id", "run_id", "phase", "capture_started_at",
                "capture_completed_at", "health", "counts", "summary", "omitted", "truncated"}
    if set(state) != required or state["phase"] != "user_feedback" or state["health"] is not None:
        raise PrivacyRejected("未知的自动采集字段或健康结论")
    if (not re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", str(state["app_version"]))
            or not re.fullmatch(r"[0-9a-f]{6}", str(state["run_id"]))
            or state["bundle_id"] != "com.skyrocket.app"):
        raise PrivacyRejected("反馈身份格式不符合采集协议")
    for key in ("capture_started_at", "capture_completed_at"):
        value = datetime.fromisoformat(state[key].replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise PrivacyRejected("采集时间缺少时区")
    counts = state["counts"]
    if not isinstance(counts, dict) or any(
            not re.fullmatch(r"[A-Za-z0-9_.:#-]{1,96}", key)
            or type(value) not in (int, bool) or not -(2 ** 53 - 1) <= value <= 2 ** 53 - 1
            for key, value in counts.items()):
        raise PrivacyRejected("自动诊断计数格式不正确")
    summary = state["summary"]
    allowed = {"capture_issue_count", "loaded_modules", "net_ok", "minute_stale", "price_is_fallback", "pct_unknown"}
    if (not isinstance(summary, dict) or set(summary) - allowed
            or any(type(value) not in (int, bool) for value in summary.values())):
        raise PrivacyRejected("自动诊断摘要包含未批准字段")
    allowed_omissions = {"raw_logs", "config", "quotes", "targets", "camera", "screenshots", "all_real_user_data"}
    if (not isinstance(state["omitted"], list) or any(value not in allowed_omissions for value in state["omitted"])
            or not isinstance(state["truncated"], list)
            or any(value not in ("automatic_collection", "counters", "diagnostic_attachments") for value in state["truncated"])):
        raise PrivacyRejected("反馈裁剪标识未知")
    return state


def parse_uploaded_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PrivacyRejected("JSON 含重复字段")
            result[key] = value
        return result

    def invalid_constant(value):
        raise PrivacyRejected("JSON 含非有限数值")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)


def read_upload_archive(path, envelope=None):
    """Shared client/receiver allow-list, without rewriting sealed content."""
    validate_archive(path)
    try:
        with zipfile.ZipFile(path) as archive:
            limits = {"manifest.json": 64 * 1024, "state.json": 48 * 1024, "problem.txt": 16 * 1024}
            manifest = parse_uploaded_json(archive.read("manifest.json"))
            full = manifest.get("diagnostic_payload_version") == 2
            if not full and Path(path).stat().st_size > 256 * 1024:
                raise PrivacyRejected("旧版精简反馈超过原有大小限制")
            if not full and "diagnostic_attachments" in manifest.get("truncated", []):
                raise PrivacyRejected("旧版反馈不能声明完整附件裁剪")
            if full:
                limits.update({"manifest.json": 16 * 1024, "state.json": 32 * 1024})
                limits["diagnostics.zip"] = MAX_DIAGNOSTIC_BYTES
            if set(archive.namelist()) != set(limits) or any(
                    archive.getinfo(name).file_size > limit for name, limit in limits.items()):
                raise PrivacyRejected("反馈包包含未批准或过大的附件")
            if full and archive.getinfo("diagnostics.zip").compress_type != zipfile.ZIP_STORED:
                raise PrivacyRejected("完整诊断附件必须原字节存放，不重复压缩")
            required = {"schema_version", "diagnostic_payload_version", "app_version", "bundle_id", "run_id",
                        "capture_started_at", "capture_completed_at", "files", "omitted", "truncated"}
            if (set(manifest) != required or any(set(entry) != {"name", "size_bytes", "sha256"}
                                              or type(entry["size_bytes"]) is not int
                                              for entry in manifest["files"])
                    or type(manifest["schema_version"]) is not int
                    or type(manifest["diagnostic_payload_version"]) is not int):
                raise PrivacyRejected("反馈清单包含未批准字段")
            state_text = archive.read("state.json").decode("utf-8")
            state = validate_uploaded_snapshot(parse_uploaded_json(state_text))
            if not full and "diagnostic_attachments" in state["truncated"]:
                raise PrivacyRejected("旧版反馈不能声明完整附件裁剪")
            if any(manifest[key] != state[key] for key in ("app_version", "bundle_id", "run_id")):
                raise PrivacyRejected("反馈清单与状态身份不一致")
            if full:
                validate_capture_context(manifest, state)
            validate_uploaded_snapshot({**state, **{key: manifest[key] for key in (
                "capture_started_at", "capture_completed_at", "omitted", "truncated")}})
            description = archive.read("problem.txt").decode("utf-8")
            if len(description) > MAX_DESCRIPTION:
                raise PrivacyRejected("问题描述过长")
            check_privacy(description)
            diagnostic_bytes = archive.read("diagnostics.zip") if full else None
            if full:
                diagnostic_files = read_diagnostics(diagnostic_bytes)
                if diagnostic_files["export.json"].get("version") != state["app_version"]:
                    raise PrivacyRejected("完整诊断与反馈应用版本不一致")
        if envelope is not None:
            names = {"schema_version", "diagnostic_payload_version", "idempotency_key", "archive_sha256",
                     "size_bytes", "consent_policy_version", "app_version", "outbox_id"}
            if (not isinstance(envelope, dict) or set(envelope) != names
                    or type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1
                    or type(envelope["diagnostic_payload_version"]) is not int
                    or envelope["diagnostic_payload_version"] != manifest["diagnostic_payload_version"]
                    or type(envelope["idempotency_key"]) is not str or type(envelope["outbox_id"]) is not str
                    or not re.fullmatch(r"[a-f0-9]{32}", str(envelope["idempotency_key"]))
                    or not re.fullmatch(r"[a-f0-9]{32}", str(envelope["outbox_id"]))
                    or envelope["consent_policy_version"] != (FULL_CONSENT_VERSION if full else CONSENT_VERSION)
                    or envelope["app_version"] != state["app_version"]
                    or type(envelope["size_bytes"]) is not int or envelope["size_bytes"] != Path(path).stat().st_size
                    or envelope["archive_sha256"] != hashlib.sha256(Path(path).read_bytes()).hexdigest()):
                raise PrivacyRejected("反馈信封与实际上传内容不匹配")
        files: dict[str, str | bytes] = {"state.json": state_text, "problem.txt": description}
        if full:
            assert diagnostic_bytes is not None
            files["diagnostics.zip"] = diagnostic_bytes
        return {"manifest": manifest, "state": state, "files": files}
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PrivacyRejected("反馈包不符合当前上传协议") from exc


def validate_upload_receipt(receipt, envelope):
    checked = validate_receipt(receipt, envelope)
    if not isinstance(checked["received_at"], str):
        raise PrivacyRejected("回执时间格式不正确")
    try:
        received = datetime.fromisoformat(checked["received_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrivacyRejected("回执时间格式不正确") from exc
    if received.tzinfo is None:
        raise PrivacyRejected("回执时间缺少时区")
    required = {"case_id", "archive_sha256", "size_bytes", "received_at", "storage_status", "validation_status",
                "retention_until", "retention_policy", "repository", "asset_id", "consent_policy_version"}
    if envelope.get("diagnostic_payload_version") == 2:
        required.add("validation_scope")
        if checked.get("validation_scope") != "container_sha256_and_member_catalog":
            raise PrivacyRejected("完整诊断回执未说明容器校验范围")
    if (set(checked) != required or checked["case_id"] != "feedback-" + envelope["idempotency_key"]
            or checked["repository"] != FEEDBACK_REPOSITORY
            or type(checked["asset_id"]) is not int or not 0 < checked["asset_id"] <= 2 ** 53 - 1
            or checked["consent_policy_version"] != envelope["consent_policy_version"]
            or checked["consent_policy_version"] not in (CONSENT_VERSION, FULL_CONSENT_VERSION)
            or checked["retention_policy"] != "manual_deletion"
            or checked["retention_until"] is not None
            or checked["storage_status"] != "stored" or checked["validation_status"] != "validated"):
        raise PrivacyRejected("回执不属于当前批准的 GitHub 反馈")
    return checked
