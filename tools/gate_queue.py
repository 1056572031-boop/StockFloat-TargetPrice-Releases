#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""排队提交：别人的整档回归占着锁时，把等待时间用来做能提前做的事，轮到自己再提交。

为什么需要：提交钩子跑的 run_tests.sh 同一时间只许一轮完整回归。别的会话占着锁时，
临时手写的排队脚本反复踩同样的坑：只等几个进程号再加固定上限、把钩子的退出码当成锁冲突的退出码、
`pgrep -f` 匹配到自己、自己的两条排队在锁释放的同一秒互抢。本工具把这些收成一处：

  status                    看两把回归锁现在是谁持有（进程号、启动时刻、工作目录）。
  wait                      等两把锁都空出来；有界，超时退出码 3。
  commit --path P... -m F   先核对现场能不能提交（暂存区非空、没有改动等，立刻失败，不白排队），再做不占锁的事
                            （便宜检查 + 改动覆盖率预判），再排队，轮到自己并再次核对冻结的输入没变，
                            才调 release_wrapup 的 commit 走真实钩子。
  merge-commit -m F         给已经 `git merge --no-ff --no-commit` 出来、尚未提交的合并排队提交。必须同时给出
    --expected-head H       审查过的三样东西：合并前的 HEAD、被合入的提交、索引树；排队前后任何一样
    --source S              变了（含工作文件被改）都拒绝提交。不做改动覆盖率预判（合并没有“待提交文件”这个
    --expected-tree T       范围），其余与 commit 相同。合并提交之后把主线快进过去不归本工具管。

只盯锁文件本身，不用 pgrep；锁的“陈旧”判定与 run_tests.sh 同口径（进程不在，或进程号被复用）。
只在钩子打印“已有另一轮完整回归”时才当作输掉抢锁并重新排队，别的失败立刻停，绝不重试掩盖真红。
不跳过钩子、不强杀别人的回归、不删别人的锁；--apply 之前仍须完成提交流程 C0 的授权与范围确认。

退出码：0 完成（或预演）；1 提交失败；2 参数错误，或现场不满足提交条件（暂存区非空、没有待提交改动、
没有进行中的合并等，领票之前就拒绝）；3 等锁超时；4 排队前后冻结的输入变了；
5 排队前的检查或预判判红（不白占十分钟关卡）。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
wrapup = importlib.import_module("tools.release_wrapup")
predict = importlib.import_module("tools.change_predict")

LOST_MARKER = "已有另一轮完整回归"
POLL_SECONDS = 2.0
DEFAULT_WAIT = 3600.0
MAX_ATTEMPTS = 8
CHEAP_CHECKS = (("静态闸门", "tools/static_gate.py"), ("书写纪律", "tools/test_discipline_scan.py"),
                ("shell 中文标点", "tools/shell_cjk_scan.py"), ("公开文档", "tools/public_docs_check.py"))
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_TIMEOUT, EXIT_CHANGED, EXIT_PRECHECK = 0, 1, 2, 3, 4, 5
CHECK_TIMEOUT = 300.0


def say_now(text):
    """后台排队时 stdout 是文件或管道，普通 print 会整块缓冲到进程结束才出现，
    “正在等谁”恰好在最需要的时候看不到；每行立刻刷出。"""
    print(text, flush=True)


def lock_paths(root, tmpdir=None):
    """run_tests.sh 取的两把锁：按临时目录一把、按本棵工作树的 build/ 一把。"""
    base = Path(tmpdir or os.environ.get("TMPDIR") or "/tmp")
    return [("共享临时目录", base / "sf_run_tests.lock"),
            ("本棵工作树的 build/", Path(root) / "build" / ".run_tests.tree.lock")]


def process_start(pid):
    """进程启动时刻（与 run_tests.sh 同口径：LC_ALL=C 的 ps lstart，空白归一）；读不到返回空串。"""
    try:
        done = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True,
                              timeout=5, env=dict(os.environ, LC_ALL="C"), check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(done.stdout.split()) if done.returncode == 0 else ""


