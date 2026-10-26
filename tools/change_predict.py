#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提交前预判：只跑与改动相关的用例，提前告知“改动行覆盖率”那一关会不会红。

为什么需要：提交钩子要把整套用例跑完（约十分钟）才轮到最后一关“改动行覆盖率”，
漏两行的改动要等十分钟才知道。本工具先做一件便宜的事：
  1. 取相对 --base（默认 HEAD，即本次提交自己的改动）改动过的文件与行；
  2. 按“哪些用例引用了这个文件”选出相关用例，相关用例太多时再按改动的符号收窄，
     改过的用例文件本身一律纳入；
  3. 在私有临时目录里只跑这些用例，覆盖率数据写到临时文件；
  4. 用与正式关卡同一把尺子（change_coverage_gate.inspect）看改动行有没有被跑到。

结论只是预警，不是放行：
  · 子集里没漏行不等于整档一定过，钩子照常完整跑；
  · 子集里漏行不等于整档一定红（别的用例也许会跑到），所以只报“预计会漏”，由人决定；
  · 选不出相关用例的文件、选出的用例太多的改动，如实报“无法预判”，不猜。
不占整档回归锁、不写关卡戳、不碰覆盖率回执；用户数据由运行器自带的隔离与红线保护。

退出码：0 未发现问题（含无可预判内容）；1 子集用例失败或预计漏行；
2 参数错误；3 无法完整预判（有文件没对应用例、选得太多、数据读不出、超时）。
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import importlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import coverage

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
change_gate = importlib.import_module("tools.change_coverage_gate")

MAX_MODULES = 30
NARROW_ABOVE = 8
DEFAULT_TIMEOUT = 900.0
TAIL_CHARS = 1500
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TEST_MODULE = re.compile(r"test_[A-Za-z0-9_]+")
INSTALLED_APP_NAMES = ("Skyrocket", "StockFloatTargetPrice")
STRIPPED_ENV = ("SF_GUARD_PROBE", "SF_LOCK_PROBE", "SF_LOCK_PROBE_DIR", "SF_TEST_DURATIONS_OUT",
                "SF_RANDOM_ORDER", "SF_RANDOM_SEED", "SF_COVER_MIN", "SF_SKIP_COVERAGE")
EXIT = {"pass": 0, "not_applicable": 0, "planned": 0, "predicted_miss": 1, "tests_failed": 1,
        "partial": 3, "too_broad": 3, "unknown": 3}


def referenced_names(tree):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            found.update((node.module or "").split("."))
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if len(text) <= 200 and not any(ch.isspace() for ch in text):
                found.update(WORD.findall(text))
    return found


def build_index(root):
    index, unreadable = {}, []
    for path in sorted((root / "tests").glob("test_*.py")):
        if not TEST_MODULE.fullmatch(path.stem):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            index[path.stem] = {"refs": referenced_names(ast.parse(text)), "text": text}
        except (OSError, SyntaxError, ValueError):
            unreadable.append(path.name)
    return index, unreadable


def changed_symbols(source, lines):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    spans = [(node.lineno, node.end_lineno or node.lineno, node.name) for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    names = set()
    for line in lines:
        inside = [span for span in spans if span[0] <= line <= span[1]]
        if inside:
            names.add(max(inside)[2])
    return names


def modules_for(index, stem):
    return sorted(name for name, entry in index.items() if stem in entry["refs"])


def narrow(index, related, symbols):
    if len(related) <= NARROW_ABOVE or not symbols:
        return related
    pattern = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(name) for name in sorted(symbols)))
    return [name for name in related if pattern.search(index[name]["text"])]


