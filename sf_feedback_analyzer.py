"""Deterministic, offline analysis of a validated diagnostic feedback ZIP."""

from __future__ import annotations

import hashlib
import html
import json
import os
import platform
import re
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sf_feedback import sha256_file, validate_archive
from sf_feedback_bundle import read_diagnostics

ANALYZER_SCHEMA_VERSION = 3
ANALYZER_BUILD_ID = "skyrocket-feedback-analyzer-github-v5"
DEFAULT_RULESET = {
    "required_members": ["state.json"],
    "health_false_conclusion": "suspected_problem",
    "missing_member_conclusion": "insufficient_evidence",
}


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError as exc:
            del exc


def _finding(code: str, severity: str, message: str) -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message}


def diagnostic_evidence(content):
    files = read_diagnostics(content)
    snapshots = {name: value for name, value in files.items() if name.endswith(".json")}
    text_evidence = {}
    for name, text in files.items():
        if not isinstance(text, str):
            continue
        lines = text.splitlines()
        relevant = [line for line in lines if re.search(r"\b(?:ERROR|WARNING|CRITICAL)\b", line)]
        if name.startswith("crash_reports/"):
            relevant = lines
        text_evidence[name] = {"line_count": len(lines), "matching_line_count": len(relevant),
                               "tail": [line[:1000] for line in relevant[-40:]],
                               "excerpt_truncated": len(relevant) > 40 or any(len(line) > 1000 for line in relevant[-40:])}
    return {"archive_sha256": hashlib.sha256(content).hexdigest(),
            "inventory": sorted(files), "snapshots": snapshots, "text_evidence": text_evidence,
            "collection_issues": files["export.json"]["issues"],
            "limitation": "日志与崩溃为历史线索，不自动认定属于本次反馈；原文保留在完整诊断附件中。"}