def holder_alive(record, start_of=process_start):
    """记录形如“进程号|启动时刻|目录”。进程不在、或进程号被别的进程复用，才算陈旧。

    启动时刻两边都读得到就严格比对；任何一边读不到就退回只看进程是否还在：
    读不到不等于已陈旧，宁可多等，不可把活着的持有者当陈旧放行。
    """
    pid_text, _, rest = record.partition("|")
    if not (pid_text.isascii() and pid_text.isdigit()) or int(pid_text) < 1:
        return False
    pid, recorded = int(pid_text), rest.partition("|")[0]
    now = start_of(pid)
    if recorded and now:
        return now == recorded
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    return True


def holders(root, tmpdir=None, start_of=process_start):
    """每把存在的锁一条：{label, record, alive}。锁不是符号链接、读不出记录时按“占着”处理。"""
    found = []
    for label, path in lock_paths(root, tmpdir):
        try:
            record = os.readlink(path)
        except FileNotFoundError:
            continue
        except OSError:
            found.append({"label": label, "record": "", "alive": True})
            continue
        found.append({"label": label, "record": record, "alive": holder_alive(record, start_of)})
    return found


def describe(entry):
    if not entry["record"]:
        return "%s：锁文件存在但读不出持有者" % entry["label"]
    pid, _, rest = entry["record"].partition("|")
    started, _, cwd = rest.partition("|")
    return "%s：进程 %s，启动于 %s，目录 %s" % (entry["label"], pid, started or "?", cwd or "?")


def ticket_dir(tmpdir=None):
    return Path(tmpdir or os.environ.get("TMPDIR") or "/tmp") / "sf_gate_queue"


def take_ticket(tmpdir=None, start_of=process_start):
    """领一张排队票：文件名按纳秒时间排序，内容是自己的“进程号|启动时刻”。先写临时名再改名，
    别人读到的要么没有这张票，要么是完整的票。"""
    directory = ticket_dir(tmpdir)
    directory.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    path = directory / ("%020d-%d" % (time.time_ns(), pid))
    scratch = directory / (".new-%d" % pid)
    scratch.write_text("%d|%s\n" % (pid, start_of(pid)), encoding="utf-8")
    os.replace(scratch, path)
    return path


def release_ticket(path):
    with contextlib.suppress(OSError):
        path.unlink()


def earlier_tickets(ticket, start_of=process_start):
    """排在自己前面、持有者还在的票。持有者已不在的陈旧票顺手清掉（只清这个工具自己的票据文件）。"""
    waiting = []
    for other in sorted(ticket.parent.iterdir()):
        if other.name.startswith(".") or other.name >= ticket.name:
            continue
        try:
            record = other.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if holder_alive(record, start_of):
            waiting.append(other.name)
        else:
            with contextlib.suppress(OSError):
                other.unlink()
    return waiting


def blockers(root, ticket=None, tmpdir=None, start_of=process_start):
    waiting = [describe(h) for h in holders(root, tmpdir, start_of) if h["alive"]]
    if ticket is not None:
        waiting += ["排在前面的提交 " + name for name in earlier_tickets(ticket, start_of)]
    return waiting


def wait_turn(root, timeout, ticket=None, tmpdir=None, start_of=process_start,
              clock=time.monotonic, sleep=time.sleep, say=say_now):
    """等到两把锁都空、且轮到自己；返回等了多少秒。超时抛 TimeoutError 并点名谁还占着。"""
    began, last = clock(), None
    while True:
        waiting = blockers(root, ticket, tmpdir, start_of)
        if not waiting:
            return round(clock() - began, 3)
        if clock() - began >= timeout:
            raise TimeoutError("等了 %.0fs 仍被占着：%s" % (timeout, "；".join(waiting)))
        if waiting != last:
            say("等待：" + "；".join(waiting))
            last = waiting
        sleep(POLL_SECONDS)

def digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return "missing"


def freeze(root, paths):
    """排队期间不许变的输入：HEAD 和每个待提交文件的内容哈希。"""
    return {"head": wrapup.git_out(Path(root), "rev-parse", "HEAD"),
            "files": {path: digest(Path(root) / path) for path in paths}}


