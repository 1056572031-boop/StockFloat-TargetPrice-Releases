"""Local, non-adversarial coverage receipt; not a signed proof or a source watcher.

Run begin, coverage (without overriding its config via CLI), finish, then check in
one cwd/environment. Temporary COVERAGE_RCFILE files must survive through check.
Edits restored between observations, forged timestamps/receipts and hostile races
are outside this evidence boundary. No environment variables are replaced here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import coverage

import importlib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
gate_stamp = importlib.import_module("tools.gate_stamp")

DEFAULT_RECEIPT = "build/.coverage-provenance.json"
RUN_OPTIONS = (
    "branch", "command_line", "concurrency", "context", "core", "cover_pylib",
    "data_file", "debug", "debug_file", "disable_warnings", "dynamic_context",
    "include", "omit", "parallel", "patch", "plugins", "relative_files",
    "sigterm", "source", "source_dirs", "source_pkgs", "timid",
)


class EvidenceError(ValueError):
    """Missing or stale local evidence."""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def receipt_path(value: str) -> Path:
    root = Path(gate_stamp.resolve_root()).resolve()
    path = Path(value).absolute().resolve()
    if not path.is_relative_to(root / "build") or path.suffix != ".json":
        raise EvidenceError("receipt must be a .json file below this tree's build/")
    if str(path.relative_to(root)) in gate_stamp.build_input_paths(str(root)):
        raise EvidenceError("receipt must not be a source fingerprint input")
    return path


def atomic_write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".provenance-", suffix=".json",
                                         delete=False) as stream:
            name = stream.name
            json.dump(obj, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def snapshot() -> dict:
    root = str(Path(gate_stamp.resolve_root()).resolve())
    source = gate_stamp.current_info(root)
    if source["files"] <= 0:
        raise EvidenceError("empty source inputs: cannot establish provenance")
    cov = coverage.Coverage()
    # sys_info and get_option are public APIs. Hash actual config bytes too, so
    # a same-named replacement cannot hide behind identical selected options.
    info = dict(cov.sys_info())
    configs = [{"path": str(Path(p).resolve()), "sha256": digest(Path(p))}
               for p in info["configs_read"]]
    options = {key: cov.get_option("run:" + key) for key in RUN_OPTIONS}
    data_option = options["data_file"]
    if not isinstance(data_option, str):
        raise EvidenceError("coverage data_file must be a path string")
    data_file = str(Path(data_option).resolve())
    return {
        "root": root, "cwd": str(Path.cwd().resolve()), "source": source,
        "config": {"files": configs, "run": options,
                   "rcfile_env": os.environ.get("COVERAGE_RCFILE"),
                   "file_env": os.environ.get("COVERAGE_FILE")},
        "runtime": {"coverage": coverage.__version__, "python": sys.version,
                    "executable": os.path.abspath(sys.executable),
                    "executable_real": os.path.realpath(sys.executable),
                    "prefix": sys.prefix},
        "data_file": data_file,
    }


def file_identity(path: Path) -> dict | None:
    if not path.exists():
        return None
    st = path.stat()
    return {"sha256": digest(path), "mtime_ns": st.st_mtime_ns,
            "ctime_ns": st.st_ctime_ns, "size": st.st_size,
            "inode": st.st_ino, "device": st.st_dev}


def load(path: Path) -> dict:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict) or obj.get("schema") != 1:
        raise EvidenceError("invalid receipt schema")
    return obj


def same_run(before: dict, now: dict) -> None:
    for key in ("root", "cwd", "config", "runtime", "data_file"):
        if before[key] != now[key]:
            raise EvidenceError("changed " + key)
    # HEAD is archival only: use the gate's content/mode/untracked algorithm.
    if (before["source"]["fingerprint"] != now["source"]["fingerprint"]
            or before["source"]["files"] != now["source"]["files"]):
        raise EvidenceError("changed source fingerprint")


def data_summary(path: Path) -> dict:
    data = coverage.CoverageData(basename=str(path))
    data.read()
    files = sorted(data.measured_files())
    if not files or not any(data.lines(f) for f in files):
        raise EvidenceError("coverage data has no measured lines")
    return {"measured_files": files, "contexts": sorted(data.measured_contexts()),
            "has_arcs": data.has_arcs()}


def begin(value: str = DEFAULT_RECEIPT) -> dict:
    path = receipt_path(value)
    # Even an unsuccessful new begin must never leave an older completed run.
    atomic_write(path, {"schema": 1, "state": "invalid"})
    start = snapshot()
    baseline = file_identity(Path(start["data_file"]))
    obj = {"schema": 1, "state": "begun", "begin": start,
           "baseline": baseline, "started_ns": time.time_ns()}
    atomic_write(path, obj)
    return obj


def finish(value: str = DEFAULT_RECEIPT, exit_code: int = 0) -> dict:
    path = receipt_path(value)
    obj = load(path)
    state = obj.get("state")
    # A failed/repeated finish cannot preserve a previous completion either.
    atomic_write(path, {**obj, "state": "failed", "exit_code": exit_code})
    if state != "begun" or exit_code != 0:
        raise EvidenceError("finish requires begun state and exit code zero")
    now = snapshot()
    same_run(obj["begin"], now)
    data_path = Path(now["data_file"])
    identity = file_identity(data_path)
    if (identity is None or identity == obj["baseline"]
            or identity["mtime_ns"] < obj["started_ns"]):
        raise EvidenceError("coverage data missing or stale (not generated after begin)")
    summary = data_summary(data_path)
    if summary["has_arcs"] != bool(now["config"]["run"]["branch"]):
        raise EvidenceError("coverage branch mode differs from configuration")
    if file_identity(data_path) != identity:
        raise EvidenceError("coverage data changed while reading")
    obj.update(state="completed", exit_code=0, finished_ns=time.time_ns(),
               data=identity, summary=summary)
    atomic_write(path, obj)
    return obj


def check(value: str = DEFAULT_RECEIPT) -> dict:
    obj = load(receipt_path(value))
    if obj.get("state") != "completed" or obj.get("exit_code") != 0:
        raise EvidenceError("receipt not completed successfully")
    same_run(obj["begin"], snapshot())
    path = Path(obj["begin"]["data_file"])
    if file_identity(path) != obj["data"]:
        raise EvidenceError("coverage database identity/hash changed or missing")
    if data_summary(path) != obj["summary"]:
        raise EvidenceError("coverage measured files/contexts/arcs changed")
    return obj


def self_test() -> int:
    # Reuse real tiny-coverage fixtures and rejection samples, not a flag-only
    # success. Tests live beside this tool in the development repository.
    import unittest
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_coverage_provenance")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and result.testsRun > 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    sub = parser.add_subparsers(dest="verb")
    for verb in ("begin", "finish", "check"):
        cmd = sub.add_parser(verb)
        cmd.add_argument("--receipt", default=DEFAULT_RECEIPT)
        if verb == "finish":
            cmd.add_argument("--exit-code", type=int, required=True)
    args = parser.parse_args(argv)
    if args.self_test:
        if args.verb:
            parser.error("--self-test cannot be combined with an action")
        return self_test()
    if not args.verb:
        parser.error("an action is required")
    try:
        if args.verb == "begin":
            begin(args.receipt)
        elif args.verb == "finish":
            finish(args.receipt, args.exit_code)
        else:
            check(args.receipt)
    except (OSError, ValueError, KeyError, TypeError, coverage.exceptions.CoverageException) as exc:
        print("coverage provenance rejected: " + str(exc), file=sys.stderr)
        return 1
    print("coverage provenance: " + args.verb + " OK (local, non-adversarial evidence)")
    return 0


if __name__ == "__main__":
    # Direct CLI execution must resolve the local tests package for --self-test.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main())
