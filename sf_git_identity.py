"""Strict Git checkout identity checks used by release tooling."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def git_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


class GitIdentityError(ValueError):
    """The checkout cannot provide an unambiguous branch/commit identity."""


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, env=git_env(),
        capture_output=True, text=True, check=False,
    )


def validate_reserved_head_ref(root: Path) -> None:
    reserved = _run(root, "show-ref", "--verify", "--quiet", "refs/heads/HEAD")
    if reserved.returncode == 0:
        raise GitIdentityError("仓库存在保留引用 refs/heads/HEAD，造成 HEAD 歧义，拒绝发布")


def checked_out_branch(root: Path) -> str:
    """Return the checked-out branch, rejecting detached and ambiguous refs."""
    result = _run(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        detail = result.stderr.strip() or "HEAD 不是明确的分支"
        raise GitIdentityError("无法确定当前分支：%s" % detail)
    if branch == "HEAD" or branch.startswith("HEAD/"):
        raise GitIdentityError("当前分支名使用保留名称 HEAD，拒绝发布")
    validate_reserved_head_ref(root)
    if any(part in {"", ".", ".."} for part in branch.split("/")):
        raise GitIdentityError("当前分支名无效：%s" % branch)
    return branch


def checked_out_commit(root: Path) -> str:
    """Return the commit behind the checked-out branch without resolving bare HEAD."""
    branch = checked_out_branch(root)
    result = _run(root, "rev-parse", "--verify", "refs/heads/%s^{commit}" % branch)
    commit = result.stdout.strip()
    if result.returncode != 0 or not commit:
        detail = result.stderr.strip() or "分支提交不可读"
        raise GitIdentityError("无法确定当前提交：%s" % detail)
    return commit


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("branch", "commit", "check"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        branch = checked_out_branch(args.root)
        commit = checked_out_commit(args.root)
    except GitIdentityError as exc:
        print("git_identity: %s" % exc, file=__import__("sys").stderr)
        return 2
    if args.command in ("branch", "check"):
        print(branch)
    if args.command in ("commit", "check"):
        print(commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
