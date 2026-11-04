#!/usr/bin/env python3
"""Fail before packaging if public release documents are missing or stale."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from release_identity import NOTES_MAX_CHARS, count_notes_chars, notes_over_limit_detail  # noqa: E402
from sf_constants import APP_VERSION  # noqa: E402
from sf_update import require_numbered_notes, UpdateError  # noqa: E402

GPL_SHA256 = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
REQUIRED = {
    "README.md": ("这版改了什么", "未签名、未公证", "隐私说明", "行情接口使用边界",
                  "GPL-3.0-only", "SECURITY.md", "发送前", "第三方"),
    "docs/INSTALL.md": ("未签名、未公证", "Apple Developer ID", "ad-hoc", "SHA-256",
                        "诊断包", "第三方", "SECURITY.md"),
    "SECURITY.md": ("支持范围", "私下报告漏洞", "security/advisories/new", "校验失败"),
    "NOTICE.md": ("PyQt6", "GPLv3", "D-DIN"),
}

# 2026-11-02 走查 F-J-01：README 的下载链接与校验文件名也必须是本版。
# 1.4.109 发版只改了 README 的版本头与变更列表，下载行停在 1.4.108，而旧判据只查
# “第一个 vX.Y.Z”与 INSTALL.md 的文件名，于是新用户点下载拿到上一版、还按上一版的校验文件核对。
README_ARTIFACT_PATTERNS = (
    ("dmg file name", r"Skyrocket-V(\d+\.\d+\.\d+)\.dmg"),
    ("checksum file name", r"Skyrocket-(\d+\.\d+\.\d+)-SHA256SUMS\.txt"),
)


def artifact_version_problems(name: str, text: str, version: str) -> list[str]:
    """Every download/dmg/checksum version token in ``text`` must equal ``version``."""
    bad = []
    visible = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    visible = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1[^\n]*$", "", visible)
    links = re.findall(r"\[([^\]]+)\]\(([^\s)]+)\)", visible)
    urls = [url for _, url in links
            if (urlsplit(url).scheme, urlsplit(url).netloc, urlsplit(url).path)
            == ("https", "skyrocket-downloads.pages.dev", "/download")]
    if not urls:
        bad.append("%s: missing current download link" % name)
    for label, url in links:
        if (re.search(r"Skyrocket-V\d+\.\d+\.\d+\.dmg", label, re.I)
                or urlsplit(url).path.lower().endswith(".dmg")) and url not in urls:
            bad.append("%s: download link has unexpected destination" % name)
    for url in urls:
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        if (query.get("version") != [version]
                or query.get("asset") != [f"Skyrocket-V{version}.dmg"]):
            bad.append("%s: download link has incorrect version or asset" % name)
    for label, pattern in README_ARTIFACT_PATTERNS:
        found = re.findall(pattern, text)
        if not found:
            bad.append("%s: missing current %s" % (name, label))
        for token in sorted(set(found) - {version}):
            bad.append("%s: %s points to v%s, not v%s" % (name, label, token, version))
    return bad


def problems(root: Path, version: str) -> list[str]:
    bad = []
    texts = {}
    for name, tokens in REQUIRED.items():
        path = root / name
        if not path.is_file():
            bad.append("missing " + name)
            continue
        text = path.read_text(encoding="utf-8")
        texts[name] = text
        for token in tokens:
            if token not in text:
                bad.append("%s: missing %s" % (name, token))
        if re.search(r"/Users/(?!example(?:/|\b)|<)[^/\s]+/", text):
            bad.append(name + ": personal absolute path")
        if "%s" in text or "SF_CONTACT_EMAIL" in text:
            bad.append(name + ": unresolved placeholder")
    for name in ("README.md", "docs/INSTALL.md"):
        versions = re.findall(r"\bv(\d+\.\d+\.\d+)", texts.get(name, ""))
        if not versions or versions[0] != version:
            bad.append(name + ": current version does not match " + version)
    if "README.md" in texts:
        bad.extend(artifact_version_problems("README.md", texts["README.md"], version))
    license_file = root / "LICENSE"
    if not license_file.is_file() or hashlib.sha256(license_file.read_bytes()).hexdigest() != GPL_SHA256:
        bad.append("LICENSE: missing or not the complete GPLv3 text")
    notes_file = root / "release_notes.txt"
    notes = notes_file.read_text(encoding="utf-8") if notes_file.is_file() else ""
    section = re.search(r"^V%s\s+[^\n]+\n(.*?)(?=^V\d+\.\d+\.\d+\s|\Z)"
                        % re.escape(version), notes, re.M | re.S)
    if not section or not section[1].strip():
        bad.append("release_notes.txt: missing this release's changes")
    elif section[1].strip() not in texts.get("README.md", ""):
        bad.append("README.md: changes do not match this release's notes")
    if section:
        try:
            require_numbered_notes(section[1].strip())
        except UpdateError as exc:
            bad.append("release_notes.txt: " + str(exc))
        used = count_notes_chars(section[0])
        if used > NOTES_MAX_CHARS:
            bad.append("release_notes.txt: this release is " + notes_over_limit_detail(used, NOTES_MAX_CHARS))
    guide = texts.get("docs/INSTALL.md", "")
    if "本构建仅用于本机覆盖安装" not in guide:
        for filename in (f"Skyrocket-V{version}.dmg", f"Skyrocket-{version}-SHA256SUMS.txt"):
            if filename not in guide:
                bad.append("docs/INSTALL.md: missing current artifact " + filename)
    return bad


def self_test() -> int:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="sfdocs_") as directory:
        root = Path(directory)
        for name in (*REQUIRED, "LICENSE", "release_notes.txt"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / name).read_bytes())
        if problems(root, APP_VERSION):
            return 1
        for name in REQUIRED:
            path = root / name
            saved_bytes = path.read_bytes()
            path.unlink()
            if not problems(root, APP_VERSION):
                return 1
            path.write_bytes(saved_bytes)
        readme = root / "README.md"
        original = readme.read_text(encoding="utf-8")
        for broken in (original.replace("v" + APP_VERSION, "v0.0.0", 1),
                       original.replace("未签名、未公证", "omitted"),
                       original + "\n/Users/real-user/private/file\n",
                       original + "\n%s\n",
                       original.replace("V" + APP_VERSION + ".dmg", "V0.0.0.dmg"),
                       original.replace("version=" + APP_VERSION, "version=0.0.0"),
                       original.replace("Skyrocket-" + APP_VERSION + "-SHA256SUMS", "Skyrocket-0.0.0-SHA256SUMS")):
            readme.write_text(broken, encoding="utf-8")
            if not problems(root, APP_VERSION):
                return 1
        readme.write_text(original, encoding="utf-8")
        (root / "LICENSE").write_text("GPL summary", encoding="utf-8")
        if not problems(root, APP_VERSION):
            return 1
    print("public_docs_check: planted missing/stale/private/license defects rejected")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--version", default=APP_VERSION)
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    bad = problems(args.root, args.version)
    for error in bad:
        print("FAIL: " + error, file=sys.stderr)
    if not bad:
        print("Public documents match v%s; license, privacy and security present" % args.version)
    return int(bool(bad))


if __name__ == "__main__":
    sys.exit(main())
