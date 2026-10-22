"""Read-only early checks for ordinary commits, never a gate or commit executor."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Use the same module identity as coverage_provenance for both entry points.
gate_stamp = importlib.import_module("tools.gate_stamp")

ROUTING = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT", "GIT_SHALLOW_FILE",
    "GIT_REPLACE_REF_BASE",
}
OPERATIONS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge",
              "rebase-apply", "sequencer", "BISECT_LOG", "index.lock")
NOT_CHECKED = ["authorization", "business_review", "git_identity", "sensitive_content_review",
               "runtime_versions", "ignored_dependencies",
               "full_gate", "commit_tree_readback", "continuous_write_isolation"]
EXTRA_INPUT_SUFFIXES = (".json", ".jsonc", ".toml", ".ini", ".cfg", ".env", ".js",
                        ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sql", ".bash", ".zsh")


class ProbeError(RuntimeError):
    pass


def screen_state() -> dict[str, bool | None]:
    """Read-only screen lock and display sleep state; None means it could not be read.

    The product's own sf_presence.screen_unavailable() deliberately fails open to False, which is
    right for power saving but wrong here: a preflight must tell "not locked" from "unknown".
    Locked is not the same as display asleep, and pmset logs cannot see a locked session.
    """
    state: dict[str, bool | None] = {"locked": None, "display_asleep": None}
    try:
        import Quartz
    except Exception:
        return state
    try:
        session = Quartz.CGSessionCopyCurrentDictionary()
        if session is not None:
            state["locked"] = int(session.get("CGSSessionScreenIsLocked", 0) or 0) == 1
    except Exception:
        pass
    try:
        state["display_asleep"] = bool(Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()))
    except Exception:
        pass
    return state


def screen_warnings(state: dict[str, bool | None]) -> list[str]:
    """Non-blocking: some tests need a live screen, so a locked one gives false reds."""
    warnings = []
    if state.get("locked"):
        warnings.append("screen_locked_full_gate_may_fail_for_unrelated_reasons")
    if state.get("display_asleep"):
        warnings.append("display_asleep_full_gate_may_fail_for_unrelated_reasons")
    if state.get("locked") is None or state.get("display_asleep") is None:
        warnings.append("screen_state_unreadable")
    return warnings


def inspect_repo(root: Path, paths: list[str], *, expected_head: str | None = None,
                 timeout: float = 15, screen_probe=screen_state) -> dict:
    started = time.perf_counter()
    root = root.resolve()
    issues: list[dict[str, Any]] = []
    report: dict[str, Any] = {"schema": 1, "checks_passed": False, "issues": issues,
                              "not_checked": list(NOT_CHECKED), "root": str(root),
                              "scope": paths, "git_calls": 0}
    # Reported even when a later check stops early; never changes checks_passed.
    report["screen"] = screen_probe()
    report["warnings"] = screen_warnings(report["screen"])
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")

    def issue(code: str, **details):
        issues.append({"code": code, **details})

    def run(*args: str, allowed=(0,)) -> subprocess.CompletedProcess[bytes]:
        remaining = timeout - (time.perf_counter() - started)
        if remaining <= 0:
            raise ProbeError("probe_timeout")
        report["git_calls"] += 1
        result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
                                 *args], cwd=root, env=env, capture_output=True,
                                timeout=min(5, remaining), check=False)
        if result.returncode not in allowed:
            raise ProbeError("git_query_failed:" + args[0])
        return result

    def query(*args: str, allowed=(0,)) -> bytes:
        return run(*args, allowed=allowed).stdout

    def quiet(*args: str) -> bool:
        # A diff exit code of 1 is a finding, while other failures must stay errors.
        return run(*args, allowed=(0, 1)).returncode == 0

    def git_path(name: str) -> Path:
        value = query("rev-parse", "--path-format=absolute", "--git-path", name)
        return Path(os.fsdecode(value.removesuffix(b"\n")))

    try:
        if not 0 < timeout <= 60:
            raise ProbeError("invalid_timeout")
        if not paths or len(set(paths)) != len(paths) or any(
            not name or name != PurePosixPath(name).as_posix() or name.startswith("/")
            or ".." in PurePosixPath(name).parts or name == "." for name in paths
        ):
            raise ProbeError("invalid_exact_file_scope")
        routing = sorted(name for name in env if env[name] and (
            name in ROUTING or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))))
        if routing:
            issue("git_environment_override", names=routing)
            return report
        quality = [name for name in ("SF_SKIP_COVERAGE", "SF_RANDOM_ORDER",
                                    "SF_PUBLIC_REGRESSION") if env.get(name, "0") != "0"]
        if env.get("SF_GUARD_PROBE"):
            quality.append("SF_GUARD_PROBE")
        if quality:
            issue("quality_environment_override", names=quality)
        if env.get("SF_RERUN_FAILED", "1") != "0":
            issue("automatic_failure_retry_enabled")
        if issues:
            return report
        actual_root = Path(os.fsdecode(query("rev-parse", "--show-toplevel").removesuffix(b"\n")))
        if actual_root.resolve() != root:
            raise ProbeError("run_from_repository_root")
        config = query("config", "--null", "--get-regexp",
                       r"^(filter\.|core\.(sparseCheckout|fsmonitor))", allowed=(0, 1))
        special = []
        for entry in config.split(b"\0"):
            if not entry:
                continue
            key, _, value = entry.partition(b"\n")
            if key.lower().startswith(b"filter.") or value.lower() not in (b"false", b"0", b""):
                special.append(os.fsdecode(key))
        if special:
            issue("special_checkout_configuration", names=special)
            return report
        if query("for-each-ref", "--format=%(refname)", "refs/replace"):
            issue("replacement_refs_require_separate_review")
            return report
        head = query("rev-parse", "--verify", "HEAD").decode().strip()
        branch = query("symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1)).decode().strip()
        report.update(head=head, branch=branch)
        if not branch:
            issue("detached_head")
        if expected_head is not None and head != expected_head:
            issue("head_changed")
        index = git_path("index")
        active = [name for name in OPERATIONS if (index.parent / name).exists()]
        if active:
            issue("operation_or_index_lock_present", names=active)
            return report
        entries = query("ls-files", "--stage", "-z").split(b"\0")
        tracked_paths = set()
        for entry in filter(None, entries):
            metadata, name = entry.split(b"\t", 1)
            mode, _, stage = metadata.split()
            tracked_paths.add(name)
            if stage != b"0":
                issue("unresolved_conflict", path=os.fsdecode(name))
            if mode == b"160000":
                issue("submodule_requires_separate_review", path=os.fsdecode(name))
            if name == b".venv" or name.startswith(b".venv/"):
                issue("tracked_runtime_environment", path=os.fsdecode(name))
        if any(item["code"] in ("unresolved_conflict", "submodule_requires_separate_review")
               for item in issues):
            return report
        hidden = [os.fsdecode(entry[2:]) for entry in
                  query("ls-files", "-v", "-z").split(b"\0") if entry and
                  (entry[:1].islower() or entry[:1] == b"S")]
        if hidden:
            issue("hidden_index_flags", paths=hidden)
            return report
        index_before = hashlib.sha256(index.read_bytes()).hexdigest()
        status_before = query("status", "--porcelain=v1", "--untracked-files=all", "-z")
        staged = [os.fsdecode(name) for name in query(
            "diff", "--no-ext-diff", "--no-textconv", "--cached", "--no-renames",
            "--name-only", "-z", "HEAD", "--").split(b"\0") if name]
        report["staged_paths"] = staged
        if not staged:
            issue("nothing_staged")
        if set(staged) - set(paths):
            issue("staged_outside_scope", paths=sorted(set(staged) - set(paths)))
        if set(paths) - set(staged):
            issue("scope_paths_not_staged", paths=sorted(set(paths) - set(staged)))
        if not quiet("diff", "--no-ext-diff", "--no-textconv", "--exit-code", "--name-only", "--"):
            issue("unstaged_tracked_changes")
        if run("diff", "--no-ext-diff", "--no-textconv", "--cached", "--check",
               allowed=(0, 2)).returncode != 0:
            issue("staged_whitespace_errors")
        untracked = [os.fsdecode(name) for name in query(
            "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if name]
        report["untracked_paths"] = untracked
        source = root / "git-hooks/pre-commit"
        actual = git_path("hooks/pre-commit")
        report["hook_path"] = str(actual)
        if actual.is_symlink() or not actual.is_file() or not os.access(actual, os.X_OK):
            issue("hook_missing_or_not_executable")
        elif b"git-hooks/pre-commit" not in tracked_paths:
            issue("hook_source_not_tracked")
        elif source.is_symlink() or not source.is_file() or source.read_bytes() != actual.read_bytes():
            issue("hook_source_mismatch")
        else:
            report["hook_sha256"] = hashlib.sha256(actual.read_bytes()).hexdigest()
        inputs = [name for name in untracked if (
            name.endswith(gate_stamp.BUILD_INPUT_SUFFIXES) or
            Path(name).name in gate_stamp.BUILD_INPUT_NAMES or
            Path(name).suffix.lower() in EXTRA_INPUT_SUFFIXES or
            Path(name).name.startswith(".env") or os.access(root / name, os.X_OK)
        ) and not ("hook_sha256" in report and root / name == actual.resolve())
            and not is_quality_evidence_record(name)]
        if inputs:
            issue("untracked_possible_execution_inputs", paths=inputs)
        if query("rev-parse", "HEAD").decode().strip() != head or (
            hashlib.sha256(index.read_bytes()).hexdigest() != index_before
            or query("status", "--porcelain=v1", "--untracked-files=all", "-z") != status_before
        ):
            issue("snapshot_changed_during_probe")
        if "hook_sha256" in report and hashlib.sha256(actual.read_bytes()).hexdigest() != report["hook_sha256"]:
            issue("hook_changed_during_probe")
        report["checks_passed"] = not issues
    except (ProbeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        issue("probe_error", reason=str(exc) if isinstance(exc, ProbeError) else type(exc).__name__)
    finally:
        report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return report


def is_quality_evidence_record(name: str) -> bool:
    """工程根目录下的质量记录与其 manifest（`quality-v<版本>[-后缀][.manifest].json`）。

    发布流程把它们放在工程根（记录里的产物路径不接受 `..`，而产物分在 dist/ 与 build/ 两处），
    不入库、不进公开导出。它们是证据数据，只由 `SF_QUALITY_RECORD` 或命令行显式指到的路径读取，
    没有任何关卡按名字去扫根目录，所以不是执行输入。别人遗留的旧版记录因此不再让预检红；
    只认根目录这一层的 `.json`，子目录里的同名文件、`.py` 之类仍按原规则拦。
    """
    return "/" not in name and name.startswith("quality-v") and name.endswith(".json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", action="append", required=True,
                        help="Exact changed file, repeated for every intended staged path")
    parser.add_argument("--expected-head", help="Full SHA captured when the scope was approved")
    parser.add_argument("--timeout", type=float, default=15, help="Whole probe timeout, up to 60 seconds")
    args = parser.parse_args(argv)
    report = inspect_repo(Path.cwd(), args.path, expected_head=args.expected_head, timeout=args.timeout)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
