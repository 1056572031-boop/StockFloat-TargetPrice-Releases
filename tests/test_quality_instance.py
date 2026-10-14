"""Real subprocess ownership instead of simulating only a socket probe."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from sf_instance import acquire_instance_lock
from tests._base import SFCase


class InstanceQualityTests(SFCase):
    def test_two_open_descriptors_cannot_both_own_the_lock(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "instance.lock")
            first = acquire_instance_lock(path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(acquire_instance_lock(path))
            finally:
                first.close()
            second = acquire_instance_lock(path)
            self.assertIsNotNone(second)
            second.close()

    def test_process_exit_releases_ownership_without_removing_lock_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "instance.lock")
            source = ("from sf_instance import acquire_instance_lock; import sys; "
                      "lease=acquire_instance_lock(sys.argv[1]); "
                      "print('owned', flush=True); sys.stdin.read()")
            child = subprocess.Popen([sys.executable, "-c", source, path],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "owned")
                self.assertIsNone(acquire_instance_lock(path))
            finally:
                child.communicate("", timeout=5)
            self.assertEqual(child.returncode, 0)
            self.assertTrue(Path(path).exists())
            lease = acquire_instance_lock(path)
            self.assertIsNotNone(lease)
            lease.close()

    def test_symlink_cannot_redirect_lock_file(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "keep.txt"
            target.write_text("do not alter", encoding="utf-8")
            link = Path(root) / "instance.lock"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                acquire_instance_lock(str(link))
            self.assertEqual(target.read_text(), "do not alter")