def plan(root, base, only=None):
    """only：只看这些路径（并行脏文件很多时，别把别人的改动算进来）；None 表示全部改动。"""
    changed, _deleted, refs = change_gate._git_changes(root, base)
    changed = {rel: lines for rel, lines in changed.items() if only is None or rel in only}
    index, unreadable = build_index(root)
    if unreadable:
        return {"status": "unknown", "reason": "无法解析测试文件：" + ", ".join(unreadable),
                "base": refs[0], "head": refs[1], "modules": [], "files": []}
    modules, files = set(), []
    for rel, lines in sorted(changed.items()):
        keep, reason = change_gate._selected(rel)
        entry = {"path": rel, "changed_lines": sorted(lines), "reason": reason}
        if rel.startswith("tests/") and Path(rel).stem in index:
            entry.update(status="planned", gated=False, symbols=[], related=[],
                         selected=[Path(rel).stem], selection_mode="changed test")
            modules.add(Path(rel).stem)
            files.append(entry)
            continue
        source = root / rel
        if not keep or not source.is_file():
            entry.update(status="excluded", reason=reason if not keep else "文件已删除，没有可预判的改动行")
            files.append(entry)
            continue
        stem = Path(rel).stem
        symbols = changed_symbols(source.read_text(encoding="utf-8"), lines)
        related = modules_for(index, stem)
        chosen = narrow(index, related, symbols)
        mode = "module reference" if chosen == related else "symbol reference"
        if not chosen:
            entry.update(status="unmapped", gated=True, symbols=sorted(symbols), related=[])
        else:
            entry.update(status="planned", gated=True, symbols=sorted(symbols), related=related,
                         selected=chosen, selection_mode=mode)
            modules.update(chosen)
        files.append(entry)
    active = [item for item in files if item["status"] != "excluded"]
    if not active:
        return finish(refs, "not_applicable", "没有待预判的可执行 Python 改动", modules, files)
    if len(modules) > MAX_MODULES:
        return finish(refs, "too_broad", "相关用例模块 %d 个，超过预判上限 %d；直接跑完整关卡"
                      % (len(modules), MAX_MODULES), modules, files)
    if not modules:
        names = ", ".join(item["path"] for item in active)
        return finish(refs, "partial", "这些 Python 改动没有可证明的相关用例：" + names, modules, files)
    return finish(refs, "planned", "", modules, files)


def finish(refs, status, reason, modules, files):
    return {"status": status, "reason": reason, "base": refs[0], "head": refs[1],
            "modules": sorted(modules), "files": files}


NICE = "/usr/bin/nice"


def spawn(cmd, cwd, env, sink, timeout):
    """独立会话里跑，超时连孙进程一起收掉（只杀直接子进程会留下孤儿）；超时抛 TimeoutError。"""
    process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=sink, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise TimeoutError("子集预判超过 %.0fs 超时" % timeout) from None


def installed_app_running():
    """正式版是否在运行，返回命中的进程名，没有则返回空串。

    只按进程名精确匹配（pgrep -x），不扫命令行：run_tests.sh 的守卫还会扫命令行里的安装路径，
    那会被别人命令行里恰好含这串路径的进程误触发；这里宁可少拦一种也不去误拦。
    查不了（pgrep 起不来）按“在运行”处理：不能确认没有正式版，就不碰用户数据目录。
    """
    for name in INSTALLED_APP_NAMES:
        try:
            found = subprocess.run(["pgrep", "-x", name], capture_output=True, check=False)
        except OSError:
            return "（无法确认）"
        if found.returncode == 0:
            return name
    return ""


