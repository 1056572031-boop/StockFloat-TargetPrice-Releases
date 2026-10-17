#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Require complete coverage for executable lines changed since a git revision.

This is deliberately an observation tool: coverage provenance is checked by the
caller, not invented here.  A result is never a proof that source and coverage
were produced by the same run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import coverage

_GATE_STAMP_SPEC = spec_from_file_location("change_coverage_gate_gate_stamp", Path(__file__).with_name("gate_stamp.py"))
if _GATE_STAMP_SPEC is None or _GATE_STAMP_SPEC.loader is None:
    raise ImportError("cannot load gate_stamp")
_GATE_STAMP = module_from_spec(_GATE_STAMP_SPEC)
_GATE_STAMP_SPEC.loader.exec_module(_GATE_STAMP)
git_env = _GATE_STAMP.git_env

PRODUCT_ROOTS = (".", "tools")
EXCLUDED = ("tests/", "build/", "dist/", "backups/", "tools/archive/")
PY_SUFFIX = ".py"


def _git_binary(root: Path, args: list[str]) -> bytes | None:
    try:
        proc = subprocess.run(["git", *args], cwd=root, env=git_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=False)
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _git_text(root: Path, args: list[str]) -> str | None:
    output = _git_binary(root, args)
    return None if output is None else output.decode("utf-8", "surrogateescape")


def _safe_rel(raw: str) -> str:
    if not raw or "\x00" in raw or os.path.isabs(raw):
        raise ValueError("非法 git 路径：%r" % raw)
    rel = raw.replace("\\", "/")
    parts = rel.split("/")
    if ".." in parts:
        raise ValueError("路径逃逸：%s" % raw)
    return rel


def _selected(rel: str) -> tuple[bool, str]:
    path = rel.replace("\\", "/")
    if not path.endswith(PY_SUFFIX):
        return False, "非 Python 文件"
    for prefix in EXCLUDED:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return False, "排除目录 " + prefix.rstrip("/")
    if path.startswith("tools/") or "/" not in path:
        return True, "活跃产品/tools Python"
    return False, "不在产品根目录或 tools/"


def _parse_hunks(diff: bytes, path: str | None = None) -> tuple[dict[str, set[int]], dict[str, list[str]]]:
    """Parse only hunk headers; the path is supplied out-of-band to avoid quoting loss."""
    text = diff.decode("utf-8", "surrogateescape")
    changed: dict[str, set[int]] = {path: set()} if path else {}
    deleted: dict[str, list[str]] = {}
    current = path
    for line in text.splitlines():
        if line.startswith("@@") and current is not None:
            import re
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2) or "1")
                changed[current].update(range(start, start + count))
            old = re.search(r"-(\d+)(?:,(\d+))?", line)
            if old and int(old.group(2) or "1"):
                deleted.setdefault(current, []).append("删除旧行 %s" % old.group(1))
    return changed, deleted


def _base_has_path(root: Path, base: str, rel: str) -> bool:
    return _git_binary(root, ["cat-file", "-e", "%s:%s" % (base, rel)]) is not None


def _resolve_base(root: Path, base: str) -> str:
    resolved = _git_text(root, ["rev-parse", "--verify", base + "^{commit}"])
    if resolved is None:
        raise ValueError("base 不存在或不是 commit：%s" % base)
    return resolved.strip()


