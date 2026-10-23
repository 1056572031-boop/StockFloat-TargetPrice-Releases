#!/usr/bin/env python3
"""Fail before packaging if public release documents are missing or stale."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from release_identity import NOTES_MAX_CHARS  # noqa: E402
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
        if len(re.sub(r"\s", "", section[0])) > NOTES_MAX_CHARS:
            bad.append("release_notes.txt: this release exceeds %d characters" % NOTES_MAX_CHARS)
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
                       original + "\n%s\n"):
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