def run_plan(root, planned, timeout, nice=True):
    if planned["status"] != "planned":
        return {"status": planned["status"], "returncode": None, "elapsed_seconds": 0.0, "log": ""}
    running = installed_app_running()
    if running:
        # 子集用例和正式版共用用户数据目录：正式版自己的保存会被运行器的红线误判成测试污染，
        # 更糟的是测试窗口的写入会覆盖用户真实配置。与 run_tests.sh 的守卫同口径：不跑，也不强关。
        return {"status": "unknown", "returncode": None, "elapsed_seconds": 0.0,
                "log": "正式版 %s 正在运行，子集用例会与它共用用户数据目录，已拒绝运行；"
                       "请先退出它，或让正式关卡给结论" % running}
    with tempfile.TemporaryDirectory(prefix="sf_change_predict_", dir="/tmp") as scratch:
        scratch_dir = Path(scratch)
        data_file, log_file = scratch_dir / ".coverage", scratch_dir / "selected.log"
        env = {key: value for key, value in os.environ.items() if key not in STRIPPED_ENV}
        env.update(QT_QPA_PLATFORM="offscreen", SF_RERUN_FAILED="0", SF_TEST_TIMEOUT="60",
                   COVERAGE_FILE=str(data_file), PYTHONPATH=str(root))
        cmd = [sys.executable, "-m", "coverage", "run", "--rcfile=.coveragerc", "tools/test_runner.py"]
        for module in planned["modules"]:
            cmd += ["--module", module]
        if nice and os.path.isfile(NICE):
            cmd = [NICE, "-n", "10"] + cmd
        began = time.monotonic()
        try:
            with log_file.open("w", encoding="utf-8") as sink:
                code = spawn(cmd, str(root), env, sink, timeout)
        except (TimeoutError, OSError) as exc:
            return {"status": "unknown", "returncode": None,
                    "elapsed_seconds": round(time.monotonic() - began, 3), "log": str(exc)}
        elapsed = round(time.monotonic() - began, 3)
        full = log_file.read_text(encoding="utf-8", errors="replace")
        if code != 0:
            return {"status": "tests_failed", "returncode": code, "elapsed_seconds": elapsed,
                    "log": failure_summary(full)}
        return judge(root, planned, data_file, elapsed, full[-TAIL_CHARS:])


def failure_summary(log):
    """子集失败时先列出红的用例名（FAIL:/ERROR: 行），再附日志末尾；只有末尾会看不到是谁红了。"""
    names = [line for line in log.splitlines() if line.startswith(("FAIL: ", "ERROR: "))]
    head = "\n".join(names[:20]) + "\n---\n" if names else ""
    return head + log[-TAIL_CHARS:]


def judge(root, planned, data_file, elapsed, log):
    """子集通过之后读覆盖率：只统计受闸门管辖、且已选到用例的文件。"""
    base = {"returncode": 0, "elapsed_seconds": elapsed, "log": log}
    if not data_file.is_file():
        return dict(base, status="unknown", log="子集通过但没有 coverage 数据")
    try:
        report = change_gate.inspect(root, planned["base"], data_file)
    except (ValueError, OSError, coverage.CoverageException) as exc:
        return dict(base, status="unknown", log="coverage 读取失败：%s" % exc)
    scope = {item["path"] for item in planned["files"] if item.get("gated") and item["status"] == "planned"}
    mine = [item for item in report["files"] if item["path"] in scope]
    missing = {item["path"]: item["missing"] for item in mine if item.get("missing")}
    executable = sum(len(item.get("executable_changed") or []) for item in mine)
    unmapped = [item["path"] for item in planned["files"] if item["status"] == "unmapped"]
    if missing:
        status = "predicted_miss"
    elif unmapped:
        status = "partial"
    elif executable == 0:
        status = "not_applicable"
    else:
        status = "pass"
    return dict(base, status=status, missing=missing, executable=executable, unmapped=unmapped)


def print_result(planned, result):
    print("== 提交前改动覆盖率预判（只预警，不替代完整关卡）==")
    print("基线: %s" % planned.get("base", ""))
    modules = planned.get("modules", [])
    print("相关用例模块: %d 个%s" % (len(modules), ("｜" + ", ".join(modules)) if modules else ""))
    if planned.get("reason"):
        print("!! " + planned["reason"])
    if result.get("elapsed_seconds"):
        print("子集耗时: %.1fs" % result["elapsed_seconds"])
    status = result.get("status", planned.get("status"))
    if status == "pass":
        print("预判未发现改动行漏测（%d 行）" % result["executable"])
        print("!! 这不是完整关卡通过，提交钩子仍会跑全量")
    elif status == "predicted_miss":
        print("!! 预判发现可能漏测的改动行：")
        for path, lines in result.get("missing", {}).items():
            print("   %s: %s" % (path, ",".join(map(str, lines))))
        print("   先补相关测试，再提交；不要用 --no-verify 绕过")
    elif status == "tests_failed":
        print("!! 相关用例子集本身未通过，未对覆盖率下结论")
        if result.get("log"):
            print(result["log"])
    elif status == "too_broad":
        print("== 相关范围过大，预判不执行；直接让正式全量关卡给结论 ==")
    elif status == "partial":
        print("!! 只能部分预判，未映射的改动不算通过")
    elif status == "not_applicable":
        print("没有可预判的可执行 Python 改动；不算覆盖率通过。")
    elif status == "planned":
        print("只列出了相关用例，未运行；不算覆盖率通过。")
    else:
        print("!! 预判无法给出完整结论：" + (result.get("log") or planned.get("reason", "未知原因")))