def _git_changes(root: Path, base: str) -> tuple[dict[str, set[int]], dict[str, list[str]], list[str]]:
    resolved_base = _resolve_base(root, base)
    names = _git_binary(root, ["-c", "core.quotePath=false", "diff", "--name-only", "-z",
                              "--no-renames", resolved_base, "--"])
    if names is None:
        raise ValueError("无法读取 changed names")
    changed: dict[str, set[int]] = {}
    deleted: dict[str, list[str]] = {}
    for raw in names.split(b"\0"):
        if raw:
            rel = _safe_rel(raw.decode("utf-8", "surrogateescape"))
            changed.setdefault(rel, set())
    for rel in list(changed):
        diff = _git_binary(root, ["-c", "core.quotePath=false", "diff", "--unified=0",
                                 "--no-renames", resolved_base, "--", rel])
        if diff is None:
            raise ValueError("无法读取文件 diff：%s" % rel)
        one_changed, one_deleted = _parse_hunks(diff, rel)
        changed[rel] = one_changed.get(rel, set())
        if rel in one_deleted:
            deleted[rel] = one_deleted[rel]
    untracked = _git_binary(root, ["ls-files", "--others", "--exclude-standard", "-z"])
    if untracked is None:
        raise ValueError("无法读取未跟踪文件")
    for raw in untracked.split(b"\0"):
        if raw:
            rel = _safe_rel(raw.decode("utf-8", "surrogateescape"))
            if rel.endswith(PY_SUFFIX):
                changed.setdefault(rel, set())
    head = _git_text(root, ["rev-parse", "HEAD"]) or ""
    return changed, deleted, [resolved_base, head.strip()]


def _resolve_source(root: Path, rel: str) -> Path:
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("源路径逃逸：%s" % rel)
    return path


