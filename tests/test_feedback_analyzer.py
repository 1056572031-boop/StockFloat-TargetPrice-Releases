import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from sf_feedback import build_archive
from sf_feedback_analyzer import analyze_archive, deterministic_view
from tests._base import SFCase


class FeedbackAnalyzerTests(SFCase):
    def _archive(self, root, state, description=""):
        path = root / "feedback.zip"
        context = state if isinstance(state, dict) else {}
        build_archive(path, {"state.json": json.dumps(state), "payload.sh": "not executable",
                             "problem.txt": description},
                      app_version="1.4.94", bundle_id="com.skyrocket.app", run_id="run-1",
                      omitted=context.get("omitted", []), truncated=context.get("truncated", []),
                      capture_started_at=context.get("capture_started_at", context.get("capture_completed_at")),
                      capture_completed_at=context.get("capture_completed_at"))
        return path

    def _feedback_state(self, **changes):
        return {"phase": "user_feedback", "health": None, "run_id": "run-1",
                "app_version": "1.4.94", "capture_completed_at": "2026-10-20T01:40:04Z",
                "counts": {}, "summary": {}, **changes}

    def test_historical_context_disagreement_is_preserved_and_never_becomes_a_current_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self._feedback_state(health=True, omitted=["raw_logs"])
            path = self._archive(root, state)
            with zipfile.ZipFile(path) as archive:
                files = {name: archive.read(name) for name in archive.namelist()}
            manifest = json.loads(files["manifest.json"])
            manifest["omitted"] = []
            files["manifest.json"] = json.dumps(manifest).encode()
            with zipfile.ZipFile(path, "w") as archive:
                for name, body in files.items():
                    archive.writestr(name, body)
            report = analyze_archive(path, root / "reports")
            self.assertEqual(report["context_mismatches"], ["omitted"])
            self.assertEqual(report["source_context"]["omitted"], [])
            self.assertEqual(report["state_context"]["omitted"], ["raw_logs"])
            self.assertEqual(report["conclusion"], "insufficient_evidence")
            self.assertEqual(report["evidence_basis"], "conflicting_context")
            self.assertIn("原始声明均保留", Path(report["markdown_path"]).read_text())

    def test_privacy_filter_count_is_information_not_a_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self._feedback_state(counts={"sf_update._secret_keys_dropped": 1})
            report = analyze_archive(self._archive(root, state), root / "reports")
            self.assertEqual(report["conclusion"], "insufficient_evidence")
            self.assertEqual(report["evidence_basis"], "no_fault_signal")
            self.assertEqual(report["findings"], [{"code": "privacy_filter", "severity": "info",
                                                   "message": "sf_update._secret_keys_dropped", "value": 1}])
            text = Path(report["markdown_path"]).read_text()
            self.assertIn("value=1", text)
            self.assertIn("问题描述为空", text)

    def test_privacy_filter_does_not_hide_real_failure_or_current_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for summary, conclusion in (({}, "insufficient_evidence"),
                                         ({"net_ok": False}, "suspected_problem")):
                with self.subTest(summary=summary):
                    state = self._feedback_state(
                        counts={"sf_update._secret_keys_dropped": 1, "quote_fail": 3}, summary=summary)
                    report = analyze_archive(self._archive(root, state), root / "reports")
                    self.assertEqual(report["conclusion"], conclusion)
                    self.assertIn({"code": "historical_counter", "severity": "warning",
                                   "message": "quote_fail", "value": 3}, report["findings"])

    def test_privacy_exception_is_exact_and_does_not_suppress_other_drops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("sf_update._secret_keys_dropped_error", "sf_update.data_dropped"):
                with self.subTest(name=name):
                    state = self._feedback_state(health=True, counts={name: 2})
                    report = analyze_archive(self._archive(root, state), root / "reports")
                    self.assertEqual(report["conclusion"], "insufficient_evidence")
                    self.assertEqual(report["evidence_basis"], "historical_only")
                    self.assertEqual(report["findings"][0]["code"], "historical_counter")

    def test_ordinary_non_positive_or_non_integer_counters_do_not_create_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self._feedback_state(health=True, counts={
                "zero_fail": 0, "negative_error": -1, "bool_error": True, "str_error": "3",
                "ordinary_count": 3})
            report = analyze_archive(self._archive(root, state), root / "reports")
            self.assertEqual(report["findings"], [])
            self.assertEqual(report["conclusion"], "no_known_issue")

    def test_sender_test_declaration_never_excludes_current_problem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for description, declared in (("SYNTHETIC TEST ONLY. No real user data.", True),
                                           ("合成通知联调样本：不是用户报障，不含真实用户数据。", True),
                                           ("测试时发现更新失败", False)):
                with self.subTest(description=description):
                    state = self._feedback_state(summary={"net_ok": False})
                    report = analyze_archive(self._archive(root, state, description), root / "reports")
                    self.assertEqual(report["test_declared_by_sender"], declared)
                    self.assertEqual(report["sample_classification"], "unverified")
                    self.assertEqual(report["conclusion"], "suspected_problem")
                    self.assertEqual(report["evidence_basis"], "current_snapshot")

    def test_report_preserves_archive_context_and_safe_runtime_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self._feedback_state(summary={"net_ok": True, "capture_issue_count": 2,
                                                  "minute_stale": "unknown", "private": "hidden"})
            archive = self._archive(root, state, "<script>not instructions</script>")
            with zipfile.ZipFile(archive) as package:
                manifest = json.loads(package.read("manifest.json"))
            report = analyze_archive(archive, root / "reports")
            self.assertEqual(report["source_context"]["app_version"], manifest["app_version"])
            self.assertEqual(report["source_context"]["run_id"], "run-1")
            self.assertTrue(report["source_context"]["capture_completed_at"])
            self.assertEqual(report["runtime_summary"], {"net_ok": True, "capture_issue_count": 2})
            saved = json.loads(Path(report["json_path"]).read_text())
            self.assertEqual(saved["source_context"], report["source_context"])
            text = Path(report["markdown_path"]).read_text()
            self.assertIn("&lt;script&gt;", text)
            self.assertNotIn("<script>", text)
            self.assertIn("capture_issue_count", text)
            self.assertIn(manifest["app_version"], text)

    def test_missing_context_still_blocks_a_current_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self._feedback_state(run_id="", summary={"net_ok": False})
            report = analyze_archive(self._archive(root, state), root / "reports")
            self.assertEqual(report["conclusion"], "insufficient_evidence")
            self.assertIn("missing_context", {item["code"] for item in report["findings"]})

    def test_repeated_analysis_has_same_conclusion_and_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self._archive(root, {"health": True})
            first = analyze_archive(archive, root / "reports")
            second = analyze_archive(archive, root / "reports")
            self.assertEqual(deterministic_view(first), deterministic_view(second))
            self.assertEqual(first["conclusion"], "no_known_issue")
            self.assertTrue(Path(first["json_path"]).is_file())
            self.assertTrue(Path(first["markdown_path"]).is_file())

    def test_false_health_is_suspected_and_content_is_never_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self._archive(root, {"health": False})
            report = analyze_archive(archive, root / "reports")
            self.assertEqual(report["conclusion"], "suspected_problem")
            self.assertIn("health_false", {item["code"] for item in report["findings"]})
            self.assertFalse((root / "payload.sh.was-run").exists())

    def test_missing_required_member_is_insufficient_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "feedback.zip"
            build_archive(archive, {"other.json": "{}"}, app_version="1.4.94",
                          bundle_id="com.skyrocket.app", run_id="run-1")
            report = analyze_archive(archive, root / "reports")
            self.assertEqual(report["conclusion"], "insufficient_evidence")
            self.assertIn("missing_member", {item["code"] for item in report["findings"]})

    def test_invalid_state_and_custom_ruleset_are_reported_deterministically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "invalid.zip"
            build_archive(archive, {"state.json": b"not-json"}, app_version="1.4.94",
                          bundle_id="com.skyrocket.app", run_id="run-1")
            report = analyze_archive(archive, root / "reports")
            self.assertEqual(report["conclusion"], "insufficient_evidence")
            self.assertIn("invalid_state_json", {item["code"] for item in report["findings"]})

            archive = self._archive(root, {"health": True})
            ruleset = {"required_members": ["state.json", "extra.json"],
                       "missing_member_conclusion": "custom-insufficient"}
            report = analyze_archive(archive, root / "custom", ruleset=ruleset,
                                     analyzer_build_id="test-analyzer")
            self.assertEqual(report["conclusion"], "custom-insufficient")
            self.assertEqual(report["analyzer_build_id"], "test-analyzer")
            self.assertIn("extra.json", report["findings"][0]["message"])

    def test_custom_health_conclusion_and_atomic_report_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self._archive(root, {"health": False})
            report = analyze_archive(archive, root / "reports",
                                     ruleset={"health_false_conclusion": "custom-problem"})
            self.assertEqual(report["conclusion"], "custom-problem")
            self.assertEqual(list((root / "reports").glob("*.tmp")), [])

    def test_ruleset_without_required_members_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self._archive(root, {"health": True})
            report = analyze_archive(archive, root / "reports", ruleset={"required_members": []})
            self.assertEqual(report["conclusion"], "no_known_issue")
            self.assertEqual(report["config_sha256"], report["config_sha256"])

    def test_invalid_ruleset_values_are_coerced_and_report_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self._archive(root, {"health": True})
            report = analyze_archive(archive, root / "reports",
                                     ruleset={"required_members": [1, None],
                                              "missing_member_conclusion": "custom-missing"})
            self.assertEqual(report["conclusion"], "custom-missing")
            self.assertEqual(report["findings"][0]["code"], "missing_member")


