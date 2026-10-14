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
