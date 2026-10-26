"""Bounded, read-only validation of the same ZIP produced by diagnostic export."""

from __future__ import annotations

import io
import json
import math
import re
import stat
import unicodedata
import zipfile

from sf_feedback import PrivacyRejected, check_zip_directory
from sf_feedback_v3 import V3_DIAGNOSTIC_FILES, is_v3_export, validate_snapshot
from sf_feedback_versions import POLICY_V2, POLICY_V3

FULL_CONSENT_VERSION = POLICY_V2
FULL_V3_CONSENT_VERSION = POLICY_V3
MAX_DIAGNOSTIC_BYTES = 32 * 1024 * 1024
MAX_DIAGNOSTIC_EXPANDED = 32 * 1024 * 1024
DIAGNOSTIC_JSON = frozenset({
    "network.json", "startup_health.json", "feedback.json", "feedback_ui.json", "targets.json",
    "runtime.json", "glass.json", "notify.json", "universe.json", "calendar.json",
    "state.json", "storage.json", "config.json", "last_failures.json", "export.json",
    *V3_DIAGNOSTIC_FILES,
})
REQUIRED_DIAGNOSTICS = DIAGNOSTIC_JSON - {"config.json", "last_failures.json", *V3_DIAGNOSTIC_FILES}
_CREDENTIAL = re.compile(
    r'''(?<![\w])["']?(password|passwd|token|api[_-]?key|private[_-]?key|cookie|authorization)["']?\s*[:=]\s*(?:Bearer\s+)?("[^"\r\n]*"?|'[^'\r\n]*'?|[^\s,;&"'{}()\[\]\\]+)''',
    re.IGNORECASE)


def check_privacy(text):
    if re.search(r"-----(?:BEGIN|END) " + r"(?:[A-Z]+ )?PRIVATE KEY-----", text):
        raise PrivacyRejected("诊断包含未脱敏私钥")
    if re.search(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", text):
        raise PrivacyRejected("诊断包含未脱敏凭据")
    for match in _CREDENTIAL.finditer(text):
        value = match.group(2).strip("\"'")
        if match.group(1).casefold() == "authorization" and value in (
                "authorized", "denied", "notDetermined", "provisional", "ephemeral", "unknown", "no_bundle"):
            continue
        if value not in ("<REDACTED>", "***REDACTED***", "", "null", "None"):
            raise PrivacyRejected("诊断包含未脱敏凭据字段")


def member_limit(name):
    if name in DIAGNOSTIC_JSON:
        return 256 * 1024
    if name in ("diagnostics.txt", "last_failures.invalid.txt"):
        return 256 * 1024
    if re.fullmatch(r"debug\.log(?:\.[1-3])?", name):
        return 5 * 1024 * 1024
    if re.fullmatch(r"crash_reports/[^/\\\x00]{1,180}\.txt", name):
        return 256 * 1024
    raise PrivacyRejected("完整诊断包包含未批准附件")


def _json(text):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise PrivacyRejected("诊断JSON含重复字段")
            out[key] = value
        return out

    def invalid(value):
        raise PrivacyRejected("诊断JSON含非有限数")

    value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)

    def bounded(item, depth=1):
        if isinstance(item, (dict, list)) and depth > 32:
            raise PrivacyRejected("诊断JSON层级过深")
        if isinstance(item, dict):
            if len(item) > 4096 or any(len(key) > 4096 for key in item):
                raise PrivacyRejected("诊断JSON字段超限")
            for child in item.values():
                bounded(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                bounded(child, depth + 1)
        elif isinstance(item, (int, float)):
            try:
                finite = math.isfinite(item)
            except OverflowError:
                finite = False
            if not finite:
                raise PrivacyRejected("诊断JSON数值超限")
    bounded(value)
    return value


def read_diagnostics(data):
    """Never extract or execute content; limits apply before and after inflation."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_DIAGNOSTIC_BYTES:
        raise PrivacyRejected("完整诊断包大小不符合限制")
    files = {}
    sizes = {}
    expanded = 0
    try:
        check_zip_directory(io.BytesIO(data), 40)
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            entries = package.infolist()
            if not entries or len(entries) > 40:
                raise PrivacyRejected("完整诊断附件数量不符合限制")
            names = set()
            for entry in entries:
                name = entry.orig_filename
                key = unicodedata.normalize("NFC", name).casefold()
                mode = entry.external_attr >> 16
                if (key in names or name.startswith(("/", "."))
                        or ".." in name.split("/") or entry.is_dir()
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or mode & 0o111 or entry.flag_bits & 1
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise PrivacyRejected("完整诊断附件路径或类型不安全")
                names.add(key)
                limit = member_limit(name)
                expanded += entry.file_size
                if entry.file_size > limit or expanded > MAX_DIAGNOSTIC_EXPANDED:
                    raise PrivacyRejected("完整诊断解压大小超过限制")
                with package.open(entry) as stream:
                    body = stream.read(limit + 1)
                if len(body) != entry.file_size or len(body) > limit:
                    raise PrivacyRejected("完整诊断附件长度不一致")
                text = body.decode("utf-8")
                try:
                    check_privacy(text)
                except PrivacyRejected as exc:
                    raise PrivacyRejected(name + ": " + str(exc)) from exc
                files[name] = _json(text) if name in DIAGNOSTIC_JSON else text
                sizes[name] = len(body)
    except PrivacyRejected:
        raise
    except (zipfile.BadZipFile, UnicodeError, ValueError, RecursionError) as exc:
        raise PrivacyRejected("完整诊断附件损坏或编码错误") from exc
    if not {"diagnostics.txt", "export.json"}.issubset(files):
        raise PrivacyRejected("完整诊断缺少导出清单或概览")
    exported = files["export.json"]
    if not isinstance(exported, dict) or not isinstance(exported.get("issues"), list):
        raise PrivacyRejected("完整诊断采集结果无效")
    v3 = is_v3_export(exported)
    if v3:
        if not V3_DIAGNOSTIC_FILES.issubset(files):
            raise PrivacyRejected("诊断v3缺少固定快照")
        if any(not validate_snapshot(name, files[name]) for name in V3_DIAGNOSTIC_FILES):
            raise PrivacyRejected("诊断v3快照格式无效")
    elif any(name in files for name in V3_DIAGNOSTIC_FILES):
        raise PrivacyRejected("旧版诊断混入v3快照")
    if any(isinstance(issue, str) and any(marker in issue for marker in (
            "identity USER", "identity USERNAME", "getpass.getuser:", "expanduser:"))
           for issue in exported["issues"]):
        raise PrivacyRejected("无法确认身份脱敏，禁止上传")
    for missing in REQUIRED_DIAGNOSTICS - set(files):
        if not any(isinstance(issue, str) and missing in issue for issue in exported["issues"]):
            raise PrivacyRejected("诊断类别缺失且没有采集失败说明")
    declared = exported.get("files")
    if (not isinstance(declared, list) or any(not isinstance(item, dict)
            or not isinstance(item.get("name"), str) or type(item.get("size_bytes")) is not int for item in declared)
            or {item.get("name") for item in declared} != set(files) - {"export.json"}
            or len(declared) != len(files) - 1
            or any(item.get("size_bytes") != sizes.get(item.get("name")) for item in declared)):
        raise PrivacyRejected("完整诊断导出清单与实际附件不一致")
    return files
