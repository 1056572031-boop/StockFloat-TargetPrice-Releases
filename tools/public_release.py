#!/usr/bin/env python3
"""Export and audit corresponding source without exposing private Git history."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sf_constants import APP_VERSION  # noqa: E402
from release_identity import REPO, SUPPORT_EMAIL, TAG  # noqa: E402
from public_docs_check import problems  # noqa: E402

PUBLIC_BRANCH = "public-source"
PUBLIC_FEEDBACK_REPO = b"1056572031-boop/Skyrocket-Private-Feedback"
PUBLIC_FEEDBACK_ORIGIN = b"https://skyrocket-feedback-1056572031.pages.dev"
EXCLUDED = ("AGENTS.md", "DELIVERY_CHECKLIST.md", "QC_CHECKLIST.md", "backups", "outputs",
            "TESTS_AUDIT_2026-09-14.md", "REFACTOR_CHANGES_V1.3.46.md", "tools/archive",
            "目标价功能技术方案_v6.md", "目标价功能评估_基于v1.2.74.md")
DOCS = ("README.md", "SECURITY.md", "LICENSE", "NOTICE.md", "docs/INSTALL.md",
        "docs/CONTRIBUTING.md")
CREDENTIALS = re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                         rb"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
PERSONAL_MAIL = re.compile(rb"[A-Za-z0-9._%+\-]+@(?:qq|foxmail|gmail|icloud|outlook|hotmail)\.com")
# 前后两侧都排除十六进制字母：锁文件里的 sha256 哈希是随机十六进制串，其中任意一段 11 位数字
# 都可能恰好以 13~19 开头。原先只在后面排除，哈希里“字母 + 13xxxxxxxxx + 空格”会被误判成手机号。
PHONE = re.compile(rb"(?<![0-9.,a-fA-F+\-])1[3-9][0-9]{9}(?![0-9.,a-fA-F])")
NUMERIC_CONTACT = re.compile(
    r"(?i)(?:qq|phone|mobile|personal[_ -]?id|电话|手机|旧号码|个人号码)"
    r"[\s:=\"']*([0-9]{5,12})(?![0-9])".encode())


def git_env() -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="Skyrocket maintainers", GIT_COMMITTER_NAME="Skyrocket maintainers",
               GIT_AUTHOR_EMAIL="skyrocket@users.noreply.github.com",
               GIT_COMMITTER_EMAIL="skyrocket@users.noreply.github.com")
    return env


def run(args, cwd=ROOT, binary=False):
    result = subprocess.run(args, cwd=cwd, env=git_env(), capture_output=True,
                            text=not binary, check=True)
    return result.stdout


def excluded(name: str) -> bool:
    return any(name == item or name.startswith(item + "/") for item in EXCLUDED)


def sanitize(text: str, home: str) -> str:
    # Keep the functional GitHub namespace in URLs; remove local identity instead.
    text = text.replace(home, "/Users/example")
    text = text.replace(".private_tools", ".private_tools")
    return text


def privacy_problems(data: bytes, home: str, secrets=()) -> list[str]:
    bad = []
    if any(secret and secret in data for secret in secrets):
        bad.append("release signing secret")
    if CREDENTIALS.search(data):
        bad.append("credential")
    # The explicitly approved support mailbox is public; do not exempt other contacts.
    public_data = re.sub(
        rb"(?<![A-Za-z0-9._%+\-])" + re.escape(SUPPORT_EMAIL.encode())
        + rb"(?![A-Za-z0-9._%+@\-])", b"public-support-mailbox", data)
    if PERSONAL_MAIL.search(public_data):
        bad.append("personal contact")
    # Exempt the exact repository slug and real GitHub owner URL components,
    # not arbitrary prose containing the owner's spelling or lookalike hosts.
    owner = REPO.split("/", 1)[0].encode()
    public_data = re.sub(
        rb"https://(?:github\.com|raw\.githubusercontent\.com|api\.github\.com/repos|"
        rb"uploads\.github\.com/repos)/" + re.escape(owner) + rb"(?=/|[\s\"')]|$)",
        b"https://github.com/maintainer", public_data)
    public_data = re.sub(
        rb"(?<![A-Za-z0-9_./\-])" + re.escape(REPO.encode())
        + rb"(?![A-Za-z0-9_.\-])", b"public-repository", public_data)
    # Only these approved functional namespaces; raw credentials are checked above.
    public_data = re.sub(
        rb"(?<![A-Za-z0-9_./:\-])" + re.escape(PUBLIC_FEEDBACK_REPO)
        + rb"(?![A-Za-z0-9_./:\-])", b"public-feedback-repository", public_data)
    public_data = re.sub(
        rb"(?<![A-Za-z0-9_./:@\-])" + re.escape(PUBLIC_FEEDBACK_ORIGIN)
        + rb"(?=/|[\s\"'`<>)]|$)", b"https://feedback.invalid", public_data)
    if PHONE.search(public_data):
        bad.append("personal phone number")
    if NUMERIC_CONTACT.search(public_data):
        bad.append("personal numeric contact")
    if home.encode() in data:
        bad.append("personal home path")
    if b"." + b"workbuddy" in data:
        bad.append("internal credential directory")
    # An existing download namespace is necessary. Do not grant a blanket number exemption.
    number = owner.split(b"-", 1)[0]
    if number.isdigit() and number in public_data:
        bad.append("personal numeric identifier outside GitHub namespace")
    return bad


def object_privacy_problems(data: bytes, home: str, secrets=(), kind="blob") -> list[str]:
    """Recognize approved public identities only in actual Git identity headers."""
    original = privacy_problems(data, home, secrets)
    if kind not in ("commit", "tag"):
        return original
    header, separator, message = data.partition(b"\n\n")
    if not separator:
        return original
    owner = REPO.split("/", 1)[0].encode()
    identity = re.compile(re.escape(owner + b" <" + SUPPORT_EMAIL.encode() + b">")
                          + rb" [0-9]+ [+-][0-9]{4}")
    fields = (b"author", b"committer") if kind == "commit" else (b"tagger",)
    normalized = []
    recognized = False
    for line in header.split(b"\n"):
        key, space, value = line.partition(b" ")
        if space and key in fields and identity.fullmatch(value):
            line = (key + b" github-public-namespace <" + SUPPORT_EMAIL.encode()
                    + b"> " + value.rsplit(b"> ", 1)[1])
            recognized = True
        normalized.append(line)
    if not recognized:
        return original
    inspected = privacy_problems(b"\n".join(normalized) + separator + message, home, secrets)
    number_label = "personal numeric identifier outside GitHub namespace"
    # A declared secret, credential, contact or home-path finding in the raw
    # object must survive normalization of its otherwise public identity.
    kept = [label for label in original if label != number_label or label in inspected]
    return list(dict.fromkeys(kept + inspected))


def signing_secrets(root: Path) -> list[bytes]:
    path = root / ".private_tools" / "update_signing_key.txt"
    if not path.is_file():
        return []
    text = path.read_bytes().strip()
    values = [text]
    try:
        values.append(bytes.fromhex(text.decode("ascii")))
    except ValueError:
        import base64
        values.append(base64.b64decode(text, validate=True))
    return values


def audit_history(root: Path, home: str, secrets=()) -> None:
    objects = run(["git", "rev-list", "--objects", "--all"], root).splitlines()
    if not objects:
        raise ValueError("No reachable objects: cannot audit an empty repository")
    issues = []
    for entry in objects:
        oid, _, name = entry.partition(" ")
        if name and (excluded(name) or re.search(r"\.(dmg|log|ips)$|(^|/)config\.json$", name)):
            issues.append("internal history entry: " + name)
        if name:
            issues.extend(label + ": object path " + name
                          for label in privacy_problems(name.encode(), home, secrets))
        kind = run(["git", "cat-file", "-t", oid], root).strip()
        if kind not in ("blob", "commit", "tag"):
            continue
        data = run(["git", "cat-file", "-p", oid], root, binary=True)
        for label in object_privacy_problems(data, home, secrets, kind):
            issues.append(label + ": " + (name or oid[:10]))
    if issues:
        raise ValueError("Public history audit failed:\n" + "\n".join(issues))
    print("Public history audit: %d reachable objects; no private history or detected secrets" % len(objects))


def audit_public_history(root: Path = ROOT, url: str | None = None) -> bool:
    """Fetch/audit only; never push, export private source or move its branch."""
    url = url or "https://github.com/%s.git" % REPO
    base = root / "build"
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="public_history_", dir=base) as directory:
        checkout = Path(directory)
        run(["git", "init", "-q", "-b", PUBLIC_BRANCH], checkout)
        refs = run(["git", "ls-remote", "--heads", url, "refs/heads/" + PUBLIC_BRANCH], checkout)
        if not refs.strip():
            print("Public history not present: no previous public-source branch; not a future export audit pass")
            return False
        run(["git", "fetch", "-q", "--no-tags", url, "refs/heads/" + PUBLIC_BRANCH], checkout)
        run(["git", "update-ref", "refs/heads/" + PUBLIC_BRANCH, "FETCH_HEAD"], checkout)
        audit_history(checkout, str(Path.home()), signing_secrets(root))
    print("Existing public-source history passed read-only preflight; no remote writes")
    return True


def export_source(out: Path, root: Path = ROOT) -> None:
    # Production code must be committed; public documents are copied from the
    # stable versioned renderer and validated before export.
    dirty = run(["git", "diff", "HEAD", "--name-only"], root).splitlines()
    untracked = run(["git", "ls-files", "--others", "--exclude-standard"], root).splitlines()
    # Untracked research/tests in a shared checkout are not application build inputs.
    product_untracked = [name for name in untracked
                         if ("/" not in name and name.endswith((".py", ".spec")))
                         or name.startswith(("updater/", "installer/", "fonts/"))]
    unsafe = [name for name in dirty + product_untracked if name not in DOCS and not excluded(name)]
    if unsafe:
        raise ValueError("Commit release inputs before exporting: " + ", ".join(unsafe))
    raw = run(["git", "archive", "--format=tar", "HEAD"], root, binary=True)
    out.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        members = [member for member in archive.getmembers() if not excluded(member.name)]
        archive.extractall(out, members=members, filter="data")
    for name in DOCS:
        destination = out / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, destination)
    home = str(Path.home())
    for path in out.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        cleaned = sanitize(text, home)
        if cleaned != text:
            path.write_text(cleaned, encoding="utf-8")
    bad = problems(out, APP_VERSION)
    if bad:
        raise ValueError("Invalid public documents: " + ", ".join(bad))
    run(["git", "init", "-q", "-b", PUBLIC_BRANCH], out)
    run(["git", "add", "-A"], out)
    run(["git", "commit", "-qm", "Corresponding source v" + APP_VERSION], out)
    audit_history(out, home, signing_secrets(root))


def push_source(out: Path) -> str:
    url = "https://github.com/%s.git" % REPO
    refs = run(["git", "ls-remote", url, "refs/heads/" + PUBLIC_BRANCH], out)
    needs_push = True
    if refs.strip():
        run(["git", "fetch", "-q", url, PUBLIC_BRANCH], out)
        tree = run(["git", "rev-parse", "HEAD^{tree}"], out).strip()
        public_tree = run(["git", "rev-parse", "FETCH_HEAD^{tree}"], out).strip()
        if tree == public_tree:
            # Retrying an already published source snapshot must not create a new
            # commit with identical content: its ZIP would have different archive
            # metadata, while the same-name release asset is intentionally immutable.
            run(["git", "update-ref", "HEAD", "FETCH_HEAD"], out)
            needs_push = False
            print("Public source tree already published; reusing existing commit")
        else:
            # Attach only to the already public branch, never to the private development commit.
            commit = run(["git", "commit-tree", tree, "-p", "FETCH_HEAD", "-m",
                          "Corresponding source v" + APP_VERSION], out).strip()
            run(["git", "update-ref", "refs/heads/" + PUBLIC_BRANCH, commit], out)
    audit_history(out, str(Path.home()), signing_secrets(ROOT))
    if needs_push:
        run(["git", "push", "-q", url, "HEAD:refs/heads/" + PUBLIC_BRANCH], out)
    return run(["git", "rev-parse", "HEAD"], out).strip()


def upload_asset(path: Path) -> None:
    # Never overwrite immutable same-name assets. A retry must verify identical content.
    assets = json.loads(run(["gh", "api", "repos/%s/releases/tags/%s" % (REPO, TAG)]))["assets"]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for asset in assets:
        if asset["name"] == path.name:
            if asset.get("digest") != "sha256:" + digest:
                raise ValueError("Existing immutable asset differs: " + path.name)
            print("Already published identical asset: " + path.name)
            return
    run(["gh", "release", "upload", TAG, str(path), "--repo", REPO])


def publish_documents(source: Path) -> None:
    # Clone the latest manifest branch, changing only documents so OTA pointers remain untouched.
    with tempfile.TemporaryDirectory(prefix="sfpublic_docs_") as directory:
        checkout = Path(directory) / "release"
        run(["git", "clone", "-q", "--depth=1", "--branch", "main",
             "https://github.com/%s.git" % REPO, str(checkout)])
        for name in DOCS + ("fonts/OFL-D-DIN.txt",):
            path = checkout / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, path)
        run(["git", "add", "--", *DOCS, "fonts/OFL-D-DIN.txt"], checkout)
        if run(["git", "diff", "--cached", "--name-only"], checkout).strip():
            run(["git", "commit", "-qm", "Public documentation v" + APP_VERSION], checkout)
            run(["git", "push", "-q", "origin", "HEAD:refs/heads/main"], checkout)


def self_test() -> int:
    home = "/Users/" + "private-owner"
    token = b"ghp_" + b"a" * 30
    cases = ((home.encode(), True), (token, True), (b"person@" + b"qq.com", True),
             (b"138" + b"00138000", True), (b"QQ: " + b"927" + b"45163", True),
             (b"old owner " + REPO.split("/", 1)[0].encode(), True),
             (SUPPORT_EMAIL.encode(), False),
             (("https://github.com/" + REPO).encode(), False),
             (b"run 387" + b"04366170", False),
             (b"safe example", False), (REPO.encode(), False))
    if any(bool(privacy_problems(data, home)) != want for data, want in cases):
        return 1
    if sanitize(home + "/project .private_tools", home) != "/Users/example/project .private_tools":
        return 1
    with tempfile.TemporaryDirectory(prefix="sfpub_self_") as directory:
        root = Path(directory)
        run(["git", "init", "-q"], root)
        path = root / "file.txt"
        path.write_bytes(b"safe")
        run(["git", "add", "."], root)
        run(["git", "commit", "-qm", "clean"], root)
        audit_history(root, home)
        path.write_bytes(token)
        run(["git", "add", "."], root)
        run(["git", "commit", "-qm", "planted leak"], root)
        path.write_bytes(b"safe again")
        run(["git", "add", "."], root)
        run(["git", "commit", "-qm", "removed from current tree"], root)
        try:
            audit_history(root, home)
        except ValueError:
            print("public_release: secret in old reachable history correctly rejected")
            return 0
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--audit-public-history", action="store_true")
    args = parser.parse_args(argv)
    if args.audit_public_history:
        if args.self_test or args.publish or args.source_only or args.audit:
            parser.error("--audit-public-history cannot be combined with another action")
        audit_public_history()
        return 0
    if args.self_test:
        return self_test()
    if args.audit:
        audit_history(args.audit, str(Path.home()), signing_secrets(ROOT))
        return 0
    base = ROOT / "build"
    base.mkdir(exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix="public_v%s_" % APP_VERSION, dir=base)) / "source"
    export_source(out)
    if args.publish:
        commit = push_source(out)
        print("Public source commit: " + commit)
        if not args.source_only:
            archive = out.parent / ("Skyrocket-%s-source.zip" % APP_VERSION)
            run(["git", "archive", "--format=zip", "--prefix=Skyrocket-source/",
                 "-o", str(archive), "HEAD"], out)
            upload_asset(archive)
            dmg = ROOT / "build" / ("Skyrocket-V%s.dmg" % APP_VERSION)
            upload_asset(dmg)
            application = ROOT / "build/update_assets" / ("V" + APP_VERSION) / (
                "Skyrocket-%s-macos.zip" % APP_VERSION)
            sums = out.parent / ("Skyrocket-%s-SHA256SUMS.txt" % APP_VERSION)
            sums.write_text("".join("%s  %s\n" % (hashlib.sha256(path.read_bytes()).hexdigest(), path.name)
                                    for path in (dmg, application, archive)), encoding="ascii")
            upload_asset(sums)
            publish_documents(out)
            import quality_evidence
            import release_state
            receipt = {"version": APP_VERSION, "public_commit": commit,
                       "source": release_state.source_snapshot(ROOT),
                       "source_zip": release_state.reference(archive),
                       "checksums": release_state.reference(sums),
                       "recorded_at": quality_evidence.now()}
            quality_evidence.atomic_json(base / ("publication-v%s.json" % APP_VERSION), receipt)
    print("Audited public source: " + str(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print("Public release failed: %s" % exc, file=sys.stderr)
        sys.exit(1)
