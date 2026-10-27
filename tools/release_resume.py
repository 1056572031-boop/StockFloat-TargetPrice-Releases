#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布后续跑与收口日志：build_release.sh 在第 6 步之后中途退出时，从没做完的那一步接着做。

背景（v1.4.104、v1.4.105 连续两次踩到）：脚本在上传读回、部署入口这些步骤中途退出时，第 2 步已把发布状态翻成
released_pending_acceptance，重跑 --publish-prepared 会在开头被拒。此前只能人按脚本顺序手工补做，而
quality_evidence.py post-release 要求发布日志里出现脚本自己才会打印的几行，手工补做的日志过不了验收。

本工具做两件事，都不绕过任何校验：

  run      按 build_release.sh 第 6.1 至 8 步的原顺序执行尚未成功的步骤，每步一份日志和退出码。
           默认只预演，--apply 才执行；已成功（退出码 0）的步骤默认跳过，--redo 点名重做。
           必须步骤失败即停（与脚本一致）；推送与桌面对账只记录结果（与脚本一致）。
  compose  把原发布日志和各步骤日志原样合成一份发布日志。只有全部必须步骤退出码为 0、线上清单读回一致时，
           才补出脚本在步骤 8 与收尾处打印的两行；否则不写文件，列出缺什么。合成日志开头写明它不是脚本一次
           连续运行的输出，并给出每个来源文件的 SHA-256。

第 6 步（上传与清单切换）只有给了 --quality-record 才包含，并且带 --resume：线上已有同名版本 ZIP 且字节与本地
快照一致才复用它接着做，不一致仍拒绝覆盖。

