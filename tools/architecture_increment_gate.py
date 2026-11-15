# -*- coding: utf-8 -*-
"""增量静态架构门禁：只检查相对基线新增的 Python 源码行。"""

from __future__ import annotations

import ast
import argparse
import os
import subprocess
from pathlib import Path


class Finding:
    __slots__ = ("rule", "path", "line", "message")

    def __init__(self, rule: str, path: str, line: int, message: str):
        self.rule = rule
        self.path = path
        self.line = line
        self.message = message

    def __eq__(self, other):
        return (isinstance(other, Finding)
                and (self.rule, self.path, self.line, self.message)
                == (other.rule, other.path, other.line, other.message))

    def __hash__(self):
        return hash((self.rule, self.path, self.line, self.message))


def _git_env():
    return {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}


class _GitViewError(RuntimeError):
    pass


def _git(root: Path, args: list[str], *, text: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            env=_git_env(),
            capture_output=True,
            text=text,
            check=False,
        )
    except OSError as exc:
        raise _GitViewError(f"git command failed: {exc}") from exc
    returncode = getattr(result, "returncode", 0)
    if returncode:
        stderr = getattr(result, "stderr", b"" if not text else "")
        if isinstance(stderr, bytes):
            detail = stderr.decode("utf-8", "replace").strip()
        elif isinstance(stderr, str):
            detail = stderr.strip()
        else:
            detail = str(stderr).strip()
        detail = detail or f"exit status {returncode}"
        raise _GitViewError(f"git {' '.join(args)} failed: {detail}")
    return result


def _decode_git_paths(raw: bytes | str) -> list[str]:
    if isinstance(raw, str):
        return [item for item in raw.split("\0") if item]
    if not isinstance(raw, bytes):
        raise _GitViewError("git returned an invalid path list")
    if not raw:
        return []
    if not raw.endswith(b"\0"):
        raise _GitViewError("git returned a non-NUL-terminated path list")
    return [item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item]


def _all_source_lines(source: str) -> set[int]:
    return set(range(1, len(source.splitlines()) + 1))


def _diff_args(base: str, root: Path) -> list[str]:
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", f"{base}^{{tree}}"],
            cwd=root,
            env=_git_env(),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise _GitViewError(f"git command failed: {exc}") from exc
    if probe.returncode == 0:
        return [base]
    if base == "HEAD":
        return ["--root"]
    detail = probe.stderr.strip() or f"cannot resolve base {base!r}"
    raise _GitViewError(detail)


def _added_lines_for_path(root: Path, base: str, relative: str) -> set[int]:
    result = _git(
        root,
        ["diff", "--cached", "--unified=0", "--no-renames", *_diff_args(base, root), "--", relative],
    )
    added: set[int] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("@@"):
            continue
        try:
            added_spec = line.split("+", 1)[1].split(" ", 1)[0]
            start_text, _, count_text = added_spec.partition(",")
            start = int(start_text)
            count = int(count_text or "1")
        except (IndexError, ValueError) as exc:
            raise _GitViewError(f"cannot parse git diff hunk for {relative!r}: {line}") from exc
        added.update(range(start, start + count))
    return added


def _index_source(root: Path, relative: str) -> str:
    result = _git(root, ["show", f":{relative}"], text=False)
    try:
        return result.stdout.decode("utf-8", "surrogateescape")
    except AttributeError as exc:
        raise _GitViewError(f"git returned invalid index content for {relative!r}") from exc


def _added_lines(source: str, added_lines: set[int] | None) -> set[int]:
    if added_lines is None:
        return set(range(1, source.count("\n") + 2))
    return set(added_lines)


def _line_is_new(node: ast.AST, added: set[int]) -> bool:
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", start)
    return any(line in added for line in range(start, end + 1))


def _is_diagnostic_function(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
        node.name == "diagnostics" or node.name.endswith("_diagnostics")
    )


def _call_name(node: ast.Call) -> str:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        parts: list[str] = []
        current: ast.expr = fn
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


NETWORK_ROOTS = {"urllib.request", "requests", "httpx"}
PROCESS_ROOTS = {"subprocess"}
THREAD_ROOTS = {"threading"}
FILE_METHODS = {"write_text", "write_bytes", "unlink", "replace", "rename"}


def _is_diagnostic_side_effect_call(node: ast.Call) -> bool:
    name = _call_name(node)
    if _is_diagnostic_side_effect(name):
        return True
    if not isinstance(node.func, ast.Attribute) or node.func.attr not in FILE_METHODS:
        return False
    value = node.func.value
    return isinstance(value, ast.Call) and _call_name(value) in {"Path", "pathlib.Path"}


def _is_diagnostic_side_effect(name: str) -> bool:
    if any(name.startswith(f"{root}.") for root in NETWORK_ROOTS):
        return True
    parts = name.split(".")
    if len(parts) == 2 and parts[0] in PROCESS_ROOTS:
        return parts[1] in {"run", "Popen", "call", "check_call", "check_output"}
    if len(parts) == 2 and parts[0] in THREAD_ROOTS:
        return parts[1] in {"Thread", "Timer"}
    return name in {"open", "os.open", "os.replace"}


