#!/usr/bin/env python3
"""Keep a local, certificate-pinned application identity across releases.

This is not Developer ID signing, notarization, or a Gatekeeper exemption.
Private material lives outside the checkout; only the certificate pin is public.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sf_constants import BUNDLE_ID  # noqa: E402

POLICY = ROOT / "tools" / "signing_policy.json"
STATE = Path.home() / "Library/Application Support/SkyrocketBuildSigning"


def run(args: list[str]) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise ValueError("%s timed out" % Path(args[0]).name) from None
    if result.returncode:
        # Never include argv: security commands contain the private keychain password.
        raise ValueError("%s failed (%d): %s" %
                         (Path(args[0]).name, result.returncode, result.stderr.strip()))
    return result.stdout + result.stderr


def load_policy(path: Path = POLICY) -> dict:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Invalid stable signing policy; expected an object")
    sha1, sha256 = data.get("certificate_sha1"), data.get("certificate_sha256")
    if (data.get("schema") != 1 or data.get("bundle_id") != BUNDLE_ID
            or not isinstance(sha1, str) or not re.fullmatch(r"[0-9A-F]{40}", sha1)
            or not isinstance(sha256, str) or not re.fullmatch(r"[0-9A-F]{64}", sha256)):
        raise ValueError("Invalid stable signing policy; refusing an unpinned identity")
    return data


def requirement(policy: dict) -> str:
    return ('identifier "%s" and certificate leaf = H"%s"' %
            (policy["bundle_id"], policy["certificate_sha1"]))


def private_file(path: Path) -> str:
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Signing secret must be a private regular file: %s" % path.name)
    return path.read_text().strip()


def check_material(policy: dict, state: Path = STATE) -> str:
    if state.is_symlink() or state.stat().st_mode & 0o077:
        raise ValueError("Signing directory must be private (0700)")
    certificate = (state / "certificate.pem").read_bytes()
    import ssl
    der = ssl.PEM_cert_to_DER_cert(certificate.decode("ascii"))
    if (hashlib.sha1(der).hexdigest().upper() != policy["certificate_sha1"]
            or hashlib.sha256(der).hexdigest().upper() != policy["certificate_sha256"]):
        raise ValueError("Certificate differs from the pinned release identity")
    keychain = state / "identity.keychain-db"
    if not keychain.is_file() or keychain.is_symlink():
        raise ValueError("Signing keychain missing; restore the original, do not regenerate")
    return private_file(state / "keychain-password")


def initialize(state: Path = STATE, policy_path: Path = POLICY) -> dict:
    """Explicit one-time provisioning; never silently replace an existing identity."""
    if state.exists() or policy_path.exists():
        raise ValueError("Identity already provisioned or partially present; restore, never rotate silently")
    # No certificate or private key may be generated inside a source checkout.
    if state.resolve().is_relative_to(ROOT):
        raise ValueError("Signing material must live outside the repository")
    old_mask = os.umask(0o077)
    try:
        return provision(state, policy_path)
    finally:
        os.umask(old_mask)


def provision(state: Path, policy_path: Path) -> dict:
    state.mkdir(parents=True, mode=0o700)
    password = secrets.token_hex(32)
    (state / "keychain-password").write_text(password)
    keychain = state / "identity.keychain-db"
    try:
        with tempfile.TemporaryDirectory(prefix="provision-", dir=state) as directory:
            work = Path(directory)
            run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes",
                 "-keyout", str(work / "private.pem"), "-out", str(state / "certificate.pem"),
                 "-days", "7300", "-subj", "/CN=Skyrocket Local Release Signing/",
                 "-addext", "keyUsage=critical,digitalSignature",
                 "-addext", "extendedKeyUsage=codeSigning"])
            # Password is read from a private file rather than exposed to openssl argv.
            run(["/usr/bin/openssl", "pkcs12", "-export", "-inkey", str(work / "private.pem"),
                 "-in", str(state / "certificate.pem"), "-out", str(work / "identity.p12"),
                 "-passout", "file:" + str(state / "keychain-password")])
            run(["/usr/bin/security", "create-keychain", "-p", password, str(keychain)])
            run(["/usr/bin/security", "unlock-keychain", "-p", password, str(keychain)])
            run(["/usr/bin/security", "import", str(work / "identity.p12"), "-k", str(keychain),
                 "-P", password, "-T", "/usr/bin/codesign"])
            run(["/usr/bin/security", "set-key-partition-list", "-S", "apple-tool:,apple:",
                 "-s", "-k", password, str(keychain)])
        import ssl
        der = ssl.PEM_cert_to_DER_cert((state / "certificate.pem").read_text())
        policy = {"schema": 1, "bundle_id": BUNDLE_ID,
                  "certificate_sha1": hashlib.sha1(der).hexdigest().upper(),
                  "certificate_sha256": hashlib.sha256(der).hexdigest().upper(),
                  "type": "self-signed-not-developer-id"}
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        with policy_path.open("x") as stream:
            json.dump(policy, stream, indent=2)
            stream.write("\n")
        return policy
    finally:
        # create-keychain can add itself to the user's search list. Do not leave it
        # there, and preserve any other entries concurrently added by the user.
        try:
            current = shlex.split(run(["/usr/bin/security", "list-keychains", "-d", "user"]))
            wanted = [p for p in current if Path(p) != keychain]
            if current != wanted:
                run(["/usr/bin/security", "list-keychains", "-d", "user", "-s", *wanted])
        finally:
            if keychain.exists():
                run(["/usr/bin/security", "lock-keychain", str(keychain)])


def verify(app: Path, policy: dict) -> dict:
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("CFBundleIdentifier") != policy["bundle_id"]:
        raise ValueError("Bundle identifier differs from the stable signing policy")
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
    run(["/usr/bin/codesign", "--verify", "--strict", "-R", "=" + requirement(policy), str(app)])
    with tempfile.TemporaryDirectory(prefix="sf_signing_cert_") as directory:
        prefix = Path(directory) / "cert"
        run(["/usr/bin/codesign", "-d", "--extract-certificates=" + str(prefix), str(app)])
        der = prefix.with_name("cert0").read_bytes()
        if hashlib.sha256(der).hexdigest().upper() != policy["certificate_sha256"]:
            raise ValueError("Embedded signing certificate SHA-256 differs from the policy")
    designated = run(["/usr/bin/codesign", "-d", "-r-", str(app)])
    # A correctly signed app can still embed a hash-bound or overly broad DR.
    match = re.search(r"^designated => (.+)$", designated, re.MULTILINE)
    if not match:
        raise ValueError("Cannot read embedded designated requirement")
    canonical = requirement(policy).lower()
    if match.group(1).lower() != canonical:
        raise ValueError("Embedded identity requirement must pin both bundle ID and certificate")
    return {"bundle_id": info["CFBundleIdentifier"],
            "version": info.get("CFBundleShortVersionString", info.get("CFBundleVersion")),
            "certificate_sha256": policy["certificate_sha256"],
            "designated_requirement": match.group(1), "signature_verified": True,
            "camera_permission_persistence": "requires_live_upgrade_test"}


def sign(app: Path, policy: dict, state: Path = STATE) -> dict:
    state = state.resolve()
    password = check_material(policy, state)
    with (app / "Contents/Info.plist").open("rb") as stream:
        if plistlib.load(stream).get("CFBundleIdentifier") != policy["bundle_id"]:
            raise ValueError("Refusing to sign a different application")
    keychain = state / "identity.keychain-db"
    run(["/usr/bin/security", "unlock-keychain", "-p", password, str(keychain)])
    # Preserve PyInstaller's nested binary signatures; only replace the outer app
    # seal and its executable identity. No hardened runtime or library-validation
    # changes are needed, and --deep must not be used for signing.
    try:
        run(["/usr/bin/codesign", "--force", "--sign", policy["certificate_sha1"],
             "--keychain", str(keychain), "--timestamp=none", "--requirements",
             "=designated => " + requirement(policy), str(app)])
    finally:
        run(["/usr/bin/security", "lock-keychain", str(keychain)])
    return verify(app, policy)


def self_test() -> int:
    """Real signing plus planted failures, without TCC or production key access."""
    if sys.platform != "darwin":
        print("Stable signing self-test requires native macOS codesign", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="sf_signing_self_") as directory:
        root = Path(directory)
        state = root / "private"
        try:
            policy = initialize(state, root / "policy.json")
            apps = []
            for version in (1, 2):
                app = root / ("v%d.app" % version)
                executable = app / "Contents/MacOS/Echo"
                executable.parent.mkdir(parents=True)
                shutil.copyfile("/bin/echo", executable)
                executable.chmod(0o755)
                with (app / "Contents/Info.plist").open("wb") as stream:
                    plistlib.dump({"CFBundleIdentifier": BUNDLE_ID, "CFBundleExecutable": "Echo",
                                   "CFBundleVersion": str(version), "CFBundlePackageType": "APPL"}, stream)
                sign(app, policy, state)
                apps.append(app)
            hashes = []
            for app in apps:
                info = run(["/usr/bin/codesign", "-d", "--verbose=4", str(app)])
                hashes.append(next(line for line in info.splitlines() if line.startswith("CDHash=")))
            if hashes[0] == hashes[1]:
                raise ValueError("Self-test variants must have different hashes")
            rejected = 0
            wrong = {**policy, "certificate_sha256": "0" * 64}
            try:
                check_material(wrong, state)
            except ValueError:
                rejected += 1
            run(["/usr/bin/codesign", "--force", "--sign", "-", str(apps[0])])
            try:
                verify(apps[0], policy)
            except ValueError:
                rejected += 1
            with (apps[1] / "Contents/Info.plist").open("ab") as stream:
                stream.write(b"\n")
            try:
                verify(apps[1], policy)
            except ValueError:
                rejected += 1
            if rejected != 3:
                raise ValueError("Stable identity gate accepted a planted invalid input")
            print("Stable signing: cross-version identity passed; wrong pin, ad-hoc and tampering rejected")
            return 0
        finally:
            keychain = state / "identity.keychain-db"
            if keychain.exists():
                run(["/usr/bin/security", "delete-keychain", str(keychain)])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("init", "preflight", "sign", "check"))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument("--policy", type=Path, default=POLICY)
    args = parser.parse_args(argv)
    args.state = args.state.resolve()
    if not args.self_test and args.action is None:
        parser.error("An action or --self-test is required")
    if args.action in ("sign", "check") and args.app is None:
        parser.error("--app is required for sign/check")
    try:
        if args.self_test:
            return self_test()
        if args.action == "init":
            report = initialize(args.state, args.policy)
        else:
            policy = load_policy(args.policy)
            if args.action == "preflight":
                check_material(policy, args.state)
                report = {"identity_available": True, **policy}
            elif args.app is not None:
                report = (sign(args.app, policy, args.state) if args.action == "sign"
                          else verify(args.app, policy))
            else:
                raise ValueError("Missing app")
        print(json.dumps(report, indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Stable signing refused: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
