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
        build_archive(path, {"state.json": json.dumps(state), "payload.sh": "not executable",
                             "problem.txt": description},
                      app_version="1.4.94", bundle_id="com.skyrocket.app", run_id="run-1")
        return path

    def _feedback_state(self, **changes):
        return {"phase": "user_feedback", "health": None, "run_id": "run-1",
                "app_version": "1.4.94", "capture_completed_at": "2026-10-20T01:40:04Z",
                "counts": {}, "summary": {}, **changes}

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



if __name__ == "__main__":
    unittest.main()
