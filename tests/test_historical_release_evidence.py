"""Immutable private release evidence and its narrowly scoped Git whitespace rule."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests._base import SFCase

ROOT = Path(__file__).resolve().parents[1]
LOG = "outputs/release-candidates/v1.4.119/publish.log"
RECEIPT = "outputs/release-candidates/v1.4.119/post_release.json"
EXPECTED = "308940c26c3183ba3e6a0c30e4c48ddbf806e417650064152296bd630c52f0b0"


def validate(log, receipt):
    if hashlib.sha256(log).hexdigest() != EXPECTED:
        raise ValueError("historical publish.log SHA256 mismatch")
    record = json.loads(receipt)["historical_publish_log"]
    if record["path"] != "publish.log" or record["sha256"] != EXPECTED:
        raise ValueError("historical publish.log receipt mismatch")


class HistoricalReleaseEvidence(SFCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("SF_PUBLIC_REGRESSION") == "1" and not (ROOT / LOG).exists():
            raise unittest.SkipTest("public snapshot excludes private release archives")

    def test_worktree_and_candidate_index(self):
        validate((ROOT / LOG).read_bytes(), (ROOT / RECEIPT).read_bytes())
        for path in (LOG, RECEIPT):
            result = subprocess.run(["git", "show", ":" + path], cwd=ROOT, capture_output=True, check=True)
            if path == LOG:
                log = result.stdout
            else:
                validate(log, result.stdout)

    def test_tampered_log_and_receipt_are_rejected(self):
        log = (ROOT / LOG).read_bytes()
        receipt = (ROOT / RECEIPT).read_bytes()
        validate(log, receipt)
        for position in (0, len(log) // 2, len(log) - 1):
            changed = log[:position] + bytes([log[position] ^ 1]) + log[position + 1:]
            with self.subTest(position=position), self.assertRaises(ValueError):
                validate(changed, receipt)
        record = json.loads(receipt)
        record["historical_publish_log"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            validate(log, json.dumps(record).encode())

    def test_git_whitespace_rule_is_narrow(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(HOME=tempfile.gettempdir(), GIT_CONFIG_NOSYSTEM="1")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, check=False)
            self.assertEqual(git("init", "-q").returncode, 0)
            (root / ".gitattributes").write_bytes((ROOT / ".gitattributes").read_bytes())
            target = root / LOG
            target.parent.mkdir(parents=True)
            target.write_bytes((ROOT / LOG).read_bytes())
            git("add", "-f", ".gitattributes", LOG)
            self.assertEqual(git("diff", "--cached", "--check").returncode, 0)
            other = root / "other.txt"
            other.write_bytes(b"bad trailing space \n")
            git("add", "other.txt")
            self.assertNotEqual(git("diff", "--cached", "--check").returncode, 0)
            other.write_bytes(b"clean\n")
            git("add", "other.txt")
            target.write_bytes(b" \tbad indent\n")
            git("add", "-f", LOG)
            result = git("diff", "--cached", "--check")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"space before tab", result.stdout)


if __name__ == "__main__":
    unittest.main()
