"""A painted first frame must not conceal a subsequent startup crash."""
import pathlib
import subprocess
import sys
import tempfile
from unittest import mock

from tools import smoke_frozen_app as smoke
from tests._base import SFCase


class FrozenSmokeLifecycleTests(SFCase):
    def _probe(self, mode):
        child = pathlib.Path(__file__).with_name("_smoke_lifecycle_child.py")
        spawn = subprocess.Popen

        def launch(args, **kwargs):
            return spawn([sys.executable, str(child), mode], **kwargs)

        with tempfile.TemporaryDirectory(prefix="sf_smoke_test_") as directory, \
                mock.patch.object(smoke.VD, "bundle_executable", return_value=sys.executable), \
                mock.patch.object(smoke.subprocess, "Popen", side_effect=launch):
            return smoke.run_smoke(pathlib.Path(directory), timeout=1)

    def test_native_failure_after_first_frame_is_rejected(self):
        rc, report = self._probe("fail")
        self.assertEqual(rc, 1)
        self.assertTrue(report["first_frame"])
        self.assertTrue(report["crashed_early"])
        self.assertEqual(report["returncode"], 6)

    def test_healthy_process_is_observed_before_the_tool_terminates_it(self):
        rc, report = self._probe("healthy")
        self.assertEqual(rc, 0)
        self.assertFalse(report["crashed_early"])
        self.assertGreaterEqual(report["post_frame_seconds"], smoke.POST_FRAME_OBSERVATION_SECONDS)
