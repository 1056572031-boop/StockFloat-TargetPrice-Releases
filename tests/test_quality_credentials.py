"""Keychain migration is verified before plaintext is removed from JSON."""
import json
import os
import tempfile

import sf_credentials
from sf_config import _atomic_write_json
from tests._base import SFCase


class MemoryKeychain:
    def __init__(self, fail=False, mismatch=False):
        self.values = {}
        self.fail = fail
        self.mismatch = mismatch

    def put(self, account, value):
        if self.fail:
            raise sf_credentials.KeychainError("locked")
        self.values[account] = value

    def get(self, account):
        return "mismatch" if self.mismatch else self.values[account]


class CredentialQualityTests(SFCase):
    def test_encoded_secret_and_userinfo_require_secure_storage(self):
        for url in ("https://example.invalid/?%74oken=secret", "https://user:pass@example.invalid/q"):
            self.assertTrue(sf_credentials.needs_keychain(url))
        self.assertFalse(sf_credentials.needs_keychain("https://example.invalid/?code=%s"))

    def test_main_and_backup_are_sealed_without_changing_runtime_url(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.json")
            url = "https://example.invalid/?token=CANARY&code=%s"
            payload = {"version": 1, "api_urls": {"quote": url}, "alerts": ["keep"]}
            _atomic_write_json(path, payload)
            _atomic_write_json(path + ".bak", payload)
            keychain = MemoryKeychain()
            self.assertEqual(sf_credentials.resolve_config_urls(path, payload, keychain=keychain)["quote"], url)
            for filename in (path, path + ".bak"):
                with open(filename, encoding="utf-8") as stream:
                    raw = stream.read()
                self.assertNotIn("CANARY", raw)
                self.assertIn("keychain://", raw)
            with open(path, encoding="utf-8") as stream:
                sealed = json.load(stream)
            self.assertEqual(sealed["alerts"], ["keep"])
            self.assertEqual(sf_credentials.resolve_config_urls(path, sealed, keychain=keychain)["quote"], url)

    def test_failure_or_mismatched_readback_preserves_original(self):
        for keychain in (MemoryKeychain(fail=True), MemoryKeychain(mismatch=True)):
            with tempfile.TemporaryDirectory() as root:
                path = os.path.join(root, "config.json")
                payload = {"api_urls": {"quote": "https://example.invalid/?token=CANARY"}}
                _atomic_write_json(path, payload)
                sf_credentials.resolve_config_urls(path, payload, keychain=keychain)
                with open(path, encoding="utf-8") as stream:
                    self.assertEqual(json.load(stream), payload)

    def test_future_config_is_never_rewritten_for_migration(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.json")
            payload = {"version": sf_credentials.CONFIG_SCHEMA_VERSION + 1,
                       "api_urls": {"quote": "https://example.invalid/?token=CANARY"}}
            _atomic_write_json(path, payload)
            keychain = MemoryKeychain()
            sf_credentials.resolve_config_urls(path, payload, keychain=keychain)
            self.assertEqual(keychain.values, {})
