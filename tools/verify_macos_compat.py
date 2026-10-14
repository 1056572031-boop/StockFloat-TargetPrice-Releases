#!/usr/bin/env python3
"""Inspect real Mach-O slices, not the build host's OS or an SDK version claim."""
from __future__ import annotations

import argparse
from importlib.metadata import version
from pathlib import Path
import plistlib
import struct
import tempfile

from macholib.MachO import MachO
from macholib.mach_o import (LC_BUILD_VERSION, LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB,
                            LC_REEXPORT_DYLIB, LC_RPATH, LC_VERSION_MIN_MACOSX)
from packaging.requirements import Requirement

ARM64 = 0x0100000C
MAGICS = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
          b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
          b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
QT_PACKAGES = ("PyQt6", "PyQt6-Qt6", "PyQt6-sip")
REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


def version_tuple(text):
    parts = str(text).split(".")
    if not 1 <= len(parts) <= 3 or any(not p.isascii() or not p.isdigit() for p in parts):
        raise ValueError("invalid macOS version: %r" % text)
    return tuple(int(p) for p in parts) + (0,) * (3 - len(parts))


def packed_version(value):
    return (value >> 16, (value >> 8) & 255, value & 255)


def read_image(path):
    slices = []
    for header in MachO(str(path)).headers:
        minimum, sdk, platform = None, None, None
        links = []
        for command, data, payload in header.commands:
            if command.cmd == LC_BUILD_VERSION:
                minimum, sdk, platform = packed_version(data.minos), packed_version(data.sdk), data.platform
            elif command.cmd == LC_VERSION_MIN_MACOSX:
                minimum, sdk, platform = packed_version(data.version), packed_version(data.sdk), 1
            elif command.cmd in (LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB, LC_REEXPORT_DYLIB, LC_RPATH):
                links.append(payload.split(b"\0", 1)[0].decode("utf-8"))
        slices.append({"cpu": header.header.cputype, "minimum": minimum, "sdk": sdk,
                       "platform": platform, "links": links})
    return slices


def check_bundle(app, targets=("26.0", "27.0")):
    app = Path(app).resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    declared = version_tuple(info["LSMinimumSystemVersion"])
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    if not executable.is_file():
        raise ValueError("bundle executable missing")
    issues, images = [], []
    for path in sorted(app.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic not in MAGICS:
            continue
        slices = read_image(path)
        arm = [part for part in slices if part["cpu"] == ARM64]
        relative = str(path.relative_to(app))
        if not arm:
            issues.append("%s: no arm64 slice" % relative)
        for part in arm:
            minimum = part["minimum"]
            if part["platform"] != 1 or minimum is None:
                issues.append("%s: missing macOS deployment target" % relative)
                continue
            if minimum > declared:
                issues.append("%s: minimum %s exceeds Info.plist %s" % (relative, minimum, declared))
            for target in targets:
                if max(minimum, declared) > version_tuple(target):
                    issues.append("%s: cannot load on macOS %s" % (relative, target))
            for link in part["links"]:
                if link.startswith("/") and not link.startswith(("/System/Library/", "/usr/lib/")):
                    issues.append("%s: build-machine dependency %s" % (relative, link))
        images.append({"path": relative, "slices": slices})
    if not images or not any(image["path"] == str(executable.relative_to(app)) for image in images):
        issues.append("no Mach-O bundle executable inspected")
    minima = [part["minimum"] for image in images for part in image["slices"]
              if part["cpu"] == ARM64 and part["minimum"] is not None]
    return {"issues": issues, "images": len(images), "minimum": max(minima, default=None),
            "declared": declared, "targets": targets}


def check_runtime():
    locks = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.name in QT_PACKAGES:
            pins = list(requirement.specifier)
            if len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version:
                raise ValueError("Qt dependency must be exactly pinned: %s" % line)
            locks[requirement.name] = pins[0].version
    if set(locks) != set(QT_PACKAGES):
        raise ValueError("Qt wheel or SIP runtime is not locked")
    issues = []
    for package, expected in locks.items():
        actual = version(package)
        if actual != expected:
            issues.append("%s: installed %s, locked %s" % (package, actual, expected))
    from PyQt6.QtCore import qVersion
    if qVersion() != locks["PyQt6-Qt6"]:
        issues.append("loaded Qt runtime differs from locked Qt wheel")
    return issues


def sample_bundle(root, minimum=13, cpu=ARM64, declared="13.0", platform=1):
    """Minimal real Mach-O fixture; the same parser is used for production images."""
    app = Path(root) / "Probe.app"
    binary = app / "Contents/MacOS/Probe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    plist = {"CFBundleExecutable": "Probe", "LSMinimumSystemVersion": declared}
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(plist))
    header = struct.pack("<8I", 0xFEEDFACF, cpu, 0, 2, 1, 24, 0, 0)
    command = struct.pack("<6I", LC_BUILD_VERSION, 24, platform, minimum << 16, 27 << 16, 0)
    binary.write_bytes(header + command)
    return app


def self_test():
    cases = [(13, ARM64, "13.0", 1, False), (13, ARM64, "12.0", 1, True),
             (27, ARM64, "27.0", 1, True), (13, 0x01000007, "13.0", 1, True),
             (13, ARM64, "13.0", 2, True)]
    with tempfile.TemporaryDirectory(prefix="sf_macos_gate_") as root:
        for index, (minimum, cpu, declared, platform, rejected) in enumerate(cases):
            app = sample_bundle(Path(root) / str(index), minimum, cpu, declared, platform)
            if bool(check_bundle(app)["issues"]) != rejected:
                print("FAIL: macOS compatibility self-test case %d" % index)
                return 1
    print("macOS compatibility self-test: positive control and 4 planted errors caught")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", nargs="?")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--runtime", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.app and not args.runtime:
        parser.error("provide an app or --runtime")
    try:
        issues = check_runtime() if args.runtime else []
        if args.app:
            report = check_bundle(args.app)
            issues.extend(report["issues"])
            print("Mach-O images=%d, actual minimum=%s, Info.plist=%s, targets=26/27" %
                  (report["images"], report["minimum"], report["declared"]))
        for issue in issues:
            print("FAIL: " + issue)
        if not issues:
            print("macOS binary/runtime compatibility passed; this is not cross-version GUI acceptance")
        return int(bool(issues))
    except Exception as exc:
        print("FAIL: cannot verify macOS compatibility: %s" % exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