def cheap_checks(root, run=subprocess.run):
    """不占回归锁的只读检查：钩子里同样会跑，先跑一遍，红了就不必排十分钟的队。"""
    results = []
    with tempfile.TemporaryDirectory(prefix="sf_gate_queue_log_") as scratch:
        env = dict(os.environ, SF_LOG_PATH=str(Path(scratch) / "debug.log"))
        for label, script in CHEAP_CHECKS:
            try:
                done = run([sys.executable, script], cwd=str(root), env=env, capture_output=True, text=True,
                           timeout=CHECK_TIMEOUT, check=False)
            except subprocess.TimeoutExpired:
                # 卡住不是“命令输错”：按没通过处理（退出码 124 是 timeout 的惯例），走预检失败的路。
                results.append({"name": label, "script": script, "returncode": 124,
                                "tail": "超过 %.0fs 仍未结束，按未通过处理" % CHECK_TIMEOUT})
                continue
            results.append({"name": label, "script": script, "returncode": done.returncode,
                            "tail": (done.stdout + done.stderr)[-400:]})
    return results


def prework(root, paths, base, do_predict=True, timeout=600.0, checks=cheap_checks):
    """排队前能做的全部事情。problems 非空表示已判红；notes 只是提示（例如无法预判）。"""
    problems, notes = [], []
    results = checks(root)
    for item in results:
        if item["returncode"] != 0:
            problems.append("%s未通过（退出码 %s）：%s" % (item["name"], item["returncode"], item["tail"]))
    status = "skipped"
    if do_predict:
        try:
            planned = predict.plan(root, base, only=set(paths))
            outcome = predict.run_plan(root, planned, timeout)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            outcome = {"status": "unknown", "log": "预判未能执行：%s" % exc}
        status = outcome["status"]
        if status in ("predicted_miss", "tests_failed"):
            problems.append("改动覆盖率预判：%s %s" % (status, outcome.get("missing") or outcome.get("log", "")))
        elif status not in ("pass", "not_applicable"):
            notes.append("改动覆盖率预判无法下结论（%s）：%s" % (status, outcome.get("log") or "见预判输出"))
    return {"checks": results, "predict_status": status, "problems": problems, "notes": notes}

