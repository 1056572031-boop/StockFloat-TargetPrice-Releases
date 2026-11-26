# -*- coding: utf-8 -*-
"""更新缓存的身份、复核与并发边界。"""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sf_update_cache


from tests._base import SFCase


class _Info:
    def __init__(self, version="1.4.119", sequence=11, body=b"verified archive",
                 published_at="2026-11-19T00:00:00Z"):
        self.version = version
        self.release_sequence = sequence
        self.archive_size = len(body)
        self.archive_sha256 = hashlib.sha256(body).hexdigest()
        self.published_at = published_at


class UpdateCacheBehaviorTests(SFCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory(prefix="sf-update-cache-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "updates"
        self.cache = sf_update_cache.UpdateCache(self.root)
        self.info = _Info()

    def test_root_layout_and_lock_safety_fail_closed(self):
        root_file = self.root.parent / "root-file"
        root_file.write_bytes(b"file")
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            sf_update_cache.UpdateCache(root_file)

        outside = self.root.parent / "outside-entry"
        outside.write_bytes(b"outside")
        (self.root / "unrecognized-link").symlink_to(outside)
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            self.cache.ensure_identity_paths_safe(self.info)
        (self.root / "unrecognized-link").unlink()

        lock = self.root / ".update-cache.lock"
        lock.symlink_to(outside)
        with mock.patch.object(self.cache, "_ensure_layout_safe"):
            with self.assertRaises(sf_update_cache.UpdateCacheError):
                with self.cache.write_lock():
                    pass

    def test_modern_identity_prevents_downgrade_and_equal_time_replay_after_restart(self):
        newer = _Info(version="2.0.0", sequence=12, published_at="2026-11-20T00:00:00Z")
        conflict = _Info(version="2.1.0", sequence=13, body=b"different",
                         published_at=newer.published_at)
        legacy = _Info(version="3.0.0", sequence=None, published_at=None)
        with self.cache.write_lock():
            self.assertTrue(self.cache.accept_manifest_identity(newer))
        restarted = sf_update_cache.UpdateCache(self.root)
        with restarted.write_lock():
            self.assertFalse(restarted.accept_manifest_identity(conflict))
            self.assertFalse(restarted.accept_manifest_identity(legacy))
            self.assertTrue(restarted.accept_manifest_identity(newer))

    def test_timestamp_only_legacy_state_binds_first_verified_identity_then_rejects_conflict(self):
        accepted = _Info(version="2.0.0", sequence=12, published_at="2026-11-20T00:00:00Z")
        conflict = _Info(version="2.1.0", sequence=13, body=b"different", published_at=accepted.published_at)
        with self.cache.write_lock():
            self.assertTrue(self.cache.accept_manifest_timestamp(accepted.published_at))
            self.cache.invalidate_stale(accepted)
            self.assertTrue(self.cache.accept_manifest_identity(accepted))
            self.assertFalse(self.cache.accept_manifest_identity(conflict))

    def test_timestamp_only_migration_does_not_assume_cached_package_is_latest_manifest(self):
        old = _Info(version="1.0.0", sequence=1, published_at="2026-11-19T00:00:00Z")
        latest = _Info(version="2.0.0", sequence=2, published_at="2026-11-20T00:00:00Z")
        with self.cache.write_lock():
            self.cache.invalidate_stale(old)
            self.assertTrue(self.cache.accept_manifest_timestamp(latest.published_at))
            self.assertTrue(self.cache.accept_manifest_identity(latest),
                            "旧包身份与最新时间戳不一定同源")

    def test_existing_timestamp_only_state_binds_identity_without_losing_replay_protection(self):
        update = _Info(version="2.0.0", sequence=12, published_at="2026-11-20T00:00:00Z")
        conflict = _Info(version="2.1.0", sequence=13, body=b"different", published_at=update.published_at)
        with self.cache.write_lock():
            self.assertTrue(self.cache.accept_manifest_timestamp(update.published_at))
            self.assertTrue(self.cache.accept_manifest_identity(update))
            self.assertFalse(self.cache.accept_manifest_identity(conflict))
        restarted = sf_update_cache.UpdateCache(self.root)
        with restarted.write_lock():
            self.assertFalse(restarted.accept_manifest_identity(conflict))
            self.assertTrue(restarted.accept_manifest_identity(update))

    def test_modern_manifest_without_published_time_is_rejected(self):
        missing = _Info(sequence=12, published_at=None)
        with self.cache.write_lock():
            self.assertFalse(self.cache.accept_manifest_identity(missing))
        self.assertFalse((self.root / "update-cache.json").exists())

    def test_identity_filename_includes_version_sequence_and_digest_prefix(self):
        identity = self.cache.identity(self.info)
        path = self.cache.archive_path(self.info)
        self.assertEqual(identity, ("1.4.119", 11, self.info.archive_sha256))  # discipline-ok(R1): fixed cache identity fixture
        self.assertEqual(path.name, "Skyrocket-1.4.119-s11-%s.zip" % self.info.archive_sha256[:8])  # discipline-ok(R1): fixed cache filename fixture

    def test_valid_archive_is_reused_after_startup_verification(self):
        body = b"verified archive"
        archive = self.cache.archive_path(self.info)
        archive.write_bytes(body)
        self.assertEqual(self.cache.validate_cached_archive(self.info), archive)
        state = json.loads((self.root / "update-cache.json").read_text(encoding="utf-8"))
        self.assertEqual(state["startup_verification"]["result"], "valid")

    def test_validation_refuses_to_write_while_another_writer_holds_lock(self):
        self.cache.archive_path(self.info).write_bytes(b"verified archive")
        other = sf_update_cache.UpdateCache(self.root)
        with other.write_lock():
            self.assertTrue(other.accept_manifest_timestamp("2026-11-20T00:00:00Z"))
            with self.assertRaises(sf_update_cache.UpdateCacheBusyError):
                self.cache.validate_cached_archive(self.info)
        self.assertFalse(other.accept_manifest_timestamp("2026-11-19T00:00:00Z"))

    def test_identity_fifo_is_rejected_before_open(self):
        path = self.cache.part_path(self.info)
        os.mkfifo(path)
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            self.cache.ensure_identity_paths_safe(self.info)

    def test_root_io_failure_is_mapped_to_cache_error(self):
        with mock.patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
            with self.assertRaises(sf_update_cache.UpdateCacheError):
                sf_update_cache.UpdateCache(self.root.parent / "blocked")

    def test_identity_metadata_stat_failure_is_classified_without_opening_file(self):
        with mock.patch.object(self.cache, "_ensure_layout_safe"), \
                mock.patch.object(sf_update_cache, "_inside_root", return_value=True), \
                mock.patch.object(Path, "lstat", side_effect=PermissionError("stat denied")):
            with self.assertRaisesRegex(sf_update_cache.UpdateCacheError, "不可读"):
                self.cache.ensure_identity_paths_safe(self.info)

    def test_root_changed_to_non_directory_after_creation_is_rejected(self):
        with mock.patch.object(Path, "exists", return_value=False), \
                mock.patch.object(Path, "is_symlink", return_value=False), \
                mock.patch.object(Path, "mkdir"), \
                mock.patch.object(Path, "is_dir", return_value=False):
            with self.assertRaisesRegex(sf_update_cache.UpdateCacheError, "不安全"):
                sf_update_cache.UpdateCache(self.root.parent / "changed")

    def test_failed_startup_verification_never_returns_the_archive(self):
        archive = self.cache.archive_path(self.info)
        archive.write_bytes(b"tampered")
        self.assertIsNone(self.cache.validate_cached_archive(self.info))
        self.assertFalse(archive.exists())
        state = json.loads((self.root / "update-cache.json").read_text(encoding="utf-8"))
        self.assertEqual(state["startup_verification"]["result"], "invalid")

    def test_identity_change_quarantines_old_archive_part_and_resume_state(self):
        old = self.info
        new = _Info(sequence=12)
        for path in (self.cache.archive_path(old), self.cache.part_path(old), self.cache.resume_path(old)):
            path.write_bytes(b"old")
        self.cache.invalidate_stale(new)
        retired = self.root / "retired"
        self.assertFalse(self.cache.archive_path(old).exists())
        self.assertFalse(self.cache.part_path(old).exists())
        self.assertFalse(self.cache.resume_path(old).exists())
        self.assertEqual(sorted(path.name for path in retired.iterdir()), sorted((
            self.cache.archive_path(old).name,
            self.cache.part_path(old).name,
            self.cache.resume_path(old).name,
        )))

    def test_missing_state_is_initialized_but_corrupt_state_fails_closed(self):
        self.assertTrue(self.cache.accept_manifest_timestamp("2026-11-19T00:00:00Z"))
        state_path = self.root / "update-cache.json"
        state_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            self.cache.accept_manifest_timestamp("2026-11-20T00:00:00Z")
        state_path.write_text(json.dumps({"schema": sf_update_cache.CACHE_SCHEMA + 1}), encoding="utf-8")
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            self.cache.diagnostics()
        state_path.unlink()
        state_path.symlink_to(self.root.parent / "outside-state.json")
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            self.cache.diagnostics()

    def test_unreadable_state_fails_closed(self):
        state_path = self.root / "update-cache.json"
        state_path.write_text('{"schema":1}', encoding="utf-8")
        state_path.chmod(0)
        self.addCleanup(lambda: state_path.chmod(0o600) if state_path.exists() else None)
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            self.cache.diagnostics()

    def test_cache_layout_rejects_symlinks_for_state_lock_retired_and_temporary_entries(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        cases = ("update-cache.json", ".update-cache.lock", "retired", "update-cache.json.tmp-old")
        for name in cases:
            with self.subTest(name=name):
                path = self.root / name
                if path.exists() or path.is_symlink():
                    if path.is_dir() and not path.is_symlink():
                        path.rmdir()
                    else:
                        path.unlink()
                if name == "retired":
                    path.symlink_to(outside, target_is_directory=True)
                else:
                    path.symlink_to(outside / name)
                with self.assertRaises(sf_update_cache.UpdateCacheError):
                    self.cache.ensure_identity_paths_safe(self.info)

    def test_retired_directory_contents_reject_symbolic_links(self):
        retired = self.root / "retired"
        retired.mkdir()
        outside = self.root.parent / "retired-outside"
        outside.write_bytes(b"outside")
        (retired / "old.zip").symlink_to(outside)
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            self.cache.ensure_identity_paths_safe(self.info)

    def test_resume_metadata_and_cache_state_are_kept_in_sync(self):
        self.cache.mark_resume(self.info, {2, 0})
        resume = json.loads(self.cache.resume_path(self.info).read_text(encoding="utf-8"))
        state = json.loads((self.root / "update-cache.json").read_text(encoding="utf-8"))
        self.assertEqual(resume["completed"], [0, 2])
        self.assertEqual(state["resume_completed"], resume["completed"])
        self.assertEqual(self.cache.load_resume(self.info), {0, 2})

        self.assertTrue(self.cache.accept_manifest_timestamp("2026-11-19T00:00:00Z"))
        self.assertFalse(self.cache.accept_manifest_timestamp("2026-11-18T23:59:59Z"))
        self.assertTrue(self.cache.accept_manifest_timestamp("2026-11-19T00:00:00Z"))
        self.assertEqual(self.cache.diagnostics()["latest_published_at"], "2026-11-19T00:00:00Z")

    def test_resume_state_is_loaded_only_for_the_same_identity(self):
        self.cache.mark_resume(self.info, {0, 2})
        self.assertEqual(self.cache.load_resume(self.info), {0, 2})
        self.assertEqual(self.cache.load_resume(_Info(body=b"new archive")), set())

    def test_cancelled_download_discards_partial_artifacts_but_network_interruption_keeps_them(self):
        self.cache.part_path(self.info).write_bytes(b"partial")
        self.cache.mark_resume(self.info, {0})
        self.cache.preserve_interrupted_download(self.info)
        self.assertTrue(self.cache.part_path(self.info).exists())
        self.assertTrue(self.cache.resume_path(self.info).exists())
        self.cache.discard_partial_download(self.info)
        self.assertFalse(self.cache.part_path(self.info).exists())
        self.assertFalse(self.cache.resume_path(self.info).exists())

    def test_insufficient_space_is_classified_before_a_partial_download_is_created(self):
        required = self.info.archive_size * 2 + sf_update_cache.UPDATE_STAGING_FREE_MARGIN_BYTES
        with mock.patch.object(sf_update_cache.shutil, "disk_usage",
                               return_value=(0, 0, required - 1)):
            with self.assertRaises(sf_update_cache.UpdateCacheSpaceError):
                self.cache.require_space(self.info)
        self.assertFalse(self.cache.part_path(self.info).exists())

    def test_exact_required_space_is_accepted(self):
        required = self.info.archive_size * 2 + sf_update_cache.UPDATE_STAGING_FREE_MARGIN_BYTES
        with mock.patch.object(sf_update_cache.shutil, "disk_usage", return_value=(0, 0, required)):
            self.cache.require_space(self.info)

    def test_directory_lock_rejects_a_second_writer(self):
        other = sf_update_cache.UpdateCache(self.root)
        with self.cache.write_lock():
            with self.assertRaises(sf_update_cache.UpdateCacheBusyError):
                with other.write_lock():
                    pass

    def test_root_symlink_is_rejected_before_initialization(self):
        root = self.root.parent / "root-link"
        root.symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            sf_update_cache.UpdateCache(root)

    def test_layout_read_failure_fails_closed(self):
        with mock.patch.object(Path, "iterdir", side_effect=OSError("unreadable")):
            with self.assertRaises(sf_update_cache.UpdateCacheError):
                self.cache.ensure_identity_paths_safe(self.info)

    def test_missing_manifest_timestamp_is_accepted_without_writing_state(self):
        self.assertTrue(self.cache.accept_manifest_timestamp(None))
        self.assertFalse((self.root / "update-cache.json").exists())

    def test_invalidate_stale_renames_colliding_retired_file(self):
        old = self.info
        new = _Info(sequence=12)
        stale = self.cache.archive_path(old)
        stale.write_bytes(b"old")
        retired = self.root / "retired"
        retired.mkdir()
        (retired / stale.name).write_bytes(b"existing")
        self.cache.invalidate_stale(new)
        self.assertTrue((retired / (stale.name + ".%s" % "")).parent.exists())
        self.assertGreaterEqual(len(list(retired.iterdir())), 2)

    def test_invalidate_stale_falls_back_to_shutil_move(self):
        old = self.info
        new = _Info(sequence=12)
        stale = self.cache.archive_path(old)
        stale.write_bytes(b"old")
        original_replace = sf_update_cache.os.replace
        def replace_stale_only(source, target):
            if Path(source) == stale:
                raise OSError("cross-device")
            return original_replace(source, target)
        with mock.patch.object(sf_update_cache.os, "replace", side_effect=replace_stale_only), \
                mock.patch.object(sf_update_cache.shutil, "move", wraps=sf_update_cache.shutil.move) as move:
            self.cache.invalidate_stale(new)
        move.assert_called_once()

    def test_resume_metadata_with_non_list_completed_is_empty(self):
        resume = self.cache.resume_path(self.info)
        resume.write_text(json.dumps({
            "version": self.info.version,
            "release_sequence": self.info.release_sequence,
            "size": self.info.archive_size,
            "sha256": self.info.archive_sha256,
            "completed": {"bad": True},
        }), encoding="utf-8")
        self.assertEqual(self.cache.load_resume(self.info), set())

    def test_write_lock_rejects_symlink_lock_path(self):
        outside = self.root.parent / "outside-lock"
        outside.write_text("", encoding="utf-8")
        (self.root / ".update-cache.lock").symlink_to(outside)
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            with self.cache.write_lock():
                pass

    def test_identity_path_resolution_failure_is_not_accepted(self):
        with mock.patch.object(Path, "resolve", side_effect=OSError("resolve failed")):
            with self.assertRaises(sf_update_cache.UpdateCacheError):
                self.cache.ensure_identity_paths_safe(self.info)

    def test_root_file_and_invalid_digest_identity_are_rejected_or_legacy(self):
        root_file = self.root.parent / "root-file"
        root_file.write_bytes(b"file")
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            sf_update_cache.UpdateCache(root_file)
        info = _Info()
        info.archive_sha256 = ""
        self.assertEqual(self.cache.identity(info), (info.version, None, ""))

    def test_state_symlink_is_rejected_after_layout_probe(self):
        outside = self.root.parent / "outside-state"
        outside.write_text("{}", encoding="utf-8")
        state = self.root / "update-cache.json"
        state.symlink_to(outside)
        with mock.patch.object(self.cache, "_ensure_layout_safe"):
            with self.assertRaises(sf_update_cache.UpdateCacheError):
                self.cache.diagnostics()

    def test_resume_read_errors_and_non_object_shapes_are_empty(self):
        resume = self.cache.resume_path(self.info)
        resume.write_text("[]", encoding="utf-8")
        self.assertEqual(self.cache.load_resume(self.info), set())
        resume.write_text("{", encoding="utf-8")
        self.assertEqual(self.cache.load_resume(self.info), set())
        with mock.patch.object(Path, "read_text", side_effect=OSError("read failed")):
            self.assertEqual(self.cache.load_resume(self.info), set())

    def test_write_lock_rejects_lock_symlink_directly(self):
        outside = self.root.parent / "direct-lock"
        outside.write_text("", encoding="utf-8")
        (self.root / ".update-cache.lock").symlink_to(outside)
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            with self.cache.write_lock():
                pass


        target = self.root.parent / "target"
        target.write_bytes(b"outside")
        os.symlink(target, self.cache.part_path(self.info))
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            self.cache.ensure_identity_paths_safe(self.info)


if __name__ == "__main__":
    unittest.main()
