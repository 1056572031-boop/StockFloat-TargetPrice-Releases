"""Credential scrubbing shared by local logs and exported diagnostics."""
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

_SECRET_HINTS = ("token", "secret", "password", "passwd", "apikey", "api_key",
                 "api-key", "private_key", "private-key", "privatekey", "cookie", "auth", "ticket", "session", "jwt", "sign")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+")
_VALUE_RE = re.compile(
    r"([\"']?(?:token|api[_-]?key|private[_-]?key|secret|sign(?:ature)?|password|passwd|credential|cookie|auth(?:code)?|session|jwt|ticket)"
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
        r"(?:\"(?:\\.|[^\"\\\r\n])*\"?|'(?:\\.|[^'\\\r\n])*'?|digest[^\r\n]*|(?:bearer\s+|basic\s+)?[^\s,;]+)",
        "Authorization=" + marker, result, flags=re.IGNORECASE)
    return _VALUE_RE.sub(lambda match: match.group(1) + marker, result)


# 前后边界只认 ASCII：Python 的 \w 和 \b 把汉字也当“单词字符”，“邮箱foo@example.com”里邮箱前面的汉字会让整条地址漏过去。
_EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,24}(?![A-Za-z0-9-])")
# 大陆手机号：1[3-9] 开头共 11 位，可带 +86/86 前缀，三段之间允许一个空格或短横线；
# 前后都不能是数字或字母（避开毫秒时间戳、十六进制编号）。
_PHONE_RE = re.compile(r"(?<![0-9A-Za-z])(?:\+?86[ -]?)?1[3-9][0-9][ -]?[0-9]{4}[ -]?[0-9]{4}(?![0-9A-Za-z])")
_IPV4_RE = re.compile(r"(?<![\w.])((?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3})(?![\w]|\.\d)")
# IPv6 先按形状捞候选，再交给标准库判断是不是真地址：时间戳“16:04:04”、MAC 地址“aa:bb:cc:dd:ee:ff”
# 形状相近，只有 ipaddress 能把它们挡在外面。至少 3 个冒号才当候选：“a::b”按语法也是合法地址，
# 但日志里它几乎都是代码符号；代价是 “2606::1” 这种只有两个冒号的极短公网地址不会被抹。
_IPV6_CANDIDATE_RE = re.compile(r"(?<![0-9A-Za-z:.])((?:[0-9A-Fa-f]{0,4}:){3,7}[0-9A-Fa-f]{0,4})(?![0-9A-Za-z:]|\.[0-9A-Fa-f])")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")


def _mask_ipv4(match):
    """保留前两段（能看出是哪个网络），后两段抹掉；本机/内网/未指定地址原样保留，排障要用。"""
    parts = match.group(1).split(".")
    first, second = int(parts[0]), int(parts[1])
    if first in (0, 127) or first == 10 or (first == 192 and second == 168) or (first == 172 and 16 <= second <= 31):
        return match.group(1)
    return "%s.%s.x.x" % (parts[0], parts[1])


def _mask_ipv6(match):
    """公网 IPv6 只留前两组；本机、内网（含 fc00::/7）、链路本地、未指定地址原样保留；不是合法地址的原样保留。"""
    import ipaddress
    try:
        address = ipaddress.IPv6Address(match.group(1))
    except ValueError:
        return match.group(1)
    if address.is_loopback or address.is_unspecified or address.is_link_local or address.is_private:
        return match.group(1)
    groups = address.exploded.split(":")
    return "%s:%s:x:x" % (groups[0].lstrip("0") or "0", groups[1].lstrip("0") or "0")


def scrub_pii(text, marker="<REDACTED>"):
    """日志附件专用：邮箱、手机号、公网 IP（v4/v6）、裸 JWT。

    **不用于用户自己写的问题描述**：用户常会主动留联系方式，抹掉等于替他做了反向决定。
    日志是程序写的，里面出现的邮箱、手机号、令牌都不是用户有意提供给维护者的。
    """
    result = _JWT_RE.sub(marker, str(text))
    result = _EMAIL_RE.sub("<email>", result)
    result = _PHONE_RE.sub("<phone>", result)
    result = _IPV6_CANDIDATE_RE.sub(_mask_ipv6, result)
    return _IPV4_RE.sub(_mask_ipv4, result)
