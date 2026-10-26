"""Retire only finished, owned updater work; never destroy recovery material."""
import os
from pathlib import Path
import shutil
import tempfile
import time
from unittest import mock

import sf_update
from tests._base import SFCase
from tests.test_update_helper import _HelperCase


class UpdateCleanupQualityTests(SFCase):
    def test_failed_real_helper_leaves_no_unpack_app_in_staging(self):
        case = _HelperCase()
        case.setUp()
        self.addCleanup(shutil.rmtree, case.tmp, True)
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        archive = case.build_archive()
        with tempfile.TemporaryDirectory(prefix="sf_recovery_") as recovery, \
                mock.patch.dict(os.environ, {"TMPDIR": recovery}):
            self.assertEqual(case.station(archive), "new_app_signature_invalid")
            self.assertEqual(list(case.work.glob("unpack.*")), [])
            self.assertTrue(list(Path(recovery).rglob("*.app.stale")))
            self.assertTrue(case.old.is_dir())

    def test_aged_orphan_is_moved_but_active_work_and_unknown_dirs_survive(self):
        with tempfile.TemporaryDirectory() as root:
            staging, destination = Path(root) / "updates", Path(root) / "recovery"
            staging.mkdir()
            orphan = staging / "unpack.ABC123"
            active = staging / "unpack.DEF456"
            unknown = staging / "my-important-folder"
            for directory in (orphan, active, unknown):
                directory.mkdir()
                (directory / "payload").write_bytes(b"12345")
            active.with_name(active.name + ".owner").write_text(str(os.getpid()), encoding="ascii")
            old = time.time() - 7200
            for directory in (orphan, active, unknown):
                os.utime(directory, (old, old))
            with mock.patch.object(sf_update, "UPDATE_STAGING_DIR", str(staging)):
                report = sf_update.prune_staging_residue(grace_sec=3600, dest_root=str(destination))
            self.assertFalse(orphan.exists())
            self.assertTrue((destination / orphan.name / "payload").is_file())
            self.assertTrue(active.exists())
            self.assertTrue(unknown.exists())
            self.assertEqual(report["moved_bytes"], 5)

    def test_residue_reason_respects_grace_current_version_and_known_shapes(self):
        cases = {
            "old.zip.part": "part",
            "old.part-1-2": "legacy-part",
            "old.zip.resume.json": "resume",
            "state.json.tmp-1-2": "state-tmp",
            "Skyrocket-older.zip": "old-zip",
            "StockFloatTargetPrice-older.zip": "old-zip",
        }
        for name, reason in cases.items():
            self.assertEqual(sf_update.residue_reason(name, 3601, "current"), reason)
        self.assertIsNone(sf_update.residue_reason("Skyrocket-current.zip", 3601, "current"))
        self.assertIsNone(sf_update.residue_reason("old.zip.part", 10, "current"))
        for name in ("update-result.json", "update_helper-test.sh", "notes.txt"):
            self.assertIsNone(sf_update.residue_reason(name, 3601, "current"))

    def test_prune_reports_current_package_and_move_errors(self):
        with tempfile.TemporaryDirectory() as root:
            staging = Path(root) / "updates"
            destination = Path(root) / "recovery"
            staging.mkdir()
            current = staging / "Skyrocket-current.zip"
            old = staging / "old.zip.part"
            current.write_bytes(b"keep")
            old.write_bytes(b"move")
            old_time = time.time() - 7200
            os.utime(old, (old_time, old_time))
            with mock.patch.object(sf_update, "UPDATE_STAGING_DIR", str(staging)), \
                 mock.patch.object(sf_update.os, "replace", side_effect=OSError("cross-device")), \
                 mock.patch.object(sf_update.shutil, "move", side_effect=OSError("copy failed")):
                report = sf_update.prune_staging_residue(
                    installed_version="current", grace_sec=3600, dest_root=str(destination))
            self.assertTrue(current.exists())
            self.assertTrue(any("old.zip.part" in item for item in report["errors"]))
            self.assertTrue(any(item["name"] == current.name for item in report["held_back"]))

    def test_inventory_reports_staging_and_rollback_sizes(self):
        with tempfile.TemporaryDirectory() as root:
            staging = Path(root) / "updates"
            apps = Path(root) / "Applications"
            staging.mkdir()
            apps.mkdir()
            (staging / "partial.zip.part").write_bytes(b"123")
            rollback = apps / ".Skyrocket.rollback-1"
            rollback.mkdir()
            (rollback / "payload").write_bytes(b"12345")
            with mock.patch.object(sf_update, "UPDATE_STAGING_DIR", str(staging)):
                result = sf_update.update_storage_inventory(str(apps))
            self.assertEqual(result["staging_bytes"], 3)
            self.assertEqual(result["rollback_bytes"], 5)
            self.assertEqual(result["rollback_apps"][0]["name"], rollback.name)

    def test_peek_does_not_delete_but_consume_does(self):
        with tempfile.TemporaryDirectory() as root:
            staging = Path(root) / "updates"
            staging.mkdir()
            result = staging / "update-result.json"
            result.write_text('{"status":"installed","message":"ok"}', encoding="utf-8")
            with mock.patch.object(sf_update, "UPDATE_STAGING_DIR", str(staging)):
                self.assertEqual(sf_update.peek_install_result()["status"], "installed")
                self.assertTrue(result.exists())
                self.assertEqual(sf_update.consume_install_result()["message"], "ok")
                self.assertFalse(result.exists())
