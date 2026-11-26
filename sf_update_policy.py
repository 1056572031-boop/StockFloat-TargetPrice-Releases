# -*- coding: utf-8 -*-
"""Pure forced-update policy rules with no Qt, IO, or host state access."""

from dataclasses import dataclass
from enum import Enum

from sf_update_policy_defaults import FORCE_GAP, PREFETCH_GAP


class PolicyState(str, Enum):
    LEGACY = "legacy"
    UP_TO_DATE = "up_to_date"
    REPLACE_OFFER = "replace_offer"
    ROLLBACK_OFFER = "rollback_offer"
    OFFER = "offer"
    PREPARE = "prepare"
    FORCE_PENDING = "force_pending"
    FORCE_READY = "force_ready"


@dataclass(frozen=True)
class PolicyInput:
    local_version: str
    local_sequence: int
    live_version: str
    live_sequence: int | None
    package_ready: bool
    manifest_verified_this_session: bool


@dataclass(frozen=True)
class PolicyDecision:
    state: PolicyState
    gap: int | None
    should_download: bool
    should_prompt: bool
    force: bool


def evaluate(input: PolicyInput, *, prefetch_gap: int = PREFETCH_GAP,
             force_gap: int = FORCE_GAP) -> PolicyDecision:
    if input.live_sequence is None:
        return PolicyDecision(PolicyState.LEGACY, None, False, False, False)
    gap = input.live_sequence - input.local_sequence
    if gap < 0:
        return PolicyDecision(PolicyState.ROLLBACK_OFFER, gap, False, True, False)
    if gap == 0 and input.live_version == input.local_version:
        return PolicyDecision(PolicyState.UP_TO_DATE, 0, False, False, False)
    if gap == 0:
        return PolicyDecision(PolicyState.REPLACE_OFFER, 0, False, True, False)
    if gap < prefetch_gap:
        return PolicyDecision(PolicyState.OFFER, gap, False, True, False)
    if gap < force_gap:
        return PolicyDecision(PolicyState.PREPARE, gap, True, False, False)
    if input.package_ready and input.manifest_verified_this_session:
        return PolicyDecision(PolicyState.FORCE_READY, gap, False, True, True)
    return PolicyDecision(PolicyState.FORCE_PENDING, gap, True, True, True)
