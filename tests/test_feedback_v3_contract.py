"""Pre-send compatibility contract for the planned feedback payload v3."""

from __future__ import annotations

import unittest

from tests._base import SFCase
from sf_constants import _FEATURE_DEFAULTS
from sf_logging import feature_enabled
from sf_feedback_versions import (
    PAYLOAD_V1,
    PAYLOAD_V2,
    PAYLOAD_V3,
    POLICY_V1,
    POLICY_V2,
    POLICY_V3,
    SUPPORTED_PAYLOADS,
    is_full_payload,
    policy_for_payload,
)
from tests.test_feedback_diagnostic_v2_contract import V2_FILES


class FeedbackV3CompatibilityContractTests(SFCase):
    def test_all_historical_and_new_payloads_have_distinct_policies(self):
        self.assertEqual(SUPPORTED_PAYLOADS, {PAYLOAD_V1, PAYLOAD_V2, PAYLOAD_V3})
        self.assertEqual(policy_for_payload(PAYLOAD_V1), POLICY_V1)
        self.assertEqual(policy_for_payload(PAYLOAD_V2), POLICY_V2)
        self.assertEqual(policy_for_payload(PAYLOAD_V3), POLICY_V3)
        self.assertEqual(len({POLICY_V1, POLICY_V2, POLICY_V3}), 3)

    def test_only_complete_payloads_contain_diagnostic_archives(self):
        self.assertFalse(is_full_payload(PAYLOAD_V1))
        self.assertTrue(is_full_payload(PAYLOAD_V2))
        self.assertTrue(is_full_payload(PAYLOAD_V3))

    def test_v3_is_an_additive_internal_diagnostic_contract(self):
        self.assertEqual(len(V2_FILES), 4)
        self.assertTrue(all(name.endswith(".json") for name in V2_FILES))
        self.assertNotIn("problem.txt", V2_FILES)
        self.assertNotIn("diagnostics.zip", V2_FILES)

    def test_v3_is_disabled_by_default(self):
        self.assertIs(_FEATURE_DEFAULTS["feedback_v3"], False)
        self.assertFalse(feature_enabled("feedback_v3"))

    def test_unknown_payloads_are_rejected_without_a_default_policy(self):
        for payload in (0, 4, -1, True, "3"):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    policy_for_payload(payload)


if __name__ == "__main__":
    unittest.main()
