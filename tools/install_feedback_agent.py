#!/usr/bin/env python3
"""Install an independent admin runtime snapshot and a thirty-minute LaunchAgent."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = ("sf_feedback.py", "sf_feedback_capture.py", "sf_feedback_github.py",
           "sf_feedback_bundle.py",
           "sf_feedback_transport.py", "sf_feedback_admin.py", "sf_feedback_analyzer.py",
           "sf_diag.py", "sf_privacy.py", "sf_feedback_notifications.py")
LABEL = "com.skyrocket.feedback-agent"


def install(destination, gh_path, *, activate=False):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    files = {name: ROOT / name for name in MODULES}
    files["tools/feedback_agent.py"] = ROOT / "tools/feedback_agent.py"
    files["tools/feedback_desktop_reports.py"] = ROOT / "tools/feedback_desktop_reports.py"
    fingerprint = hashlib.sha256(b"".join(path.read_bytes() for path in files.values())).hexdigest()
    runtime = destination / "runtimes" / fingerprint[:16]
    runtime.mkdir(parents=True, exist_ok=True)
    for name, source in files.items():
        target = runtime / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise RuntimeError("refusing to overwrite a different installed runtime")
        shutil.copy2(source, target)
    interpreter = str(Path(vars(sys).get("_base_executable", sys.executable)).resolve())
    manifest = {"fingerprint": fingerprint, "python": interpreter,
                "version": sys.version.split()[0],
                "files": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}}
    (runtime / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    plist = {
        "Label": LABEL,
        "ProgramArguments": [interpreter, "-B", str(runtime / "tools/feedback_agent.py"),
                             "--data-dir", str(destination / "data"), "--gh", str(Path(gh_path).resolve()),
                             "--desktop-dir", str(Path.home() / "Desktop/Skyrocket 用户反馈报告")],
        "WorkingDirectory": str(runtime), "RunAtLoad": True, "StartInterval": 1800,
        "ProcessType": "Background", "EnvironmentVariables": {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin",
                                                               "PYTHONNOUSERSITE": "1"},
        "StandardOutPath": str(destination / "agent.stdout.log"),
        "StandardErrorPath": str(destination / "agent.stderr.log"),
    }
    prepared = destination / (LABEL + ".plist")
    prepared.write_bytes(plistlib.dumps(plist))
    if activate:
        launch_dir = Path.home() / "Library/LaunchAgents"
        launch_dir.mkdir(parents=True, exist_ok=True)
        target = launch_dir / prepared.name
        if target.exists() and target.read_bytes() != prepared.read_bytes():
            raise RuntimeError("existing LaunchAgent differs; review it before replacing")
        shutil.copy2(prepared, target)
        subprocess.run(["/bin/launchctl", "bootstrap", "gui/%d" % os.getuid(), str(target)],
                       check=True, capture_output=True, timeout=15)
    return {"runtime": str(runtime), "manifest": manifest,
            "plist": str(prepared), "activated": activate}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--gh", required=True)
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if sys.version_info[:3] != (3, 14, 7):
        parser.error("use the project Python 3.14.7")
    print(json.dumps(install(args.destination, args.gh, activate=args.activate), ensure_ascii=False))


if __name__ == "__main__":
    main()
