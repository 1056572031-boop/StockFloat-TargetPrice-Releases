#!/usr/bin/env python3
"""Independent branch observation; never change the line-coverage ratchet.

--collect runs the existing full gate with an ephemeral branch-enabled copy of
.coveragerc and an independent data file. --input-json reads an already exported
snapshot without reinterpreting its line numbers against changed source files.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json

import gate_stamp
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "build" / "branch-coverage" / "branch.coverage"
DEFAULT_OUTPUT = ROOT / "build" / "branch-coverage"
FOCUS = ("stock_float_window.py", "sf_glass.py", "sf_presence.py",
         "sf_ui_render.py", "sf_logging.py")
EXCLUDED = ("tests/", "backups/", "build/", "dist/")


def percent(covered: int, total: int):
    return round(100 * covered / total, 2) if total else None


def counts(summary: dict) -> dict:
    if not isinstance(summary, dict):
        raise ValueError("Coverage summary must be an object")
    keys = ("num_statements", "covered_lines", "missing_lines", "num_branches",
            "covered_branches", "missing_branches", "num_partial_branches")
    values = {}
    for key in keys:
        value = summary.get(key)
        if type(value) is not int or value < 0:
            raise ValueError("Invalid coverage count %s: %r" % (key, value))
        values[key] = value
    if values["covered_lines"] + values["missing_lines"] != values["num_statements"]:
        raise ValueError("Line totals do not reconcile")
    if values["covered_branches"] + values["missing_branches"] != values["num_branches"]:
        raise ValueError("Branch totals do not reconcile")
    values["line_percent"] = percent(values["covered_lines"], values["num_statements"])
    values["branch_percent"] = percent(values["covered_branches"], values["num_branches"])
    return values


def summarize(raw: dict, focus=FOCUS) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("meta"), dict):
        raise ValueError("Coverage JSON must have object metadata")
    if raw["meta"].get("branch_coverage") is not True:
        raise ValueError("Input contains line coverage only; collect with branch=True first")
    if not isinstance(raw.get("files"), dict):
        raise ValueError("Coverage files must be an object")
    files = []
    for filename, entry in raw.get("files", {}).items():
        if not isinstance(filename, str) or not isinstance(entry, dict):
            raise ValueError("Coverage file entries must be named objects")
        name = filename.replace("\\", "/")
        if name.startswith(EXCLUDED):
            continue
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError("Expected source-root-relative filename: %s" % name)
        stats = counts(entry.get("summary", {}))
        functions = []
        functions_data = entry.get("functions", {})
        if not isinstance(functions_data, dict):
            raise ValueError("Coverage functions must be an object")
        for function, details in functions_data.items():
            if not function:
                continue
            if not isinstance(function, str) or not isinstance(details, dict):
                raise ValueError("Coverage functions must be named objects")
            fs = counts(details.get("summary", {}))
            if fs["missing_branches"]:
                functions.append({"name": function, **fs,
                                  "missing_edges": details.get("missing_branches", [])})
        functions.sort(key=lambda item: (-item["missing_branches"], item["name"]))
        files.append({"name": name, **stats,
                      "missing_edges": entry.get("missing_branches", []),
                      "functions": functions})
    if not files:
        raise ValueError("No product or chain files in branch input")
    names = {item["name"] for item in files}
    missing = sorted(set(focus) - names)
    if missing:
        raise ValueError("Focus modules absent from input: %s" % ", ".join(missing))
    tiers = {}
    for tier, is_chain in (("product", False), ("chain", True)):
        selected = [f for f in files if f["name"].startswith("tools/") == is_chain]
        fields = ("num_statements", "covered_lines", "missing_lines", "num_branches",
                  "covered_branches", "missing_branches", "num_partial_branches")
        stats = counts({key: sum(f[key] for f in selected) for key in fields})
        tiers[tier] = {"files": len(selected), **stats}
    files.sort(key=lambda item: (-item["missing_branches"], item["name"]))
    return {"schema": 1, "kind": "branch-observation", "release_gate": False,
            "coverage_meta": raw.get("meta", {}), "tiers": tiers,
            "focus": [next(f for f in files if f["name"] == name) for name in focus],
            "files": files}


def markdown(report: dict) -> str:
    def ratio(value):
        return "n/a" if value is None else "%.2f%%" % value

    evidence = report.get("evidence", {"status": "unverified", "origin": "in-memory-summary"})
    lines = ["# Branch Coverage Observation", "",
             "Evidence: %s (%s)." % (evidence["status"], evidence["origin"]), "",
             "Not a release gate. Line and branch percentages use separate denominators.",
             "Parent-process statistics do not automatically include subprocesses.", "",
             "Exception recovery still needs behavioral tests; branch counts do not enumerate every exception.", "",
             "| Scope | Line | Branch | Missing Branches | Partial Branch Lines |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for name, stats in report["tiers"].items():
        lines.append("| %s | %s | %s | %d | %d |" % (
            name, ratio(stats["line_percent"]), ratio(stats["branch_percent"]),
            stats["missing_branches"], stats["num_partial_branches"]))
    lines += ["", "## High-Risk Modules", "",
              "| Module | Line | Branch | Missing Branches |",
              "| --- | ---: | ---: | ---: |"]
    for item in report["focus"]:
        lines.append("| %s | %s | %s | %d |" % (
            item["name"], ratio(item["line_percent"]), ratio(item["branch_percent"]),
            item["missing_branches"]))
    lines += ["", "## Remaining Function Edges", "",
              "Edges are source-line pairs; negative targets denote function exits.",
              "An unmeasured edge is not automatically a missing behavioral test.", ""]
    for item in report["focus"]:
        lines += ["### " + item["name"], ""]
        for function in item["functions"][:20]:
            lines.append("- `%s`: %d missing edges, branch %s; `%s`" % (
                function["name"], function["missing_branches"],
                ratio(function["branch_percent"]), function["missing_edges"]))
        if not item["functions"]:
            lines.append("No missing function edges recorded.")
        lines.append("")
    return "\n".join(lines) + "\n"


def raw_bytes(raw: dict) -> bytes:
    return (json.dumps(raw, ensure_ascii=False) + "\n").encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_info() -> dict:
    info = gate_stamp.current_info(str(ROOT))
    if info["files"] <= 0:
        raise ValueError("Empty source fingerprint inputs")
    return {"root": str(ROOT.resolve()), "fingerprint": info["fingerprint"],
            "files": info["files"]}


def receipt_path(data: Path) -> Path:
    return data.with_name(data.name + ".branch-receipt.json")


def observed_input(data: Path) -> tuple[dict, dict]:
    path = receipt_path(data)
    if not path.exists():
        return read_data(data), {"status": "unverified", "origin": "data-without-receipt",
                                 "source": None, "command": None}
    evidence = json.loads(path.read_text(encoding="utf-8"))
    required = ("raw", "status", "origin", "source", "command", "provenance_receipt")
    if (not isinstance(evidence, dict) or evidence.get("state") != "completed"
            or any(key not in evidence for key in required)):
        raise ValueError("Branch receipt not completed or incomplete")
    if evidence["status"] != "verified-local" or evidence["origin"] != "collect-run-tests":
        raise ValueError("Branch receipt origin/status mismatch")
    if evidence.get("source") != source_info():
        raise ValueError("Branch receipt source fingerprint mismatch")
    if (evidence.get("data_file") != str(data.resolve())
            or evidence.get("data_sha256") != digest(data.read_bytes())):
        raise ValueError("Branch receipt database mismatch")
    raw = evidence["raw"]
    if evidence.get("raw_sha256") != digest(raw_bytes(raw)):
        raise ValueError("Branch receipt raw hash mismatch")
    return raw, {key: evidence[key] for key in
                 ("status", "origin", "source", "command", "provenance_receipt")}


def collect(data: Path, log: Path) -> int:
    if data.resolve() == (ROOT / ".coverage").resolve():
        raise ValueError("Branch collection must not overwrite default line .coverage")
    config = configparser.ConfigParser()
    if not config.read(ROOT / ".coveragerc", encoding="utf-8") or not config.has_section("run"):
        raise ValueError("Missing project .coveragerc [run] configuration")
    config.set("run", "branch", "true")
    data.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    receipt = receipt_path(data)
    receipt.write_text(json.dumps({"state": "invalid"}), encoding="utf-8")
    before = source_info()
    command = [str(ROOT / "run_tests.sh")]
    # Nested fixture sockets must fit macOS's short AF_UNIX pathname limit.
    with tempfile.TemporaryDirectory(prefix="sfbr_", dir="/tmp") as tmp:
        ini = Path(tmp) / "coverage.ini"
        with ini.open("w", encoding="utf-8") as handle:
            config.write(handle)
        env = dict(os.environ, COVERAGE_FILE=str(data.resolve()), COVERAGE_RCFILE=str(ini),
                   SF_SKIP_COVERAGE="0", SF_RANDOM_ORDER="0", TMPDIR=tmp + os.sep)
        env.pop("SF_GUARD_PROBE", None)
        with log.open("w", encoding="utf-8") as handle:
            result = subprocess.run(command, cwd=ROOT, env=env,
                                    stdout=handle, stderr=subprocess.STDOUT, check=False)
        if before != source_info():
            raise ValueError("Source fingerprint changed during branch collection")
        if result.returncode:
            return result.returncode
        # run_tests already finished this receipt. Check again while the exact
        # temporary config still exists, not after TemporaryDirectory cleanup.
        provenance = ROOT / "build" / ".coverage-provenance.json"
        checked = subprocess.run(
            [str(ROOT / ".venv/bin/python3"), str(ROOT / "tools/coverage_provenance.py"),
             "check", "--receipt", str(provenance)], cwd=ROOT, env=env,
            text=True, capture_output=True, check=False)
        if checked.returncode:
            raise ValueError("Collection provenance rejected: " + (checked.stderr or checked.stdout))
        proof = json.loads(provenance.read_text(encoding="utf-8"))
        if (proof.get("state") != "completed" or proof.get("exit_code") != 0
                or proof.get("begin", {}).get("data_file") != str(data.resolve())
                or proof.get("begin", {}).get("source", {}).get("fingerprint") != before["fingerprint"]
                or proof.get("data", {}).get("sha256") != digest(data.read_bytes())):
            raise ValueError("Collection provenance does not match branch data/source")
        data_hash = digest(data.read_bytes())
        raw = read_data(data, env=env)
        if before != source_info() or data_hash != digest(data.read_bytes()):
            raise ValueError("Source fingerprint or data changed during branch export")
        evidence = {"state": "completed", "status": "verified-local",
                    "origin": "collect-run-tests", "source": before, "command": command,
                    "data_file": str(data.resolve()), "data_sha256": data_hash,
                    "raw": raw, "raw_sha256": digest(raw_bytes(raw)),
                    "provenance_receipt": proof}
        receipt.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
    return 0


def read_data(data: Path, env=None) -> dict:
    if not data.is_file():
        raise ValueError("Branch data does not exist: %s" % data)
    with tempfile.TemporaryDirectory(prefix="sf_branch_json_") as tmp:
        output = Path(tmp) / "coverage.json"
        proc = subprocess.run([sys.executable, "-m", "coverage", "json",
                               "--data-file=" + str(data.resolve()), "-o", str(output), "-q"],
                              cwd=ROOT, env=env, text=True, capture_output=True, check=False)
        if proc.returncode or not output.is_file():
            raise ValueError("Cannot export branch data: %s" % (proc.stderr or proc.stdout))
        return json.loads(output.read_text(encoding="utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--data", type=Path, default=DEFAULT_DATA)
    source.add_argument("--input-json", type=Path)
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.collect and args.input_json:
        parser.error("--collect cannot be combined with --input-json")
    try:
        if args.collect:
            code = collect(args.data, args.output_dir / "run.log")
            if code:
                print("Full gate failed (exit %d); no branch verdict. Log: %s" %
                      (code, args.output_dir / "run.log"), file=sys.stderr)
                return code
        if args.input_json:
            raw = json.loads(args.input_json.read_text(encoding="utf-8"))
            evidence = {"status": "unverified", "origin": "historical-input-json",
                        "source": None, "command": None}
        else:
            raw, evidence = observed_input(args.data)
        report = summarize(raw)
        report["evidence"] = {**evidence, "input_path": str((args.input_json or args.data).resolve()),
                              "raw_sha256": digest(raw_bytes(raw)),
                              "boundary": "Local non-adversarial evidence; endpoint fingerprints only. "
                              "Parent-process statistics do not automatically include subprocesses."}
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "raw.json").write_bytes(raw_bytes(raw))
        (args.output_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.output_dir / "report.md").write_text(markdown(report), encoding="utf-8")
        print("Branch observation written to %s (not a release gate)" % args.output_dir)
        return 0
    except (OSError, ValueError) as exc:
        print("No branch conclusion: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
