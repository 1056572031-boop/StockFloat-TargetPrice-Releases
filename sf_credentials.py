"""Private API URLs in the system Keychain; JSON contains only opaque references."""
import hashlib
import re
from urllib.parse import urlsplit

from sf_constants import CONFIG_SCHEMA_VERSION
from sf_logging import log_line

SERVICE = "com.skyrocket.private-api-urls"
REFERENCE_PREFIX = "keychain://skyrocket/api/"
_REFERENCE = re.compile(r"keychain://skyrocket/api/([a-f0-9]{64})\Z")
_failure_count = 0
_last_error = ""
_last_storage_mode = "unused"


class KeychainError(OSError):
    pass


class MacKeychain:
    @staticmethod
    def _framework():
        import Security
        return Security

    @staticmethod
    def _status(result):
        return int(result[0] if isinstance(result, tuple) else result)

    def _query(self, account, protected):
        security = self._framework()
        query = {security.kSecClass: security.kSecClassGenericPassword,
                 security.kSecAttrService: SERVICE, security.kSecAttrAccount: account,
                 security.kSecUseAuthenticationUI: security.kSecUseAuthenticationUIFail}
        if protected:
            query[security.kSecUseDataProtectionKeychain] = True
        return query

    def put(self, account, value):
        from Foundation import NSData
        security = self._framework()
        data = value.encode("utf-8")
        for protected in (True, False):
            query = self._query(account, protected)
            attrs = dict(query)
            attrs[security.kSecValueData] = NSData.dataWithBytes_length_(data, len(data))
            if protected:
                attrs[security.kSecAttrAccessible] = security.kSecAttrAccessibleWhenUnlockedThisDeviceOnly
                attrs[security.kSecAttrSynchronizable] = False
            status = self._status(security.SecItemAdd(attrs, None))
            if status == security.errSecDuplicateItem:
                status = self._status(security.SecItemUpdate(
                    query, {security.kSecValueData: attrs[security.kSecValueData]}))
            if status == 0:
                global _last_storage_mode
                _last_storage_mode = "data-protection" if protected else "login-keychain-default-app-acl"
                return
            if not protected or status not in (-34018, -50):
                raise KeychainError("Keychain store failed (%d)" % status)

    def get(self, account):
        security = self._framework()
        for protected in (True, False):
            query = self._query(account, protected)
            query[security.kSecReturnData] = True
            query[security.kSecMatchLimit] = security.kSecMatchLimitOne
            result = security.SecItemCopyMatching(query, None)
            status = self._status(result)
            if status == 0 and isinstance(result, tuple):
                return bytes(result[1]).decode("utf-8")
            if not protected or status not in (-34018, -50, -25300):
                raise KeychainError("Keychain read failed (%d)" % status)
        raise KeychainError("Keychain item is unavailable")

    def delete(self, account):
        security = self._framework()
        for protected in (True, False):
            status = self._status(security.SecItemDelete(self._query(account, protected)))
            if status not in (0, -34018, -50, -25300):
                raise KeychainError("Keychain delete failed (%d)" % status)


def needs_keychain(url):
    """All custom HTTPS destinations are private, regardless of credential syntax."""
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and bool(parts.hostname)
                and not any(char.isspace() or ord(char) < 32 for char in url))
    except (TypeError, ValueError):
        return False


def seal_url(url, keychain):
    if len(url) > 8192 or not needs_keychain(url):
        raise KeychainError("Private API URL is invalid")
    account = hashlib.sha256(url.encode("utf-8")).hexdigest()
    keychain.put(account, url)
    if keychain.get(account) != url:
        raise KeychainError("Keychain readback mismatch; original configuration preserved")
    return REFERENCE_PREFIX + account


def resolve_config_urls(path, config, *, keychain=None):
    """Seal and read back before replacing JSON; never remove a usable original on failure."""
    from sf_config import _atomic_write_json, _read_json_or_none
    keychain = keychain or MacKeychain()
    entries = config.get("api_urls", {})
    if not isinstance(entries, dict):
        return {}
    stored, resolved = dict(entries), {}
    may_write = not (isinstance(config.get("version"), int)
                     and config["version"] > CONFIG_SCHEMA_VERSION)
    for key, value in entries.items():
        if not isinstance(value, str):
            continue
        try:
            reference = _REFERENCE.fullmatch(value)
            if reference:
                resolved[key] = keychain.get(reference.group(1))
            elif needs_keychain(value):
                resolved[key] = value
                if may_write:
                    stored[key] = seal_url(value, keychain)
        except Exception as exc:
            global _failure_count, _last_error
            _failure_count += 1
            _last_error = type(exc).__name__
            log_line("private API secure storage unavailable (%s); original configuration preserved"
                     % _last_error, "WARNING")
    if may_write:
        if stored != entries:
            # Preserve fields saved since the caller took its startup snapshot.
            payload = _read_json_or_none(path)
            if (isinstance(payload, dict) and payload.get("api_urls") == entries
                    and not (isinstance(payload.get("version"), int)
                             and payload["version"] > CONFIG_SCHEMA_VERSION)):
                payload["api_urls"] = stored
                _atomic_write_json(path, payload)
        backup_path = path + ".bak"
        backup = _read_json_or_none(backup_path)
        if (isinstance(backup, dict) and isinstance(backup.get("api_urls"), dict)
                and not (isinstance(backup.get("version"), int)
                         and backup["version"] > CONFIG_SCHEMA_VERSION)):
            backup_api = dict(backup["api_urls"])
            for key, value in backup_api.items():
                if isinstance(value, str) and needs_keychain(value):
                    try:
                        backup_api[key] = seal_url(value, keychain)
                    except Exception as exc:
                        _failure_count += 1
                        _last_error = type(exc).__name__
                        log_line("private API backup secure storage unavailable (%s); backup preserved"
                                 % _last_error, "WARNING")
            if backup_api != backup["api_urls"]:
                backup["api_urls"] = backup_api
                _atomic_write_json(backup_path, backup)
    return resolved