def outcome(planned, result):
    status = result.get("status", planned["status"])
    return {"schema": 1, "status": status, "exit_code": EXIT.get(status, 3),
            "base": planned.get("base"), "head": planned.get("head"),
            "modules": planned.get("modules", []), "reason": planned.get("reason", ""),
            "files": planned.get("files", []), "elapsed_seconds": result.get("elapsed_seconds", 0.0),
            "missing": result.get("missing", {}), "unmapped": result.get("unmapped", []),
            "executable": result.get("executable", 0)}


def write_json(path, value, root):
    root = Path(root).resolve()
    target = Path(path).expanduser()
    target = (target if target.is_absolute() else root / target).resolve()
    if not target.is_relative_to(root):
        raise ValueError("report path escapes worktree")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def self_test():
    """不跑产品用例：只在临时仓库验证选取和“无法预判不许报绿”的判定。"""
    # 钩子环境带着指向真仓库的 GIT_*，临时仓库里的 git 必须洗掉，否则会写到真仓库上。
    env = change_gate.git_env()
    with tempfile.TemporaryDirectory(prefix="change_predict_self_") as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q"], cwd=root, env=env, check=True)
        (root / "tests").mkdir()
        (root / "sf_probe.py").write_text("def changed():\n    return 1\n", encoding="utf-8")
        (root / "tests" / "test_probe.py").write_text(
            "from sf_probe import changed\n\ndef test_changed():\n    assert changed() == 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, env=env, check=True)
        subprocess.run(["git", "-c", "user.name=gate", "-c", "user.email=gate@example.invalid",
                        "commit", "-qm", "base"], cwd=root, env=env, check=True)
        (root / "sf_probe.py").write_text("def changed():\n    return 2\n", encoding="utf-8")
        good = plan(root, "HEAD")
        if good["status"] != "planned" or good["modules"] != ["test_probe"]:
            print("self-test failed: imported product module was not mapped to its test", file=sys.stderr)
            return 1
        (root / "sf_orphan.py").write_text("VALUE = 1\n", encoding="utf-8")
        bad = plan(root, "HEAD")
        if bad["status"] != "planned" or bad["modules"] != ["test_probe"]:
            print("self-test failed: unmapped file must not discard mapped files", file=sys.stderr)
            return 1
        synthetic = {"status": "partial", "elapsed_seconds": 0.0, "log": ""}
        if outcome(bad, synthetic)["exit_code"] != 3:
            print("self-test failed: partial prediction was allowed", file=sys.stderr)
            return 1
    print("== change predict self-test passed ==")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="HEAD", help="git revision to compare against (default: HEAD)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--list", action="store_true", help="only list selected test modules; do not run them")
    parser.add_argument("--json-out", help="write the structured result below the current worktree")
    parser.add_argument("--no-nice", action="store_true", help="do not lower the subset run's CPU priority")
    parser.add_argument("--path", action="append", help="only consider this changed file; repeat for several")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        if (args.base != "HEAD" or args.timeout != DEFAULT_TIMEOUT or args.list or args.json_out
                or args.no_nice or args.path):
            parser.error("--self-test 不接受其他动作参数")
        return self_test()
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    root = Path.cwd().resolve()
    try:
        planned = plan(root, args.base, only=set(args.path) if args.path else None)
        result = ({"status": planned["status"], "elapsed_seconds": 0.0, "log": ""} if args.list
                  else run_plan(root, planned, args.timeout, nice=not args.no_nice))
        print_result(planned, result)
        value = outcome(planned, result)
        if args.json_out:
            write_json(args.json_out, value, root)
        return value["exit_code"]
    except (OSError, ValueError, subprocess.SubprocessError, coverage.CoverageException) as exc:
        print("!! change predict rejected: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
