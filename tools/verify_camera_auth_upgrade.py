#!/usr/bin/env python3
"""Interactive TCC upgrade acceptance using a separate, camera-free probe app.

baseline builds two executable variants and launches a user-initiated permission
request. upgrade replaces the first app at its exact path. restart checks again.
Only baseline may request authorization. No TCC reset or database access is used.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stable_signing as signing  # noqa: E402

PROBE_ID = "com.skyrocket.camera-auth-probe"
SOURCE = Path(__file__).with_name("camera_auth_probe.swift")


def wait_result(path: Path, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return json.loads(path.read_text())
        time.sleep(0.1)
    raise ValueError("No native authorization result; do not infer success from open exit status")


def wait_exit(app: Path) -> None:
    executable = str(app / "Contents/MacOS/CameraProbe")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = subprocess.run(["/usr/bin/pgrep", "-f", re.escape(executable)],
                                capture_output=True)
        if result.returncode == 1:
            return
        if result.returncode != 0:
            raise ValueError("Cannot determine whether the probe has exited")
        time.sleep(0.1)
    raise ValueError("Probe still running; will not replace a live app")


def launch(app: Path, output: Path, may_request: bool = False) -> None:
    if output.exists():
        raise ValueError("Result already exists; do not overwrite acceptance evidence")
    signing.run(["/usr/bin/open", "-n", "--env", "SF_CAMERA_PROBE_RESULT=" + str(output),
                 "--env", "SF_CAMERA_PROBE_ALLOW_REQUEST=" + ("1" if may_request else "0"),
                 str(app)])


def prepare(out: Path, policy: dict, state: Path) -> None:
    if out.exists():
        raise ValueError("Output already exists; use a new directory")
    signing.check_material(policy, state)
    out.mkdir(parents=True)
    for version in (1, 2):
        app = out / ("baseline.app" if version == 1 else "candidate.app")
        executable = app / "Contents/MacOS/CameraProbe"
        executable.parent.mkdir(parents=True)
        command = ["/usr/bin/swiftc", str(SOURCE), "-target", "arm64-apple-macosx13.0",
                   "-framework", "AppKit", "-framework", "AVFoundation", "-o", str(executable)]
        if version == 2:
            command += ["-D", "UPGRADE_RELEASE"]
        signing.run(command)
        with (app / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleIdentifier": PROBE_ID, "CFBundleExecutable": "CameraProbe",
                          "CFBundleName": "Skyrocket Camera Auth Probe", "CFBundleVersion": str(version),
                          "CFBundlePackageType": "APPL", "LSMinimumSystemVersion": "13.0",
                          "NSCameraUsageDescription": "Verify authorization survives an upgrade. "
                          "No images are captured, saved, or transmitted."}, stream)
        signing.sign(app, policy, state)
    live = out / "Installed/CameraProbe.app"
    shutil.copytree(out / "baseline.app", live, symlinks=True)
    launch(live, out / "baseline-result.json", may_request=True)


def upgrade(out: Path, policy: dict) -> dict:
    baseline = json.loads((out / "baseline-result.json").read_text())
    if baseline["status"] not in ("granted_by_user", "authorized_without_request"):
        raise ValueError("Baseline not authorized: allow the baseline once before upgrading")
    live = out / "Installed/CameraProbe.app"
    candidate = out / "candidate.app"
    wait_exit(live)
    old = signing.verify(live, policy)
    new = signing.verify(candidate, policy)
    if old["version"] == new["version"]:
        raise ValueError("Not an upgrade")
    hashes = []
    for app in (live, candidate):
        info = signing.run(["/usr/bin/codesign", "-d", "--verbose=4", str(app)])
        hashes.append(next(line for line in info.splitlines() if line.startswith("CDHash=")))
    if hashes[0] == hashes[1]:
        raise ValueError("Executable identity hash did not change; invalid upgrade experiment")
    previous = out / "previous.app.stale"
    live.rename(previous)
    try:
        shutil.copytree(candidate, live, symlinks=True)
    except OSError:
        if live.exists():
            live.rename(out / "failed-candidate.app.stale")
        previous.rename(live)
        raise
    launch(live, out / "upgrade-result.json")
    result = wait_result(out / "upgrade-result.json")
    report = {"baseline": baseline, "upgrade": result, "hashes": hashes,
              "certificate_sha256": policy["certificate_sha256"],
              "exact_same_install_path": True,
              "passed": result["status"] == "authorized_without_request",
              "scope": "native authorization probe; not a released Skyrocket upgrade"}
    (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("baseline", "upgrade", "restart"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=signing.POLICY)
    parser.add_argument("--state", type=Path, default=signing.STATE)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    try:
        policy = {**signing.load_policy(args.policy), "bundle_id": PROBE_ID}
        if args.action == "baseline":
            prepare(out, policy, args.state.resolve())
            print("Baseline launched; authorization is pending, NOT a pass")
            return 0
        if args.action == "upgrade":
            report = upgrade(out, policy)
        else:
            report = json.loads((out / "result.json").read_text())
            if not report["passed"]:
                raise ValueError("Upgrade has not passed")
            app = out / "Installed/CameraProbe.app"
            wait_exit(app)
            signing.verify(app, policy)
            launch(app, out / "restart-result.json")
            result = wait_result(out / "restart-result.json")
            report["restart"] = result
            report["passed"] = result["status"] == "authorized_without_request"
            (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 0 if report["passed"] else 1
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Camera authorization acceptance failed: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
