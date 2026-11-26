import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import sf_update_cache
from sf_update import UpdateCancelled, UpdateInfo, UpdateFatal
from sf_update_cache import UpdateCache
from sf_update_flow import UpdateFlowController, UpdateFlowPort
from sf_update_policy import PolicyState
from tests._base import SFCase


class FakeCache:
    def __init__(self):
        self.ready = {}
        self.validated = []

    def validate_cached_archive(self, update):
        self.validated.append(update)
        return self.ready.get(update.archive_sha256)

    def accept_manifest_timestamp(self, published_at):
        return True

    def accept_manifest_identity(self, update):
        return self.accept_manifest_timestamp(update.published_at)

    def write_lock(self):
        from contextlib import nullcontext
        return nullcontext()


class FakePort:
    def __init__(self, cache):
        self.cache = cache
        self.local_version = "1.0.0"
        self.local_sequence = 10
        self.downloads = []
        self.cancels = []
        self.prompts = []
        self.messages = []
        self.installs = []
        self.checks = []

    def get_local_version(self):
        return self.local_version

    def get_local_sequence(self):
        return self.local_sequence

    def start_download(self, update, *, prompt_when_ready, show_progress, token=None):
        self.downloads.append((update, prompt_when_ready, show_progress))

    def cancel_download(self, update=None, *, token=None):
        self.cancels.append(update)

    def prompt_update(self, update, *, kind, generation=None, identity=None):
        self.prompts.append((update, kind))

    def show_download_progress(self):
        pass

    def show_update_message(self, title, body):
        self.messages.append((title, body))

    def install_update(self, update, archive):
        self.installs.append((update, archive))

    def request_check(self, manual):
        self.checks.append(manual)


