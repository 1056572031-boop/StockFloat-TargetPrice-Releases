"""Public configuration never needs permission; private storage never prompts."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

from tests._base import SFCase
import sf_credentials as C
from sf_config import _atomic_write_json
from sf_constants import API_URLS_DEFAULT
from tests.test_quality_credentials import MemoryKeychain


class PublicURLTests(SFCase):
    def resolve(self, entries, vault, *, version=1, backup=None):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "config.json")
            payload = {"version": version, "api_urls": entries, "alerts": ["keep"]}
            _atomic_write_json(path, payload)
            _atomic_write_json(path + ".bak", dict(payload, api_urls=backup or entries))
            resolved = C.resolve_config_urls(path, payload, keychain=vault)
            return resolved, [json.loads(Path(name).read_text())
                              for name in (path, path + ".bak")]

    def test_all_public_defaults_need_no_keychain_calls(self):
        vault = mock.Mock()
        resolved, files = self.resolve(API_URLS_DEFAULT, vault)
        self.assertEqual(resolved, API_URLS_DEFAULT)
        self.assertEqual(vault.mock_calls, [])
        self.assertTrue(all(p["api_urls"] == API_URLS_DEFAULT for p in files))
        for url in API_URLS_DEFAULT.values():
            self.assertFalse(C.needs_keychain(url))

    def test_previously_sealed_defaults_recover_without_keychain(self):
        entries = {key: C.REFERENCE_PREFIX + hashlib.sha256(url.encode()).hexdigest()
                   for key, url in API_URLS_DEFAULT.items()}
        vault = mock.Mock()
        resolved, files = self.resolve(entries, vault)
        self.assertEqual(resolved, API_URLS_DEFAULT)
        self.assertEqual(vault.mock_calls, [])
        for payload in files:
            self.assertEqual(payload["api_urls"], API_URLS_DEFAULT)
            self.assertEqual(payload["alerts"], ["keep"])

    def test_previous_public_minute_reference_needs_no_permission(self):
        url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=%s"
        reference = C.REFERENCE_PREFIX + hashlib.sha256(url.encode()).hexdigest()
        vault = mock.Mock()
        resolved, files = self.resolve({"minute": reference}, vault)
        self.assertEqual(resolved, {"minute": API_URLS_DEFAULT["minute"]})
        self.assertEqual(files[0]["api_urls"], resolved)
        self.assertEqual(vault.mock_calls, [])

    def test_future_schema_recovers_public_runtime_without_rewriting(self):
        url = API_URLS_DEFAULT["quote"]
        entries = {"quote": C.REFERENCE_PREFIX + hashlib.sha256(url.encode()).hexdigest()}
        vault = mock.Mock()
        resolved, files = self.resolve(entries, vault, version=C.CONFIG_SCHEMA_VERSION + 1)
        self.assertEqual(resolved, {"quote": url})
        self.assertTrue(all(p["api_urls"] == entries for p in files))
        self.assertEqual(vault.mock_calls, [])

    def test_same_host_or_default_with_extra_secret_is_still_private(self):
        for url in (API_URLS_DEFAULT["quote"] + "&token=CANARY",
                    "https://qt.gtimg.cn/CANARY", "https://qt.gtimg.cn/q=CANARY",
                    "https://qt.gtimg.cn/q=%s#CANARY"):
            with self.subTest(url=url):
                vault = MemoryKeychain()
                resolved, files = self.resolve({"quote": url}, vault)
                self.assertEqual(resolved, {"quote": url})
                self.assertTrue(C.needs_keychain(url))
                self.assertNotIn("CANARY", json.dumps(files))
                self.assertEqual(len(vault.values), 1)

    def test_duplicate_custom_values_are_written_and_read_only_once(self):
        vault = MemoryKeychain()
        url = "https://example.invalid/CANARY?code=%s"
        with mock.patch.object(vault, "put", wraps=vault.put) as put, \
                mock.patch.object(vault, "get", wraps=vault.get) as get:
            resolved, _ = self.resolve({"quote": url, "minute": url}, vault)
        self.assertEqual(resolved, {"quote": url, "minute": url})
        self.assertEqual((put.call_count, get.call_count), (1, 1))

    def test_duplicate_references_are_read_once(self):
        vault = MemoryKeychain()
        url = "https://example.invalid/CANARY"
        reference = C.seal_url(url, vault)
        with mock.patch.object(vault, "get", wraps=vault.get) as get:
            resolved, _ = self.resolve({"quote": reference, "minute": reference}, vault)
        self.assertEqual(resolved, {"quote": url, "minute": url})
        get.assert_called_once()

    def test_missing_reference_does_not_block_a_different_valid_account(self):
        vault = mock.Mock()
        url = "https://example.invalid/CANARY"
        vault.get.side_effect = [C.KeychainItemUnavailable("missing (-25300)"), url]
        entries = {"quote": C.REFERENCE_PREFIX + "a" * 64,
                   "minute": C.REFERENCE_PREFIX + "b" * 64}
        with mock.patch.object(C, "log_line"):
            resolved, files = self.resolve(entries, vault)
        self.assertEqual(resolved, {"minute": url})
        self.assertEqual(vault.get.call_args_list, [mock.call("a" * 64), mock.call("b" * 64)])
        vault.put.assert_not_called()
        self.assertTrue(all(p["api_urls"] == entries for p in files))

    def test_first_denial_stops_main_and_backup_retries_but_recovers_defaults(self):
        vault = mock.Mock()
        vault.get.side_effect = C.KeychainError("denied")
        entries = {"quote": C.REFERENCE_PREFIX + "a" * 64,
                   "search": C.REFERENCE_PREFIX + "b" * 64,
                   "minute": "https://example.invalid/CANARY",
                   "quote_fallback": API_URLS_DEFAULT["quote_fallback"]}
        with mock.patch.object(C, "log_line") as logged:
            resolved, files = self.resolve(entries, vault)
        vault.get.assert_called_once_with("a" * 64)
        vault.put.assert_not_called()
        self.assertEqual(resolved, {"minute": entries["minute"],
                                    "quote_fallback": entries["quote_fallback"]})
        self.assertTrue(all(p["api_urls"] == entries for p in files))
        self.assertNotIn("CANARY", str(logged.call_args_list))


class NoninteractiveKeychainTests(SFCase):
    def setUp(self):
        super().setUp()
        self.security = SimpleNamespace(
            SecKeychainGetUserInteractionAllowed=mock.Mock(return_value=(0, True)),
            SecKeychainSetUserInteractionAllowed=mock.Mock(return_value=0))
        self.vault = C.MacKeychain()
        self.track_patch(mock.patch.object(self.vault, "_framework", return_value=self.security))

    def test_get_put_delete_disable_and_restore_interaction(self):
        for method, args in (("get", ("account",)), ("put", ("account", "CANARY")),
                             ("delete", ("account",))):
            with self.subTest(method=method):
                setter = self.security.SecKeychainSetUserInteractionAllowed
                setter.reset_mock()

                def operation(*values):
                    self.assertEqual(values, args)
                    self.assertEqual(setter.call_args_list, [mock.call(False)])
                    return "result"

                with mock.patch.object(self.vault, "_" + method, side_effect=operation):
                    result = getattr(self.vault, method)(*args)
                if method == "get":
                    self.assertEqual(result, "result")
                self.assertEqual(setter.call_args_list, [mock.call(False), mock.call(True)])

    def test_operation_exception_still_restores_original_policy(self):
        with mock.patch.object(self.vault, "_get", side_effect=C.KeychainError("denied")):
            with self.assertRaises(C.KeychainError):
                self.vault.get("account")
        self.assertEqual(self.security.SecKeychainSetUserInteractionAllowed.call_args_list,
                         [mock.call(False), mock.call(True)])

    def test_already_disabled_policy_is_not_enabled_after_access(self):
        self.security.SecKeychainGetUserInteractionAllowed.return_value = (0, False)
        with mock.patch.object(self.vault, "_get", return_value="CANARY"):
            self.assertEqual(self.vault.get("account"), "CANARY")
        self.assertEqual(self.security.SecKeychainSetUserInteractionAllowed.call_args_list,
                         [mock.call(False), mock.call(False)])

    def test_cannot_read_or_disable_policy_means_no_item_access(self):
        for result, setter_status in (((-1, True), 0), (0, 0), ((0, True), -1)):
            with self.subTest(result=result, setter_status=setter_status):
                self.security.SecKeychainGetUserInteractionAllowed.return_value = result
                self.security.SecKeychainSetUserInteractionAllowed.return_value = setter_status
                with mock.patch.object(self.vault, "_get") as get:
                    with self.assertRaises(C.KeychainError):
                        self.vault.get("account")
                get.assert_not_called()

    def test_restore_failure_is_reported(self):
        self.security.SecKeychainSetUserInteractionAllowed.side_effect = [0, -1]
        with mock.patch.object(self.vault, "_get", return_value="CANARY"):
            with self.assertRaisesRegex(C.KeychainError, "restore"):
                self.vault.get("account")


class SecurityBackendTests(SFCase):
    def setUp(self):
        super().setUp()
        names = ("kSecClass", "kSecClassGenericPassword", "kSecAttrService", "kSecAttrAccount",
                 "kSecUseAuthenticationUI", "kSecUseAuthenticationUIFail",
                 "kSecUseDataProtectionKeychain", "kSecAttrAccessible",
                 "kSecAttrAccessibleWhenUnlockedThisDeviceOnly", "kSecAttrSynchronizable",
                 "kSecValueData", "kSecReturnData", "kSecMatchLimit", "kSecMatchLimitOne")
        self.allowed = True
        self.security = SimpleNamespace(**{name: name for name in names}, errSecDuplicateItem=-25299)
        self.security.SecKeychainGetUserInteractionAllowed = lambda _: (0, self.allowed)

        def set_allowed(allowed):
            self.allowed = allowed
            return 0

        self.security.SecKeychainSetUserInteractionAllowed = set_allowed
        self.vault = C.MacKeychain()
        self.track_patch(mock.patch.object(self.vault, "_framework", return_value=self.security))

    def checked(self, result):
        def operation(query, *args):
            self.assertFalse(self.allowed, "Legacy Keychain would be allowed to open a password dialog")
            self.assertEqual(query["kSecUseAuthenticationUI"], "kSecUseAuthenticationUIFail")
            return result
        return operation

    def test_read_fallback_is_noninteractive_and_returns_private_url(self):
        copy = mock.Mock(side_effect=[self.checked((-34018, None)), self.checked((0, b"CANARY"))])
        self.security.SecItemCopyMatching = lambda *args: copy(*args)(*args)
        self.assertEqual(self.vault.get("account"), "CANARY")
        self.assertEqual(copy.call_count, 2)
        self.assertTrue(self.allowed)


    def test_final_missing_item_has_a_specific_error(self):
        for first_status in (-25300, -34018, -50):
            with self.subTest(first_status=first_status):
                copy = mock.Mock(side_effect=[self.checked((first_status, None)),
                                              self.checked((-25300, None))])
                self.security.SecItemCopyMatching = lambda *args: copy(*args)(*args)
                with self.assertRaises(C.KeychainItemUnavailable):
                    self.vault.get("account")
                self.assertEqual(copy.call_count, 2)
                self.assertTrue(self.allowed)

    def test_login_denial_does_not_retry_or_relax_policy(self):
        copy = mock.Mock(side_effect=[self.checked((-25300, None)), self.checked((-25308, None))])
        self.security.SecItemCopyMatching = lambda *args: copy(*args)(*args)
        with self.assertRaisesRegex(C.KeychainError, "-25308") as raised:
            self.vault.get("account")
        self.assertNotIsInstance(raised.exception, C.KeychainItemUnavailable)
        self.assertEqual(copy.call_count, 2)
        self.assertTrue(self.allowed)

    def test_write_fallback_and_duplicate_update_are_noninteractive(self):
        add = mock.Mock(side_effect=[self.checked(-50), self.checked(-25299)])
        self.security.SecItemAdd = lambda *args: add(*args)(*args)
        self.security.SecItemUpdate = self.checked(0)
        self.vault.put("account", "CANARY")
        self.assertEqual(add.call_count, 2)
        self.assertTrue(self.allowed)

    def test_delete_both_backends_is_noninteractive(self):
        self.security.SecItemDelete = mock.Mock(side_effect=self.checked(0))
        self.vault.delete("account")
        self.assertEqual(self.security.SecItemDelete.call_count, 2)
        self.assertTrue(self.allowed)


class NativeKeychainTests(SFCase):
    def test_real_login_keychain_acl_denial_returns_without_prompt(self):
        if sys.platform != "darwin":
            self.skipTest("Requires the macOS Security framework")
        # Own a separate keychain and never edit a user's existing items or ACLs.
        with tempfile.TemporaryDirectory(prefix="sf_keychain_") as root:
            keychain = str(Path(root) / "isolated.keychain-db")
            password = secrets.token_hex(24)

            def run(*args):
                return subprocess.run(["/usr/bin/security", *args], capture_output=True, timeout=15)

            created = run("create-keychain", "-p", password, keychain)
            self.assertEqual(created.returncode, 0, "Cannot create isolated test keychain")
            try:
                added = run("add-generic-password", "-a", "no-prompt-test", "-s", C.SERVICE,
                            "-w", "CANARY", "-T", "/usr/bin/security", keychain)
                self.assertEqual(added.returncode, 0, "Cannot create isolated ACL fixture")
                env = dict(os.environ, SF_TEST_KEYCHAIN=keychain)
                probe = subprocess.run([sys.executable, "-c", NATIVE_PROBE], env=env,
                                       capture_output=True, text=True, timeout=15)
                self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
                self.assertIn("denied without interaction", probe.stdout)
            finally:
                self.assertEqual(run("delete-keychain", keychain).returncode, 0,
                                 "Cannot retire isolated test keychain")


NATIVE_PROBE = '''
import os
import Security as S
from sf_credentials import MacKeychain, KeychainError
status, keychain = S.SecKeychainOpen(os.fsencode(os.environ["SF_TEST_KEYCHAIN"]), None)
assert status == 0
before = S.SecKeychainGetUserInteractionAllowed(None)
class IsolatedKeychain(MacKeychain):
    def _query(self, account, protected):
        query = super()._query(account, protected)
        query.pop(S.kSecUseDataProtectionKeychain, None)
        query[S.kSecMatchSearchList] = [keychain]
        return query
try:
    IsolatedKeychain().get("no-prompt-test")
except KeychainError as exc:
    assert "-25308" in str(exc) or "-25293" in str(exc), str(exc)
    print("denied without interaction")
else:
    raise AssertionError("Foreign ACL unexpectedly allowed a private read")
assert S.SecKeychainGetUserInteractionAllowed(None) == before
'''
