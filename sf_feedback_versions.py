"""Version declarations for feedback payload compatibility.

These constants are deliberately separate from the currently enabled upload
path.  v3 becomes sendable only after all protocol endpoints and the client
capture pipeline pass the v3 compatibility suite.
"""

from __future__ import annotations

PAYLOAD_V1 = 1
PAYLOAD_V2 = 2
PAYLOAD_V3 = 3
SUPPORTED_PAYLOADS = frozenset({PAYLOAD_V1, PAYLOAD_V2, PAYLOAD_V3})

POLICY_V1 = "github-private-feedback-v1"
POLICY_V2 = "github-private-feedback-full-v2"
POLICY_V3 = "github-private-feedback-full-v3"


def policy_for_payload(payload: int) -> str:
    if type(payload) is not int:
        raise ValueError("unsupported feedback payload")
    policies = {
        PAYLOAD_V1: POLICY_V1,
        PAYLOAD_V2: POLICY_V2,
        PAYLOAD_V3: POLICY_V3,
    }
    try:
        return policies[payload]
    except KeyError as exc:
        raise ValueError("unsupported feedback payload") from exc


def is_full_payload(payload: int) -> bool:
    return type(payload) is int and payload in (PAYLOAD_V2, PAYLOAD_V3)