def _host_private_attribute(node: ast.Attribute) -> bool:
    if not node.attr.startswith("_"):
        return False
    value: ast.expr = node.value
    attributes: list[str] = []
    while isinstance(value, ast.Attribute):
        attributes.append(value.attr)
        value = value.value
    if not isinstance(value, ast.Name):
        return False
    if value.id in {"host", "window", "owner"}:
        return True
    return value.id == "self" and bool({"host", "window", "owner"} & set(attributes))


def scan_source(path: str, source: str, added_lines: set[int] | None = None) -> list[Finding]:
    """Scan one source string; omitted added_lines means every line is new.

    The explicit line set is the seam that keeps historical debt out of the gate.
    """
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [Finding("syntax.error", path, exc.lineno or 1, str(exc))]
    added = _added_lines(source, added_lines)
    findings: list[Finding] = []
    window = Path(path).name == "stock_float_window.py"

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.diagnostic_depth = 0

        def visit_FunctionDef(self, node):
            diagnostic = _is_diagnostic_function(node)
            if diagnostic:
                self.diagnostic_depth += 1
            self._visit_node(node)
            self.generic_visit(node)
            if diagnostic:
                self.diagnostic_depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Attribute(self, node):
            self._visit_node(node)
            self.generic_visit(node)

        def visit_Call(self, node):
            self._visit_node(node)
            self.generic_visit(node)

        def _visit_node(self, node):
            if not _line_is_new(node, added):
                return
            if isinstance(node, ast.Attribute) and _host_private_attribute(node):
                findings.append(Finding(
                    "integration.host_private_state", path, node.lineno,
                    "新增代码直接读取宿主私有状态，请通过公开控制器接口接入",
                ))
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if window and (name.startswith(("urllib.", "requests.", "httpx."))
                               or name in {"urlopen", "http_get", "safe_urlopen"}):
                    findings.append(Finding(
                        "window.external_io", path, node.lineno,
                        "主窗口新增网络入口，请移入功能控制器或 Adapter",
                    ))
                if window and (name.startswith(("sqlite3.",)) or name in {"connect", "save_config", "save_alerts"}):
                    findings.append(Finding(
                        "window.external_io", path, node.lineno,
                        "主窗口新增存储/配置写入入口，请移入功能控制器或存储 Adapter",
                    ))
                if self.diagnostic_depth and (
                    _is_diagnostic_side_effect(name)
                    or _is_diagnostic_side_effect_call(node)
                ):
                    findings.append(Finding(
                        "diagnostics.side_effect", path, node.lineno,
                        "诊断函数新增写盘、网络或线程副作用",
                    ))

    Visitor().visit(tree)
    unique = {(item.rule, item.path, item.line, item.message): item for item in findings}
    return list(unique.values())


def _untracked_python_files(root: Path) -> dict[str, set[int]]:
    result = _git(root, ["ls-files", "--others", "--exclude-standard", "-z"], text=False)
    changes: dict[str, set[int]] = {}
    for relative in _decode_git_paths(result.stdout):
        if not relative.endswith(".py"):
            continue
        path = root / relative
        if path.is_file():
            try:
                source = path.read_text(encoding="utf-8", errors="surrogateescape")
            except (OSError, UnicodeError) as exc:
                raise _GitViewError(
                    f"cannot read untracked Python file {relative!r}: {exc}"
                ) from exc
            changes[relative] = _all_source_lines(source)
    return changes


def _cached_python_paths(root: Path, base: str) -> list[str]:
    result = _git(
        root,
        ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR", *_diff_args(base, root), "--", "*.py"],
        text=False,
    )
    return [relative for relative in _decode_git_paths(result.stdout) if relative.endswith(".py")]


def changed_python_files(root: Path, base: str = "HEAD") -> dict[str, set[int]]:
    """Return added index line numbers, plus genuinely untracked working-tree files."""
    changes: dict[str, set[int]] = {}
    for relative in _cached_python_paths(root, base):
        source = _index_source(root, relative)
        added = _added_lines_for_path(root, base, relative)
        if added:
            changes[relative] = added
    changes.update(_untracked_python_files(root))
    return changes


def scan_repo(root: Path, base: str = "HEAD") -> list[Finding]:
    try:
        changes = changed_python_files(root, base)
        indexed = set(_cached_python_paths(root, base))
    except _GitViewError as exc:
        return [Finding("git.error", str(root), 1, str(exc))]

    findings: list[Finding] = []
    for relative, added in changes.items():
        path = root / relative
        try:
            source = _index_source(root, relative) if relative in indexed else path.read_text(
                encoding="utf-8", errors="surrogateescape"
            )
        except (_GitViewError, OSError, UnicodeError) as exc:
            findings.append(Finding("git.error", relative, 1, f"cannot read scan view: {exc}"))
            continue
        findings.extend(scan_source(relative, source, added))
    return findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="HEAD")
    args = parser.parse_args(argv)
    findings = scan_repo(args.root, args.base)
    for item in findings:
        print(f"{item.path}:{item.line}: {item.rule}: {item.message}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
