"""Deterministic, offline analysis of a validated diagnostic feedback ZIP."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sf_feedback import sha256_file, validate_archive

ANALYZER_SCHEMA_VERSION = 1
ANALYZER_BUILD_ID = "skyrocket-feedback-analyzer-local-v1"
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


def _finding(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


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
    findings: list[dict[str, str]] = []
    state: dict[str, Any] | None = None
    with zipfile.ZipFile(archive) as package:
        members = set(package.namelist())
        missing = sorted(set(required) - members)
        for name in missing:
            findings.append(_finding("missing_member", "warning", f"required member omitted: {name}"))
        if "state.json" in members:
            try:
                state = json.loads(package.read("state.json").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                findings.append(_finding("invalid_state_json", "warning", f"state.json is not valid JSON: {exc}"))
            if isinstance(state, dict) and state.get("health") is False:
                findings.append(_finding("health_false", "warning", "diagnostic health snapshot reports false"))
    if missing or any(item["code"] == "invalid_state_json" for item in findings):
        conclusion = str(active_ruleset.get("missing_member_conclusion", "insufficient_evidence"))
    elif any(item["code"] == "health_false" for item in findings):
        conclusion = str(active_ruleset.get("health_false_conclusion", "suspected_problem"))
    else:
        conclusion = "no_known_issue"
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
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "machine": platform.machine(),
    }
    output = Path(output_dir)
    json_path = output / f"analysis-{run_id}.json"
    md_path = output / f"analysis-{run_id}.md"
    _atomic_text(json_path, json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    _atomic_text(md_path, "# Diagnostic Analysis\n\n"
                 f"- Conclusion: `{conclusion}`\n"
                 f"- Archive SHA-256: `{archive_hash}`\n"
                 f"- Analyzer build: `{analyzer_build_id}`\n\n"
                 + "\n".join(f"- {item['severity']}: {item['message']}" for item in findings)
                 + "\n")
    report["json_path"] = str(json_path)
    report["markdown_path"] = str(md_path)
    return report


def deterministic_view(report: dict[str, Any]) -> dict[str, Any]:
    """Remove run-time fields before comparing repeated analyses."""
    return {key: value for key, value in report.items()
            if key not in {"analysis_run_id", "analyzed_at", "machine", "json_path", "markdown_path"}}
