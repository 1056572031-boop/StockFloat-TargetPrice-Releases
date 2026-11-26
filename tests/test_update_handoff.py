"""Update handoff acknowledgement contract tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sf_update
from stock_float_window import FloatWindow
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

    def test_modern_handoff_rejects_missing_or_invalid_sequence_and_size(self):
        for kwargs in (
            {"release_sequence": 17},
            {"archive_size": 1234},
            {"release_sequence": 0, "archive_size": 1234},
            {"release_sequence": 17, "archive_size": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with mock.patch.object(sf_update, "installed_bundle_path", return_value="/tmp/app"), \
                        mock.patch.object(sf_update.os.path, "isdir", return_value=True), \
                        mock.patch.object(sf_update, "bundled_helper_path", return_value="/tmp/helper"):
                    with self.assertRaises(sf_update.UpdateError):
                        sf_update.launch_installer("/tmp/archive.zip", "a" * 64,
                                                   self.ACK_VERSION, **kwargs)

    def test_modern_handoff_validates_protocol_then_forwards_sequence_and_size(self):
        target = self.root / "Skyrocket.app"
        target.mkdir()
        helper = self.root / "update_helper.sh"
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        for kwargs in (
            {"release_sequence": 17},
            {"archive_size": 1234},
            {"release_sequence": 0, "archive_size": 1234},
            {"release_sequence": 17, "archive_size": 0},
        ):
            with self.subTest(kwargs=kwargs), \
                    mock.patch.object(sf_update, "installed_bundle_path", return_value=str(target)), \
                    mock.patch.object(sf_update, "bundled_helper_path", return_value=str(helper)):
                with self.assertRaises(sf_update.UpdateError):
                    sf_update.launch_installer("/tmp/archive.zip", "a" * 64,
                                                self.ACK_VERSION, **kwargs)

        process = SimpleNamespace(pid=123)
        with mock.patch.object(sf_update, "installed_bundle_path", return_value=str(target)), \
                mock.patch.object(sf_update, "bundled_helper_path", return_value=str(helper)), \
                mock.patch.object(sf_update.subprocess, "Popen", return_value=process) as popen:
            launch = sf_update.launch_installer(
                "/tmp/archive.zip", "a" * 64, self.ACK_VERSION,
                release_sequence=17, archive_size=1234, handoff_id=self.token)
        command = popen.call_args.args[0]
        self.assertIn("--sequence", command)
        self.assertIn("17", command)
        self.assertIn("--size", command)
        self.assertIn("1234", command)
        self.assertEqual(launch.process, process)

    def test_modern_update_handoff_forwards_sequence_and_archive_size(self):
        quit_calls = []
        host = SimpleNamespace(
            _runtime_session=None,
            _runtime_session_id=None,
            quit=lambda: quit_calls.append("quit"),
            _show_update_message=lambda *args: self.fail("modern handoff failed: %r" % (args,)),
        )
        update = SimpleNamespace(
            archive_sha256="a" * 64,
            version=self.ACK_VERSION,
            release_sequence=17,
            archive_size=1234,
        )
        with mock.patch.object(sf_update, "launch_installer") as launch:
            FloatWindow._launch_downloaded_update(host, update, "/tmp/fixture.zip")  # discipline-ok(R5): direct lifecycle handler under test

        launch.assert_called_once_with(
            "/tmp/fixture.zip", "a" * 64, self.ACK_VERSION,
            release_sequence=17, archive_size=1234, handoff_id=mock.ANY)
        self.assertEqual(quit_calls, ["quit"])

    def test_malformed_context_is_refused_without_consuming_it(self):
        context = sf_update.write_handoff_context(self.token, expected_version=self.ACK_VERSION)
        context.write_text("{", encoding="utf-8")
        self.assertFalse(sf_update.acknowledge_pending_handoff(version=self.ACK_VERSION,
                                                               handoff_id=self.token))
        self.assertTrue(context.exists())