不做的事：不改发布状态；不替代发布后读回与点击走查。
"""
from __future__ import annotations

import argparse
import ast
import datetime
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import quality_evidence as evidence  # noqa: E402

DEFAULT_APP = Path("/Applications/Skyrocket.app")
# 单步超时可配、写错当场报错（与 publish_update_release 的超时同一口径：上传耗时由上行带宽决定，
# 钉死任何一个数换台机器就可能变成“这一步必红”，而被杀的上传会留下半截资产、同名重跑又被拒）。
STEP_TIMEOUT_ENV = "SF_RESUME_STEP_TIMEOUT_SEC"
STEP_TIMEOUT_DEFAULT = 1800.0
STEP_TIMEOUT_FLOOR = 60.0
# 第 6 步（上传 + 读回）单独的预算：publish_update_release 里上传一次最多 900 秒，读回最多 4 次、
# 每次也最多 900 秒，最坏要 4500 秒以上；沿用 1800 秒的通用默认值，会在慢上行下把还在正常上传的
# 发布工具整组杀掉，留下半截同名资产（同名重跑又被拒）。取 5 个 900 秒再留 900 秒余量。
PUBLISH_TIMEOUT_ENV = "SF_RESUME_PUBLISH_TIMEOUT_SEC"
PUBLISH_TIMEOUT_DEFAULT = 5400.0
PUBLISH_TIMEOUT_FLOOR = 900.0
OS_ERROR_RC = 127      # 命令根本没能启动
TIMEOUT_RC = 124       # 与 coreutils timeout 同值：是被我们杀掉的，不是命令自己退出的
PENDING_STAGE = "released_pending_acceptance"
COMPLETE_STAGE = "complete"
AHEAD_FILE = "rest-7-ahead-after-push.txt"
LIVE_MANIFEST_MARK = "线上清单读回一致"
SMOKE_VERDICT = "结论: 冒烟通过"
SMOKE_STEP = "8-final-smoke"
PUBLISH_STEP = "6-publish"
# 与回归关卡探针相关的开关不许带进真实发布步骤（build_release.sh 同样不继承它们）。
SCRUBBED_ENV = ("SF_GUARD_PROBE", "SF_LOCK_PROBE", "SF_LOCK_PROBE_DIR", "SF_PUBLIC_REGRESSION",
                "SF_SKIP_COVERAGE", "SF_RANDOM_ORDER", "SF_LANES", "SF_REJECT_NETWORK",
                "SF_TEST_REJECT_NETWORK")


def app_version(root: Path) -> str:
    tree = ast.parse((root / "sf_constants.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError("sf_constants.py 里找不到 APP_VERSION")


def plan(py: str, version: str, branch: str, app: Path,
         record: Path | None = None) -> list[tuple[str, bool, list[str]]]:
    """build_release.sh 第 6.1 至 8 步的原顺序；第二项为 True 表示失败即停（脚本里是 exit 1）。

    给了 record（本轮已绑定的质量记录）才在最前面加上第 6 步：用 publish_update_release.py --resume 接着做
    上传之后的清单切换，线上已有同名版本 ZIP 且字节一致才复用，否则照旧拒绝覆盖。"""
    steps = [
        ("6.1-download-entry", True, ["bash", "-c", "cd services/download-analytics && node deploy-pages.mjs"]),
        ("6.2-render-guide", True, [py, "tools/render_install_guide.py"]),
        ("6.2-check-guide", True, [py, "tools/render_install_guide.py", "--check"]),
        ("6.2-public-release", True, [py, "tools/public_release.py", "--publish"]),
        ("7-push", False, ["git", "push", "-q", "origin", "HEAD:refs/heads/" + branch]),
        ("7.1-desktop", False, [py, "tools/desktop_folder_sync.py", "--refresh", "--version", version]),
        ("8-final-smoke", True, [py, "tools/smoke_frozen_app.py", str(app), "--platform", "cocoa",
                                 "--timeout", "30"]),
    ]
    if record is not None:
        steps.insert(0, (PUBLISH_STEP, True, [
            "/usr/bin/env", "SF_QUALITY_RECORD=" + str(record), py, "tools/publish_update_release.py",
            "--assets", "build/update_assets/V" + version, "--notes", "release_notes.txt", "--resume"]))
    return steps


def step_header(app: Path) -> str:
    """脚本在步骤 8 开头打印的标题行；测试拿脚本真实输出核对过。"""
    return "== 8) 最终启动复验：%s ==" % app


def closing_line(version: str, sync_note: str = "") -> str:
    """脚本收尾处打印的那一行；quality_evidence.publish_log_markers 要求它出现在发布日志里。"""
    if sync_note:
        return "== 发布动作完成 v%s，但遗留一项：%s（发布后验收待完成） ==" % (version, sync_note)
    return "== 发布动作完成 v%s，发布后验收待完成 ==" % version


def clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in SCRUBBED_ENV}


def step_rc(directory: Path, name: str) -> int | None:
    path = directory / ("rest-%s.rc" % name)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return int(text) if text.isdigit() else None


def step_timeout() -> float:
    """读单步超时；写错当场失败，绝不静默退回默认值（静默退回会让人以为自己放宽成功了）。"""
    raw = os.environ.get(STEP_TIMEOUT_ENV, "").strip()
    if not raw:
        return STEP_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("%s 必须是秒数，现在=%r（拒绝静默退回默认值）" % (STEP_TIMEOUT_ENV, raw)) from None
    if value < STEP_TIMEOUT_FLOOR:
        raise ValueError("%s 不得低于 %.0f 秒，现在=%r" % (STEP_TIMEOUT_ENV, STEP_TIMEOUT_FLOOR, raw))
    return value


class ProcessGroupInterrupted(BaseException):
    """发布步骤被外部终止信号打断，必须先清理整组子进程再退出。"""


class SignalHandler:
    """在当前进程把终止信号转成可清理的异常，并在步骤结束后恢复原处理器。"""

    def __init__(self):
        self.previous = {}

    def __enter__(self):
        if threading.current_thread() is not threading.main_thread():
            return self                       # 只有主线程能装信号处理器；别线程调用时保持原行为（超时与 Ctrl-C 仍清理）

        def raise_interrupted(signum, _frame):
            raise ProcessGroupInterrupted(signum)

        signals = [signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):
            signals.append(signal.SIGHUP)
        for signum in signals:
            current = signal.getsignal(signum)
            if current == signal.SIG_IGN:
                continue                      # 用户用 nohup 之类明确忽略的信号，不替他改成“会终止”
            self.previous[signum] = current
            signal.signal(signum, raise_interrupted)
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)
        return False


def _timeout_from_env(name: str, default: float, floor: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("%s 必须是秒数，现在=%r（拒绝静默退回默认值）" % (name, raw)) from None
    if value < floor:
        raise ValueError("%s 不得低于 %.0f 秒，现在=%r" % (name, floor, raw))
    return value


def publish_step_timeout() -> float:
    return _timeout_from_env(PUBLISH_TIMEOUT_ENV, PUBLISH_TIMEOUT_DEFAULT, PUBLISH_TIMEOUT_FLOOR)


def timeout_for_step(name: str, default: float) -> float:
    """第 6 步用自己的预算：显式设了 PUBLISH 环境变量就听它的，否则取“专用默认值”与通用预算中较大的一个
    （有人把通用预算调到更大，发布步骤不该反而比别的步骤更短）。其余步骤用通用预算。"""
    if name != PUBLISH_STEP:
        return default
    if os.environ.get(PUBLISH_TIMEOUT_ENV, "").strip():
        return publish_step_timeout()
    return max(PUBLISH_TIMEOUT_DEFAULT, default)


def step_evidence_fresh(directory: Path, name: str, since: datetime.datetime | None) -> bool:
    if since is None:
        return True
    cutoff = since.timestamp()
    paths = [directory / ("rest-%s.rc" % name), directory / ("rest-%s.log" % name)]
    return all(path.is_file() and path.stat().st_mtime >= cutoff for path in paths)


def run_group(argv, *, cwd, env, stdout, stderr, timeout, popen=subprocess.Popen):
    """像 subprocess.run，但子进程自成一组：超时或被打断（Ctrl-C）时整组一起杀。

    发布步骤是 bash -c / node / gh 这类会再起孙进程的命令。subprocess.run 超时只杀直接子进程，
    孙进程会留着继续写线上（上传、部署），而同一条命令重跑又会与它们并发；Ctrl-C 则因为子进程
    不在终端的前台进程组里而根本到不了它，所以这里不只处理超时。"""
    proc = popen(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
    try:
        with SignalHandler():
            data, _ = proc.communicate(timeout=timeout)
    except BaseException as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass                                           # 整组已经自己退完了
        data, _ = proc.communicate()
        if isinstance(exc, subprocess.TimeoutExpired):
            exc.output = data                              # 把被杀前已经打印的部分带出去
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, data)


def execute(runner, argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    try:
        done = runner(argv, cwd=cwd, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        partial = exc.output if isinstance(exc.output, bytes) else b""
        return TIMEOUT_RC, partial.decode("utf-8", "replace") + (
            "超时 %.0f 秒，已终止整个进程组（%s 可调）：%s\n" % (timeout, STEP_TIMEOUT_ENV, " ".join(argv)))
    except OSError as exc:
        return OS_ERROR_RC, "无法执行：%r\n" % (exc,)
    return done.returncode, done.stdout.decode("utf-8", "replace")


def run_steps(root: Path, directory: Path, steps, branch: str, *, apply: bool, redo=(),
              runner=run_group, out=print, timeout: float = STEP_TIMEOUT_DEFAULT,
              since: datetime.datetime | None = None) -> int:
    known = [name for name, _, _ in steps]
    unknown = [name for name in redo if name not in known]
    if unknown:
        # 拼错的 --redo 若静默无效，输出全是“跳过”、退出码 0，人会以为已经重做过了。
        raise ValueError("--redo 点名了计划里没有的步骤：%s；可选：%s" % ("、".join(unknown), "、".join(known)))
    for name, hard, argv in steps:
        if (step_rc(directory, name) == 0 and name not in redo
                and step_evidence_fresh(directory, name, since)):
            out("跳过 %s：上次已成功（退出码 0）" % name)
            continue
        if step_rc(directory, name) == 0 and name not in redo and since is not None:
            out("重跑 %s：已有证据早于本次发布开始" % name)
        out("%s %s：%s" % ("执行" if apply else "预演", name, " ".join(argv)))
        if not apply:
            continue
        directory.mkdir(parents=True, exist_ok=True)       # 预演不创建任何文件或目录
        # 先作废旧退出码再执行：重做途中被打断时，旧的 0 不能继续替这一步作证。
        (directory / ("rest-%s.rc" % name)).unlink(missing_ok=True)
        rc, text = execute(runner, argv, root, timeout_for_step(name, timeout))
        (directory / ("rest-%s.log" % name)).write_text(text, encoding="utf-8")
        (directory / ("rest-%s.rc" % name)).write_text("%d\n" % rc, encoding="utf-8")
        out("   退出码 %d" % rc)
        if name == "7-push":
            _, ahead = execute(runner, ["git", "rev-list", "--count", "origin/%s..HEAD" % branch], root, timeout)
            (directory / AHEAD_FILE).write_text(ahead, encoding="utf-8")
        if rc != 0 and hard:
            out("!! %s 失败，按脚本口径停在这里；修好原因后再运行本命令，已成功的步骤会被跳过" % name)
            return rc
    return 0


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def embedded(text: str) -> str:
    """附进合成日志的文字：每行去掉行尾空白、整体去掉末尾空行。

    工具原始输出（wrangler 的表格、git 的 remote 提示）带行尾空白，而提交钩子不接受带行尾空白的
    文件；合成日志要入库归档，所以只能在附入时去掉。SHA-256 仍然是来源文件原始字节的，不受影响。"""
    return "\n".join(line.rstrip() for line in text.rstrip("\n").split("\n"))


def stale_evidence(directory: Path, publish_logs: list[Path], names, since: datetime.datetime) -> list[str]:
    """修改时间早于“本次发布开始”的证据文件：它们不可能是这次发布产生的（旧版本留下的不能沿用）。"""
    candidates = list(publish_logs) + [directory / AHEAD_FILE]
    for name in names:
        candidates += [directory / ("rest-%s.rc" % name), directory / ("rest-%s.log" % name)]
    cutoff = since.timestamp()
    return [path.name for path in candidates if path.is_file() and path.stat().st_mtime < cutoff]


def compose(root: Path, directory: Path, publish_logs: list[Path], app: Path, out_path: Path,
            since: datetime.datetime) -> tuple[bool, list[str]]:
    version = app_version(root)
    steps = [(name, hard) for name, hard, _ in plan("python", version, "branch", app, Path("record"))
             if name != PUBLISH_STEP or (directory / ("rest-%s.rc" % name)).exists()]
    results = {name: step_rc(directory, name) for name, _ in steps}
    problems = []
    for name, hard in steps:
        code = results[name]
        if not hard:
            continue
        if code != 0:
            problems.append("必须步骤 %s %s" % (name, "没有记录" if code is None else "退出码为 %d" % code))
        elif not (directory / ("rest-%s.log" % name)).is_file():
            # 只有退出码、没有日志，就无从核对它到底做了什么；不能凭一个数字合成“发布动作完成”。
            problems.append("必须步骤 %s 退出码为 0，但没有它的步骤日志" % name)
    old = stale_evidence(directory, publish_logs, [name for name, _ in steps], since)
    if old:
        problems.append("这些证据文件早于本次发布开始（%s），不是这次发布产生的：%s" % (since.isoformat(), "、".join(old)))
    sources = "\n".join(read_text(path) for path in publish_logs)
    publish_log = directory / ("rest-%s.log" % PUBLISH_STEP)
    if any(name == PUBLISH_STEP for name, _ in steps) and publish_log.is_file():
        # 本工具自己跑的第 6 步：published_archive= 与“线上清单读回一致”正是发布工具真实打印的，算来源。
        sources += "\n" + read_text(publish_log)
    if LIVE_MANIFEST_MARK not in sources:
        problems.append("来源日志里没有「%s」：第 6 步的上传与清单切换没有读回成功" % LIVE_MANIFEST_MARK)
    # 这三行只能出自原始发布日志；步骤日志里碰巧出现同样的文字不算数。
    absent = [marker for marker in evidence.publish_log_markers(app, version)
              if marker.startswith(("已安装: ", "版本: ", "published_archive=")) and marker not in sources]
    if absent:
        problems.append("来源日志仍缺少：" + "；".join(absent))
    # 「冒烟通过」候选阶段的日志里也有一条，只认最终启动复验自己那份日志。
    smoke_log = directory / ("rest-%s.log" % SMOKE_STEP)
    if not smoke_log.is_file() or SMOKE_VERDICT not in read_text(smoke_log):
        problems.append("最终启动复验（%s）自己的日志里没有「%s」" % (SMOKE_STEP, SMOKE_VERDICT))
    if problems:
        return False, problems
    ahead_path = directory / AHEAD_FILE
    ahead = ahead_path.read_text(encoding="utf-8").strip() if ahead_path.is_file() else ""
    sync_note = "" if results["7-push"] == 0 and ahead == "0" else "源码未同步"
    lines = ["== 续跑合成日志（tools/release_resume.py compose，v%s）==" % version,
             "本文件不是 build_release.sh 一次连续运行的输出。下面各段按来源附上（每行只去掉行尾空白），",
             "并标明来源文件与该文件原始字节的 SHA-256；",
             "只有「步骤 8 标题行」与「收尾行」是本工具在核对全部必须步骤退出码为 0、线上清单读回一致之后补出的，",
             "文字与脚本打印的一致（由测试拿脚本真实输出核对）。",
             "合成时刻：" + evidence.now(),
             "步骤退出码：" + "，".join("%s=%s" % (name, results[name]) for name, _ in steps),
             "推送后落后远端提交数：" + (ahead or "未记录")]
    for path in publish_logs:
        lines += ["", "== 来源：%s  sha256=%s ==" % (path.name, sha256_of(path)), embedded(read_text(path))]
    for name, _ in steps:
        log = directory / ("rest-%s.log" % name)
        if name == "8-final-smoke":
            lines += ["", step_header(app)]
        lines += ["", "== 步骤 %s（退出码 %s）日志：%s  sha256=%s ==" % (
            name, results[name], log.name, sha256_of(log) if log.is_file() else "缺失"),
            embedded(read_text(log)) if log.is_file() else ""]
    lines += ["", closing_line(version, sync_note), ""]
    body = "\n".join(lines)
    missing = [marker for marker in evidence.publish_log_markers(app, version) if marker not in body]
    if missing:
        return False, ["合成后仍缺少发布日志必须出现的行：" + "；".join(missing)]
    if out_path.exists():
        raise ValueError("不覆盖已有文件: " + str(out_path))
    out_path.write_text(body, encoding="utf-8")
    return True, []


def git_output(root: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True, timeout=30,
                          env=evidence.gate_stamp.git_env())
    return done.stdout.strip()


def load_state(state_path: Path, root: Path, stages) -> dict:
    """读发布状态，并要求它描述的就是 --root 这棵树：版本和提交都对得上才往下走。

    不核对就可能拿着 A 版本的状态去操作 B 棵树——续跑里有公开发布和推送，那是推错源码。"""
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("stage") not in stages:
        raise ValueError("发布状态是 %r，不在允许的阶段 %s 内" % (state.get("stage"), "、".join(stages)))
    version = app_version(root)
    if state.get("version") != version:
        raise ValueError("状态文件是 v%s，--root 这棵树是 v%s：不是同一次发布，拒绝继续" % (state.get("version"), version))
    expected = (state.get("source") or {}).get("head")
    actual = git_output(root, "rev-parse", "HEAD")
    if expected != actual:
        raise ValueError("状态文件记的提交是 %s，--root 这棵树的 HEAD 是 %s：不是同一次发布，拒绝继续"
                         % (expected, actual))
    return state


def publish_started_at(state: dict) -> datetime.datetime:
    for item in state.get("history", []):
        if item.get("to") == PENDING_STAGE:
            return datetime.datetime.fromisoformat(item["at"])
    raise ValueError("发布状态历史里没有进入 %s 的记录，无法确定本次发布从何时开始" % PENDING_STAGE)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "compose"):
        item = sub.add_parser(name)
        # 默认值是本工具所在的树，不一定是要续跑的那棵发布树；--state 里的版本与提交必须对得上 --root。
        item.add_argument("--root", type=Path, default=ROOT)
        item.add_argument("--state", type=Path, required=True)
        item.add_argument("--evidence", type=Path, required=True)
        item.add_argument("--app", type=Path, default=DEFAULT_APP)
    run_parser = sub.choices["run"]
    run_parser.add_argument("--apply", action="store_true")
    run_parser.add_argument("--redo", action="append", default=[])
    run_parser.add_argument("--quality-record", type=Path,
                            help="本轮已绑定的质量记录；给了才包含第 6 步（--resume 接着做上传之后的清单切换）")
    compose_parser = sub.choices["compose"]
    compose_parser.add_argument("--publish-log", type=Path, action="append", required=True)
    compose_parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            state = load_state(args.state, args.root, (PENDING_STAGE,))
            timeout = step_timeout()
            publish_step_timeout()                         # 写错当场报错，不等跑到第 6 步才发现
            branch = git_output(args.root, "rev-parse", "--abbrev-ref", "HEAD")
            record = args.quality_record.resolve() if args.quality_record else None
            if record is not None and not record.is_file():
                raise ValueError("--quality-record 不是文件：" + str(record))
            steps = plan(str(args.root / ".venv/bin/python3"), app_version(args.root), branch, args.app, record)
            return run_steps(args.root, args.evidence, steps, branch, apply=args.apply, redo=args.redo,
                             timeout=timeout, since=publish_started_at(state))
        state = load_state(args.state, args.root, (PENDING_STAGE, COMPLETE_STAGE))
        ok, problems = compose(args.root, args.evidence, args.publish_log, args.app, args.out,
                               publish_started_at(state))
    except ProcessGroupInterrupted as exc:
        signum = exc.args[0] if exc.args else signal.SIGTERM
        print("release_resume: 被信号 %s 终止，整个进程组已清理；已完成的步骤证据保留，被打断的那一步没有退出码，"
              "重跑本命令会重新执行它" % signal.Signals(signum).name, file=sys.stderr)
        return 128 + int(signum)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print("release_resume: %s" % exc, file=sys.stderr)
        return 2
    for problem in problems:
        print("!! " + problem, file=sys.stderr)
    if ok:
        print("已写出合成发布日志：" + str(args.out))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
