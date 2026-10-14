"""Notification callback lifecycle and external-helper launch contracts.

No notification center or external process is created. Helper bytes are really
copied to an isolated staging directory, and callbacks are delivered through the
same delegate entry point as UN uses.
"""
from __future__ import annotations

import collections
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from _base import SFCase
except ImportError:  # pragma: no cover
    from tests._base import SFCase

import sf_notify as notify
import sf_update as update

install_delegate = notify._install_delegate  # discipline-ok(R5): UN creates this callback entry internally
notification_identifier = notify._notification_identifier  # discipline-ok(R5): response adapter under test
RELEASE_VERSION = "9.9.91"
DIGEST = "ab" * 32
HELPER_BYTES = b"#!/bin/sh\nexit 0\n"


class ObjectBase:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


def response(identifier):
    return types.SimpleNamespace(notification=lambda: types.SimpleNamespace(
        request=lambda: types.SimpleNamespace(identifier=lambda: identifier)))


class NotificationClickLifecycle(SFCase):
    def setUp(self):
        super().setUp()
        self.handlers = collections.OrderedDict()
        self.pending = []
        self.center = mock.Mock()
        self.track_patch(mock.patch.object(notify, "NSObject", ObjectBase))
        self.track_patch(mock.patch.object(notify, "_DELEGATE", None))
        self.track_patch(mock.patch.object(notify, "_CLICK_HANDLERS", self.handlers))
        self.track_patch(mock.patch.object(notify, "dispatch_get_main_queue", return_value="main"))
        self.track_patch(mock.patch.object(
            notify, "dispatch_async", side_effect=lambda queue, cb: self.pending.append((queue, cb))))
        self.log = mock.Mock()
        self.track_patch(mock.patch.object(notify, "log_line", self.log))
        install_delegate(self.center)
        self.delegate = self.center.setDelegate_.call_args.args[0]

    def click(self, ident, complete):
        self.delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
            self.center, response(ident), complete)

    def drain(self):
        queue, callback = self.pending.pop(0)
        self.assertEqual(queue, "main")
        callback()

    def test_delegate_is_a_singleton_and_click_restores_once_on_main_queue(self):
        events = []
        self.handlers["target"] = lambda: events.append("restore")
        install_delegate(self.center)
        self.center.setDelegate_.assert_called_once()
        self.click("target", lambda: events.append("complete"))
        self.assertEqual(events, [], "worker delivery must only schedule restoration")
        self.drain()
        self.assertEqual(events, ["restore", "complete"])
        self.assertEqual(self.handlers, {})
        self.click("target", lambda: events.append("complete"))
        self.drain()
        self.assertEqual(events, ["restore", "complete", "complete"])

    def test_unknown_notification_still_completes(self):
        complete = mock.Mock()
        self.click("unknown", complete)
        self.drain()
        complete.assert_called_once_with()

    def test_broken_restore_does_not_skip_completion_or_leave_stale_handler(self):
        self.handlers["target"] = mock.Mock(side_effect=RuntimeError("restore failed"))
        complete = mock.Mock()
        self.click("target", complete)
        self.drain()
        complete.assert_called_once_with()
        self.assertEqual(self.handlers, {})

    def test_completion_error_does_not_escape_native_delegate(self):
        complete = mock.Mock(side_effect=RuntimeError("completion failed"))
        self.click("unknown", complete)
        self.drain()
        complete.assert_called_once_with()

    def test_dispatch_error_completes_directly_without_running_restore(self):
        restored, complete = mock.Mock(), mock.Mock()
        self.handlers["target"] = restored
        with mock.patch.object(notify, "dispatch_async", side_effect=RuntimeError("queue lost")):
            self.click("target", complete)
        complete.assert_called_once_with()
        restored.assert_not_called()
        self.assertIn("target", self.handlers)

    def test_dispatch_and_completion_both_failing_remains_non_throwing(self):
        complete = mock.Mock(side_effect=RuntimeError("completion lost"))
        with mock.patch.object(notify, "dispatch_async", side_effect=RuntimeError("queue lost")):
            self.click("unknown", complete)
        complete.assert_called_once_with()

    def test_missing_completion_is_allowed(self):
        self.click("unknown", None)
        self.drain()

    def test_property_style_response_and_broken_identifier(self):
        property_response = types.SimpleNamespace(notification=types.SimpleNamespace(
            request=types.SimpleNamespace(identifier="property-id")))
        self.assertEqual(notification_identifier(property_response), "property-id")
        broken = types.SimpleNamespace(identifier=mock.Mock(side_effect=RuntimeError("gone")))
        broken_response = types.SimpleNamespace(notification=lambda: types.SimpleNamespace(
            request=lambda: broken))
        self.assertEqual(notification_identifier(broken_response), "")