class ForcedUpdateFlowMatrixTest(SFCase):
    def test_public_port_methods_are_abstract_until_host_implements_them(self):
        port = UpdateFlowPort()
        calls = (
            lambda: port.get_local_version(),
            lambda: port.get_local_sequence(),
            lambda: port.start_download(None, prompt_when_ready=False, show_progress=False),
            lambda: port.cancel_download(),
            lambda: port.prompt_update(None, kind="offer"),
            lambda: port.show_download_progress(),
            lambda: port.show_update_message("title", "body"),
            lambda: port.install_update(None, "archive"),
            lambda: port.request_check(True),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(NotImplementedError):
                    call()


    def setUp(self):
        super().setUp()
        self.cache = FakeCache()
        self.port = FakePort(self.cache)
        self.flow = UpdateFlowController(port=self.port, cache=self.cache)

    def update_for_gap(self, gap, *, sequence=None, digest=None):
        sequence = self.port.local_sequence + gap if sequence is None else sequence
        digest = digest or ("%064x" % gap)
        return UpdateInfo(
            "2.%d.0" % gap,
            "本版说明",
            "https://github.com/example/repo/releases/download/v/a.zip",
            8,
            digest,
            release_sequence=sequence,
            published_at="2026-11-19T00:00:00Z",
        )

    def test_policy_neutral_none_and_cached_force_ready_paths(self):
        self.assertIsNone(self.flow._evaluate())  # discipline-ok(R5): exercise unverified flow policy boundary
        update = self.update_for_gap(8)
        with patch("sf_update_policy.evaluate", return_value=None):
            self.assertIsNone(self.flow.on_manifest_verified(update, automatic=True))
        self.assertEqual(self.port.downloads, [])
        self.assertEqual(self.port.prompts, [])

        self.flow = UpdateFlowController(port=self.port, cache=self.cache)
        cached = self.update_for_gap(8, digest="f" * 64)
        self.cache.ready[cached.archive_sha256] = "/tmp/cached.zip"
        decision = self.flow.on_manifest_verified(cached, automatic=True)
        self.assertEqual(decision.state, PolicyState.FORCE_READY)
        self.assertEqual(self.port.prompts, [(cached, "force_ready")])
        self.assertEqual(self.port.downloads, [])

    def test_first_frame_with_unavailable_cache_never_claims_ready(self):
        update = self.update_for_gap(8)
        from dataclasses import replace
        update = replace(update, release_sequence=None)
        self.flow = UpdateFlowController(port=self.port, cache=None)
        self.flow.on_manifest_verified(update, automatic=False)
        self.flow.on_first_frame_ready()
        self.assertNotIn((update, "force_ready"), self.port.prompts)

    def test_start_failure_allows_retry_and_rejects_stale_terminal(self):
        update = self.update_for_gap(1)
        self.flow.on_manifest_verified(update, automatic=False)
        tokens = []

        def start(item, *, prompt_when_ready, show_progress, token=None):
            tokens.append(token)
            if len(tokens) == 1:
                raise TypeError("start failed")
            self.port.downloads.append((item, prompt_when_ready, show_progress))

        self.port.start_download = start
        with self.assertRaisesRegex(TypeError, "start failed"):
            self.flow.start_download(prompt_when_ready=True, show_progress=True)
        self.assertFalse(self.flow.diagnostics()["download_active"])
        self.assertIsNone(self.flow.download_token)
        self.assertTrue(self.flow.start_download(prompt_when_ready=True, show_progress=True))
        self.assertFalse(self.flow.on_download_finished(update, None, RuntimeError("late"), tokens[0]))
        self.assertIs(self.flow.download_token, tokens[1])
        self.assertEqual(self.port.downloads, [(update, True, True)])

    def test_prompt_failure_can_be_retried_without_duplicate_success(self):
        update = self.update_for_gap(8)
        attempts = []

        def prompt(item, *, kind, generation=None, identity=None):
            attempts.append(kind)
            if len(attempts) == 1:
                raise RuntimeError("dialog failed")
            self.port.prompts.append((item, kind))

        self.port.prompt_update = prompt
        with self.assertRaisesRegex(RuntimeError, "dialog failed"):
            self.flow.on_manifest_verified(update, automatic=True)
        self.flow.on_manifest_verified(update, automatic=True)
        self.flow.on_manifest_verified(update, automatic=True)
        self.assertEqual(attempts, ["force_pending", "force_pending"])
        self.assertEqual(self.port.prompts, [(update, "force_pending")])

    def test_automatic_force_start_failure_still_shows_required_prompt(self):
        update = self.update_for_gap(8)
        self.port.start_download = Mock(side_effect=RuntimeError("start failed"))
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            self.flow.on_manifest_verified(update, automatic=True)
        self.assertEqual(self.port.prompts, [(update, "force_pending")])
        self.assertFalse(self.flow.diagnostics()["download_active"])

    def test_force_terminal_prompt_failure_still_records_download_failure(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=True)
        token = self.flow.download_token
        self.port.prompt_update = Mock(side_effect=RuntimeError("dialog failed"))
        self.flow.on_download_finished(update, None, RuntimeError("network failed"), token)
        self.assertFalse(self.flow.diagnostics()["download_active"])
        self.assertTrue(self.port.messages)
        self.assertEqual(self.flow.diagnostics()["errors"]["last"]["category"], "download")

    def test_automatic_force_start_prompt_failure_preserves_start_error(self):
        update = self.update_for_gap(8)
        self.port.start_download = Mock(side_effect=RuntimeError("start failed"))
        self.port.prompt_update = Mock(side_effect=RuntimeError("dialog failed"))
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            self.flow.on_manifest_verified(update, automatic=True)

    def test_force_manual_start_failure_restores_prompt(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=False)
        self.port.start_download = Mock(side_effect=RuntimeError("start failed"))
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            self.flow.handle_force_action("开始下载", generation=self.flow.generation)
        self.assertEqual(self.port.prompts, [(update, "force_pending"), (update, "force_pending")])
        self.assertFalse(self.flow.diagnostics()["download_active"])

    def test_force_manual_start_reentry_does_not_replace_ready_prompt(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=False)
        generation = self.flow.generation

        def start(item, *, prompt_when_ready, show_progress, token=None):
            self.cache.ready[item.archive_sha256] = "/tmp/cached.zip"
            self.flow.on_first_frame_ready()
            raise RuntimeError("start failed")

        self.port.start_download = start
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            self.flow.handle_force_action("开始下载", generation=generation)
        self.assertEqual(self.flow.generation, generation)
        self.assertEqual(self.flow.state, PolicyState.FORCE_READY)
        self.assertEqual(self.port.prompts, [(update, "force_pending"), (update, "force_ready")])
        self.assertFalse(self.flow.diagnostics()["download_active"])

    def test_force_manual_start_and_prompt_failures_preserve_start_error(self):
        update = self.update_for_gap(8)
        start_error = RuntimeError("start failed")
        prompt_error = RuntimeError("prompt failed")
        swallowed = Mock()
        self.flow = UpdateFlowController(port=self.port, cache=self.cache, swallow=swallowed)
        self.flow.on_manifest_verified(update, automatic=False)
        self.port.start_download = Mock(side_effect=start_error)
        self.port.prompt_update = Mock(side_effect=prompt_error)
        with self.assertRaises(RuntimeError) as raised:
            self.flow.handle_force_action("开始下载", generation=self.flow.generation)
        self.assertIs(raised.exception, start_error)
        swallowed.assert_called_once_with("sf_update_flow.force_prompt_after_start", prompt_error)
        self.assertFalse(self.flow.diagnostics()["download_active"])

    def test_manual_fatal_download_reports_failure(self):
        update = self.update_for_gap(1)
        self.flow.on_manifest_verified(update, automatic=False)
        self.flow.start_download(prompt_when_ready=True, show_progress=True)
        token = self.flow.download_token
        self.flow.on_download_finished(update, None, UpdateFatal("disk"), token)
        self.assertTrue(self.port.messages)

    def test_pending_manifest_survives_second_cache_busy_after_download_finishes(self):
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(4)
        replacement = self.update_for_gap(5)
        self.flow.on_manifest_verified(original, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(replacement, automatic=True)
            self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), token)
        # Redelivery after the lock clears must not require another network check.
        self.flow.retry_pending_manifest()
        self.assertEqual(self.flow.state, PolicyState.PREPARE)
        self.assertEqual(self.port.downloads[-1][0], replacement)

    def test_pending_retry_rejects_older_verified_manifest_without_losing_newer_one(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(4)
        newer = replace(self.update_for_gap(5), published_at="2026-11-21T00:00:00Z")
        older = replace(self.update_for_gap(6), published_at="2026-11-20T00:00:00Z")
        self.flow.on_manifest_verified(original, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(newer, automatic=True)
            self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), token)
        self.flow.on_manifest_verified(older, automatic=True)
        self.flow.retry_pending_manifest()
        self.assertEqual(self.port.downloads[-1][0], newer)

    def test_replay_rejection_discards_pending_retry_and_notifies_manual_user(self):
        from sf_update_cache import UpdateCacheBusyError
        original, replacement = self.update_for_gap(4), self.update_for_gap(5)
        self.flow.on_manifest_verified(original, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(replacement, automatic=False)
            self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), token)
        with patch.object(self.cache, "accept_manifest_timestamp", return_value=False) as accept:
            self.flow.retry_pending_manifest()
            self.flow.retry_pending_manifest()
        self.assertEqual(accept.call_count, 1)
        self.assertTrue(self.port.messages)
        self.assertNotEqual(self.port.downloads[-1][0], replacement)

    def test_foreground_nonfatal_download_reports_failure(self):
        update = self.update_for_gap(1)
        self.flow.on_manifest_verified(update, automatic=False)
        self.flow.start_download(prompt_when_ready=True, show_progress=True)
        self.flow.on_download_finished(update, None, RuntimeError("offline"), self.flow.download_token)
        self.assertEqual(self.port.messages[-1][0], "更新未完成")

    def test_background_download_remains_silent_until_user_reveals_progress(self):
        update = self.update_for_gap(4)
        self.flow.on_manifest_verified(update, automatic=True)
        self.flow.on_download_finished(update, None, UpdateFatal("disk"), self.flow.download_token)
        self.assertEqual(self.port.messages, [])

    def test_revealed_background_download_reports_fatal_failure(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=True)
        self.flow.handle_force_action("开始下载")
        self.flow.on_download_finished(update, None, UpdateFatal("disk"), self.flow.download_token)
        self.assertTrue(self.port.messages)

    def test_legacy_manifest_during_pending_retry_does_not_crash_or_displace_newer(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original, pending = self.update_for_gap(4), self.update_for_gap(5)
        legacy = replace(self.update_for_gap(6), release_sequence=None, published_at=None)
        self.flow.on_manifest_verified(original, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(pending, automatic=True)
            self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), token)
        self.assertIsNone(self.flow.on_manifest_verified(legacy, automatic=True))
        self.flow.retry_pending_manifest()
        self.assertEqual(self.port.downloads[-1][0], pending)

    def test_pending_newer_same_identity_cannot_be_replaced_by_older_timestamp(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(4)
        newer = replace(self.update_for_gap(5), published_at="2026-11-21T00:00:00Z")
        older = replace(newer, published_at="2026-11-20T00:00:00Z")
        self.flow.on_manifest_verified(original, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(newer, automatic=True)
            self.flow.on_manifest_verified(older, automatic=True)
            self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), token)
        self.assertIsNone(self.flow.on_manifest_verified(older, automatic=True))
        self.flow.retry_pending_manifest()
        self.assertEqual(self.port.downloads[-1][0].published_at, newer.published_at)

    def test_newer_same_package_metadata_replaces_pending_when_cache_busy(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(4)
        pending = replace(self.update_for_gap(5), published_at="2026-11-20T00:00:00Z")
        newer = replace(pending, published_at="2026-11-21T00:00:00Z", notes="new notes")
        self.flow.on_manifest_verified(original, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(pending, automatic=True)
            self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), token)
            self.flow.on_manifest_verified(newer, automatic=True)
        self.flow.retry_pending_manifest()
        self.assertEqual(self.port.downloads[-1][0].published_at, newer.published_at)

    def test_newer_different_identity_is_retained_if_cache_still_busy_after_handoff(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(4)
        pending = replace(self.update_for_gap(5), published_at="2026-11-20T00:00:00Z")
        newer = replace(self.update_for_gap(6), published_at="2026-11-21T00:00:00Z")
        self.flow.on_manifest_verified(original, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(pending, automatic=True)
            self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), token)
            self.flow.on_manifest_verified(newer, automatic=True)
        self.flow.retry_pending_manifest()
        self.assertEqual(self.port.downloads[-1][0], newer)

    def test_newer_identity_waiting_on_busy_cache_invalidates_old_prompt_immediately(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(8)
        pending = replace(self.update_for_gap(9), published_at="2026-11-20T00:00:00Z")
        newer = replace(self.update_for_gap(10), published_at="2026-11-21T00:00:00Z")
        self.flow.on_manifest_verified(original, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(pending, automatic=True)
            self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), token)
            generation = self.flow.generation
            self.flow.on_manifest_verified(newer, automatic=True)
        self.assertGreater(self.flow.generation, generation)

    def test_newer_pending_manifest_during_active_download_cancels_worker_once(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(4)
        pending = replace(self.update_for_gap(5), published_at="2026-11-20T00:00:00Z")
        newer = replace(self.update_for_gap(6), published_at="2026-11-21T00:00:00Z")
        self.flow.on_manifest_verified(original, automatic=True)
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(pending, automatic=True)
            self.flow.on_manifest_verified(newer, automatic=True)
        self.assertEqual(self.port.cancels, [original])
        self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), self.flow.download_token)
        self.assertEqual(self.port.downloads[-1][0], newer)

    def test_pending_newer_manual_check_is_not_downgraded_to_automatic(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(4)
        pending = replace(self.update_for_gap(5), published_at="2026-11-20T00:00:00Z")
        newer = replace(self.update_for_gap(6), published_at="2026-11-21T00:00:00Z")
        self.flow.on_manifest_verified(original, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(pending, automatic=False)
            self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), token)
            self.flow.on_manifest_verified(newer, automatic=True)
        self.flow.retry_pending_manifest()
        self.assertIn((newer, "offer"), self.port.prompts)

    def test_pending_manual_check_survives_newer_automatic_result_when_lock_clears(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(4)
        pending = replace(self.update_for_gap(5), published_at="2026-11-20T00:00:00Z")
        newer = replace(self.update_for_gap(6), published_at="2026-11-21T00:00:00Z")
        self.flow.on_manifest_verified(original, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(pending, automatic=False)
            self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), token)
        self.flow.on_manifest_verified(newer, automatic=True)
        self.assertIn((newer, "offer"), self.port.prompts)

    def test_same_timestamp_different_identity_is_rejected_even_when_lock_is_free(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(4)
        pending = replace(self.update_for_gap(5), published_at="2026-11-20T00:00:00Z")
        conflicting = replace(self.update_for_gap(6), published_at=pending.published_at)
        self.flow.on_manifest_verified(original, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(pending, automatic=True)
            self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), token)
        self.assertIsNone(self.flow.on_manifest_verified(conflicting, automatic=True))
        self.flow.retry_pending_manifest()
        self.assertEqual(self.port.downloads[-1][0], pending)

    def test_pending_same_time_same_package_different_signed_content_is_rejected(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(4)
        pending = replace(self.update_for_gap(5), published_at="2026-11-20T00:00:00Z",
                          manifest_digest="a" * 64)
        conflicting = replace(pending, notes="other", manifest_digest="b" * 64)
        self.flow.on_manifest_verified(original, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(pending, automatic=True)
            self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), token)
        self.assertIsNone(self.flow.on_manifest_verified(conflicting, automatic=True))
        self.flow.retry_pending_manifest()
        self.assertEqual(self.port.downloads[-1][0], pending)

    def test_manual_stale_manifest_reports_failure_when_newer_is_pending(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(4)
        pending = replace(self.update_for_gap(5), published_at="2026-11-21T00:00:00Z")
        stale = replace(self.update_for_gap(6), published_at="2026-11-20T00:00:00Z")
        self.flow.on_manifest_verified(original, automatic=True)
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(pending, automatic=True)
            self.flow.on_manifest_verified(stale, automatic=False)
        self.assertEqual(self.port.messages[-1][0], "检查更新失败")

    def test_cancelled_older_terminal_does_not_process_pending_while_new_download_runs(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(4)
        pending = replace(self.update_for_gap(5), published_at="2026-11-20T00:00:00Z")
        newest = replace(self.update_for_gap(6), published_at="2026-11-21T00:00:00Z")
        self.flow.on_manifest_verified(original, automatic=True)
        old_token = self.flow.download_token
        self.flow.cancel_download()
        self.flow.on_manifest_verified(newest, automatic=True)
        new_token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            later = replace(pending, published_at="2026-11-22T00:00:00Z")
            self.flow.on_manifest_verified(later, automatic=True)
        self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), old_token)
        self.assertIs(self.flow.download_token, new_token)
        self.assertEqual(self.port.downloads[-1][0], newest)

    def test_duplicate_terminal_cannot_accept_an_unowned_none_token(self):
        update = self.update_for_gap(1)
        self.flow.on_manifest_verified(update, automatic=False)
        self.flow.start_download(prompt_when_ready=True, show_progress=True)
        token = self.flow.download_token
        self.flow.on_download_finished(update, None, UpdateFatal("disk"), token)
        messages = list(self.port.messages)
        self.assertFalse(self.flow.accepts_download_terminal(update, None))
        self.flow.on_download_finished(update, None, UpdateFatal("late"), None)
        self.assertEqual(self.port.messages, messages)

    def test_manual_conflicting_identity_reports_error(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(4)
        pending = replace(self.update_for_gap(5), published_at="2026-11-20T00:00:00Z")
        conflict = replace(self.update_for_gap(6), published_at=pending.published_at)
        self.flow.on_manifest_verified(original, automatic=True)
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(pending, automatic=True)
            self.flow.on_manifest_verified(conflict, automatic=False)
        self.assertEqual(self.port.messages[-1][0], "检查更新失败")

    def test_manual_check_retains_pending_and_schedules_retry_when_busy(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        original = self.update_for_gap(4)
        pending = replace(self.update_for_gap(5), published_at="2026-11-20T00:00:00Z")
        self.port.schedule_manifest_retry = Mock()
        self.flow.on_manifest_verified(original, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(pending, automatic=True)
            self.flow.on_download_finished(original, None, UpdateCancelled("cancel"), token)
            self.flow.on_manifest_verified(pending, automatic=False)
        self.assertGreaterEqual(self.port.schedule_manifest_retry.call_count, 1)
        self.flow.retry_pending_manifest()
        self.assertIn((pending, "offer"), self.port.prompts)

    def test_manual_recheck_of_same_offer_shows_prompt_again(self):
        update = self.update_for_gap(1)
        self.flow.on_manifest_verified(update, automatic=False)
        self.flow.on_manifest_verified(update, automatic=False)
        self.assertEqual(self.port.prompts, [(update, "offer"), (update, "offer")])

    def test_manual_recheck_of_active_same_download_reveals_progress_when_cache_busy(self):
        from sf_update_cache import UpdateCacheBusyError
        update = self.update_for_gap(4)
        self.port.show_download_progress = Mock()
        self.flow.on_manifest_verified(update, automatic=True)
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(update, automatic=False)
        self.port.show_download_progress.assert_called_once_with()
        self.assertEqual(self.port.messages, [])

    def test_gap_one_to_three_only_prompts_and_does_not_download(self):
        for gap in range(1, 4):
            with self.subTest(gap=gap):
                self.port.downloads.clear()
                self.port.prompts.clear()
                decision = self.flow.on_manifest_verified(
                    self.update_for_gap(gap), automatic=True)
                self.assertEqual(decision.state, PolicyState.OFFER)
                self.assertEqual(len(self.port.prompts), 1)
                self.assertEqual(self.port.downloads, [])

    def test_gap_four_to_seven_silently_prefetches_and_prompts_when_ready(self):
        for gap in range(4, 8):
            with self.subTest(gap=gap):
                self.port.downloads.clear()
                self.port.prompts.clear()
                update = self.update_for_gap(gap)
                decision = self.flow.on_manifest_verified(update, automatic=True)
                self.assertEqual(decision.state, PolicyState.PREPARE)
                self.assertEqual(self.port.downloads, [(update, True, False)])
                self.assertEqual(self.port.prompts, [])
                self.cache.ready[update.archive_sha256] = "/tmp/%s.zip" % gap
                self.flow.on_download_ready(update, "/tmp/%s.zip" % gap)
                self.assertEqual(self.port.prompts[-1][1], "ready")

    def test_reverified_same_identity_cancels_original_download_when_identity_changes(self):
        from dataclasses import replace
        initial = self.update_for_gap(4)
        self.flow.on_manifest_verified(initial, automatic=True)
        self.flow.on_manifest_verified(replace(initial), automatic=True)
        replacement = self.update_for_gap(5)
        self.flow.on_manifest_verified(replacement, automatic=True)
        self.assertIs(self.port.cancels[0], initial)

    def test_rejected_replay_invalidates_previous_prompt_and_ready_archive(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory(prefix="sf-replay-expire-") as root:
            cache = UpdateCache(Path(root))
            port = FakePort(cache)
            flow = UpdateFlowController(port=port, cache=cache)
            current = self.update_for_gap(8)
            with patch("sf_update_cache._file_matches", return_value=True):
                flow.on_manifest_verified(current, automatic=False)
                conflict = replace(current, archive_sha256="f" * 64)
                self.assertIsNone(flow.on_manifest_verified(conflict, automatic=False))
                self.assertFalse(flow.install_ready(), "拒绝冲突后不能安装旧提示的包")
                self.assertFalse(flow.start_download(prompt_when_ready=False, show_progress=False))
                self.assertEqual(port.installs, [])

    def test_busy_download_does_not_ignore_equal_time_different_manifest_digest(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        current = replace(self.update_for_gap(8), manifest_digest="a" * 64)
        conflicting = replace(current, manifest_digest="b" * 64)
        self.flow.on_manifest_verified(current, automatic=True)
        self.assertTrue(self.flow._impl._download_active)  # discipline-ok(R5): deterministic busy-download precondition
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(conflicting, automatic=False)
        self.assertFalse(self.flow.is_current(),
                         "同时间不同清单不能沿用旧会话")
        self.assertEqual(self.port.cancels, [current])

    def test_busy_download_queues_newer_same_package_manifest(self):
        from dataclasses import replace
        from sf_update_cache import UpdateCacheBusyError
        current = replace(self.update_for_gap(4), stage="beta", manifest_digest="a" * 64)
        promoted = replace(current, stage="stable", published_at="2026-11-20T00:00:00Z",
                           manifest_digest="b" * 64)
        self.flow.on_manifest_verified(current, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=UpdateCacheBusyError("busy")):
            self.assertIsNone(self.flow.on_manifest_verified(promoted, automatic=True))
        self.assertTrue(self.flow.has_pending_manifest, "较新签名清单必须等待缓存锁")
        self.flow.on_download_finished(current, None, UpdateCancelled("cancelled"), token)
        self.assertTrue(self.flow.is_current())
        self.assertEqual(self.flow.diagnostics()["update"]["stage"], "stable")

    def test_check_failure_cancels_active_download_before_clearing_session(self):
        update = self.update_for_gap(4)
        self.flow.on_manifest_verified(update, automatic=True)
        self.flow.on_check_failed(OSError("offline"), automatic=True)
        self.assertEqual(self.port.cancels, [update])
        self.assertIsNone(self.flow.state)

    def test_manual_check_at_current_version_reports_result(self):
        update = self.update_for_gap(0)
        update = UpdateInfo(self.port.local_version, update.notes, update.archive_url,
                            update.archive_size, update.archive_sha256,
                            release_sequence=self.port.local_sequence,
                            published_at=update.published_at)
        decision = self.flow.on_manifest_verified(update, automatic=False)
        self.assertEqual(decision.state, PolicyState.UP_TO_DATE)
        self.assertEqual(self.port.messages, [("检查更新", "当前已是最新版本。")])

    def test_older_signed_manifest_cannot_use_cached_package(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory(prefix="sf-replay-flow-") as root:
            cache = UpdateCache(Path(root))
            update = self.update_for_gap(8)
            newer = replace(update, published_at="2026-11-20T00:00:00Z")
            older = replace(update, published_at="2026-11-19T00:00:00Z")
            archive = cache.archive_path(older)
            archive.write_bytes(b"cached-data")
            with cache.write_lock():
                self.assertTrue(cache.accept_manifest_timestamp(newer.published_at))
            port = FakePort(cache)
            flow = UpdateFlowController(port=port, cache=cache)
            with patch("sf_update_cache._file_matches", return_value=True):
                decision = flow.on_manifest_verified(older, automatic=True)
            self.assertIsNone(decision)
            self.assertEqual(port.prompts, [])
            self.assertEqual(port.installs, [])
            self.assertIsNone(flow.state)

    def test_invalid_forced_ready_archive_returns_to_forced_pending(self):
        update = self.update_for_gap(8)
        self.cache.ready[update.archive_sha256] = "/tmp/ready.zip"
        self.flow.on_manifest_verified(update, automatic=False)
        self.assertEqual(self.flow.state, PolicyState.FORCE_READY)
        self.cache.ready.clear()
        self.assertFalse(self.flow.install_ready())
        self.assertEqual(self.flow.state, PolicyState.FORCE_PENDING)
        self.assertEqual(self.port.prompts[-1][1], "force_pending")
        self.assertEqual(self.port.installs, [])

    def test_busy_cache_recheck_preserves_running_download_and_identity(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=True)
        original_state = self.flow.state
        with patch.object(self.cache, "write_lock", side_effect=RuntimeError("busy")):
            self.assertIsNone(self.flow.on_manifest_verified(update, automatic=True))
        self.assertEqual(self.flow.state, original_state)
        self.assertEqual(self.port.cancels, [])
        self.assertEqual(len(self.port.downloads), 1)

    def test_replayed_manifest_does_not_consume_latest_session(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory(prefix="sf-replay-session-") as root:
            cache = UpdateCache(Path(root))
            port = FakePort(cache)
            flow = UpdateFlowController(port=port, cache=cache)
            latest = replace(self.update_for_gap(8), published_at="2026-11-20T00:00:00Z")
            older = replace(latest, published_at="2026-11-19T00:00:00Z")
            flow.on_manifest_verified(latest, automatic=False)
            self.assertIsNone(flow.on_manifest_verified(older, automatic=False))
            self.assertEqual(flow.state, PolicyState.FORCE_PENDING)
            self.assertEqual(len(port.prompts), 1)

    def test_newer_signed_rollback_is_offered_to_user(self):
        from dataclasses import replace
        rollback = replace(self.update_for_gap(1), release_sequence=self.port.local_sequence - 1)
        decision = self.flow.on_manifest_verified(rollback, automatic=False)
        self.assertEqual(decision.state, PolicyState.ROLLBACK_OFFER)
        self.assertEqual([kind for _, kind in self.port.prompts], ["rollback"])
        self.assertEqual(self.port.installs, [])

    def test_manual_check_reports_cache_lock_failure(self):
        update = self.update_for_gap(1)
        with patch.object(self.cache, "write_lock", side_effect=RuntimeError("busy")):
            self.assertIsNone(self.flow.on_manifest_verified(update, automatic=False))
        self.assertEqual(self.port.messages, [("检查更新失败", "暂时无法检查更新。")])
        self.assertIsNone(self.flow.state)

    def test_clicked_force_pending_prompt_is_restored_after_cancelled_download(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=False)
        self.assertTrue(self.flow.handle_force_action("开始下载"))
        token = self.flow.download_token
        self.flow.on_download_finished(update, None, UpdateCancelled("cancelled"), token)
        self.assertEqual(self.flow.state, PolicyState.FORCE_PENDING)
        self.assertEqual([kind for _, kind in self.port.prompts], ["force_pending", "force_pending"])

    def test_failed_forced_handoff_restores_install_or_exit_prompt(self):
        update = self.update_for_gap(8)
        self.cache.ready[update.archive_sha256] = "/tmp/ready.zip"
        self.flow.on_manifest_verified(update, automatic=False)
        self.port.install_update = lambda update, archive: False
        self.assertFalse(self.flow.install_ready())
        self.assertEqual([kind for _, kind in self.port.prompts], ["force_ready", "force_ready"])

    def test_new_verified_identity_waits_for_busy_download_then_switches(self):
        from contextlib import nullcontext
        old, new = self.update_for_gap(4), self.update_for_gap(8)
        self.flow.on_manifest_verified(old, automatic=True)
        old_token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=sf_update_cache.UpdateCacheBusyError("busy")):
            self.assertIsNone(self.flow.on_manifest_verified(new, automatic=True))
        self.assertEqual(self.port.cancels, [old])
        self.assertEqual(len(self.port.downloads), 1)
        with patch.object(self.cache, "write_lock", return_value=nullcontext()):
            self.flow.on_download_finished(old, None, UpdateCancelled("cancelled"), old_token)
        self.assertEqual(self.flow.state, PolicyState.FORCE_PENDING)
        self.assertEqual(self.port.downloads[-1][0], new)

    def test_busy_recheck_prevents_old_ready_package_install(self):
        old, new = self.update_for_gap(8), self.update_for_gap(9)
        self.flow.on_manifest_verified(old, automatic=True)
        self.cache.ready[old.archive_sha256] = "/tmp/old.zip"
        with patch.object(self.cache, "write_lock", side_effect=sf_update_cache.UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(new, automatic=True)
        self.assertFalse(self.flow.install_ready())
        self.assertEqual(self.port.installs, [])

    def test_pending_recheck_invalidates_old_dialog_generation_immediately(self):
        old, new = self.update_for_gap(8), self.update_for_gap(9)
        self.flow.on_manifest_verified(old, automatic=True)
        old_generation = self.flow.generation
        with patch.object(self.cache, "write_lock", side_effect=sf_update_cache.UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(new, automatic=True)
        self.assertFalse(self.flow.is_current(generation=old_generation))
        self.assertFalse(self.flow.handle_force_action("开始下载", generation=old_generation))

    def test_older_manifest_does_not_replace_newer_pending_manifest(self):
        from dataclasses import replace
        from contextlib import nullcontext
        old = replace(self.update_for_gap(4), published_at="2026-11-19T00:00:00Z")
        latest = replace(self.update_for_gap(8), published_at="2026-11-21T00:00:00Z")
        stale = replace(self.update_for_gap(9), published_at="2026-11-20T00:00:00Z")
        self.flow.on_manifest_verified(old, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=sf_update_cache.UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(latest, automatic=True)
            self.flow.on_manifest_verified(stale, automatic=True)
        with patch.object(self.cache, "write_lock", return_value=nullcontext()):
            self.flow.on_download_finished(old, None, UpdateCancelled("cancelled"), token)
        self.assertIs(self.port.downloads[-1][0], latest)

    def test_busy_switch_cancels_real_worker_before_applying_new_manifest(self):
        import threading
        from dataclasses import replace
        from sf_update_controller import UpdateController
        started, finished = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory(prefix="sf-switch-real-") as root:
            cache = UpdateCache(Path(root))
            old = replace(self.update_for_gap(4), published_at="2026-11-19T00:00:00Z")
            new = replace(self.update_for_gap(8), published_at="2026-11-20T00:00:00Z")
            messages = []
            backend = UpdateController(messages.append, lambda: "1.0.0")
            self.addCleanup(backend.stop)
            port = FakePort(cache)
            def start(update, *, prompt_when_ready, show_progress, token=None):
                port.downloads.append((update, prompt_when_ready, show_progress))
                backend.download(update, threading.Event(), token)
            port.start_download = start
            port.cancel_download = backend.cancel_download
            flow = UpdateFlowController(port=port, cache=cache)
            def download(update, cancel_event, progress_callback):
                if update is old:
                    with UpdateCache(Path(root)).write_lock():
                        started.set()
                        if not cancel_event.wait(3):
                            raise RuntimeError("cancel was not received")
                    finished.set()
                    raise UpdateCancelled("cancelled")
                return "/tmp/new.zip"
            with patch("sf_update.download_update", side_effect=download):
                flow.on_manifest_verified(old, automatic=True)
                self.assertTrue(started.wait(2))
                self.assertIsNone(flow.on_manifest_verified(new, automatic=True))
                self.assertTrue(finished.wait(2))
                from tests._base import wait_for
                self.assertTrue(wait_for(lambda: any(item[0] == "update_download" for item in messages)))
                old_message = next(item for item in messages if item[0] == "update_download")
                flow.on_download_finished(*old_message[1])
                self.assertEqual(flow.state, PolicyState.FORCE_PENDING)
                self.assertIs(port.downloads[-1][0], new)

    def test_queued_replacement_cancelled_for_new_manifest_receives_terminal(self):
        import threading
        from dataclasses import replace
        from sf_update_controller import UpdateController
        from tests._base import wait_for
        started, release = threading.Event(), threading.Event()
        messages = []
        backend = UpdateController(messages.append, lambda: "1.0.0")
        self.addCleanup(backend.stop)
        self.addCleanup(release.set)
        original = replace(self.update_for_gap(4), published_at="2026-11-19T00:00:00Z")
        queued = replace(self.update_for_gap(5), published_at="2026-11-20T00:00:00Z")
        newest = replace(self.update_for_gap(6), published_at="2026-11-21T00:00:00Z")
        port = FakePort(self.cache)
        def start(update, *, prompt_when_ready, show_progress, token=None):
            port.downloads.append((update, prompt_when_ready, show_progress))
            backend.download(update, threading.Event(), token)
        port.start_download = start
        port.cancel_download = backend.cancel_download
        flow = UpdateFlowController(port=port, cache=self.cache)
        def download(update, cancel_event, progress_callback):
            if update is original:
                started.set()
                if not release.wait(3):
                    raise RuntimeError("test worker timeout")
                raise UpdateCancelled("cancelled")
            return "/tmp/ready.zip"
        with patch("sf_update.download_update", side_effect=download):
            flow.on_manifest_verified(original, automatic=True)
            self.assertTrue(started.wait(2))
            flow.cancel_download()
            flow.on_manifest_verified(queued, automatic=True)
            with patch.object(self.cache, "write_lock", side_effect=sf_update_cache.UpdateCacheBusyError("busy")):
                flow.on_manifest_verified(newest, automatic=True)
            release.set()
            self.assertTrue(wait_for(lambda: len([m for m in messages if m[0] == "update_download"]) >= 2, timeout=3))
            for kind, payload in list(messages):
                if kind == "update_download":
                    flow.on_download_finished(*payload)
            self.assertEqual(port.downloads[-1][0], newest)

    def test_click_on_running_forced_download_shows_progress_without_restart(self):
        update = self.update_for_gap(8)
        self.port.show_download_progress = Mock()
        self.flow.on_manifest_verified(update, automatic=True)
        self.assertTrue(self.flow.handle_force_action("开始下载"))
        self.port.show_download_progress.assert_called_once_with()
        self.assertEqual(len(self.port.downloads), 1)

    def test_download_success_does_not_enter_force_ready_without_cache_verification(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=True)
        token = self.flow.download_token
        self.assertFalse(self.flow.on_download_finished(update, "/tmp/unverified.zip", None, token))
        self.assertEqual(self.flow.state, PolicyState.FORCE_PENDING)
        self.assertNotIn("force_ready", [kind for _, kind in self.port.prompts])
        self.assertEqual(self.port.installs, [])

    def test_direct_download_ready_also_requires_verified_cached_identity(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=False)
        self.assertIsNone(self.flow.on_download_ready(update, "/tmp/unverified.zip"))
        self.assertEqual(self.flow.state, PolicyState.FORCE_PENDING)
        self.assertNotIn("force_ready", [kind for _, kind in self.port.prompts])

    def test_download_ready_verifies_once_before_reporting_success(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "validate_cached_archive", side_effect=["/tmp/a.zip", None]) as verify:
            self.assertTrue(self.flow.on_download_finished(update, "/tmp/a.zip", None, token))
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(self.flow.state, PolicyState.FORCE_READY)

    def test_cancelled_pending_replacement_releases_and_processes_new_manifest(self):
        old, new = self.update_for_gap(8), self.update_for_gap(9)
        self.flow.on_manifest_verified(old, automatic=True)
        token = self.flow.download_token
        with patch.object(self.cache, "write_lock", side_effect=sf_update_cache.UpdateCacheBusyError("busy")):
            self.flow.on_manifest_verified(new, automatic=True)
            self.flow.cancel_download()
        self.flow.on_download_finished(old, None, UpdateCancelled("cancelled"), token)
        self.assertEqual(self.flow.state, PolicyState.FORCE_PENDING)
        self.assertIs(self.port.downloads[-1][0], new)

    def test_concurrent_install_requests_launch_helper_only_once(self):
        update = self.update_for_gap(8)
        self.cache.ready[update.archive_sha256] = "/tmp/ready.zip"
        self.flow.on_manifest_verified(update, automatic=False)
        self.assertTrue(self.flow.install_ready())
        self.assertFalse(self.flow.install_ready())
        self.assertEqual(len(self.port.installs), 1)

    def test_cancellation_passes_the_active_task_token(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=True)
        token = self.flow.download_token
        self.port.cancel_download = Mock()
        self.flow.cancel_download()
        self.port.cancel_download.assert_called_once_with(update, token=token)

    def test_cancel_invalidates_task_and_rejects_late_success(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=True)
        token = self.flow.download_token
        self.flow.cancel_download()
        self.assertFalse(self.flow.is_current_download(update, token))
        self.cache.ready[update.archive_sha256] = "/tmp/late.zip"
        self.assertFalse(self.flow.on_download_finished(update, "/tmp/late.zip", None, token))
        self.assertEqual(self.flow.state, PolicyState.FORCE_PENDING)
        self.assertEqual(self.port.installs, [])

    def test_public_check_result_and_busy_manifest_feedback(self):
        update = self.update_for_gap(8)
        decision = self.flow.on_check_result(update, None, automatic=False)
        self.assertEqual(decision.state, PolicyState.FORCE_PENDING)
        self.assertIsNone(self.flow.on_check_result(None, None))
        self.assertIsNone(self.flow.state)
        self.assertFalse(self.flow.on_check_result(None, OSError("offline"), automatic=False))
        self.assertEqual(self.port.messages[-1][0], "检查更新失败")
        with patch.object(self.cache, "write_lock", side_effect=sf_update_cache.UpdateCacheBusyError("busy")):
            self.assertIsNone(self.flow.on_manifest_verified(update, automatic=False))
        self.assertEqual(self.flow.diagnostics()["errors"]["counts"]["manifest_busy"], 1)
        self.assertEqual(self.port.messages[-1][0], "检查更新失败")

    def test_manual_offer_start_download_and_fatal_terminal(self):
        update = self.update_for_gap(1)
        self.flow.on_manifest_verified(update, automatic=False)
        self.assertTrue(self.flow.start_download(prompt_when_ready=True, show_progress=True))
        token = self.flow.download_token
        self.assertTrue(self.flow.accepts_download_terminal(update, token))
        self.assertFalse(self.flow.start_download(prompt_when_ready=True, show_progress=True))
        self.assertFalse(self.flow.on_download_finished(update, None, UpdateFatal("disk"), token))
        self.assertEqual(self.flow.diagnostics()["errors"]["counts"]["download_fatal"], 1)
        self.assertEqual(self.port.installs, [])

    def test_install_port_exception_releases_idempotency_guard_for_retry(self):
        update = self.update_for_gap(8)
        self.cache.ready[update.archive_sha256] = "verified.zip"
        self.flow.on_manifest_verified(update, automatic=False)
        self.port.install_update = Mock(side_effect=[OSError("helper unavailable"), True])
        with self.assertRaisesRegex(OSError, "helper unavailable"):
            self.flow.install_ready()
        self.assertTrue(self.flow.install_ready())
        self.assertEqual(self.port.install_update.call_count, 2)

    def test_same_sequence_replacement_is_offered_then_ready_with_explicit_prompt(self):
        from dataclasses import replace
        update = replace(self.update_for_gap(1), release_sequence=self.port.local_sequence)
        decision = self.flow.on_manifest_verified(update, automatic=False)
        self.assertEqual(decision.state, PolicyState.REPLACE_OFFER)
        self.assertEqual(self.port.prompts[-1][1], "offer")
        self.cache.ready[update.archive_sha256] = "verified.zip"
        decision = self.flow.on_download_ready(update, "verified.zip")
        self.assertTrue(decision.should_prompt)
        self.assertEqual(self.port.prompts[-1][1], "ready")

    def test_gap_eight_or_more_enters_force_pending_without_auto_download(self):
        update = self.update_for_gap(8)
        decision = self.flow.on_manifest_verified(update, automatic=False)
        self.assertEqual(decision.state, PolicyState.FORCE_PENDING)
        self.assertEqual(self.port.downloads, [])
        self.assertEqual(self.port.prompts, [(update, "force_pending")])

    def test_gap_eight_or_more_automatically_starts_silent_prefetch(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=True)
        self.assertEqual(self.port.downloads, [(update, False, False)])
        self.assertEqual(self.port.prompts, [(update, "force_pending")])

    def test_force_pending_respects_policy_should_download_flag(self):
        update = self.update_for_gap(8)
        with patch("sf_update_policy.evaluate") as evaluate:
            evaluate.return_value = type("Decision", (), {
                "state": PolicyState.FORCE_PENDING,
                "gap": 8,
                "should_download": False,
                "should_prompt": True,
                "force": True,
            })()
            self.flow.on_manifest_verified(update, automatic=True)
        self.assertEqual(self.port.downloads, [])

        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=False)
        self.assertTrue(self.flow.handle_force_action("开始下载"))
        self.assertEqual(self.port.downloads, [(update, False, True)])
        self.assertTrue(self.flow.handle_force_action("继续使用"))
        self.assertEqual(self.port.cancels, [])
        self.assertEqual(self.flow.state, PolicyState.FORCE_PENDING)

    def test_download_ready_enters_force_ready_and_install_requires_explicit_action(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=False)
        self.cache.ready[update.archive_sha256] = "/tmp/forced.zip"
        self.flow.on_download_ready(update, "/tmp/forced.zip")
        self.assertEqual(self.flow.state, PolicyState.FORCE_READY)
        self.assertEqual(self.port.installs, [])
        self.assertTrue(self.flow.install_ready())
        self.assertEqual(self.port.installs, [(update, "/tmp/forced.zip")])

    def test_install_ready_refuses_when_current_cache_validation_fails(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=False)
        self.flow.on_download_ready(update, "/tmp/stale.zip")
        self.cache.ready.clear()
        self.assertFalse(self.flow.install_ready())
        self.assertEqual(self.port.installs, [])

    def test_startup_cache_validation_can_make_force_update_ready(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=False)
        self.cache.ready[update.archive_sha256] = "/tmp/startup.zip"
        decision = self.flow.on_first_frame_ready()
        self.assertEqual(decision.state, PolicyState.FORCE_READY)
        self.assertEqual(self.cache.validated, [update, update])
        self.assertEqual(self.port.prompts, [(update, "force_pending"), (update, "force_ready")])

    def test_same_identity_is_prompted_only_once_per_session(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=True)
        self.flow.on_manifest_verified(update, automatic=True)
        self.assertEqual(self.port.prompts, [(update, "force_pending")])

    def test_new_identity_cancels_old_download_before_starting_latest(self):
        old = self.update_for_gap(4)
        new = self.update_for_gap(5, sequence=15, digest="b" * 64)
        self.flow.on_manifest_verified(old, automatic=True)
        self.flow.on_manifest_verified(new, automatic=True)
        self.assertEqual(self.port.cancels, [old])
        self.assertEqual(self.port.downloads[-1], (new, True, False))

    def test_cancel_keeps_force_pending_without_immediate_redownload(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=True)
        self.flow.handle_force_action("开始下载")
        self.flow.cancel_download()
        self.assertEqual(self.port.cancels, [update])
        self.assertEqual(self.flow.state, PolicyState.FORCE_PENDING)
        self.assertEqual(len(self.port.downloads), 1)

    def test_cancelled_download_does_not_prompt_or_retry(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=True)
        self.flow.handle_force_action("开始下载")
        self.assertFalse(self.flow.on_download_finished(update, None, UpdateCancelled("下载已取消")))
        self.assertEqual(self.port.prompts, [(update, "force_pending")])
        self.assertEqual(len(self.port.downloads), 1)

    def test_deterministic_download_error_stops_without_install(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=True)
        self.flow.handle_force_action("开始下载")
        self.assertFalse(self.flow.on_download_finished(update, None, UpdateFatal("更新暂存空间不足")))
        self.assertEqual(self.port.installs, [])
        self.assertEqual(len(self.port.downloads), 1)

    def test_offline_start_without_verified_manifest_does_not_force(self):
        decision = self.flow.on_first_frame_ready()
        self.assertIsNone(decision)
        self.assertNotEqual(self.flow.state, PolicyState.FORCE_PENDING)
        self.assertEqual(self.port.prompts, [])
        self.assertEqual(self.cache.validated, [])

    def test_legacy_manifest_uses_existing_manual_check_path(self):
        update = UpdateInfo(
            "2.0.0", "legacy", "https://github.com/example/repo/a.zip", 8,
            "c" * 64)
        decision = self.flow.on_manifest_verified(update, automatic=False)
        self.assertEqual(decision.state, PolicyState.LEGACY)
        self.assertEqual(self.port.prompts, [])
        self.flow.manual_check()
        self.assertEqual(self.port.checks, [True])

    def test_real_window_port_factory_owns_cache_and_flow(self):
        import stock_float_window as window

        with tempfile.TemporaryDirectory() as root:
            with patch.object(window, "UPDATE_STAGING_DIR", root, create=True):
                fixture = SimpleNamespace(
                    update_controller=Mock(return_value=Mock()),
                    _q_put=Mock(),
                    _bundle_version=Mock(return_value="1.0.0"),
                )
                port = window.make_update_flow_port(fixture)
                self.assertIsInstance(port.cache, UpdateCache)  # discipline-ok(R5): verifying production assembly port
                self.assertIs(port.flow._port, port)  # discipline-ok(R5): verifying production assembly port
                self.assertIs(port.flow._cache, port.cache)  # discipline-ok(R5): verifying production assembly port

    def test_flow_no_cache_and_unverified_download_classification_are_safe(self):
        port = FakePort(None)
        flow = UpdateFlowController(port=port, cache=None)
        self.assertIsNone(flow.on_first_frame_ready())
        update = self.update_for_gap(8)
        self.assertIsNone(flow.on_manifest_verified(update, automatic=False))
        self.assertFalse(flow.on_download_finished(update, None, RuntimeError("network")))
        self.assertIsNone(flow.state)
        self.assertEqual(flow.diagnostics()["errors"]["counts"], {"manifest_replay": 1})

    def test_download_classification_import_failure_is_recorded(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=False)
        self.flow.start_download(prompt_when_ready=False, show_progress=True)
        token = self.flow.download_token
        with patch.dict("sys.modules", {"sf_update": None}):
            self.assertFalse(self.flow.on_download_finished(update, None, RuntimeError("broken import"), token))
        self.assertEqual(self.flow.diagnostics()["errors"]["counts"],
                         {"download_classification": 1, "download": 1})

    def test_diagnostics_is_read_only_snapshot(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=True)
        snapshot = self.flow.diagnostics()
        self.assertEqual(snapshot["state"], PolicyState.FORCE_PENDING.value)
        self.assertEqual(snapshot["identity"], (update.version, update.release_sequence,
                                                 update.archive_sha256))
        self.assertEqual(self.port.downloads, [(update, False, False)])

    def test_download_ready_prepare_and_offer_states_prompt_once(self):
        prepare = self.update_for_gap(4)
        self.flow.on_manifest_verified(prepare, automatic=False)
        self.cache.ready[prepare.archive_sha256] = "/tmp/prepare.zip"
        self.flow.on_download_ready(prepare, "/tmp/prepare.zip")
        self.assertEqual(self.port.prompts[-1][1], "ready")

        offer = self.update_for_gap(2, sequence=12, digest="e" * 64)
        self.flow.on_manifest_verified(offer, automatic=False)
        self.cache.ready[offer.archive_sha256] = "/tmp/offer.zip"
        self.flow.on_download_ready(offer, "/tmp/offer.zip")
        self.assertEqual(self.port.prompts[-1][1], "ready")

    def test_first_frame_offer_rechecks_cache_and_prompts(self):
        update = self.update_for_gap(2)
        self.flow.on_manifest_verified(update, automatic=False)
        decision = self.flow.on_first_frame_ready()
        self.assertEqual(decision.state, PolicyState.OFFER)
        self.assertEqual(self.port.prompts[-1][1], "offer")

    def test_cache_validation_failure_is_swallowed_and_recorded(self):
        class BrokenCache(FakeCache):
            def validate_cached_archive(self, update):
                raise OSError("cache unreadable")

        cache = BrokenCache()
        port = FakePort(cache)
        flow = UpdateFlowController(port=port, cache=cache)
        decision = flow.on_manifest_verified(self.update_for_gap(8), automatic=False)
        self.assertEqual(decision.state, PolicyState.FORCE_PENDING)
        self.assertEqual(flow.diagnostics()["errors"]["counts"], {"cache_validation": 1})
        self.assertEqual(port.prompts[-1][1], "force_pending")

    def test_stale_download_ready_and_finished_results_are_ignored(self):
        old = self.update_for_gap(8)
        new = self.update_for_gap(8, sequence=18, digest="d" * 64)
        self.flow.on_manifest_verified(new, automatic=False)
        self.assertIsNone(self.flow.on_download_ready(old, "/tmp/old.zip"))
        self.assertFalse(self.flow.on_download_finished(old, None, RuntimeError("old")))
        self.assertEqual(self.port.installs, [])
        self.assertEqual(self.port.messages, [])

    def test_successful_download_is_recorded_and_prompts_ready(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=False)
        self.assertTrue(self.flow.handle_force_action("开始下载"))
        token = self.flow.download_token
        self.cache.ready[update.archive_sha256] = "/tmp/ready.zip"
        self.assertTrue(self.flow.on_download_finished(update, "/tmp/ready.zip", None, token))
        self.assertEqual(self.flow.state, PolicyState.FORCE_READY)
        self.assertEqual(self.port.prompts[-1][1], "force_ready")

    def test_ordinary_download_error_shows_message_and_records_error(self):
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=False)
        self.flow.start_download(prompt_when_ready=False, show_progress=True)
        token = self.flow.download_token
        self.assertFalse(self.flow.on_download_finished(update, None, OSError("network"), token))
        self.assertEqual(self.port.messages, [("更新未完成", "下载或校验失败。")])
        self.assertEqual(self.flow.diagnostics()["errors"]["counts"], {"download": 1})

    def test_force_action_and_install_refuse_wrong_state(self):
        self.assertFalse(self.flow.handle_force_action("开始下载"))
        self.assertFalse(self.flow.handle_force_action("继续使用"))
        self.assertFalse(self.flow.install_ready())
        update = self.update_for_gap(8)
        self.flow.on_manifest_verified(update, automatic=False)
        self.assertFalse(self.flow.handle_force_action("未知动作"))
        self.assertFalse(self.flow.install_ready())



if __name__ == "__main__":
    unittest.main()