class FullEvidenceAnalyzerTests(SFCase):
    def analyze(self, root, extra=None, issues=None, description="用户看到卡顿"):
        from sf_constants import APP_VERSION, BUNDLE_ID
        from tests.test_feedback_full_bundle import diagnostic_bytes

        path = root / "feedback.zip"
        state = {"phase": "user_feedback", "health": None, "run_id": "run-1",
                 "app_version": APP_VERSION, "capture_completed_at": "2026-10-20T01:40:04Z"}
        build_archive(path, {"state.json": json.dumps(state), "problem.txt": description,
                             "diagnostics.zip": diagnostic_bytes(extra, issues)},
                      app_version=APP_VERSION, bundle_id=BUNDLE_ID, run_id="run-1",
                      capture_completed_at=state["capture_completed_at"])
        return analyze_archive(path, root / "reports")

    def log(self, run, message, level="WARNING", second=1):
        return f"[2026-10-20 09:40:{second:02d}.123 {run}] {level}: {message}\n"

    def test_rotated_current_logs_and_history_are_separated_by_identity_not_file_or_date(self):
        extra = {"debug.log": self.log("old-run", "old failure", "ERROR").encode(),
                 "debug.log.1": (self.log("run-1", "current warning") +
                                 self.log("run-1", "current error", "ERROR", 2)).encode(),
                 "last_failures.json": b'{"run":"old-run","errors":9}'}
        with tempfile.TemporaryDirectory() as directory:
            report = self.analyze(Path(directory), extra)
            analysis = report["full_diagnostics"]["analysis"]
            self.assertEqual(report["conclusion"], "suspected_problem")
            self.assertEqual(report["evidence_basis"], "current_run_logs")
            self.assertEqual(analysis["logs"]["current"]["line_count"], 2)
            self.assertEqual(analysis["logs"]["historical"]["line_count"], 1)
            self.assertEqual(analysis["last_failures_scope"], "historical")
            self.assertEqual([row["message"] for row in analysis["current_timeline"]],
                             ["current warning", "current error"])
            self.assertIsNone(analysis["incident_count"])

    def test_old_and_malformed_warnings_never_become_current_faults(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.analyze(Path(directory), {
                "debug.log": (self.log("old-run", "notify auth status error: old") +
                              "WARNING: no identity\n" +
                              "[2026-99-20 09:40:01.123 run-1] ERROR: invalid date\n").encode(),
                "last_failures.json": b'{"run":"old-run"}'})
            analysis = report["full_diagnostics"]["analysis"]
            self.assertEqual(report["conclusion"], "insufficient_evidence")
            self.assertEqual(report["evidence_basis"], "historical_only")
            self.assertEqual(analysis["current_timeline"], [])
            self.assertEqual(analysis["logs"]["unattributed"]["line_count"], 2)
            self.assertNotIn("current_log_signals", {row["code"] for row in report["findings"]})

    def test_unknown_notification_status_requires_current_log_for_api_explanation(self):
        with tempfile.TemporaryDirectory() as directory:
            for run, expected in (("old-run", "原因尚不能确定"), ("run-1", "不等于用户拒绝授权")):
                with self.subTest(run=run):
                    report = self.analyze(Path(directory), {
                        "debug.log": self.log(run, "notify auth status error: synthetic").encode(),
                        "notify.json": b'{"authorization":"unknown"}'})
                    finding = next(row for row in report["findings"]
                                   if row["code"] == "notify_authorization_unknown")
                    self.assertIn(expected, finding["message"])
                    if run == "old-run":
                        self.assertEqual(finding["severity"], "info")
                    else:
                        self.assertEqual(finding["severity"], "warning")

    def test_chinese_report_escapes_description_logs_and_collection_text(self):
        hostile = '</pre><script>alert(1)</script>\n# forged\n[click](https://example.invalid)'
        with tempfile.TemporaryDirectory() as directory:
            report = self.analyze(Path(directory), {"debug.log": self.log("run-1", hostile).encode()},
                                  issues=[hostile], description=hostile)
            text = Path(report["markdown_path"]).read_text()
            for heading in ("诊断反馈分析", "## 用户描述", "## 本次线索", "## 历史", "## 证据不足", "## 采集说明"):
                self.assertIn(heading, text)
            self.assertNotIn("<script>", text)
            self.assertIn("&lt;/pre&gt;&lt;script&gt;", text)
            self.assertEqual(report["user_description"], hostile)

    def test_collection_issues_are_classified_without_becoming_incidents(self):
        issues = ["debug.log.1: missing", "debug.log: missing", "runtime.json: unreadable",
                  "debug.log: source truncated", "calendar.json: synthetic read failure",
                  "last_failures.json: 无记录（说明，非故障：尚未产生）", {"unexpected": [1]}]
        with tempfile.TemporaryDirectory() as directory:
            report = self.analyze(Path(directory), issues=issues)
            analysis = report["full_diagnostics"]["analysis"]
            self.assertEqual([row["category"] for row in analysis["collection_issue_classification"]],
                             ["optional_rotation_not_produced", "missing_evidence", "read_failure_or_truncation",
                              "read_failure_or_truncation", "read_failure_or_truncation", "no_failure_record", "unknown"])
            self.assertEqual(report["full_diagnostics"]["collection_issues"], issues)
            self.assertEqual(report["conclusion"], "insufficient_evidence")

    def test_wrong_nested_types_and_camera_fps_are_not_latency_diagnoses(self):
        with tempfile.TemporaryDirectory() as directory:
            for runtime in (None, [], {"performance": []}, {"performance": {"samples": 3}},
                            {"face_guard": {"camera": {"fps_applied": False}}},
                            {"face_guard": {"camera": [False], "fps_applied": "false"}}):
                with self.subTest(runtime=runtime):
                    report = self.analyze(Path(directory), {
                        "runtime.json": json.dumps(runtime).encode(), "notify.json": b'[]',
                        "last_failures.json": b'{"run":["run-1"]}'})
                    analysis = report["full_diagnostics"]["analysis"]
                    self.assertEqual(report["conclusion"], "insufficient_evidence")
                    self.assertEqual(analysis["last_failures_scope"], "unattributed")
                    self.assertIsNone(analysis["last_failures_run_id"])
                    self.assertTrue(any("runtime.performance" in gap for gap in analysis["evidence_gaps"]))
                    available = (
                        isinstance(runtime, dict)
                        and isinstance(runtime.get("performance"), dict)
                        and runtime["performance"].get("available") is True
                        and runtime["performance"].get("schema") == "performance.v1"
                        and isinstance(runtime["performance"].get("recent_samples"), list)
                        and bool(runtime["performance"]["recent_samples"])
                    )
                    self.assertEqual(analysis["performance_available"], available)
                    if isinstance(runtime, dict) and runtime.get("face_guard", {}).get("camera") == {"fps_applied": False}:
                        self.assertIn("face_guard_fps_not_applied", {row["code"] for row in report["findings"]})
                        self.assertIn("不能据此认定界面卡顿", Path(report["markdown_path"]).read_text())

    def test_performance_summary_is_numeric_bounded_and_explains_units(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.analyze(Path(directory), {"runtime.json": json.dumps({
                "performance": {"available": True, "schema": "performance.v1",
                                "recent_samples": [{"event_loop_lag_ms": 12.0, "cpu_single_core_pct": 20,
                                                    "peak_rss_bytes": 1024},
                                                   {"event_loop_lag_ms": 30.0, "cpu_single_core_pct": "bad",
                                                    "peak_rss_bytes": True}],
                                "window": {"samples_discarded": 5}}}).encode()})
            summary = report["full_diagnostics"]["analysis"]["performance_summary"]
            self.assertEqual(summary["event_loop_lag_ms"], {"sample_count": 2, "max": 30.0})
            self.assertEqual(summary["cpu_single_core_pct"]["sample_count"], 1)
            self.assertEqual(summary["peak_rss_bytes"]["sample_count"], 1)
            text = Path(report["markdown_path"]).read_text()
            self.assertIn("事件循环迟到（毫秒）", text)
            self.assertIn("非当前内存", text)
            self.assertIn("不是屏幕 FPS", text)

    def test_unavailable_performance_reason_is_bounded_and_visible_in_report(self):
        reason = "monitor unavailable <script>" + "x" * 220
        with tempfile.TemporaryDirectory() as directory:
            report = self.analyze(Path(directory), {"runtime.json": json.dumps({
                "performance": {"available": False, "schema": "performance.v1",
                                "recent_samples": [], "reason": reason}}).encode()})
            analysis = report["full_diagnostics"]["analysis"]
            self.assertFalse(analysis["performance_available"])
            gap = next(item for item in analysis["evidence_gaps"] if "runtime.performance" in item)
            self.assertIn("采集说明：" + reason[:200], gap)
            self.assertNotIn(reason[:201], gap)
            self.assertIn("无法确认卡顿程度或原因", gap)
            text = Path(report["markdown_path"]).read_text()
            self.assertIn("采集说明：monitor unavailable &lt;script&gt;", text)
            self.assertNotIn("<script>", text)
            self.assertEqual(report["conclusion"], "insufficient_evidence")

    def test_collection_section_limits_display_but_preserves_all_issues_in_json(self):
        with tempfile.TemporaryDirectory() as directory:
            for count in (40, 41):
                with self.subTest(count=count):
                    issues = [f"material_{index:03d}.json: missing" for index in range(count)]
                    report = self.analyze(Path(directory), issues=issues)
                    saved = json.loads(Path(report["json_path"]).read_text())
                    self.assertEqual(saved["full_diagnostics"]["collection_issues"], issues)
                    classified = saved["full_diagnostics"]["analysis"]["collection_issue_classification"]
                    self.assertEqual([row["issue"] for row in classified], issues)
                    self.assertTrue(all(row["category"] == "missing_evidence" for row in classified))
                    text = Path(report["markdown_path"]).read_text()
                    section = text.split("## 采集说明\n", 1)[1].split("## 当前状态摘要", 1)[0]
                    for issue in issues[:40]:
                        self.assertIn(issue, section)
                    self.assertEqual("其余采集说明见 JSON。" in section, count > 40)
                    if count > 40:
                        self.assertNotIn(issues[-1], section)
                    self.assertEqual(report["conclusion"], "insufficient_evidence")

    def test_performance_schema_requires_available_schema_and_samples(self):
        from sf_feedback_analyzer import diagnostic_evidence
        from tests.test_feedback_full_bundle import diagnostic_bytes

        for performance in (
            {"available": True, "schema": "performance.v1", "recent_samples": []},
            {"available": False, "schema": "performance.v1", "recent_samples": [{"event_loop_lag_ms": 1}]},
            {"available": True, "schema": "performance.v2", "recent_samples": [{"event_loop_lag_ms": 1}]},
            {"available": True, "schema": "performance.v1", "recent_samples": [{"event_loop_lag_ms": 1}]},
        ):
            with self.subTest(performance=performance):
                content = diagnostic_bytes({"runtime.json": json.dumps({"performance": performance}).encode()})
                analysis = diagnostic_evidence(content, "run-1")["analysis"]
                self.assertEqual(analysis["performance_available"], bool(performance["recent_samples"]) and performance["available"] and performance["schema"] == "performance.v1")

        from sf_feedback_analyzer import diagnostic_evidence
        from tests.test_feedback_full_bundle import diagnostic_bytes

        content = diagnostic_bytes({"debug.log": self.log("run-1", "warning").encode(),
                                    "last_failures.json": b'{"run":"run-1"}'})
        for run in (None, "", [], {"run": "run-1"}, "x" * 129):
            with self.subTest(run=run):
                analysis = diagnostic_evidence(content, run)["analysis"]
                self.assertEqual(analysis["logs"]["current"]["line_count"], 0)
                self.assertEqual(analysis["logs"]["unattributed"]["line_count"], 1)
                self.assertEqual(analysis["current_timeline"], [])
                self.assertEqual(analysis["last_failures_scope"], "unattributed")
        analysis = diagnostic_evidence(content, "run-1")["analysis"]
        self.assertEqual(analysis["last_failures_scope"], "current")
        self.assertTrue(any("不证明当前仍然失效" in row["message"] for row in analysis["findings"]))

    def test_current_timeline_is_bounded_sorted_and_marks_omitted_lines(self):
        from sf_feedback_analyzer import LOG_MESSAGE_LIMIT, LOG_TIMELINE_LIMIT

        logs = "".join(self.log("run-1", "x" * (LOG_MESSAGE_LIMIT + 1), second=n)
                       for n in reversed(range(LOG_TIMELINE_LIMIT + 5)))
        with tempfile.TemporaryDirectory() as directory:
            report = self.analyze(Path(directory), {"debug.log": logs.encode()})
            analysis = report["full_diagnostics"]["analysis"]
            timeline = analysis["current_timeline"]
            self.assertEqual(len(timeline), LOG_TIMELINE_LIMIT)
            self.assertEqual(analysis["timeline_omitted"], 5)
            self.assertEqual([row["at"] for row in timeline], sorted(row["at"] for row in timeline))
            self.assertTrue(all(row["message_truncated"] for row in timeline))
            self.assertTrue(all(len(row["message"]) == LOG_MESSAGE_LIMIT for row in timeline))
            self.assertEqual(deterministic_view(report), deterministic_view(
                analyze_archive(Path(directory) / "feedback.zip", Path(directory) / "repeat")))


if __name__ == "__main__":
    unittest.main()
