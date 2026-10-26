#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""车道总指挥：把整套回归拆成几条车道并行跑，再合并成与串行等价的一个结论。

只做调度与合并，不改判定：每条车道跑的仍是 tools/test_runner.py --module ...
（单用例超时、失败重跑、用户数据红线、弹窗拦截全部沿用）。覆盖率每条车道各写一份数据文件，
跑完用 coverage combine 合成默认的 .coverage，后面的回执、改动行覆盖、分档门禁照旧读它。

判红规则（任何一条成立整轮就是红，没有“没结果当通过”）：
  · 任一车道退出码非 0，或没写出结果文件，或结果里的总数与分给它的用例数对不上；
  · 模块被重复分配；零个模块；车道超时；
  · 要覆盖率却合并失败，或合并后没有数据。
退出码：0 全绿；1 有失败或结果不完整；2 用户数据被改（红线，优先级最高）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parent.parent
LANES_DIR = ROOT / "build" / "lanes"
WEIGHTS_FILE = Path(__file__).with_name("lane_weights.json")
DEFAULT_SEC_PER_CASE = 0.14      # 没有权重记录的新模块：按全套平均每条用例估
MAX_LANES = 8                    # 实测 5 条以上不再更快；每条车道约 0.4~0.8GB，敲错成 64 会把机器拖垮
LANE_TIMEOUT_SEC = 2400.0
POLL_SEC = 1.0                   # 轮询车道进程的间隔
PROGRESS_SEC = 60.0              # 多久打一行进度；只是让长时间静默的日志有心跳
TERM_GRACE_SEC = 15.0            # 温和结束之后等多久才强杀

# 这几个模块会真拉起带生产标识（com.skyrocket.app / 可执行名 Skyrocket）的替身程序，
# 按进程名与包路径判断“程序是否在运行”，同一时刻只能有一个在跑，所以必须同一条车道里串行。
SERIAL_CORE = {
    "test_update_helper": "起替身并走真实安装助手",
    "test_install_path_routes": "真挂卷、真装、起替身",
    "test_installer_script_branches": "起替身并检查“旧版是否在运行”",
}