def analyze_archive(archive_path: os.PathLike[str] | str, output_dir: os.PathLike[str] | str,
                    *, ruleset: dict[str, Any] | None = None,
                    analyzer_build_id: str = ANALYZER_BUILD_ID) -> dict[str, Any]:
    """Analyze one archive without extracting or executing its members."""
    archive = Path(archive_path)
    manifest = validate_archive(archive)
    active_ruleset = dict(DEFAULT_RULESET if ruleset is None else ruleset)
    ruleset_hash = _canonical_hash(active_ruleset)
    archive_hash = sha256_file(archive)
    run_id = uuid.uuid4().hex
    required = [str(item) for item in active_ruleset.get("required_members", [])]
    findings: list[dict[str, Any]] = []
    state: dict[str, Any] | None = None
    description = ""
    full_diagnostics = None
    with zipfile.ZipFile(archive) as package:
        members = set(package.namelist())
        if manifest["diagnostic_payload_version"] == 2:
            full_diagnostics = diagnostic_evidence(package.read("diagnostics.zip"))
        if "problem.txt" in members:
            description = package.read("problem.txt").decode("utf-8", errors="replace")[:4000]
        missing = sorted(set(required) - members)
        for name in missing:
            findings.append(_finding("missing_member", "warning", f"required member omitted: {name}"))
        if "state.json" in members:
            try:
                state = json.loads(package.read("state.json").decode("utf-8"))
                if not isinstance(state, dict):
                    raise ValueError("state.json is not an object")
            except (UnicodeDecodeError, ValueError) as exc:
                findings.append(_finding("invalid_state_json", "warning", f"state.json is not valid JSON: {exc}"))
            if isinstance(state, dict) and state.get("health") is False:
                findings.append(_finding("health_false", "warning", "diagnostic health snapshot reports false"))
            if isinstance(state, dict) and state.get("phase") == "user_feedback":
                summary = state.get("summary", {})
                counts = state.get("counts", {})
                if isinstance(summary, dict):
                    for name, bad in (("net_ok", False), ("minute_stale", True),
                                      ("price_is_fallback", True), ("pct_unknown", True)):
                        if summary.get(name) is bad:
                            findings.append(_finding("runtime_warning", "warning", name))
                if isinstance(counts, dict):
                    for name, value in counts.items():
                        if type(value) is not int or value <= 0:
                            continue
                        if name.rsplit(".", 1)[-1] == "_secret_keys_dropped":
                            item = _finding("privacy_filter", "info", str(name)[:96])
                        elif any(word in name for word in ("fail", "error", "dropped")):
                            item = _finding("historical_counter", "warning", str(name)[:96])
                        else:
                            continue
                        item["value"] = value
                        findings.append(item)
                if not state.get("run_id") or not state.get("app_version") or not state.get("capture_completed_at"):
                    findings.append(_finding("missing_context", "warning", "session/version/time unavailable"))
    if missing or any(item["code"] in ("invalid_state_json", "missing_context") for item in findings):
        conclusion = str(active_ruleset.get("missing_member_conclusion", "insufficient_evidence"))
    elif any(item["code"] == "health_false" for item in findings):
        conclusion = str(active_ruleset.get("health_false_conclusion", "suspected_problem"))
    elif any(item["code"] == "runtime_warning" for item in findings):
        conclusion = "suspected_problem"
    elif (any(item["code"] == "historical_counter" for item in findings)
          or (isinstance(state, dict) and state.get("phase") == "user_feedback"
              and state.get("health") is not True)):
        conclusion = "insufficient_evidence"
    else:
        conclusion = "no_known_issue"
    codes = {item["code"] for item in findings}
    evidence_basis = ("current_snapshot" if codes & {"health_false", "runtime_warning"}
                      else "historical_only" if "historical_counter" in codes
                      else "no_fault_signal")
    # A sender's description is only a hint, never authority to exclude a case.
    test_declared = description.strip().startswith((
        "SYNTHETIC TEST ONLY.", "SYNTHETIC NATIVE UI TEST ONLY.",
        "SYNTHETIC PUBLIC HTTPS TEST ONLY.", "SYNTHETIC MAXIMUM LEGAL TEST ONLY.",
        "合成通知联调样本：不是用户报障",
    ))
    context = {key: manifest.get(key) for key in (
        "app_version", "bundle_id", "run_id", "capture_started_at", "capture_completed_at",
    )}
    context.update({key: manifest.get(key, []) for key in ("omitted", "truncated")})
    summary = state.get("summary", {}) if isinstance(state, dict) else {}
    runtime_summary = {key: summary[key] for key in (
        "net_ok", "minute_stale", "price_is_fallback", "pct_unknown", "capture_issue_count",
    ) if isinstance(summary, dict) and key in summary and type(summary[key]) in (bool, int)}
    report = {
        "analysis_schema_version": ANALYZER_SCHEMA_VERSION,
        "analysis_run_id": run_id,
        "archive_sha256": archive_hash,
        "archive_size_bytes": archive.stat().st_size,
        "diagnostic_payload_version": manifest["diagnostic_payload_version"],
        "source_manifest_schema_version": manifest["schema_version"],
        "analyzer_build_id": analyzer_build_id,
        "ruleset_sha256": ruleset_hash,
        "config_sha256": _canonical_hash({"required_members": required}),
        "conclusion": conclusion,
        "findings": findings,
        "evidence_basis": evidence_basis,
        "source_context": context,
        "runtime_summary": runtime_summary,
        "sample_classification": "unverified",
        "test_declared_by_sender": test_declared,
        "full_diagnostics": full_diagnostics,
        "user_description": description,
        "limitation": ("规则未检出不等于没有问题；历史计数不证明本次故障；隐私过滤数量不代表功能故障或泄露。"
                       "测试声明未经来源核实，不自动排除反馈；视觉问题需独立取证。"),
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "machine": platform.machine(),
    }
    output = Path(output_dir)
    json_path = output / f"analysis-{run_id}.json"
    md_path = output / f"analysis-{run_id}.md"
    _atomic_text(json_path, json.dumps(report, ensure_ascii=False, sort_keys=True,
                                    indent=None if full_diagnostics is not None else 2) + "\n")
    _atomic_text(md_path, "# Diagnostic Analysis\n\n"
                 f"- Conclusion: `{conclusion}`\n"
                 f"- Archive SHA-256: `{archive_hash}`\n"
                 f"- Analyzer build: `{analyzer_build_id}`\n\n"
                 f"- Evidence basis: `{evidence_basis}`\n"
                 "- Sample classification: `unverified` (需人工核实来源，不自动排除)\n"
                 f"- Sender declares test: `{test_declared}` (仅来自不可信描述)\n"
                 f"- Analyzer machine (not user device): `{platform.machine()}`\n\n"
                 "## Source Context\n\n"
                 + "\n".join(f"- {key}: <code>{html.escape(str(value))}</code>"
                             for key, value in context.items())
                 + "\n\n## Runtime Summary\n\n"
                 + ("\n".join(f"- {key}: `{value}`" for key, value in runtime_summary.items())
                    or "未提供可用的当前状态。")
                 + "\n\n## Findings\n\n"
                 + "\n".join(
                     f"- {item['severity']} [{item['code']}]: {html.escape(item['message'])}"
                     + (f" (value={item['value']})" if "value" in item else "")
                     for item in findings)
                 + ("\n\n隐私过滤：已按规则排除字段，不据此判断功能失败或泄露。"
                    if "privacy_filter" in codes else "")
                 + ("\n\n历史计数仅供排查，无法确认发生时间、是否恢复或与本次反馈的关系。"
                    if "historical_counter" in codes else "")
                 + ("\n\n问题描述为空：尚不知道用户做了什么、看到了什么、期望什么。"
                    if not description.strip() else "")
                 + ("\n\n## 完整诊断资料\n\n"
                    + "\n".join("- " + html.escape(name) for name in full_diagnostics["inventory"])
                    + "\n\n采集问题：<pre>" + html.escape(json.dumps(
                        full_diagnostics["collection_issues"], ensure_ascii=False, indent=2)) + "</pre>"
                    + "\n\n运行、网络、存储等原始快照及日志/崩溃摘要见同名JSON报告。"
                    + "\n\n" + full_diagnostics["limitation"]
                    if full_diagnostics is not None else "\n\n此为旧版精简反馈，不含完整诊断附件。")
                 + "\n\n## User Description (Untrusted Text)\n\n<pre>"
                 + html.escape(description) + "</pre>\n\n" + report["limitation"] + "\n")
    report["json_path"] = str(json_path)
    report["markdown_path"] = str(md_path)
    return report


def deterministic_view(report: dict[str, Any]) -> dict[str, Any]:
    """Remove run-time fields before comparing repeated analyses."""
    return {key: value for key, value in report.items()
            if key not in {"analysis_run_id", "analyzed_at", "machine", "json_path", "markdown_path"}}
