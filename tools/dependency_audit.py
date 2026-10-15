#!/usr/bin/env python3
"""Fail closed on lock drift, known OSV advisories or an inconclusive query."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import sys
import time

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sf_http import RestrictedRequest, read_limited, safe_urlopen  # noqa: E402

OSV_URL = "https://api.osv.dev/v1/querybatch"


def locked_packages(path: Path) -> dict[str, str]:
    packages: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", " ", "\t")):
            continue
        requirement = Requirement(line.removesuffix("\\").strip())
        versions = list(requirement.specifier)
        if (len(versions) != 1 or versions[0].operator != "=="
                or "*" in versions[0].version or requirement.url or requirement.marker):
            raise ValueError("Dependency lock must contain exact unconditional versions")
        name = canonicalize_name(requirement.name)
        if name in packages:
            raise ValueError("Duplicate dependency lock entry: " + name)
        packages[name] = versions[0].version
    if not packages:
        raise ValueError("Empty dependency lock; refusing to report success")
    return packages


def check_lock(locked: dict[str, str], installed: dict[str, str]) -> None:
    drift = ["%s: expected %s, installed %s" % (name, version, installed.get(name, "missing"))
             for name, version in locked.items() if installed.get(name) != version]
    if drift:
        raise ValueError("Dependency lock mismatch: " + "; ".join(drift))


def findings(packages: list[tuple[str, str]], payload: object) -> list[str]:
    if not packages or not isinstance(payload, dict):
        raise ValueError("Empty input or invalid OSV response")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != len(packages):
        raise ValueError("Incomplete OSV response; refusing to report success")
    issues = []
    for (name, version), result in zip(packages, results, strict=True):
        if (not isinstance(result, dict) or "error" in result
                or result.get("next_page_token")):
            raise ValueError("OSV package query failed: " + name)
        advisories = result.get("vulns", [])
        if not isinstance(advisories, list):
            raise ValueError("Invalid OSV advisory list: " + name)
        for advisory in advisories:
            if not isinstance(advisory, dict) or not isinstance(advisory.get("id"), str):
                raise ValueError("Invalid OSV advisory: " + name)
            issues.append("%s==%s: %s" % (name, version, advisory["id"]))
    return issues


def query(packages: list[tuple[str, str]]) -> object:
    deadline = time.monotonic() + 30
    body = {"queries": [{"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
                        for name, version in packages]}
    request = RestrictedRequest(OSV_URL, data=json.dumps(body).encode("utf-8"),
                                headers={"Content-Type": "application/json"},
                                allowed_hosts={"api.osv.dev"}, deadline=deadline)
    with safe_urlopen(request, timeout=15) as response:
        return json.loads(read_limited(response, deadline, 2 * 1024 * 1024))


def self_test() -> int:
    packages = [("example", "1.0")]
    if findings(packages, {"results": [{}]}):
        return 1
    if not findings(packages, {"results": [{"vulns": [{"id": "TEST-1"}]}]}):
        return 1
    invalid: list[object] = [None, {}, {"results": []}, {"results": [None]},
                            {"results": [{"error": "unavailable"}]}, {"results": [{"vulns": None}]}]
    for payload in invalid:
        try:
            findings(packages, payload)
        except ValueError:
            continue
        return 1
    print("dependency audit self-test: valid, vulnerable and 6 inconclusive results distinguished")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=ROOT / "requirements.lock")
    parser.add_argument("--requirements", type=Path, default=ROOT / "requirements.txt")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    try:
        locked = locked_packages(args.lock)
        check_lock(locked_packages(args.requirements), locked)
        installed: dict[str, str] = {canonicalize_name(dist.metadata["Name"]): dist.version
                                     for dist in importlib.metadata.distributions()}
        check_lock(locked, installed)
        packages = sorted(installed.items())
        issues = findings(packages, query(packages))
        if issues:
            for issue in issues:
                print("Known vulnerability: " + issue)
            return 1
    except Exception as exc:
        print("Dependency audit inconclusive: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    print("Dependency lock matches; OSV returned no known advisories for %d installed PyPI packages" % len(packages))
    print("Scope excludes unindexed native components and unknown vulnerabilities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
