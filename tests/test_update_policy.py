# -*- coding: utf-8 -*-
"""Forced-update policy contract tests."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sf_constants import FORCE_GAP, PREFETCH_GAP
from sf_update_policy import PolicyDecision, PolicyInput, PolicyState, evaluate
from tests._base import SFCase


class TestUpdatePolicy(SFCase):
    def decision(self, gap, *, package_ready=False, verified=False,
                 live_version="1.4.119", local_version="1.4.118"):  # discipline-ok(R1): fixed policy fixtures
        return evaluate(PolicyInput(
            local_version=local_version,
            local_sequence=10,
            live_version=live_version,
            live_sequence=10 + gap,
            package_ready=package_ready,
            manifest_verified_this_session=verified,
        ))

    def test_policy_import_does_not_require_qt_or_mutate_environment(self):
        root = Path(__file__).resolve().parents[1]
        script = """
import builtins
import os

before = dict(os.environ)
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == "PyQt6" or name.startswith("PyQt6."):
        raise ModuleNotFoundError("PyQt6 intentionally blocked")
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
import sf_update_policy
assert dict(os.environ) == before
assert sf_update_policy.evaluate
"""
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=root, env=dict(os.environ),
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


    def test_legacy_manifest_keeps_old_path(self):
        decision = evaluate(PolicyInput(
            local_version="1.4.118", local_sequence=10,  # discipline-ok(R1): fixed policy fixture
            live_version="1.4.119", live_sequence=None,  # discipline-ok(R1): fixed legacy fixture
            package_ready=True, manifest_verified_this_session=True,
        ))
        self.assertEqual(decision, PolicyDecision(PolicyState.LEGACY, None, False, False, False))

    def test_zero_gap_same_version_is_up_to_date(self):
        self.assertEqual(self.decision(0, live_version="1.4.118"),  # discipline-ok(R1): fixed policy fixture

                         PolicyDecision(PolicyState.UP_TO_DATE, 0, False, False, False))

    def test_zero_gap_different_version_offers_replacement(self):
        self.assertEqual(self.decision(0),
                         PolicyDecision(PolicyState.REPLACE_OFFER, 0, False, True, False))

    def test_positive_gap_one_and_three_are_offers(self):
        for gap in (1, 3):
            with self.subTest(gap=gap):
                self.assertEqual(self.decision(gap),
                                 PolicyDecision(PolicyState.OFFER, gap, False, True, False))

    def test_positive_gap_four_and_seven_prepare_download(self):
        for gap in (4, 7):
            with self.subTest(gap=gap):
                self.assertEqual(self.decision(gap),
                                 PolicyDecision(PolicyState.PREPARE, gap, True, False, False))

    def test_positive_gap_eight_and_nine_are_forced(self):
        for gap in (8, 9):
            with self.subTest(gap=gap):
                self.assertEqual(self.decision(gap),
                                 PolicyDecision(PolicyState.FORCE_PENDING, gap, True, True, True))

    def test_lower_live_sequence_offers_rollback(self):
        self.assertEqual(self.decision(-1),
                         PolicyDecision(PolicyState.ROLLBACK_OFFER, -1, False, True, False))

    def test_force_is_ready_only_when_package_and_current_manifest_are_ready(self):
        self.assertEqual(self.decision(8, package_ready=True, verified=True),
                         PolicyDecision(PolicyState.FORCE_READY, 8, False, True, True))
        self.assertEqual(self.decision(8, package_ready=True, verified=False),
                         PolicyDecision(PolicyState.FORCE_PENDING, 8, True, True, True))
        self.assertEqual(self.decision(8, package_ready=False, verified=True),
                         PolicyDecision(PolicyState.FORCE_PENDING, 8, True, True, True))

    def test_thresholds_are_injected_only_for_rule_probes(self):
        decision = self.decision(2)
        self.assertEqual(evaluate(PolicyInput(  # discipline-ok(R1): fixed policy probe fixture

            local_version="1.4.118", local_sequence=10,  # discipline-ok(R1): fixed policy probe fixture
            live_version="1.4.119", live_sequence=12,  # discipline-ok(R1): fixed gap fixture
            package_ready=False, manifest_verified_this_session=False,
        ), prefetch_gap=2, force_gap=3),
            PolicyDecision(PolicyState.PREPARE, 2, True, False, False))
        self.assertEqual(PREFETCH_GAP, 4)
        self.assertEqual(FORCE_GAP, 8)
        self.assertEqual(decision.state, PolicyState.OFFER)


if __name__ == "__main__":
    unittest.main()
