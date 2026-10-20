"""Credential scrubbing shared by local logs and exported diagnostics."""
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

_SECRET_HINTS = ("token", "secret", "password", "passwd", "apikey", "api_key",
                 "api-key", "cookie", "auth", "ticket", "session", "jwt", "sign")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+")
_VALUE_RE = re.compile(
    r"([\"']?(?:token|api[_-]?key|secret|sign(?:ature)?|password|passwd|credential|cookie|auth(?:code)?|session|jwt|ticket)"
    r"[\"']?\s*[:=]\s*)(?:\"(?:\\.|[^\"\\\r\n])*\"?|'(?:\\.|[^'\\\r\n])*'?|[^\s,;&\"']+)",
    re.IGNORECASE)


def secret_query_name(name):
    decoded = str(name)
    for _ in range(3):
        value = unquote(decoded)
        if value == decoded:
            break
        decoded = value
    lowered = decoded.casefold()
    return lowered == "key" or any(hint in lowered for hint in _SECRET_HINTS)


def redact_url(value, marker="***REDACTED***"):
    """Keep only the origin and a literal template hint, never opaque URL data."""
    try:
        if any(ord(char) < 32 or char in "\u200b\ufeff" for char in str(value)):
            return marker
        parts = urlsplit(value)
        if not parts.scheme or not parts.netloc:
            return value
        if not parts.hostname or parts.port is not None and not 0 < parts.port < 65536:
            return marker
        # Parameter names cannot establish whether a path, query or fragment is public.
        path = ""
        if parts.path or parts.query or parts.fragment:
            path = "/" + quote(marker, safe="*")
            if "%s" in parts.path + parts.query + parts.fragment:
                path += "/%s"
        return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1],
                           path, "", ""))
    except (ValueError, TypeError, UnicodeError):
        return marker


def redact_urls_in_text(text, marker="<REDACTED>"):
    return _URL_RE.sub(lambda match: redact_url(match.group(0), marker), str(text))


def scrub_secrets(text, marker="<REDACTED>"):
    result = redact_urls_in_text(text, marker)
    result = re.sub(
        r"[\"']?\bauthorization\b[\"']?\s*[:=]\s*"
        r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|digest[^\r\n]*|(?:bearer\s+|basic\s+)?[^\s,;]+)",
        "Authorization=" + marker, result, flags=re.IGNORECASE)
    return _VALUE_RE.sub(lambda match: match.group(1) + marker, result)