class Stop(Exception):
    """流程在某一步有结论地停下，code 就是退出码。"""

    def __init__(self, code, message=""):
        super().__init__(message)
        self.code = code


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_atomic(path, value):
    """整体替换，读的人要么看到旧文件要么看到完整的新文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    scratch.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(scratch, path)


class Timeline:
    """按事件记录“墙上时刻”和“自开始的秒数”，用来把等锁与关卡本身的耗时分开说清。"""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.began = clock()
        self.events = []

    def mark(self, event, **extra):
        self.events.append(dict(extra, event=event, at=stamp(), t=round(self.clock() - self.began, 3)))


def save_timeline(path, timeline, frozen):
    if path:
        write_atomic(path, {"schema": 1, "generated_at": stamp(), "events": timeline.events, "frozen": frozen})


def default_deps():
    return {"commit": wrapup.commit, "ready": wrapup.check_ready, "prework": prework, "wait": wait_turn,
            "take_ticket": take_ticket, "clock": time.monotonic, "say": say_now,
            "merge": wrapup.merge_commit, "merge_ready": wrapup.check_merge_ready,
            "merge_state": wrapup.merge_state}


def require_ready(deps, timeline, timeline_path, frozen, check):
    """领票之前先问一遍“现在能不能提交”：不能就当场停下，不白排队再在轮到时才发现。"""
    try:
        check()
    except wrapup.WrapupError as exc:
        timeline.mark("not_ready", error=str(exc))
        save_timeline(timeline_path, timeline, frozen)
        raise Stop(EXIT_USAGE, "现场不满足提交条件，未排队：%s" % exc) from None


def run_prework(root, paths, deps, timeline, timeline_path, frozen, no_predict, predict_timeout):
    """排队前能做的全部事情；判红就在领票之前停下，不占十分钟关卡。"""
    deps["say"]("排队前检查开始：便宜检查 + 改动覆盖率预判（不占回归锁）")
    before = deps["prework"](root, paths, frozen["head"], do_predict=not no_predict, timeout=predict_timeout)
    timeline.mark("prework", predict=before["predict_status"], problems=len(before["problems"]))
    deps["say"]("排队前检查结束：预判 %s，问题 %d 处" % (before["predict_status"], len(before["problems"])))
    for note in before["notes"]:
        deps["say"]("提示：" + note)
    if before["problems"]:
        timeline.mark("prework_failed", problems=before["problems"])
        save_timeline(timeline_path, timeline, frozen)
        raise Stop(EXIT_PRECHECK, "排队前检查未过：\n" + "\n".join(before["problems"]))


def queue_turns(root, deps, timeline, wait_timeout, refreeze, frozen, act):
    """领票、等轮到、核对冻结的输入没变、执行 act；只有输给另一轮完整回归才重新排队。

    commit 与 merge-commit 共用这一段，等待预算、重试和票据归还只有一份实现。"""
    ticket = deps["take_ticket"]()
    timeline.mark("ticket", name=ticket.name)
    deps["say"]("已领票 %s，开始排队（总等待预算 %.0fs，重新排队不重置）" % (ticket.name, wait_timeout))
    spent = 0.0
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = max(0.0, wait_timeout - spent)
            deps["say"]("排队尝试 %d/%d，剩余等待预算 %.0fs" % (attempt, MAX_ATTEMPTS, remaining))
            try:
                waited = deps["wait"](root, remaining, ticket=ticket, say=deps["say"])
            except TimeoutError as exc:
                timeline.mark("timeout", error=str(exc), spent=round(spent, 3))
                raise Stop(EXIT_TIMEOUT, "%s（总等待预算 %.0fs，已用 %.0fs）" % (exc, wait_timeout, spent)) from None
            spent += waited
            timeline.mark("turn", attempt=attempt, waited=waited, spent=round(spent, 3))
            current = refreeze()
            if current != frozen:
                timeline.mark("inputs_changed", current=current)
                raise Stop(EXIT_CHANGED, "排队期间冻结的输入变了，已拒绝提交；重新审查、预检和排队")
            timeline.mark("commit_started", attempt=attempt)
            try:
                result = act()
            except wrapup.WrapupError as exc:
                text = str(exc)
                timeline.mark("commit_failed", attempt=attempt, lock_lost=LOST_MARKER in text)
                if LOST_MARKER not in text:
                    raise Stop(EXIT_FAILED, text) from None
                deps["say"]("刚好输给另一轮完整回归，保留输入不动，重新排队")
                continue
            timeline.mark("committed", commit=result.get("commit", ""))
            return result
        raise Stop(EXIT_TIMEOUT, "连续 %d 次在锁释放时输给别的关卡，未提交任何内容" % MAX_ATTEMPTS)
    finally:
        release_ticket(ticket)


def run_commit_queue(root, paths, message, author, expected_head, apply, wait_timeout, hook_timeout,
                     predict_timeout, no_predict, log, timeline_path=None, deps=None):
    """完整排队提交。正常 hook 的失败只对“锁冲突”重试，其他失败原样停止。"""
    deps = dict(default_deps(), **(deps or {}))
    root = Path(root).resolve()
    wrapup.check_paths(paths)
    if not message.is_file():
        raise Stop(EXIT_USAGE, "提交说明文件不存在：%s" % message)
    frozen = freeze(root, paths)
    timeline = Timeline(deps["clock"])
    timeline.mark("frozen", head=frozen["head"], paths=paths)
    if expected_head and expected_head != frozen["head"]:
        timeline.mark("expected_head_changed", expected=expected_head, actual=frozen["head"])
        save_timeline(timeline_path, timeline, frozen)
        raise Stop(EXIT_CHANGED, "当前 HEAD 与 --expected-head 不符，未开始排队")
    if not apply:
        # 预演只校验路径、说明和现场，不跑检查、不领票、不等锁，也不提交。
        deps["commit"](root, paths, message, author, expected_head or frozen["head"], False)
        timeline.mark("dry_run")
        save_timeline(timeline_path, timeline, frozen)
        return {"status": "dry_run", "timeline": timeline.events, "frozen": frozen}
    head = expected_head or frozen["head"]
    require_ready(deps, timeline, timeline_path, frozen,
                  lambda: deps["ready"](root, paths, message, author, expected_head))
    run_prework(root, paths, deps, timeline, timeline_path, frozen, no_predict, predict_timeout)
    try:
        result = queue_turns(root, deps, timeline, wait_timeout, lambda: freeze(root, paths), frozen,
                             lambda: deps["commit"](root, paths, message, author, head, True,
                                                    timeout=hook_timeout, log=log))
    finally:
        save_timeline(timeline_path, timeline, frozen)
    return {"status": "committed", "result": result, "timeline": timeline.events, "frozen": frozen}


def run_merge_queue(root, message, author, expected_head, source, expected_tree, apply, wait_timeout,
                    hook_timeout, log, timeline_path=None, deps=None):
    """给尚未提交的合并排队提交。HEAD、被合入的提交、索引树、工作文件任何一样变了都拒绝。"""
    deps = dict(default_deps(), **(deps or {}))
    root = Path(root).resolve()
    if not message.is_file():
        raise Stop(EXIT_USAGE, "提交说明文件不存在：%s" % message)
    frozen = deps["merge_state"](root)
    timeline = Timeline(deps["clock"])
    timeline.mark("frozen", head=frozen["head"], merge_heads=frozen["merge_heads"], tree=frozen["tree"])
    wanted = {"head": expected_head, "merge_heads": [source], "tree": expected_tree}
    drift = sorted(key for key, value in wanted.items() if frozen[key] != value)
    if drift:
        timeline.mark("expected_changed", drift=drift)
        save_timeline(timeline_path, timeline, frozen)
        raise Stop(EXIT_CHANGED, "现场与审查过的合并不符（%s），未开始排队" % "、".join(drift))
    if not apply:
        # 预演只核对现场并打印计划，不跑检查、不领票、不等锁，也不提交。
        deps["merge"](root, message, author, expected_head, source, expected_tree, False)
        timeline.mark("dry_run")
        save_timeline(timeline_path, timeline, frozen)
        return {"status": "dry_run", "timeline": timeline.events, "frozen": frozen}
    require_ready(deps, timeline, timeline_path, frozen,
                  lambda: deps["merge_ready"](root, message, author, expected_head, source, expected_tree))
    run_prework(root, [], deps, timeline, timeline_path, frozen, True, 0.0)
    try:
        result = queue_turns(root, deps, timeline, wait_timeout, lambda: deps["merge_state"](root), frozen,
                             lambda: deps["merge"](root, message, author, expected_head, source, expected_tree,
                                                   True, timeout=hook_timeout, log=log))
    finally:
        save_timeline(timeline_path, timeline, frozen)
    return {"status": "committed", "result": result, "timeline": timeline.events, "frozen": frozen}


def command_status(root, tmpdir=None):
    entries = holders(root, tmpdir)
    if not entries:
        print("== 完整回归锁空闲 ==")
        return EXIT_OK
    for entry in entries:
        print(("占用：" if entry["alive"] else "陈旧：") + describe(entry))
    return EXIT_TIMEOUT if any(entry["alive"] for entry in entries) else EXIT_OK


def command_wait(root, timeout, tmpdir=None):
    try:
        waited = wait_turn(root, timeout, tmpdir=tmpdir)
    except TimeoutError as exc:
        print("!! " + str(exc), file=sys.stderr)
        return EXIT_TIMEOUT
    print("== 完整回归锁已空闲（等待 %.1fs）==" % waited)
    return EXIT_OK

def self_test():
    """不碰本仓：验证活锁、陈旧锁、排队顺序和“只有锁冲突才重试”四个关键边界。"""
    with tempfile.TemporaryDirectory(prefix="sf_gate_queue_self_") as directory:
        root = Path(directory)
        alive = "123|started|/work"
        if not holder_alive(alive, start_of=lambda _pid: "started"):
            print("self-test failed: live lock was treated as stale", file=sys.stderr)
            return 1
        if holder_alive(alive, start_of=lambda _pid: "other"):
            print("self-test failed: reused pid was treated as live", file=sys.stderr)
            return 1
        if holder_alive("bad|started|/work", start_of=lambda _pid: "started"):
            print("self-test failed: malformed holder was accepted", file=sys.stderr)
            return 1
        ticket = take_ticket(directory, start_of=lambda _pid: "started")
        try:
            older = ticket.parent / ("%020d-older" % (int(ticket.name.split("-", 1)[0]) - 1))
            older.write_text("456|started|/other\n", encoding="utf-8")
            if not earlier_tickets(ticket, start_of=lambda _pid: "started"):
                print("self-test failed: earlier live ticket did not block", file=sys.stderr)
                return 1
            older.write_text("456|old|/other\n", encoding="utf-8")
            if earlier_tickets(ticket, start_of=lambda _pid: "started") or older.exists():
                print("self-test failed: stale ticket was not retired", file=sys.stderr)
                return 1
        finally:
            release_ticket(ticket)
    print("== gate queue self-test passed ==")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    status = sub.add_parser("status")
    status.add_argument("--tmpdir")
    wait = sub.add_parser("wait")
    wait.add_argument("--timeout", type=float, default=DEFAULT_WAIT)
    wait.add_argument("--tmpdir")
    commit = sub.add_parser("commit")
    commit.add_argument("--path", action="append", required=True)
    commit.add_argument("--expected-head")
    commit.add_argument("--predict-timeout", type=float, default=600.0)
    commit.add_argument("--no-predict", action="store_true")
    merge = sub.add_parser("merge-commit")
    merge.add_argument("--expected-head", required=True, help="合并前审查过的 HEAD 完整 SHA")
    merge.add_argument("--source", required=True, help="被合入的提交完整 SHA（MERGE_HEAD）")
    merge.add_argument("--expected-tree", required=True, help="审查过的索引树完整 SHA（git write-tree）")
    for queued in (commit, merge):
        queued.add_argument("-m", "--message", type=Path, required=True)
        queued.add_argument("--author")
        queued.add_argument("--apply", action="store_true")
        queued.add_argument("--wait-timeout", type=float, default=DEFAULT_WAIT,
                            help="总等待预算（秒）：多次重新排队合计不超过它，不含钩子本身的运行时间")
        queued.add_argument("--hook-timeout", type=float, default=1800.0)
        queued.add_argument("--log", type=Path)
        queued.add_argument("--timeline", type=Path)
    args = parser.parse_args(argv)
    if args.command == "self-test":
        return self_test()
    try:
        root = wrapup.repo_root(Path.cwd())
        if args.command == "status":
            return command_status(root, args.tmpdir)
        if args.command == "wait":
            if args.timeout <= 0:
                parser.error("--timeout 必须大于 0")
            return command_wait(root, args.timeout, args.tmpdir)
        for name in ("wait_timeout", "hook_timeout", "predict_timeout"):
            if getattr(args, name, 1) <= 0:
                parser.error("--%s 必须大于 0" % name.replace("_", "-"))
        log = args.log
        if log is None and args.apply:
            # 预演不会跑钩子，也就不需要日志文件；别在 /tmp 里留一个没人用的空文件。
            handle, name = tempfile.mkstemp(prefix="sf-gate-queue-", suffix=".log")
            os.close(handle)
            log = Path(name)
        if args.command == "merge-commit":
            result = run_merge_queue(root, args.message, args.author, args.expected_head, args.source,
                                     args.expected_tree, args.apply, args.wait_timeout, args.hook_timeout, log,
                                     args.timeline)
        else:
            result = run_commit_queue(root, args.path, args.message, args.author, args.expected_head, args.apply,
                                      args.wait_timeout, args.hook_timeout, args.predict_timeout, args.no_predict,
                                      log, args.timeline)
        print("== 排队提交 %s ==" % result["status"])
        if result["status"] == "committed":
            print("提交: " + result["result"]["commit"])
        return EXIT_OK
    except Stop as exc:
        print("!! " + str(exc), file=sys.stderr)
        return exc.code
    except (OSError, ValueError, wrapup.WrapupError, subprocess.SubprocessError) as exc:
        print("!! gate queue rejected: %s" % exc, file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
