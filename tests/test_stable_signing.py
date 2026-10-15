"""Stable identity tests: changed versions match, different signers do not."""
from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import stable_signing as S

try:
    from _base import SFCase
except ImportError:
    from tests._base import SFCase


class PolicyTests(SFCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(prefix="sf_signing_policy_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.policy = {"schema": 1, "bundle_id": S.BUNDLE_ID,
                       "certificate_sha1": "A" * 40, "certificate_sha256": "B" * 64}
        self.path = self.root / "policy.json"

    def test_rejects_missing_or_malformed_pins_and_wrong_bundle(self):
        for key, value in (("schema", 2), ("bundle_id", "com.example.other"),
                           ("certificate_sha1", ""), ("certificate_sha256", "123")):
            with self.subTest(key=key):
                data = {**self.policy, key: value}
                self.path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    S.load_policy(self.path)

    def test_policy_load_and_certificate_anchored_requirement(self):
        self.path.write_text(json.dumps(self.policy))
        self.assertEqual(S.load_policy(self.path), self.policy)
        self.assertEqual(S.requirement(self.policy),
                         'identifier "com.skyrocket.app" and certificate leaf = H"%s"' % ("A" * 40))

    def test_policy_requires_an_object_and_string_fingerprints(self):
        for data in ([], None, {**self.policy, "certificate_sha1": 1},
                     {**self.policy, "certificate_sha256": None}):
            with self.subTest(data=data):
                self.path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    S.load_policy(self.path)

    def test_no_silent_identity_recreation(self):
        self.path.write_text(json.dumps(self.policy))
        with self.assertRaisesRegex(ValueError, "never rotate"):
            S.initialize(self.root / "new-state", self.path)
        with self.assertRaisesRegex(ValueError, "never rotate"):
            S.initialize(self.root, self.root / "new-policy")

    def test_secrets_cannot_be_generated_in_repository(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            S.initialize(S.ROOT / "new-signing-private", self.root / "new-policy")

    def test_secret_permissions_and_symlink_are_rejected(self):
        secret = self.root / "password"
        secret.write_text("example")
        secret.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            S.private_file(secret)
        secret.chmod(0o600)
        self.assertEqual(S.private_file(secret), "example")
        link = self.root / "link"
        link.symlink_to(secret)
        with self.assertRaisesRegex(ValueError, "private"):
            S.private_file(link)

    def test_cli_failure_is_nonzero_and_does_not_initialize(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(S.main(["preflight", "--policy", str(self.root / "missing")]), 2)

    def test_process_errors_do_not_print_command_password(self):
        failed = subprocess.CompletedProcess([], 1, "", "failed")
        with mock.patch.object(S.subprocess, "run", return_value=failed):
            with self.assertRaises(ValueError) as caught:
                S.run(["/usr/bin/security", "-p", "must-never-be-printed"])
        self.assertNotIn("must-never-be-printed", str(caught.exception))

    def test_process_timeout_does_not_print_command_password(self):
        command = ["/usr/bin/security", "-p", "must-never-be-printed"]
        with mock.patch.object(S.subprocess, "run", side_effect=subprocess.TimeoutExpired(command, 90)):
            with self.assertRaises(ValueError) as caught:
                S.run(command)
        self.assertNotIn("must-never-be-printed", str(caught.exception))

    def test_signing_search_list_is_scoped_and_preserves_concurrent_entries(self):
        keychain = self.root / "identity.keychain-db"
        existing, concurrent = "/tmp/existing.keychain-db", "/tmp/concurrent.keychain-db"
        commands = []

        def run(args):
            commands.append(args)
            if args[-1] == "user":
                paths = [existing] if len(commands) == 1 else [existing, str(keychain), concurrent]
                return "\n".join(json.dumps(path) for path in paths)
            return ""

        with mock.patch.object(S, "check_material", return_value="private-password"), \
                mock.patch.object(S, "run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "injected"):
                with S.signing_keychain(self.policy, self.root) as selected:
                    self.assertEqual(selected, keychain)
                    raise ValueError("injected")
        self.assertIn(["/usr/bin/security", "list-keychains", "-d", "user", "-s",
                       existing, str(keychain)], commands)
        self.assertIn(["/usr/bin/security", "list-keychains", "-d", "user", "-s",
                       existing, concurrent], commands)
        self.assertEqual(commands[-1], ["/usr/bin/security", "lock-keychain", str(keychain)])

    def test_existing_search_list_membership_is_not_removed(self):
        keychain = self.root / "identity.keychain-db"
        with mock.patch.object(S, "check_material", return_value="private-password"), \
                mock.patch.object(S, "run", return_value=json.dumps(str(keychain))) as run:
            with S.signing_keychain(self.policy, self.root):
                pass
        self.assertFalse(any("-s" in call.args[0] for call in run.call_args_list))
        self.assertEqual(run.call_args_list[-1].args[0],
                         ["/usr/bin/security", "lock-keychain", str(keychain)])

    def test_cli_requires_app_and_rejects_unknown_arguments(self):
        for command in (["sign"], ["check"], ["unexpected"]):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    S.main(command)
                self.assertEqual(caught.exception.code, 2)

    def test_cli_dispatch_is_explicit_and_has_no_adhoc_fallback(self):
        self.path.write_text(json.dumps(self.policy))
        for action, function in (("init", "initialize"), ("preflight", "check_material"),
                                 ("sign", "sign"), ("check", "verify")):
            with self.subTest(action=action):
                with mock.patch.object(S, function, return_value={"tested": True}) as method:
                    with contextlib.redirect_stdout(io.StringIO()):
                        code = S.main([action, "--app", str(self.root / "app"),
                                       "--policy", str(self.path), "--state", str(self.root / "private")])
                self.assertEqual(code, 0)
                method.assert_called_once()


@unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/codesign").exists(),
                     "Requires native macOS codesign/keychain; no TCC access")
class NativeSigningTests(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="sf_signing_native_")
        cls.root = Path(cls.tmp.name)
        cls.state = cls.root / "private"
        cls.policy_path = cls.root / "policy.json"
        try:
            cls.policy = S.initialize(cls.state, cls.policy_path)
        except Exception:
            cls.tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            S.run(["/usr/bin/security", "delete-keychain", str(cls.state / "identity.keychain-db")])
        finally:
            cls.tmp.cleanup()

    def app(self, name, version="1", bundle_id=S.BUNDLE_ID):
        app = self.root / (name + ".app")
        contents = app / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        executable = contents / "MacOS/Echo"
        # copy2 would copy the system binary's restricted flags as well.
        shutil.copyfile("/bin/echo", executable)
        executable.chmod(0o755)
        with (contents / "Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleExecutable": "Echo",
                          "CFBundleVersion": version, "CFBundlePackageType": "APPL"}, stream)
        return app

    def test_native_self_test_rejects_planted_invalid_signatures(self):
        self.assertEqual(S.main(["--self-test"]), 0)

    def test_new_version_has_different_hash_but_satisfies_old_identity(self):
        old = self.app("old")
        new = self.app("new", "2")
        S.sign(old, self.policy, self.state)
        report = S.sign(new, self.policy, self.state)
        self.assertTrue(report["signature_verified"])
        old_info = S.run(["/usr/bin/codesign", "-d", "--verbose=4", str(old)])
        new_info = S.run(["/usr/bin/codesign", "-d", "--verbose=4", str(new)])
        old_hash = next(line for line in old_info.splitlines() if line.startswith("CDHash="))
        new_hash = next(line for line in new_info.splitlines() if line.startswith("CDHash="))
        self.assertNotEqual(old_hash, new_hash)
        old_dr = S.run(["/usr/bin/codesign", "-d", "-r-", str(old)])
        old_rule = next(line.removeprefix("designated => ") for line in old_dr.splitlines()
                        if line.startswith("designated => "))
        S.run(["/usr/bin/codesign", "--verify", "-R", "=" + old_rule, str(new)])
        self.assertEqual(report["camera_permission_persistence"], "requires_live_upgrade_test")

    def test_adhoc_with_same_bundle_id_is_rejected(self):
        app = self.app("adhoc")
        S.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)])
        with self.assertRaises(ValueError):
            S.verify(app, self.policy)

    def test_different_certificate_with_same_bundle_id_is_rejected(self):
        state = self.root / "different-private"
        policy = S.initialize(state, self.root / "different-policy.json")
        try:
            app = self.app("different-signer")
            S.sign(app, policy, state)
            with self.assertRaises(ValueError):
                S.verify(app, self.policy)
        finally:
            S.run(["/usr/bin/security", "delete-keychain", str(state / "identity.keychain-db")])

    def test_incorrect_embedded_identity_rule_is_rejected(self):
        app = self.app("broad")
        with S.signing_keychain(self.policy, self.state) as keychain:
            S.run(["/usr/bin/codesign", "--force", "--sign", self.policy["certificate_sha1"],
                   "--keychain", str(keychain), "--timestamp=none", "--requirements",
                   '=designated => identifier "com.skyrocket.app"', str(app)])
        with self.assertRaisesRegex(ValueError, "pin both"):
            S.verify(app, self.policy)

    def test_bundle_mismatch_is_rejected_before_signing(self):
        app = self.app("wrongbundle", bundle_id="com.example.other")
        with self.assertRaisesRegex(ValueError, "different application"):
            S.sign(app, self.policy, self.state)

    def test_tampered_artifact_is_rejected(self):
        app = self.app("tampered")
        S.sign(app, self.policy, self.state)
        with (app / "Contents/Info.plist").open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaises(ValueError):
            S.verify(app, self.policy)

    def test_wrong_certificate_material_and_public_directory_are_rejected(self):
        wrong = copy.deepcopy(self.policy)
        wrong["certificate_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "differs"):
            S.check_material(wrong, self.state)
        self.state.chmod(0o755)
        try:
            with self.assertRaisesRegex(ValueError, "private"):
                S.check_material(self.policy, self.state)
        finally:
            self.state.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
