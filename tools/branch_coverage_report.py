#!/usr/bin/env python3
"""Independent branch observation; never change the line-coverage ratchet.

--collect runs the existing full gate with an ephemeral branch-enabled copy of
.coveragerc and an independent data file. --input-json reads an already exported
snapshot without reinterpreting its line numbers against changed source files.
"""
from __future__ import annotations

import argparse
import configparser
import json
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

    lines = ["# Branch Coverage Observation", "",
             "Not a release gate. Line and branch percentages use separate denominators.",
             "Subprocess-native paths are not measured by this parent-process dataset.", "",
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


def collect(data: Path, log: Path) -> int:
    if data.resolve() == (ROOT / ".coverage").resolve():
        raise ValueError("Branch collection must not overwrite default line .coverage")
    config = configparser.ConfigParser()
    if not config.read(ROOT / ".coveragerc", encoding="utf-8") or not config.has_section("run"):
        raise ValueError("Missing project .coveragerc [run] configuration")
    config.set("run", "branch", "true")
    data.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    # Nested fixture sockets must fit macOS's short AF_UNIX pathname limit.
    with tempfile.TemporaryDirectory(prefix="sfbr_", dir="/tmp") as tmp:
        ini = Path(tmp) / "coverage.ini"
        with ini.open("w", encoding="utf-8") as handle:
            config.write(handle)
        env = dict(os.environ, COVERAGE_FILE=str(data.resolve()), COVERAGE_RCFILE=str(ini),
                   SF_SKIP_COVERAGE="0", SF_RANDOM_ORDER="0", TMPDIR=tmp + os.sep)
        env.pop("SF_GUARD_PROBE", None)
        with log.open("w", encoding="utf-8") as handle:
            result = subprocess.run([str(ROOT / "run_tests.sh")], cwd=ROOT, env=env,
                                    stdout=handle, stderr=subprocess.STDOUT, check=False)
    return result.returncode


def read_data(data: Path) -> dict:
    if not data.is_file():
        raise ValueError("Branch data does not exist: %s" % data)
    with tempfile.TemporaryDirectory(prefix="sf_branch_json_") as tmp:
        output = Path(tmp) / "coverage.json"
        proc = subprocess.run([sys.executable, "-m", "coverage", "json",
                               "--data-file=" + str(data.resolve()), "-o", str(output), "-q"],
                              cwd=ROOT, text=True, capture_output=True, check=False)
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
        raw = (json.loads(args.input_json.read_text(encoding="utf-8")) if args.input_json
               else read_data(args.data))
        report = summarize(raw)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "raw.json").write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")
        (args.output_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.output_dir / "report.md").write_text(markdown(report), encoding="utf-8")
        print("Branch observation written to %s (not a release gate)" % args.output_dir)
        return 0
    except (OSError, ValueError) as exc:
        print("No branch conclusion: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
