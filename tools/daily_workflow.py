"""Read-only topic navigation for focused tests; never executes suggested commands."""
from __future__ import annotations

import argparse
import ast
import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOPICS = {
    "diagnostics": ("Diagnostic export, privacy and startup observations", (
        "tests.test_diagnostics_payload", "tests.test_diagnostics_controller",
        "tests.test_startup_health", "tests.test_feedback_protocol",
    )),
    "data": ("Quotes, minute dates, routing, network and stock search", (
        "tests.test_minute_recovery_audit", "tests.test_quote_runtime",
        "tests.test_quote_request_lifecycle", "tests.test_window_poll_message_routing",
        "tests.test_quality_http", "tests.test_coverage_universe",
    )),
    "storage": ("Configuration, recovery and asynchronous snapshots", (
        "tests.test_coverage_targets_config", "tests.test_migrate",
        "tests.test_quality_storage_tasks", "tests.test_minute_recovery_audit",
    )),
    "targets": ("Target evaluation, persistence and received price extremes", (
        "tests.test_targets", "tests.test_target_lifecycle",
        "tests.test_target_commit_retry", "tests.test_quality_quote_delivery",
    )),
    "ui": ("Offscreen rendering, geometry and popup behavior", (
        "tests.test_render", "tests.test_window_geometry",
        "tests.test_popup_materials", "tests.test_target_panel",
    )),
    "lifecycle": ("Worker ownership, switching, refresh and shutdown", (
        "tests.test_window_lifecycle", "tests.test_quit_lifecycle",
        "tests.test_stock_transition", "tests.test_manual_refresh",
    )),
    "performance": ("Benchmark contracts, power controls and resource observation", (
        "tests.test_performance_audit", "tests.test_resource_soak",
        "tests.test_power_v1410",
    )),
    "infrastructure": ("Test discipline, static constraints and navigation", (
        "tests.test_arch_constraints", "tests.test_code_governance",
        "tests.test_daily_workflow", "tests.test_coverage_runner_and_gate_failures",
        "tests.test_ui_dialog_guard",
    )),
}


def modules_for(topics: list[str]) -> list[str]:
    if not topics:
        raise ValueError("no topics selected; empty navigation is not valid")
    modules = []
    for topic in topics:
        if not isinstance(topic, str) or not re.fullmatch(r"[a-z][a-z_]*", topic):
            raise ValueError("invalid topic identifier")
        if topic not in TOPICS:
            raise ValueError(f"unknown topic: {topic}")
        entry = TOPICS[topic]
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise ValueError(f"{topic}: invalid topic entry")
        description, targets = entry
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"{topic}: missing description")
        if not isinstance(targets, (tuple, list)) or not targets:
            raise ValueError(f"{topic}: no test modules")
        seen = set()
        for module in targets:
            if not isinstance(module, str) or not re.fullmatch(r"tests\.test_[A-Za-z0-9_]+", module):
                raise ValueError(f"{topic}: invalid test module")
            if module in seen:
                raise ValueError(f"{topic}: duplicate module {module}")
            seen.add(module)
            modules.append(module)
    return list(dict.fromkeys(modules))


def check_navigation(root: Path, topics: list[str] | None = None) -> list[str]:
    errors = []
    try:
        modules = modules_for(list(TOPICS) if topics is None else topics)
    except ValueError as exc:
        return [str(exc)]
    for module in modules:
        path = root.joinpath(*module.split(".")).with_suffix(".py")
        try:
            if not path.resolve().is_relative_to(root.resolve()):
                errors.append(f"{module}: target outside repository")
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            errors.append(f"{module}: {type(exc).__name__}")
            continue
        if not any(isinstance(node, ast.ClassDef) and node.bases and any(
                isinstance(method, ast.FunctionDef) and method.name.startswith("test_")
                for method in node.body) for node in tree.body):
            errors.append(f"{module}: no module-level test class with synchronous test methods")
    return errors


def test_command(modules: list[str], root: Path | None = None) -> str:
    if not modules:
        raise ValueError("no test modules; refusing to suggest full discovery")
    root = (ROOT if root is None else root).resolve()
    command = ["env", "QT_QPA_PLATFORM=offscreen", "SF_RERUN_FAILED=0",
               str(root / ".venv/bin/python"), str(root / "tools/test_runner.py")]
    for module in modules:
        command.extend(("--module", module))
    return shlex.join(command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--list", action="store_true", help="List available test topics")
    actions.add_argument("--topic", action="append", choices=tuple(TOPICS),
                         help="Suggest a test command; repeat to combine topics")
    actions.add_argument("--check", action="store_true", help="Check navigation targets only")
    args = parser.parse_args(argv)
    if args.list:
        try:
            modules_for(list(TOPICS))
        except ValueError as exc:
            print(f"Navigation error: {exc}")
            return 1
        for name, (description, modules) in TOPICS.items():
            print(f"{name}: {description} ({len(modules)} modules)")
        return 0
    errors = check_navigation(ROOT, args.topic)
    if errors:
        for error in errors:
            print(f"Navigation error: {error}")
        return 1
    if args.check:
        print(f"Navigation valid: {len(TOPICS)} topics, "
              f"{len(modules_for(list(TOPICS)))} distinct test modules.")
        print("Static class shape only; no tests executed, imports checked or coverage certified.")
        return 0
    print("Suggestion only. Review callers, isolation and impact; expand tests as needed.")
    print("Absolute paths; guarded runner, no automatic retry. Not the full gate; no gate stamp.")
    print(test_command(modules_for(args.topic)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