class InstallerLaunchEdges(SFCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.app = self.root / "Installed.app"
        self.app.mkdir()
        self.helper = self.root / "original-helper.sh"
        self.helper.write_bytes(HELPER_BYTES)
        self.archive = self.root / "update.zip"
        self.archive.write_bytes(b"zip")
        self.staging = self.root / "staging"
        self.track_patch(mock.patch.object(update, "UPDATE_STAGING_DIR", str(self.staging)))
        self.track_patch(mock.patch.object(update, "bundled_helper_path", return_value=str(self.helper)))
        self.track_patch(mock.patch.object(update, "installed_bundle_path", return_value=str(self.app)))
        self.track_patch(mock.patch.object(update, "UPDATE_SIGNING_TEAM_ID", ""))
        self.track_patch(mock.patch.object(update, "UPDATE_SIGNING_CERT_SHA1", ""))
        self.process = mock.Mock()
        self.track_patch(mock.patch.object(update.subprocess, "Popen", self.process))

    def launch(self, version=RELEASE_VERSION):
        return update.launch_installer(str(self.archive), DIGEST, version)

    def test_real_helper_copy_is_private_and_detached_with_original_arguments(self):
        command = self.launch()
        copied = pathlib.Path(command[1])
        self.assertEqual(copied.read_bytes(), HELPER_BYTES)
        self.assertEqual(copied.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.helper.read_bytes(), HELPER_BYTES)
        self.assertEqual(command[0], "/bin/sh")
        args = dict(zip(command[2::2], command[3::2]))
        self.assertEqual(args["--archive"], str(self.archive))
        self.assertEqual(args["--sha256"], DIGEST)
        self.assertEqual(args["--target"], str(self.app))
        self.assertEqual(args["--pid"], str(os.getpid()))
        self.assertEqual(args["--result"], str(self.staging / "update-result.json"))
        self.process.assert_called_once_with(command, close_fds=True, start_new_session=True)

    def test_missing_target_refuses_before_staging(self):
        self.app.rmdir()
        with self.assertRaises(update.UpdateError):
            self.launch()
        self.process.assert_not_called()
        self.assertFalse(self.staging.exists())

    def test_missing_helper_refuses_before_staging(self):
        self.helper.unlink()
        with self.assertRaisesRegex(update.UpdateError, "助手不可用"):
            self.launch()
        self.process.assert_not_called()
        self.assertFalse(self.staging.exists())

    def test_malformed_version_cannot_start_a_process(self):
        with self.assertRaises(update.UpdateError):
            self.launch("not-a-version")
        self.process.assert_not_called()

    def test_partial_signing_identity_cannot_start_a_process(self):
        for key in ("UPDATE_SIGNING_TEAM_ID", "UPDATE_SIGNING_CERT_SHA1"):
            with self.subTest(key=key), mock.patch.object(update, key, "only-one-half"):
                with self.assertRaisesRegex(update.UpdateError, "配置不完整"):
                    self.launch()
        self.process.assert_not_called()

    def test_popen_failure_removes_copied_helper_and_preserves_original(self):
        self.process.side_effect = OSError("cannot spawn")
        with self.assertRaisesRegex(update.UpdateError, "无法启动"):
            self.launch()
        self.assertEqual(list(self.staging.glob("update_helper-*.sh")), [])
        self.assertEqual(self.helper.read_bytes(), HELPER_BYTES)

    def test_repeated_launches_have_distinct_helper_copies(self):
        with mock.patch.object(update.time, "time_ns", side_effect=[1234, 5678]):
            first, second = self.launch(), self.launch()
        self.assertNotEqual(first[1], second[1])
        self.assertEqual(len(list(self.staging.glob("update_helper-*.sh"))), 2)

    def test_helper_path_uses_frozen_resource_root_or_source_root(self):
        # Undo only this fixture's path adapter while testing the real path resolver.
        resolver = update.bundled_helper_path
        with mock.patch.object(update, "bundled_helper_path", ORIGINAL_HELPER_PATH), \
             mock.patch.object(sys, "_MEIPASS", str(self.root), create=True):
            self.assertEqual(update.bundled_helper_path(),
                             str(self.root / "updater" / "update_helper.sh"))
        self.assertIs(update.bundled_helper_path, resolver)

    def test_install_result_invalid_json_is_consumed_once(self):
        self.staging.mkdir()
        result = self.staging / "update-result.json"
        result.write_bytes(b"{broken")
        self.assertIsNone(update.consume_install_result())
        self.assertFalse(result.exists())
        self.assertIsNone(update.consume_install_result())


ORIGINAL_HELPER_PATH = update.bundled_helper_path


if __name__ == "__main__":
    unittest.main()
