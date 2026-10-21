"""Deterministic, offline analysis of a validated diagnostic feedback ZIP."""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import platform
import re
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from sf_feedback import sha256_file, validate_archive
from sf_feedback_bundle import read_diagnostics

ANALYZER_SCHEMA_VERSION = 3
ANALYZER_BUILD_ID = "skyrocket-feedback-analyzer-github-v7"
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


class _LogBucket(TypedDict):
    levels: dict[str, int]
    first_at: str | None
    last_at: str | None
    line_count: int


class _AnalysisFinding(TypedDict):
    code: str
    severity: str
    message: str
    scope: str


LOG_TIMELINE_LIMIT = 40
LOG_MESSAGE_LIMIT = 1000
_LOG_RECORD = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) ([\w-]{1,128})\] "
    r"(DEBUG|INFO|WARNING|ERROR|CRITICAL): (.*)$")


def _object(value):
    return value if isinstance(value, dict) else {}


def _full_analysis(files: dict[str, Any], run_id: Any) -> dict[str, Any]:
    """Attribute only explicit log identities; line counts are not incident counts."""
    run_id = run_id if isinstance(run_id, str) and 0 < len(run_id) <= 128 else None
    groups: dict[str, _LogBucket] = {
        scope: {"levels": {}, "first_at": None, "last_at": None, "line_count": 0}
        for scope in ("current", "historical", "unattributed")
    }
    timeline: list[dict[str, Any]] = []
    notify_error = False
    for name, text in sorted(files.items()):
        if not re.fullmatch(r"debug\.log(?:\.[1-3])?", name) or not isinstance(text, str):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            match = _LOG_RECORD.fullmatch(line)
            if match:
                stamp, identity, level, message = match.groups()
                try:
                    datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
                except ValueError:
                    match = None
            if not match:
                groups["unattributed"]["line_count"] += 1
                continue
            scope = "current" if run_id and identity == run_id else "historical" if run_id else "unattributed"
            bucket = groups[scope]
            bucket["line_count"] += 1
            levels = bucket["levels"]
            levels[level] = levels.get(level, 0) + 1
            first_at = bucket["first_at"]
            bucket["first_at"] = stamp if first_at is None else min(first_at, stamp)
            last_at = bucket["last_at"]
            bucket["last_at"] = stamp if last_at is None else max(last_at, stamp)
            if scope == "current" and level in ("WARNING", "ERROR", "CRITICAL"):
                notify_error |= bool(re.search(
                    r"notify (?:auth status|auth settings|auth query|auth center) (?:error|failed):|"
                    r"notify delivery_stats auth failed:", message))
                timeline.append({"at": stamp, "level": level, "source": name, "line": number,
                                 "message": message[:LOG_MESSAGE_LIMIT],
                                 "message_truncated": len(message) > LOG_MESSAGE_LIMIT})
                timeline.sort(key=lambda item: (item["at"], item["source"], item["line"]))
                timeline = timeline[-LOG_TIMELINE_LIMIT:]
    findings: list[_AnalysisFinding] = []

    def add(code: str, severity: str, message: str, scope: str) -> None:
        findings.append({"code": code, "severity": severity, "message": message, "scope": scope})

    warning_count = sum(groups["current"]["levels"].get(level, 0)
                        for level in ("WARNING", "ERROR", "CRITICAL"))
    if warning_count:
        add("current_log_signals", "warning",
            f"本次运行有 {warning_count} 行警告/错误；仅为候选线索，同一事件可能多次留痕，不能按行数计算事故。", "current")
    last = _object(files.get("last_failures.json"))
    last_run = last.get("run", last.get("run_id"))
    last_run = last_run if isinstance(last_run, str) and len(last_run) <= 128 else None
    failure_scope = ("current" if run_id and last_run == run_id else
                     "historical" if run_id and isinstance(last_run, str) and last_run else "unattributed")
    if last:
        add("last_failures_scope", "info",
            "last_failures 运行标识与本次不符，属于历史，不能当作本次故障。" if failure_scope == "historical"
            else "last_failures 仅为留痕记录，不证明当前仍然失效；无标识时无法归属。", failure_scope)
    notify = _object(files.get("notify.json"))
    if notify.get("authorization") == "unknown":
        add("notify_authorization_unknown", "warning" if notify_error else "info",
            "通知授权状态未知；本次运行有 auth status/settings/query/center error，不等于用户拒绝授权。"
            if notify_error else
            "通知授权状态未知；没有本次查询错误可证明故障，可能正在异步查询或尚未取得答案，原因尚不能确定。", "snapshot")
    runtime = _object(files.get("runtime.json"))
    face = _object(runtime.get("face_guard"))
    # Camera settings have appeared nested in different snapshot versions.
    settings = [face, _object(face.get("camera")), _object(face.get("capture")),
                _object(face.get("capture_settings")), _object(face.get("applied_profile"))]
    if any(item.get("fps_applied") is False for item in settings):
        add("face_guard_fps_not_applied", "info",
            "摄像头帧率设置未应用；不能据此认定界面卡顿或其根因。", "snapshot")
    performance = _object(runtime.get("performance"))
    raw_samples = performance.get("recent_samples")
    performance_samples = [item for item in raw_samples[-60:] if isinstance(item, dict)] if isinstance(raw_samples, list) else []
    performance_available = (
        performance.get("available") is True
        and performance.get("schema") == "performance.v1"
        and bool(performance_samples)
    )
    performance_summary: dict[str, Any] = {}
    if performance_available:
        for key in ("event_loop_lag_ms", "cpu_single_core_pct", "peak_rss_bytes"):
            values = [item.get(key) for item in performance_samples]
            numeric = [float(value) for value in values
                       if isinstance(value, (int, float)) and not isinstance(value, bool)
                       and math.isfinite(value) and value >= 0]
            performance_summary[key] = {"sample_count": len(numeric), "max": max(numeric) if numeric else None}
        performance_summary["window"] = _object(performance.get("window"))
        performance_summary["paint"] = {key: _object(performance.get("paint")).get(key)
                                         for key in ("count", "max_ms", "samples_discarded")}
    gaps = []
    if not performance_available:
        reasons = []
        if performance.get("available") is not True:
            reasons.append("available 不是 true")
        if performance.get("schema") != "performance.v1":
            reasons.append("schema 不是 performance.v1")
        if not isinstance(performance.get("recent_samples"), list) or not performance["recent_samples"]:
            reasons.append("recent_samples 缺失或为空")
        reason = performance.get("reason")
        if isinstance(reason, str) and reason:
            reasons.append("采集说明：" + reason[:200])
        gaps.append("未提供可用的 runtime.performance 性能证据（" + "、".join(reasons) + "）；"
                    "无法确认卡顿程度或原因。v1 字段仅表示事件循环延迟、单核 CPU、进程峰值 RSS 和绘制耗时；"
                    "峰值 RSS 不是当前 RSS，paint 也不是屏幕 FPS。")
    else:
        gaps.append("runtime.performance v1 样本可读；字段仅表示事件循环延迟、单核 CPU、进程峰值 RSS 和绘制耗时，"
                    "峰值 RSS 不是当前 RSS，paint 也不是屏幕 FPS，不能单独认定卡顿根因。")
    if not run_id or not groups["current"]["line_count"]:
        gaps.append("没有可归属本次 run_id 的日志，不能把历史或无标识日志当成本次故障。")
    issues = []
    for issue in files["export.json"]["issues"]:
        text = issue if isinstance(issue, str) else json.dumps(issue, ensure_ascii=False)
        if re.fullmatch(r"debug\.log\.[1-3]: missing", text):
            category = "optional_rotation_not_produced"
        elif text.startswith("last_failures.json: 无记录（说明，非故障："):
            category = "no_failure_record"
        elif re.search(r"truncat|截断|unreadable|PermissionError|OSError|invalid JSON|读取失败|read failure", text, re.IGNORECASE):
            category = "read_failure_or_truncation"
        elif re.fullmatch(r"[\w.]+: missing", text):
            category = "missing_evidence"
        else:
            category = "unknown"
        issues.append({"category": category, "issue": issue})
    return {"run_id": run_id, "logs": groups, "current_timeline": timeline,
            "timeline_omitted": max(0, warning_count - len(timeline)),
            "incident_count": None, "last_failures_scope": failure_scope,
            "last_failures_run_id": last_run, "findings": findings,
            "evidence_gaps": gaps, "performance_available": performance_available,
            "performance_summary": performance_summary,
            "collection_issue_classification": issues}


