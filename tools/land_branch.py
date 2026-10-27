#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分支落地编排：把“已提交、已审查”的自建任务分支并回开发主线，一条命令串起来。

背景：docs/BRANCH_MERGE_WORKFLOW.md 的 M0-M5 每次都要手工重复同一串动作——在独立树里做双亲合并、
排队提交合并、核对主线没动且没有撞上主树的脏文件、快进、推送、清理树和分支。2026-10-26 一晚上手工做了
四遍，每遍中间要等一轮完整关卡。本工具把这串动作收成一处，判据与手工做法逐条一致：

  1. 任务分支必须已提交、任务树干净，并且 --reviewed-sha 必须等于分支末端（审批绑定固定提交）；
  2. 主线必须还是任务分支的祖先（主线动过就拒绝，回去重新集成、审查、过关卡）；
  3. 在新建的独立合并树里 `git merge --no-ff --no-commit`，合并树必须与已审查的分支树逐字节同树；
  4. 合并提交走 `gate_queue.py merge-commit`（完整关卡，不跳过 hook）；
  5. 快进前再核对一次主线 SHA、再核对合并内容与主树脏文件无重叠；
  6. 只有明确给了 --push 才推送（走 release_wrapup push，只快进、按实际推送地址读回）；
     只有明确给了 --retire 才收掉合并树和任务树（走 release_wrapup retire，`-d`，不用 `-D`）。

默认只预演：做只读核对、打印将要做的事，不建树、不建分支、不提交。每一步都按 git 里的事实判断“做过没有”，
所以中断后重跑同一条命令即可接着做。本工具不替代审查，也不判断范围与授权：`--apply` 前仍须按
AGENTS.md 的授权口径确认这是自建分支、审查已闭环；合并不自动授权打包、安装或发布。

