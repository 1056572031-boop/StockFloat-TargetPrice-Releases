"""Exercise lock acquisition/exit before any build or credential access."""
import os
import subprocess
import tempfile
from pathlib import Path

from tests._base import SFCase

ROOT = Path(__file__).resolve().parents[1]


class ReleaseLockCleanup(SFCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="sf_release_lock_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "build_release.sh"
        self.script.write_bytes((ROOT / "build_release.sh").read_bytes())
        self.lock = self.root / ".build_release_targetprice.lock"

    def invoke(self, *args):
        return subprocess.run(["/bin/bash", str(self.script), *(args or ("--invalid-lock-probe",))],
                              capture_output=True, text=True, timeout=10)

    def test_exclusive_modes_reject_every_argument_order_before_environment_or_credentials(self):
        for first, second in (("--local-only", "--prepare-only"),
                              ("--local-only", "--publish-prepared"),
                              ("--prepare-only", "--publish-prepared")):
            for args in ((first, second), (second, first)):
                with self.subTest(args=args):
                    result = self.invoke(*args)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertFalse(self.lock.exists())

    def test_no_install_is_preserved_regardless_of_prepared_mode_order(self):
        # Execute only argument parsing and the owned lock, never build/install/publish commands.
        prefix = self.script.read_text().split("PUBLIC_REGRESSION=", 1)[0]
        for args in (("--no-install", "--publish-prepared"), ("--publish-prepared", "--no-install")):
            result = subprocess.run(["/bin/bash", "-c", prefix + '\nprintf "install=%s\\n" "$DO_INSTALL"',
                                     str(self.script), *args], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "install=0")
            self.assertFalse(self.lock.exists())

    def test_repeated_early_exit_releases_own_pid_and_lock(self):
        for _ in range(2):
            result = self.invoke()
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("未知参数", result.stderr)
            self.assertFalse(self.lock.exists())

    def test_dead_owner_pid_is_reclaimed_but_live_owner_is_preserved(self):
        child = subprocess.Popen(["/usr/bin/true"])
        child.wait(timeout=10)
        self.lock.mkdir()
        (self.lock / "pid").write_text(str(child.pid))
        self.assertEqual(self.invoke().returncode, 2)
        self.assertFalse(self.lock.exists())
        self.lock.mkdir()
        pid = self.lock / "pid"
        pid.write_text(str(os.getpid()))
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(pid.read_text(), str(os.getpid()))

    def test_unknown_files_and_malformed_owner_are_not_deleted(self):
        self.lock.mkdir()
        marker = self.lock / "unknown"
        marker.write_text("preserve")
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(marker.read_text(), "preserve")
        pid = self.lock / "pid"
        pid.write_text("invalid")
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(pid.read_text(), "invalid")