def diagnostic_evidence(content, run_id=None):
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
            "analysis": _full_analysis(files, run_id),
            "collection_issues": files["export.json"]["issues"],
            "limitation": "日志与崩溃为历史线索，不自动认定属于本次反馈；原文保留在完整诊断附件中。"}


def _readable_evidence(description, analysis):
    def safe(value, limit=1000):
        return html.escape(str(value)[:limit])

    text = "## 用户描述\n\n<pre>" + safe(description, 4000) + "</pre>\n\n## 本次线索\n\n"
    if not analysis:
        return text + "旧版精简反馈：仅有外层状态，未提供完整证据。\n\n## 历史\n\n历史计数不证明本次故障。\n\n## 证据不足\n\n规则未检出不等于健康。\n\n## 采集说明\n\n见原始采集声明。\n\n"
    for scope, label in (("current", "本次运行"), ("historical", "历史运行"), ("unattributed", "无法归属")):
        bucket = analysis["logs"][scope]
        text += (f"- {label}：{bucket['line_count']} 行；级别 {safe(bucket['levels'])}；"
                 f"时间 {safe(bucket['first_at'])} ～ {safe(bucket['last_at'])}（日志本地时间，非采集窗口）。\n")
    text += "\n警告/错误行数不等于事故次数；同一分时事件可能同时记录 WARNING 和 ERROR。\n"
    for finding in analysis["findings"]:
        if finding["scope"] not in ("historical", "unattributed"):
            text += "- " + safe(finding["message"]) + "\n"
    text += "\n<pre>" + "\n".join(
        safe(f"{item['at']} {item['level']} {item['source']}:{item['line']} {item['message']}", 1200)
        for item in analysis["current_timeline"]) + "</pre>\n"
    text += f"时间线另省略 {analysis['timeline_omitted']} 行，完整原文仍在附件。\n\n## 历史\n\n"
    text += "last_failures 归属：" + safe(analysis["last_failures_scope"]) + "；记录运行：" + safe(analysis["last_failures_run_id"]) + "。\n"
    for finding in analysis["findings"]:
        if finding["scope"] in ("historical", "unattributed"):
            text += "- " + safe(finding["message"]) + "\n"
    text += "\n历史及无标识材料不能升级为本次故障。\n\n## 性能证据\n\n"
    performance = analysis.get("performance_summary", {})
    if performance:
        labels = {"event_loop_lag_ms": "事件循环迟到（毫秒）", "cpu_single_core_pct": "单核 CPU（%）",
                  "peak_rss_bytes": "进程峰值 RSS（字节，非当前内存）"}
        for key, label in labels.items():
            value = performance.get(key, {})
            text += "- " + label + "：有效样本 " + safe(value.get("sample_count")) + "，最大值 " + safe(value.get("max")) + "。\n"
        text += "- 留存区间（单调时钟秒）及淘汰数：<code>" + safe(performance.get("window")) + "</code>\n"
        text += "- 绘制调用统计（不是屏幕 FPS）：<code>" + safe(performance.get("paint")) + "</code>\n"
    else:
        text += "没有可解释的性能采样；不能根据网络正常判断界面流畅。\n"
    text += "\n## 证据不足\n\n"
    text += "\n".join("- " + safe(gap) for gap in analysis["evidence_gaps"])
    text += "\n\n## 采集说明\n\nmissing 不自动代表产品故障；可选轮转日志缺失，可能未产生，读取失败/截断意味着证据不完整。原 issues 在 JSON 保留。\n"
    labels = {"optional_rotation_not_produced": "可选轮转日志缺失，可能未产生",
              "no_failure_record": "没有失败留痕（不等于健康）", "missing_evidence": "采集材料缺失",
              "read_failure_or_truncation": "读取失败或截断", "unknown": "未知，需人工核实"}
    for item in analysis["collection_issue_classification"][:40]:
        text += "- " + labels[item["category"]] + "：\n<pre>" + safe(item["issue"], 500) + "</pre>\n"
    if len(analysis["collection_issue_classification"]) > 40:
        text += "其余采集说明见 JSON。\n"
    return text + "\n"


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
    context_fields = ("app_version", "bundle_id", "run_id", "capture_started_at", "capture_completed_at", "omitted", "truncated")
    context_mismatches: list[str] = []
    with zipfile.ZipFile(archive) as package:
        members = set(package.namelist())
        if manifest["diagnostic_payload_version"] == 2:
            full_diagnostics = diagnostic_evidence(package.read("diagnostics.zip"), manifest.get("run_id"))
        if "problem.txt" in members:
            description = package.read("problem.txt").decode("utf-8", errors="replace")[:4000]
        missing = sorted(set(required) - members)
        for name in missing:
            findings.append(_finding("missing_member", "warning", f"required member omitted: {name}"))
        if "state.json" in members:
            try:
                state = json.loads(package.read("state.json").decode("utf-8"))
                if not isinstance(state, dict):
                    raise TypeError("state.json is not an object")
            except (UnicodeDecodeError, TypeError, ValueError) as exc:
                findings.append(_finding("invalid_state_json", "warning", f"state.json is not valid JSON: {exc}"))
            if isinstance(state, dict) and state.get("health") is False:
                findings.append(_finding("health_false", "warning", "diagnostic health snapshot reports false"))
            if isinstance(state, dict) and state.get("phase") == "user_feedback":
                context_mismatches = [key for key in context_fields if key in state and manifest.get(key) != state[key]]
                if context_mismatches:
                    findings.append(_finding("context_mismatch", "info", "清单与状态中的采集声明不一致：" + ", ".join(context_mismatches)))
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
    full_analysis = full_diagnostics["analysis"] if full_diagnostics else None
    if full_analysis:
        findings.extend(full_analysis["findings"])
    if missing or any(item["code"] in ("invalid_state_json", "missing_context", "context_mismatch") for item in findings):
        conclusion = str(active_ruleset.get("missing_member_conclusion", "insufficient_evidence"))
    elif any(item["code"] == "health_false" for item in findings):
        conclusion = str(active_ruleset.get("health_false_conclusion", "suspected_problem"))
    elif any(item["code"] in ("runtime_warning", "current_log_signals") for item in findings):
        conclusion = "suspected_problem"
    elif ((full_analysis and (full_analysis["findings"] or full_analysis["evidence_gaps"]
                              or full_analysis["collection_issue_classification"]))
          or any(item["code"] == "historical_counter" for item in findings)
          or (isinstance(state, dict) and state.get("phase") == "user_feedback"
              and state.get("health") is not True)):
        conclusion = "insufficient_evidence"
    else:
        conclusion = "no_known_issue"
    codes = {item["code"] for item in findings}
    evidence_basis = ("conflicting_context" if context_mismatches
                      else "current_snapshot" if codes & {"health_false", "runtime_warning"}
                      else "current_run_logs" if "current_log_signals" in codes
                      else "historical_only" if "historical_counter" in codes or (
                          full_analysis and full_analysis["last_failures_scope"] == "historical")
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
    state_context = ({key: state[key] for key in context_fields if key in state} if isinstance(state, dict) else {})
    summary = state.get("summary", {}) if isinstance(state, dict) else {}
    runtime_summary = {key: summary[key] for key in (
        "net_ok", "minute_stale", "price_is_fallback", "pct_unknown", "capture_issue_count",
    ) if isinstance(summary, dict) and key in summary and type(summary[key]) in (bool, int)}
    capture_issues = full_diagnostics["collection_issues"] if full_diagnostics else []
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
        "state_context": state_context,
        "context_mismatches": context_mismatches,
        "runtime_summary": runtime_summary,
        "capture_issue_count_outer": runtime_summary.get("capture_issue_count"),
        "capture_issue_count_inner": len(capture_issues),
        "capture_issue_count_scopes": {
            "outer": "state.json summary.capture_issue_count：采集流程汇总计数",
            "inner": "diagnostics.zip export.json issues：完整诊断附件问题条数",
        },
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
    conclusion_labels = {"suspected_problem": "发现待核实线索", "insufficient_evidence": "证据不足",
                         "no_known_issue": "规则未检出已知问题（不等于健康）"}
    _atomic_text(md_path, "# 诊断反馈分析\n\n"
                 + "- 分析结论：<code>" + html.escape(conclusion_labels.get(conclusion, conclusion)) + "</code>\n"
                 f"- 附件 SHA-256: `{archive_hash}`\n"
                 "- 分析器版本：<code>" + html.escape(str(analyzer_build_id)) + "</code>\n\n"
                 f"- 证据依据: `{evidence_basis}`\n"
                 "- 样本来源：未核实（需人工核实来源，不自动排除）\n"
                 f"- 发送者声明测试：`{test_declared}`（仅来自不可信描述）\n"
                 f"- 分析器机器（不是用户设备）：`{platform.machine()}`\n\n"
                 "## 来源上下文\n\n"
                 + "\n".join(f"- {key}: <code>{html.escape(str(value))}</code>"
                             for key, value in context.items())
                 + ("\n\n## 采集声明差异\n\n原始声明均保留，不自动改写，也不据此认定本次功能故障。"
                    "状态记录：<pre>" + html.escape(json.dumps(state_context, ensure_ascii=False, indent=2)) + "</pre>"
                    if context_mismatches else "")
                 + "\n\n" + _readable_evidence(description, full_analysis)
                 + "## 当前状态摘要\n\n"
                 + ("当前恢复快照（net_ok / minute_stale）：<code>"
                    + html.escape(str({key: runtime_summary[key] for key in ("net_ok", "minute_stale")
                                      if key in runtime_summary})) + "</code>\n"
                    + "外层采集问题计数：<code>" + html.escape(str(runtime_summary.get("capture_issue_count")))
                    + "</code>（state.json 汇总）；内层 issues：<code>" + html.escape(str(len(capture_issues)))
                    + "</code>（diagnostics.zip/export.json 条目，统计范围不同）\n"
                    + "\n".join(f"- {key}: `{value}`" for key, value in runtime_summary.items())
                    or "未提供可用的当前状态。")
                 + "\n\n## 规则线索（历史计数不代表本次故障）\n\n<pre>"
                 + "\n".join(
                     f"- {item['severity']} [{item['code']}]: {html.escape(item['message'])}"
                     + (f" (value={item['value']})" if "value" in item else "")
                     for item in findings) + "</pre>"
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
                 + "\n\n" + report["limitation"] + "\n")
    report["json_path"] = str(json_path)
    report["markdown_path"] = str(md_path)
    return report


def deterministic_view(report: dict[str, Any]) -> dict[str, Any]:
    """Remove run-time fields before comparing repeated analyses."""
    return {key: value for key, value in report.items()
            if key not in {"analysis_run_id", "analyzed_at", "machine", "json_path", "markdown_path"}}