退出码：0 完成（或预演）；1 拒绝或失败；2 参数错误。
"""
from __future__ import annotations

import argparse
import importlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
gate_stamp = importlib.import_module("tools.gate_stamp")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "feat/target-price"
GATE_WAIT_SEC = 7200
GATE_HOOK_SEC = 3600
RUN_MARGIN_SEC = 900
MIN_SHA = 12

Runner = Callable[[list, Path, Path], int]


class LandError(RuntimeError):
    """拒绝：消息写明缺什么或哪里不安全。"""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise LandError(message)


def git(root: Path, *args: str, ok: tuple = (0,)) -> tuple:
    done = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=root, env=gate_stamp.git_env(),
                          capture_output=True, text=True, timeout=120)
    require(done.returncode in ok, "git %s 失败：%s" % (" ".join(args), (done.stderr or done.stdout).strip()))
    return done.returncode, done.stdout.rstrip("\n")


def rev(root: Path, ref: str) -> str:
    return git(root, "rev-parse", ref)[1]


def is_ancestor(root: Path, older: str, newer: str) -> bool:
    return git(root, "merge-base", "--is-ancestor", older, newer, ok=(0, 1))[0] == 0


def dirty_paths(root: Path) -> list:
    """有改动的路径（含未跟踪），按 git 的 NUL 格式解析，保留空格和非 ASCII。"""
    raw = subprocess.run(["git", "-c", "core.quotePath=false", "status", "--porcelain=v1", "-z"],
                         cwd=root, env=gate_stamp.git_env(), capture_output=True, check=True).stdout
    parts = raw.decode("utf-8", "surrogateescape").split("\0")
    paths = []
    i = 0
    while i < len(parts) - 1:
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if status[0] in "RC" or status[1] in "RC":
            # -z 格式下重命名/复制是“新名、旧名”两项；两个路径都被动过，都算脏。
            paths.append(parts[i])
            i += 1
    return paths


def git_quiet(root: Path, *args: str) -> tuple:
    """像 git() 但不因非零退出码抛错，返回 (退出码, 去掉首尾空白的输出)。"""
    done = subprocess.run(["git", *args], cwd=root, env=gate_stamp.git_env(), capture_output=True, text=True,
                          timeout=120)
    return done.returncode, done.stdout.strip()


def resumable_merge(merge_tree: Path, merge_branch: str, base_sha: str, tip: str) -> bool:
    """上次关卡没过时，合并树里还留着“已暂存、未提交”的这次合并：可以原地接着提交。

    判据全部问 git：这棵树在合并分支上、HEAD 还是合并前的主线、MERGE_HEAD 正是这次要合入的任务末端。
    （linked worktree 的 MERGE_HEAD 不在 .git/ 下，不能靠拼路径读。）合并被 abort 过、树被换过、
    分支前进过，任何一条对不上都不续，交给调用方拒绝。"""
    if not merge_tree.exists():
        return False
    code, branch = git_quiet(merge_tree, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0 or branch != merge_branch:
        return False
    head = git_quiet(merge_tree, "rev-parse", "HEAD")[1]
    code, pending = git_quiet(merge_tree, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    return head == base_sha and code == 0 and pending == tip


TAIL_LINES = 12
TAIL_LINE_CHARS = 300


def say(text: str) -> None:
    """进度行：每行立刻冲刷。

    后台运行并把输出重定向到文件时 Python 用块缓冲，一轮完整关卡要几分钟，期间文件一直是空的，
    人无从判断是在等关卡还是卡死了。"""
    print(text, flush=True)


def log_size(log: Path) -> int:
    try:
        return log.stat().st_size
    except OSError:
        return 0


def log_tail(log: Path, start: int = 0) -> str:
    """日志里从 start 字节起（也就是这一次命令写的部分）的最后几行；读不到返回空串。

    日志是追加写的，同一份会装着多次运行；不从 start 起读，失败信息里就会混进上一次的旧原因。
    单行截断：拒绝原因里可能带整条进程命令行，长到看不清。"""
    try:
        with log.open("rb") as stream:
            stream.seek(start)
            text = stream.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join("  | " + line[:TAIL_LINE_CHARS] for line in lines[-TAIL_LINES:])


def run_step(runner: Runner, argv: list, cwd: Path, log: Path) -> tuple:
    """跑一步并返回 (退出码, 失败时这一步写进日志的尾部)。成功时尾部为空。"""
    start = log_size(log)
    rc = runner(argv, cwd, log)
    return rc, (log_tail(log, start) if rc != 0 else "")


def with_detail(message: str, detail: str) -> str:
    return message + ("\n" + detail if detail else "")


def run_logged(argv: list, cwd: Path, log: Path) -> int:
    """跑一条长命令，输出追加进日志；起不来或超时按 127 记，不抛。"""
    release_resume = importlib.import_module("tools.release_resume")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        stream.write(("$ " + " ".join(argv) + "\n").encode())
        try:
            return subprocess.run(argv, cwd=cwd, env=release_resume.clean_env(), stdout=stream,
                                  stderr=subprocess.STDOUT,
                                  timeout=GATE_WAIT_SEC + GATE_HOOK_SEC + RUN_MARGIN_SEC).returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            stream.write(("无法执行：%r\n" % (exc,)).encode())
            return 127


def completed_merge(repo: Path, merge_branch: str, base_sha: str, tip: str) -> str:
    """合并分支已经存在时：必须正是“主线 + 任务末端”的合并提交且同树，返回它；不是就拒绝。没有分支返回空串。"""
    code, sha = git(repo, "rev-parse", "--verify", "-q", "refs/heads/" + merge_branch, ok=(0, 1))
    if code != 0:
        return ""
    if sha == base_sha:
        return ""                              # 合并分支还停在合并前的主线：合并没提交完，交给 stage_merge 判断能不能续
    parents = git(repo, "log", "-1", "--format=%P", sha)[1].split()
    same_tree = rev(repo, sha + "^{tree}") == rev(repo, tip + "^{tree}")
    require(parents == [base_sha, tip] and same_tree,
            "分支 %s 已存在但不是这次落地的合并提交（父提交或树对不上）：人工检查后移走再来" % merge_branch)
    return sha


def stage_merge(repo: Path, merge_tree: Path, merge_branch: str, base_sha: str, tip: str) -> str:
    """建独立合并树并暂存双亲合并，返回暂存出的树；必须与已审查的分支树同树。"""
    if not resumable_merge(merge_tree, merge_branch, base_sha, tip):
        require(not merge_tree.exists(), "合并树 %s 已存在但没有完成的合并提交，也不是可续的暂存合并：人工检查后移走再来" % merge_tree)
        require(git(repo, "rev-parse", "--verify", "-q", "refs/heads/" + merge_branch, ok=(0, 1))[0] == 1,
                "合并分支 %s 已存在而合并树不在：人工检查后移走再来" % merge_branch)
        git(repo, "worktree", "add", "-b", merge_branch, str(merge_tree), base_sha)
        venv = repo / ".venv"
        if venv.exists():
            (merge_tree / ".venv").symlink_to(venv)
        git(merge_tree, "merge", "--no-ff", "--no-commit", tip)
    staged = git(merge_tree, "write-tree")[1]
    require(staged == rev(repo, tip + "^{tree}"), "暂存出的合并树与已审查的分支树不一致：不提交")
    return staged


def fast_forward(repo: Path, base: str, base_sha: str, merge_sha: str) -> None:
    require(git(repo, "rev-parse", "--abbrev-ref", "HEAD")[1] == base,
            "主树当前已切到别的分支：不快进，回去核对现场")
    require(rev(repo, base) == base_sha, "主线 %s 在合并期间前进了：不快进，回去重新集成、审查、过关卡" % base)
    changed = set(filter(None, subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--name-only", "-z", base_sha, merge_sha], cwd=repo,
        env=gate_stamp.git_env(), capture_output=True, text=True, check=True, timeout=120).stdout.split("\0")))
    overlap = sorted(changed & set(dirty_paths(repo)))
    require(not overlap, "合并内容与主树里别人的未提交文件重叠：%s" % "、".join(overlap))
    git(repo, "merge", "--ff-only", merge_sha)
    require(rev(repo, base) == merge_sha, "快进之后主线不等于合并提交")


def land(repo: Path, base: str, branch: str, tree: Path, reviewed: str, merge_message: Path, *, apply: bool,
         push: bool, retire: bool, evidence: Path, author: str = "", runner: Runner | None = None,
         python: str = "", out: Callable[[str], None] = say) -> str:
    runner = runner or run_logged
    require(git(repo, "rev-parse", "--abbrev-ref", "HEAD")[1] == base, "主树当前不在 %s 上" % base)
    require(git(tree, "rev-parse", "--abbrev-ref", "HEAD")[1] == branch, "任务树 %s 当前不在 %s 上" % (tree, branch))
    require(not dirty_paths(tree), "任务树还有未提交的改动：先提交（走完整关卡）并审查，再来落地")
    tip = rev(tree, "HEAD")
    require(len(reviewed) >= MIN_SHA and tip.startswith(reviewed),
            "--reviewed-sha 与分支 %s 的末端 %s 不符：审批绑定固定提交，末端变了就要重新审查" % (branch, tip[:12]))
    base_sha = rev(repo, base)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")
    merge_branch = "merge/land-" + name
    merge_tree = repo.parent / (".sf-merge-land-" + name)
    python = python or str(repo / ".venv" / "bin" / "python3")
    merged = is_ancestor(repo, tip, base_sha)
    merge_sha = ""
    if merged:
        out("分支 %s 已经在 %s 里，不再合并" % (branch, base))
    else:
        require(is_ancestor(repo, base_sha, tip),
                "主线 %s 在分支之后前进了：先把主线并进任务分支、重新审查并重新过关卡，再来落地" % base)
        merge_sha = completed_merge(repo, merge_branch, base_sha, tip)
        out("合并 %s（%s）进 %s（%s）%s" % (branch, tip[:12], base, base_sha[:12],
                                         "：已有完成的合并提交 " + merge_sha[:12] if merge_sha else ""))
    out("计划：%s%s%s" % ("合并+快进" if not merged else "跳过合并", "，推送" if push else "，不推送",
                       "，收掉合并树与任务树" if retire else "，保留树与分支"))
    if not apply:
        out("预演（没有 --apply）：什么都没改")
        return merge_sha
    if not merged:
        require(merge_message.is_file(), "合并说明文件不存在：" + str(merge_message))
        if not merge_sha:
            staged = stage_merge(repo, merge_tree, merge_branch, base_sha, tip)
            who = author or git(repo, "log", "-1", "--format=%an <%ae>", tip)[1]
            out("提交合并并过完整关卡（通常要几分钟；日志 %s）" % (evidence / "land-gate.log"))
            rc, detail = run_step(
                runner, [python, "tools/gate_queue.py", "merge-commit", "--expected-head", base_sha, "--source", tip,
                         "--expected-tree", staged, "--author", who, "--wait-timeout", str(GATE_WAIT_SEC),
                         "--hook-timeout", str(GATE_HOOK_SEC), "--log", str(evidence / "land-hook.log"),
                         "--timeline", str(evidence / "land-timeline.json"), "--apply", "-m", str(merge_message)],
                merge_tree, evidence / "land-gate.log")
            require(rc == 0, with_detail("合并提交没有完成（退出码 %d）；合并树留在 %s，日志在 %s"
                                         % (rc, merge_tree, evidence), detail))
            require(rev(repo, merge_branch) != base_sha, "关卡返回成功但没有生成合并提交（合并分支没有前进）")
            merge_sha = completed_merge(repo, merge_branch, base_sha, tip)
        fast_forward(repo, base, base_sha, merge_sha)
        out("主线 %s 已快进到 %s" % (base, merge_sha[:12]))
    if push:
        out("推送 %s 并按实际推送地址读回" % base)
        rc, detail = run_step(runner, [python, "tools/release_wrapup.py", "push", "--branch", base, "--apply"],
                              repo, evidence / "land-push.log")
        require(rc == 0, with_detail("推送没有完成（退出码 %d），日志在 %s" % (rc, evidence), detail))
        out("已推送并读回")
    if retire:
        for leftover in (merge_tree, tree):
            if leftover.exists():
                out("收树 %s" % leftover)
                rc, detail = run_step(
                    runner, [python, "tools/release_wrapup.py", "retire", "--tree", str(leftover), "--base", base,
                             "--apply"], repo, evidence / "land-retire.log")
                require(rc == 0, with_detail("收树没有完成（退出码 %d）：%s" % (rc, leftover), detail))
        out("合并树与任务树已收掉（树进废纸篓，分支用 -d 删）")
    return merge_sha


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--reviewed-sha", required=True)
    parser.add_argument("--merge-message", type=Path, required=True)
    parser.add_argument("--author", default="")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--retire", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", args.branch).strip("-")
    # 路径一律转成绝对路径再往下传：相对路径若以 - 开头，交给 git 和子工具时会被当成选项。
    repo, tree, message = args.repo.absolute(), args.tree.absolute(), args.merge_message.absolute()
    evidence = (args.evidence or repo / "build" / "land" / name).absolute()
    try:
        land(repo, args.base, args.branch, tree, args.reviewed_sha, message, apply=args.apply,
             push=args.push, retire=args.retire, evidence=evidence, author=args.author)
    except (LandError, OSError, subprocess.SubprocessError) as exc:
        print("land_branch: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