def load_weights(path: Path = WEIGHTS_FILE) -> dict[str, float]:
    """模块 -> 估计秒数。读不到就返回空表，全部退回“按用例数估”，不影响正确性只影响均衡。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in data.get("seconds", {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def discover(python: str) -> dict[str, int]:
    """让运行器只发现、不运行，返回 {模块: 用例数}。发现失败直接抛，不降级。"""
    LANES_DIR.mkdir(parents=True, exist_ok=True)
    out = LANES_DIR / "modules.json"
    out.unlink(missing_ok=True)
    proc = subprocess.run([python, str(ROOT / "tools" / "test_runner.py"), "--list-json", str(out)],
                          cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0 or not out.is_file():
        raise RuntimeError("模块发现失败 rc=%s：%s" % (proc.returncode, (proc.stdout + proc.stderr)[-300:]))
    modules = json.loads(out.read_text(encoding="utf-8")).get("modules", {})
    if not modules or sum(modules.values()) <= 0:
        raise RuntimeError("模块发现结果为空：拒绝把零用例当通过")
    return {str(k): int(v) for k, v in modules.items()}


def plan_lanes(modules: dict[str, int], lanes: int, weights: dict[str, float]) -> list[list[str]]:
    """把模块分到若干车道。串行核心先全部放进 0 号车道，其余按“最长优先、放到最空的车道”均衡。

    车道内按模块名字母序排，和串行时 discover 的先后一致，尽量不改变模块之间的相邻关系。
    """
    lanes = max(1, min(int(lanes), len(modules)))

    def weight(name: str) -> float:
        return weights.get(name, modules[name] * DEFAULT_SEC_PER_CASE)

    buckets: list[list[str]] = [[] for _ in range(lanes)]
    loads = [0.0] * lanes
    for name in sorted(n for n in SERIAL_CORE if n in modules):
        buckets[0].append(name)
        loads[0] += weight(name)
    rest = [n for n in modules if n not in SERIAL_CORE]
    for name in sorted(rest, key=lambda n: (-weight(n), n)):
        i = loads.index(min(loads))
        buckets[i].append(name)
        loads[i] += weight(name)
    return [sorted(b) for b in buckets if b]


def lane_files(index: int) -> dict[str, Path]:
    stem = LANES_DIR / ("lane%d" % index)
    return {"log": stem.with_suffix(".log"), "result": stem.with_suffix(".result.json"),
            "durations": stem.with_suffix(".durations.json"), "cov": stem.with_suffix(".cov")}


def lane_command(python: str, index: int, modules: list[str], coverage: bool) -> list[str]:
    cmd = [python]
    if coverage:
        cmd += ["-m", "coverage", "run", "--data-file=%s" % lane_files(index)["cov"]]
    cmd.append(str(ROOT / "tools" / "test_runner.py"))
    for name in modules:
        cmd += ["--module", name]
    return cmd

def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def done_count(log: Path) -> int:
    """车道日志里已出结论的用例条数，只用于进度提示，不参与判定。"""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return len(re.findall(r" \.\.\. (?:ok|skipped\b.*|FAIL|ERROR)\s*$", text, flags=re.M))


def run_lanes(python: str, plan: list[list[str]], coverage: bool, timeout: float,
              extra_env: dict[str, str]) -> tuple[dict[int, dict], float, bool]:
    """同时起所有车道，等到全部结束。收到中断/终止信号时先温和结束子进程，再强杀。"""
    LANES_DIR.mkdir(parents=True, exist_ok=True)
    procs: dict[int, subprocess.Popen] = {}
    handles: dict[int, TextIO] = {}
    stop: list[int] = []
    for index in range(len(plan)):
        for path in lane_files(index).values():
            path.unlink(missing_ok=True)

    def on_signal(signum, _frame):
        stop.append(signum)

    old = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    started = time.monotonic()
    ended: dict[int, float] = {}
    timed_out = False
    try:
        for index, modules in enumerate(plan):
            files = lane_files(index)
            env = dict(os.environ, **extra_env)
            env["SF_TEST_DURATIONS_OUT"] = str(files["durations"])
            env["SF_LANE_RESULT_OUT"] = str(files["result"])
            env.setdefault("SF_GC_AT_TEST_BOUNDARY", "1")   # 见 test_runner.gc_at_test_boundary 的说明
            handles[index] = open(files["log"], "w", encoding="utf-8")
            procs[index] = subprocess.Popen(lane_command(python, index, modules, coverage), cwd=ROOT,
                                            env=env, stdout=handles[index], stderr=subprocess.STDOUT)
        last_note = started
        while any(p.poll() is None for p in procs.values()):
            now = time.monotonic()
            for index, proc in procs.items():
                if proc.poll() is not None and index not in ended:
                    ended[index] = now - started
            if stop:
                break
            if now - started > timeout:
                timed_out = True
                break
            if now - last_note >= PROGRESS_SEC:
                last_note = now
                print("   … 已 %ds，各车道已完成用例：%s" % (
                    now - started, " ".join("L%d=%d" % (i, done_count(lane_files(i)["log"])) for i in procs)),
                    flush=True)
            time.sleep(POLL_SEC)
        for index, proc in procs.items():
            if proc.poll() is None:
                proc.terminate()
        deadline = time.monotonic() + TERM_GRACE_SEC
        for proc in procs.values():
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        end = time.monotonic()
        for index in procs:
            ended.setdefault(index, end - started)
    finally:
        # 正常路径上这里一个都不会命中（上面已经等完所有车道）。只有启动到一半抛异常
        # （例如后面某条车道起不来）时，前面已经起好的车道还在跑：不回收的话，总指挥退出、
        # 回归锁释放，它们却继续在共享的临时文件上跑，会污染下一轮关卡。
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        for handle in handles.values():
            handle.close()
        for sig, handler in old.items():
            signal.signal(sig, handler)
    if stop:
        raise SystemExit(128 + stop[0])
    lanes = {i: {"rc": p.returncode, "seconds": ended.get(i, 0.0)} for i, p in procs.items()}
    return lanes, time.monotonic() - started, timed_out

def judge(plan: list[list[str]], modules: dict[str, int], lanes: dict[int, dict],
          timed_out: bool) -> tuple[int, list[str], dict]:
    """把各车道的结构化结果合成一个结论。返回 (退出码, 问题清单, 汇总)。

    “没有结果”永远是红：车道被杀、崩溃、超时、结果文件写失败，都不能当作通过。
    """
    problems: list[str] = []
    flat = [m for lane in plan for m in lane]
    if not flat:
        problems.append("没有任何模块被分配：拒绝把零用例当通过")
    dup = sorted({m for m in flat if flat.count(m) > 1})
    if dup:
        problems.append("模块被重复分配：%s" % ", ".join(dup[:5]))
    lost = sorted(set(modules) - set(flat))
    if lost:
        problems.append("有模块没分到任何车道：%s" % ", ".join(lost[:5]))
    if timed_out:
        problems.append("车道整体超时，已强制结束")
    total = failures = flaky = ui = 0
    dirty: list[str] = []
    failed_ids: list[str] = []
    for index, lane in enumerate(plan):
        info = lanes.get(index, {})
        result = read_json(lane_files(index)["result"])
        expected = sum(modules.get(m, 0) for m in lane)
        if result is None:
            problems.append("L%d 没有结果文件（rc=%s）：被杀、崩溃或超时" % (index, info.get("rc")))
            continue
        if result.get("total") != expected:
            problems.append("L%d 用例数对不上：实际 %s，应为 %d" % (index, result.get("total"), expected))
        if result.get("rc") != info.get("rc"):
            problems.append("L%d 退出码与结果文件不一致（%s vs %s）" % (index, info.get("rc"), result.get("rc")))
        total += int(result.get("total") or 0)
        failures += len(result.get("failures") or [])
        flaky += len(result.get("flaky") or [])
        ui += int(result.get("ui_violations") or 0)
        dirty += list(result.get("dirty") or [])
        failed_ids += list(result.get("failures") or [])
        if info.get("rc") not in (0, 1, 2):
            problems.append("L%d 异常退出（rc=%s）" % (index, info.get("rc")))
    if dirty:
        code = 2
    elif problems or failures or ui or any(l.get("rc") not in (0,) for l in lanes.values()):
        code = 1
    else:
        code = 0
    return code, problems, {"total": total, "failures": failures, "flaky": flaky, "ui": ui,
                            "dirty": dirty, "failed_ids": failed_ids}


def combine_coverage(python: str, plan_len: int, target: Path | None = None,
                     runner=subprocess.run) -> tuple[bool, str]:
    """把各车道的覆盖率数据合成默认的 .coverage。失败或合并后没数据都算失败。

    runner 只给自检注入假的执行函数用（造“命令成功却没产出文件”这种真实环境里很难构造的形态）。
    """
    target = target or Path(os.environ.get("COVERAGE_FILE") or (ROOT / ".coverage"))
    parts = [str(lane_files(i)["cov"]) for i in range(plan_len)]
    missing = [p for p in parts if not Path(p).is_file()]
    if missing:
        return False, "缺少车道覆盖率数据：%s" % ", ".join(Path(p).name for p in missing)
    proc = runner([python, "-m", "coverage", "combine", "--keep", "--data-file=%s" % target] + parts,
                  cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        return False, "coverage combine 失败 rc=%s：%s" % (proc.returncode, (proc.stdout + proc.stderr)[-200:])
    if not target.is_file() or target.stat().st_size == 0:
        return False, "合并后的覆盖率数据为空：%s" % target
    return True, str(target)


def merge_durations(plan_len: int, wall: float, out: str, extra: dict) -> str:
    """各车道的按模块耗时直接相加（车道之间模块不重叠），写成与串行同一个结构。失败只提示不改结论。"""
    seconds: dict[str, float] = {}
    cases: dict[str, int] = {}
    for i in range(plan_len):
        data = read_json(lane_files(i)["durations"]) or {}
        for row in data.get("modules", []):
            seconds[row["module"]] = seconds.get(row["module"], 0.0) + float(row["seconds"])
            cases[row["module"]] = cases.get(row["module"], 0) + int(row["cases"])
    report = {"schema": 1, "under_coverage": bool(extra.get("coverage")), "wall_seconds": round(wall, 3),
              "case_seconds": round(sum(seconds.values()), 3), "lanes": extra,
              "modules": [{"module": m, "seconds": round(s, 3), "cases": cases[m]}
                          for m, s in sorted(seconds.items(), key=lambda kv: (-kv[1], kv[0]))]}
    try:
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(".durations-lanes.tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, target)
        return ""
    except OSError as exc:
        return "%s: %s" % (type(exc).__name__, exc)

def _judge_checks(tmp: Path) -> dict[str, bool]:
    """judge 的判红规则逐条喂合成结果：该红的必须红、该绿的必须绿、数据红线必须压过失败。"""
    mods = {"a": 10, "b": 5, "c": 7}
    plan = [["a", "b"], ["c"]]

    def put(i: int, **kw) -> None:
        base = {"schema": 1, "rc": 0, "total": [15, 7][i], "failures": [], "flaky": [],
                "ui_violations": 0, "dirty": []}
        base.update(kw)
        lane_files(i)["result"].write_text(json.dumps(base), encoding="utf-8")

    def code(plan_=plan, r0=None, r1=None, lanes=None, timed_out=False) -> int:
        for i in (0, 1):
            lane_files(i)["result"].unlink(missing_ok=True)
        if r0 is not None:
            put(0, **r0)
        if r1 is not None:
            put(1, **r1)
        got = lanes if lanes is not None else {0: {"rc": (r0 or {}).get("rc", 0)},
                                               1: {"rc": (r1 or {}).get("rc", 0)}}
        return judge(plan_, mods, got, timed_out)[0]

    return {
        "全绿应为 0": code(r0={}, r1={}) == 0,
        "某车道失败应为 1": code(r0={}, r1={"rc": 1, "failures": ["x"]}) == 1,
        "用户数据被改应为 2，且压过失败": code(r0={"rc": 2, "dirty": ["config.json"]},
                                             r1={"rc": 1, "failures": ["x"]}) == 2,
        "缺结果文件应为 1": code(r1={}) == 1,
        "用例数对不上应为 1": code(r0={}, r1={"total": 6}) == 1,
        # 进程退出 0、结果文件却写 rc=1 且没有失败项：别的判据都看不出，只有一致性检查能抓到。
        "退出码与结果文件不一致应为 1": code(r0={}, r1={"rc": 1}, lanes={0: {"rc": 0}, 1: {"rc": 0}}) == 1,
        "模块没分到车道应为 1": code(plan_=[["a", "b"], []], r0={}, r1={}) == 1,
        "模块重复分配应为 1": code(plan_=[["a", "b"], ["c", "a"]], r0={}, r1={"total": 17}) == 1,
        "整体超时应为 1": code(r0={}, r1={}, timed_out=True) == 1,
        "空方案应为 1": judge([], {}, {}, False)[0] == 1,
        "弹窗违规应为 1": code(r0={}, r1={"ui_violations": 1}) == 1,
        "只有 flaky 应为 0": code(r0={}, r1={"flaky": ["z"]}) == 0,
    }

def _plan_checks(tmp: Path) -> dict[str, bool]:
    """分车道的不变量：每个模块恰好一次、串行核心同车道、车道数有上下限、权重真起作用。"""
    mods = {"m%02d" % i: 3 + i for i in range(12)}
    mods.update({name: 5 for name in SERIAL_CORE})
    plan3 = plan_lanes(mods, 3, {})
    flat = [m for lane in plan3 for m in lane]
    # 四个用例数相同的模块、两条车道：没有权重只会平分 2+2；有权重时那个重模块必须独占一条。
    heavy = plan_lanes({"x": 1, "y": 1, "z": 1, "w": 1}, 2, {"x": 100.0, "y": 1.0, "z": 1.0, "w": 1.0})
    alone = sorted(len(lane) for lane in heavy) == [1, 3] and ["x"] in heavy
    weights = tmp / "w.json"
    weights.write_text("{not json", encoding="utf-8")
    bad = load_weights(weights)
    weights.write_text(json.dumps({"seconds": {"a": 1.5}}), encoding="utf-8")
    good = load_weights(weights)
    log = tmp / "lane.log"
    log.write_text("t1 ... ok\nt2 ... FAIL\nt3 ... skipped 'why'\nnoise\nt4 ... ERROR\n", encoding="utf-8")
    return {
        "每个模块恰好出现一次": sorted(flat) == sorted(mods),
        "串行核心全在同一条车道": len({i for i, lane in enumerate(plan3) for m in lane if m in SERIAL_CORE}) == 1,
        "车道数不超过模块数": len(plan_lanes({"a": 1, "b": 1}, 9, {})) == 2,
        "车道数不低于 1": len(plan_lanes({"a": 1}, 0, {})) == 1,
        "单车道把所有模块放一起": len(plan_lanes(mods, 1, {})) == 1,
        "权重让最重的模块独占一条车道": alone,
        "权重文件坏了退回空表": bad == {},
        "权重文件正常能读": good == {"a": 1.5},
        "进度计数认 ok/FAIL/skipped/ERROR": done_count(log) == 4,
        "读不到日志时进度为 0": done_count(tmp / "missing.log") == 0,
        "自动车道数在 1 到 4 之间": 1 <= auto_lanes() <= 4,
        "读不到的文件日志尾部有说明": "读不到" in tail(tmp / "missing.log"),
        "日志尾部取最后几行": tail(log, 1).strip().endswith("ERROR"),
    }


def _merge_checks(tmp: Path) -> dict[str, bool]:
    """耗时合并按模块相加；覆盖率合并缺数据/命令失败/合并后为空都必须报失败。"""
    for i, rows in enumerate(([{"module": "a", "seconds": 1.0, "cases": 2}],
                              [{"module": "b", "seconds": 3.0, "cases": 4},
                               {"module": "a", "seconds": 0.5, "cases": 1}])):
        lane_files(i)["durations"].write_text(json.dumps({"modules": rows}), encoding="utf-8")
    out = tmp / "merged.json"
    blocker = tmp / "blocker"
    blocker.write_text("a regular file where a directory is needed", encoding="utf-8")
    bad_write = merge_durations(2, 5.0, str(blocker / "x.json"), {"coverage": False})
    problem = merge_durations(2, 5.0, str(out), {"coverage": True})
    merged = read_json(out) or {}
    by_module = {r["module"]: (r["seconds"], r["cases"]) for r in merged.get("modules", [])}
    target = tmp / "combined.cov"
    ok_missing, why_missing = combine_coverage(sys.executable, 2, target)
    for i in (0, 1):
        lane_files(i)["cov"].write_text("not a coverage db", encoding="utf-8")
    target.write_text("pre-existing non-empty file, so only the command's own failure can turn this red",
                      encoding="utf-8")
    ok_broken, _why = combine_coverage(sys.executable, 2, target)
    # 命令“成功”却没产出合并文件：第二道守卫必须独立判红。
    target.unlink()
    ok_empty, why_empty = combine_coverage(
        sys.executable, 2, target,
        runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    return {
        "合并命令成功却没产出文件应失败": ok_empty is False and "为空" in why_empty,
        "耗时按模块相加": by_module == {"a": (1.5, 3), "b": (3.0, 4)},
        "合并耗时写成功没有提示": problem == "",
        "合并耗时记录是否采覆盖率": merged.get("under_coverage") is True,
        "合并耗时写不进去会给原因": bool(bad_write),
        "缺车道覆盖率数据应失败": ok_missing is False and "缺少" in why_missing,
        "损坏的覆盖率数据应失败": ok_broken is False,
        "读不了的 JSON 返回 None": read_json(tmp / "nope.json") is None,
    }


def self_test() -> int:
    """全部在临时目录里造样本，不起任何车道、不碰真实 build/ 和 .coverage。"""
    import tempfile
    global LANES_DIR
    saved = LANES_DIR
    with tempfile.TemporaryDirectory(prefix="sf_lane_selftest_") as raw:
        LANES_DIR = Path(raw)
        try:
            checks: dict[str, bool] = {}
            for part in (_judge_checks, _plan_checks, _merge_checks):
                checks.update(part(Path(raw)))
        finally:
            LANES_DIR = saved
    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print("FAIL", name)
    if failed:
        return 1
    print("self-test 通过：%d 项（判红规则、分车道不变量、合并与失败路径）" % len(checks))
    return 0


def auto_lanes() -> int:
    """没指定车道数时按核数取：核数少的机器（例如云端运行器）自动退回少车道，1 条就等于串行。"""
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def tail(path: Path, lines: int = 25) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "(日志读不到)"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lanes", type=int, default=0, help="车道数；0 = 按核数自动")
    parser.add_argument("--coverage", action="store_true", help="每条车道各自采集覆盖率并在结束后合并")
    parser.add_argument("--plan-only", action="store_true", help="只打印分配方案，不运行")
    parser.add_argument("--only", action="append", default=[], metavar="MODULE",
                        help="只跑这些模块（冒烟用，不是完整关卡，结尾会明说）")
    parser.add_argument("--timeout", type=float, default=LANE_TIMEOUT_SEC, help="整体超时秒数")
    parser.add_argument("--self-test", action="store_true", help="判红规则与分车道不变量自检")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    python = sys.executable
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        modules = discover(python)
    except RuntimeError as exc:
        print("!! " + str(exc))
        return 1
    subset = bool(args.only)
    if subset:
        unknown = [m for m in args.only if m not in modules]
        if unknown:
            print("!! --only 里有不存在的模块：%s" % ", ".join(unknown))
            return 2
        modules = {m: modules[m] for m in args.only}
    lanes_wanted = args.lanes or auto_lanes()
    if lanes_wanted > MAX_LANES:
        print("   车道数 %d 超过上限，按 %d 条跑（上限的理由见 MAX_LANES 的注释）" % (lanes_wanted, MAX_LANES))
        lanes_wanted = MAX_LANES
    plan = plan_lanes(modules, lanes_wanted, load_weights())
    weights = load_weights()
    print("== 车道总指挥：%d 个模块 / %d 条用例 → %d 条车道%s ==" % (
        len(modules), sum(modules.values()), len(plan), "（冒烟子集，不是完整关卡）" if subset else ""))
    for index, lane in enumerate(plan):
        est = sum(weights.get(m, modules[m] * DEFAULT_SEC_PER_CASE) for m in lane)
        core = [m for m in lane if m in SERIAL_CORE]
        print("   L%d: %3d 个模块 %5d 条 估计 %4.0fs%s" % (
            index, len(lane), sum(modules[m] for m in lane), est,
            "  串行核心：" + ",".join(core) if core else ""))
    if args.plan_only:
        return 0

    lanes, wall, timed_out = run_lanes(python, plan, args.coverage, args.timeout, {})
    for index, lane in enumerate(plan):
        print("\n===== 车道 L%d 日志（%d 个模块）=====" % (index, len(lane)))
        try:
            print(lane_files(index)["log"].read_text(encoding="utf-8", errors="replace"), end="")
        except OSError:
            print("(日志读不到)")
    code, problems, summary = judge(plan, modules, lanes, timed_out)

    cov_note = ""
    if args.coverage:
        ok, note = combine_coverage(python, len(plan))
        cov_note = note
        if not ok:
            problems.append(note)
            code = max(code, 1)
    out = os.environ.get("SF_TEST_DURATIONS_OUT", "").strip()
    if out:
        bad = merge_durations(len(plan), wall, out, {
            "count": len(plan), "coverage": args.coverage,
            "per_lane": [{"modules": len(plan[i]), "seconds": round(lanes[i]["seconds"], 1)} for i in range(len(plan))]})
        print("!! 耗时明细没写成（不影响本轮结论）：" + bad if bad else "耗时明细已写入 " + out)

    print("\n== 汇总（%d 条车道并行，总墙钟 %.1fs）==" % (len(plan), wall))
    for index, lane in enumerate(plan):
        print("   L%d: rc=%s 用时 %.1fs（%d 个模块）" % (index, lanes[index]["rc"], lanes[index]["seconds"], len(lane)))
    print("用例总数 %d｜失败/错误 %d｜flaky %d" % (summary["total"], summary["failures"], summary["flaky"]))
    if summary["dirty"]:
        print("!! 用户数据被改动：%s" % "; ".join(summary["dirty"][:5]))
    else:
        print("用户数据未改动（config / securities / debug.log / feedback）")
    for pid in summary["failed_ids"][:20]:
        print("!! 失败：%s" % pid)
    for line in problems:
        print("!! " + line)
    if code:
        for index in range(len(plan)):
            if lanes[index]["rc"] not in (0, None) or not lane_files(index)["result"].is_file():
                print("\n--- L%d 日志末尾 ---\n%s" % (index, tail(lane_files(index)["log"])))
    if subset:
        print("== 冒烟子集运行（%s）：不代表完整关卡 ==" % ("覆盖率已合并到 " + cov_note if cov_note else "未采集覆盖率"))
    return code


if __name__ == "__main__":
    sys.exit(main())