def _digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _list_length(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _coverage_lines(cov: coverage.Coverage, source: Path) -> tuple[set[int], set[int]]:
    try:
        result = cov.analysis2(str(source))
    except (coverage.CoverageException, OSError, SyntaxError) as exc:
        raise ValueError("coverage analysis2 无法解析 %s：%s" % (source, exc)) from exc
    statements = set(int(x) for x in result[1])
    missing = set(int(x) for x in result[3])
    return statements, missing


def inspect(root: Path, base: str, data_file: Path) -> dict:
    changed, deleted, refs = _git_changes(root, base)
    if not data_file.is_file() or data_file.stat().st_size == 0:
        raise ValueError("coverage 数据文件缺失或为空")
    data = coverage.CoverageData(basename=str(data_file))
    try:
        data.read()
    except Exception as exc:
        raise ValueError("coverage 数据损坏或不可读：%s" % exc) from exc
    root_abs = root.resolve()
    measured_files = data.measured_files()
    if not measured_files:
        raise ValueError("coverage 数据没有 measured 文件")
    outside = []
    for filename in measured_files:
        candidate = Path(filename)
        candidate = (candidate if candidate.is_absolute() else root_abs / candidate).resolve()
        if not candidate.is_relative_to(root_abs):
            outside.append(filename)
    if outside:
        raise ValueError("coverage measured 文件在工作树外：%s" % ", ".join(sorted(outside)))
    # Normalize into a memory-only copy: never erase or write the source database.
    cov = coverage.Coverage(data_file=None, config_file=False)
    cov.get_data().update(data, map_path=lambda filename: str(
        (Path(filename) if Path(filename).is_absolute() else root_abs / filename).resolve()))
    data = cov.get_data()
    files: list[dict[str, object]] = []
    excluded = []
    for rel, new_lines in sorted(changed.items()):
        keep, reason = _selected(rel)
        if not keep:
            excluded.append({"path": rel, "reason": reason})
            continue
        source = _resolve_source(root, rel)
        if not source.exists():
            files.append({"path": rel, "status": "deleted", "changed_lines": sorted(new_lines),
                          "executable_changed": [], "missing": [], "deleted": deleted.get(rel, [])})
            continue
        statements, missing = _coverage_lines(cov, source)
        is_new = not _base_has_path(root, refs[0], rel)
        target = statements if is_new else statements & new_lines
        target_missing = target & missing
        entry: dict[str, object] = {"path": rel, "status": "new" if is_new else "modified",
                                    "changed_lines": sorted(new_lines),
                                    "executable_changed": sorted(target),
                                    "missing": sorted(target_missing),
                                    "deleted": deleted.get(rel, []),
                                    "source_sha256": _digest(source)}
        if data.has_arcs():
            arcs = data.arcs(str(source)) or []
            entry["changed_source_arcs_observed"] = len(arcs)
            entry["branch_note"] = "仅观察 coverage arcs；未将未知边自动视为通过"
        files.append(entry)
    applicable = [f for f in files if f.get("status") != "deleted"]
    executable = sum(_list_length(f.get("executable_changed")) for f in applicable)
    missing_count = sum(_list_length(f.get("missing")) for f in applicable)
    if not changed:
        status = "not_applicable"
        ok = False
    elif executable == 0:
        status = "not_applicable"
        ok = False
    else:
        status = "pass" if missing_count == 0 else "fail"
        ok = missing_count == 0
    return {"schema": 1, "status": status, "ok": ok, "base": refs[0], "head": refs[1],
            "selection": {"roots": list(PRODUCT_ROOTS), "excluded": list(EXCLUDED),
                           "changed_candidates": len(changed), "applicable_files": len(files)},
            "data_file": str(data_file), "data_sha256": _digest(data_file),
            "has_arcs": data.has_arcs(), "files": files, "excluded_files": excluded,
            "deleted_files": deleted, "summary": {"executable_changed": executable,
            "missing": missing_count}}


def _self_test() -> int:
    # Real temporary repository + real coverage database; deliberately no writes
    # to the checkout.  More detailed mutation cases live in tests.
    with tempfile.TemporaryDirectory(prefix="change_cov_") as td:
        root = Path(td)
        subprocess.run(["git", "init", "-q"], cwd=root, env=git_env(), check=True)
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.py"], cwd=root, env=git_env(), check=True)
        subprocess.run(["git", "-c", "user.email=gate@example.invalid",
                        "-c", "user.name=gate", "commit", "-qm", "base"],
                       cwd=root, env=git_env(), check=True)
        (root / "a.py").write_text("x = 2\n", encoding="utf-8")
        source = (root / "a.py").resolve()
        for executed, expected, missing in (("\npass\n", "fail", [1]),
                                            (source.read_text(), "pass", [])):
            cov = coverage.Coverage(data_file=str(root / ".coverage"), config_file=False)
            cov.start()
            try:
                exec(compile(executed, str(source), "exec"), {})
            finally:
                cov.stop()
                cov.save()
            result = inspect(root, "HEAD", root / ".coverage")
            if (result["status"] != expected or result["ok"] != (expected == "pass")
                    or result["summary"] != {"executable_changed": 1, "missing": len(missing)}
                    or len(result["files"]) != 1
                    or result["files"][0]["executable_changed"] != [1]
                    or result["files"][0]["missing"] != missing):
                print("self-test failed: missing must fail and measured line must pass (nonempty)",
                      file=sys.stderr)
                return 1
    print("== change coverage gate self-test passed ==")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--report-json")
    parser.add_argument("--data-file", default=None)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        if args.base or args.report_json or args.data_file is not None:
            parser.error("--self-test 不接受其他动作参数")
        return _self_test()
    if not args.base:
        parser.error("--base 必填；工具不会自动回退到 HEAD")
    root = Path.cwd().resolve()
    data_file = Path(args.data_file or os.environ.get("COVERAGE_FILE", ".coverage")).expanduser()
    if not data_file.is_absolute():
        data_file = (root / data_file).resolve()
    try:
        report = inspect(root, args.base, data_file)
    except (ValueError, OSError, coverage.CoverageException) as exc:
        print("!! change coverage rejected: %s" % exc, file=sys.stderr)
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report_json:
        out = Path(args.report_json).expanduser()
        if not out.is_absolute():
            out = (root / out).resolve()
        if not out.is_relative_to(root):
            print("!! report path escapes worktree", file=sys.stderr)
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    print("== change coverage: %s (%d executable, %d missing) ==" %
          (report["status"], report["summary"]["executable_changed"], report["summary"]["missing"]))
    for entry in report["files"]:
        if entry.get("missing"):
            print("!! %s 未覆盖行：%s" % (entry["path"], ",".join(map(str, entry["missing"]))))
    if report["status"] == "not_applicable":
        print("无适用执行行，不阻断发版；不算 coverage 通过。")
        return 0
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
