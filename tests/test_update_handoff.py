"""Update handoff acknowledgement contract tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import sf_update
from tests._base import SFCase


class UpdateHandoffTests(SFCase):
    ACK_VERSION = ".".join(("1", "4", "104"))
    OTHER_VERSION = ".".join(("1", "4", "103"))

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="update-handoff-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stage = self.root / "updates"
        self.track_patch(mock.patch.object(sf_update, "UPDATE_STAGING_DIR", str(self.stage)))
        self.token = "a" * 32

    def test_matching_context_creates_ack_and_consumes_only_that_context(self):
        context = sf_update.write_handoff_context(self.token, expected_version=self.ACK_VERSION)
        self.assertTrue(context.exists())
        self.assertTrue(sf_update.acknowledge_pending_handoff(version=self.ACK_VERSION,
                                                               session_id="session",
                                                               handoff_id=self.token))
        self.assertFalse(context.exists())
        ack = sf_update.handoff_ack_path(self.token)
        self.assertEqual(__import__("json").loads(ack.read_text()), {
            "acknowledged_at": mock.ANY,
            "bundle_id": "com.skyrocket.app",
            "handoff_id": self.token,
            "session_id": "session",
            "version": self.ACK_VERSION,
        })

    def test_wrong_version_does_not_acknowledge_or_delete_context(self):
        context = sf_update.write_handoff_context(self.token, expected_version=self.ACK_VERSION)
        self.assertFalse(sf_update.acknowledge_pending_handoff(version=self.OTHER_VERSION))
        self.assertTrue(context.exists())
        self.assertFalse(sf_update.handoff_ack_path(self.token).exists())

    def test_wrong_bundle_does_not_acknowledge_or_delete_context(self):
        context = sf_update.write_handoff_context(self.token, expected_version=self.ACK_VERSION)
        self.assertFalse(sf_update.acknowledge_pending_handoff(version=self.ACK_VERSION,
                                                               bundle_id="com.other.app",
                                                               handoff_id=self.token))
        self.assertTrue(context.exists())
        self.assertFalse(sf_update.handoff_ack_path(self.token).exists())

    def test_unreadable_context_is_refused_without_creating_ack(self):
        context = sf_update.write_handoff_context(self.token, expected_version=self.ACK_VERSION)
        with mock.patch.object(type(context), "read_text", side_effect=OSError("gone")):
            self.assertFalse(sf_update.acknowledge_pending_handoff(version=self.ACK_VERSION,
                                                                    handoff_id=self.token))
        self.assertTrue(context.exists())
        self.assertFalse(sf_update.handoff_ack_path(self.token).exists())

    def test_context_path_rejects_an_invalid_identifier(self):
        with self.assertRaises(sf_update.UpdateError):
            sf_update.handoff_context_path("not-a-token")

    def test_malformed_context_is_refused_without_consuming_it(self):
        context = sf_update.write_handoff_context(self.token, expected_version=self.ACK_VERSION)
        context.write_text("{", encoding="utf-8")
        self.assertFalse(sf_update.acknowledge_pending_handoff(version=self.ACK_VERSION,
                                                               handoff_id=self.token))
        self.assertTrue(context.exists())
