import json
import tempfile
import unittest
from pathlib import Path

from sf_feedback import build_archive
from sf_feedback_analyzer import analyze_archive, deterministic_view
from tests._base import SFCase


class FeedbackAnalyzerTests(SFCase):
    def _archive(self, root, state):
        path = root / "feedback.zip"
        build_archive(path, {"state.json": json.dumps(state), "payload.sh": "not executable"},
                      app_version="1.4.94", bundle_id="com.skyrocket.app", run_id="run-1")
        return path

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
