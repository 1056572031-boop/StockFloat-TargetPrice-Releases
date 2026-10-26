#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.3.68：带稳定性兜底的 unittest 运行器（替代裸 `unittest discover`）。

背景（见 TESTS_AUDIT_2026-09-14.md 之 P3-9）：原来只用 `unittest discover` 跑一遍，
四类问题都压在被称作"红一次"的人工判断上 —— 平均每次要花十几分钟才能确定
"是真坏了 / 抖了一下 / 是数字该改了 / 是顺序依赖"。本运行器把其中三件自动化：

  ① **单用例超时**（默认 60s）：hang 住的用例不再让整轮无限等待 ——
     到点用 faulthandler 打印全部线程栈并退出，栈里直接写明卡在哪一行。
  ② **失败自动重跑 + [flaky] 标记**：重跑一次仍红才算真失败；一次红一次绿在结尾
     明确列为 flaky（"这条用例不稳定，需要治理"），而不是让人当场猜。
  ③ **随机顺序**（默认关）：按类级洗牌，用于发版前暴露"只在特定顺序下才红"的依赖。
     注释：默认关闭是因为随机序会让每次 CI 日志不可比，只用于主动排查。
  ④ **用户数据零改动校验**：跑之前记录 ~/Library/Application Support/StockFloatTargetPrice
     下 config.json / securities.json / debug.log 的大小与 mtime，跑之后比对。
     本仓有血泪教训：一次全量回归把用户真实配置的 bg 从 glass 写成 translucent，
     症状伪装成"毛玻璃消失"，排障方向被带偏到渲染/并发。这类污染必须当场发现，
     而不是等用户在界面上看到异常。
  ⑤ **桌面弹窗拦截**：任何用例真去 `osascript … display dialog/notification`
     都会被拦下并记为违规（报红）。一条漏打桩的用例曾让用户桌面在每次提交时
     凭空弹出「软件更新」窗口 —— 详见 install_ui_dialog_guard() 的注释。

用完**保留原有行为**：不加任何开关时，结果与 `unittest discover` 完全一致
（同一 discover 规则、同一退出码语义），只是多了超时与数据校验两道兜底。

用法：
    tools/test_runner.py                    # 等价原 discover
    SF_RANDOM_ORDER=1 tools/test_runner.py  # 随机顺序跑一次（排查顺序依赖）
    SF_RERUN_FAILED=0 tools/test_runner.py  # 不做失败重跑
    SF_TEST_TIMEOUT=120 tools/test_runner.py
    tools/test_runner.py --module test_daily_workflow --module test_code_governance
    SF_TEST_DURATIONS_OUT=build/test-durations.json tools/test_runner.py  # 另存按模块的耗时明细

每轮结尾都会打印"用例耗时"：按模块汇总主轮用例体耗时（不含失败重跑），并与主轮墙钟对照，
差额即类/模块级准备与收尾和框架开销。这只是观测，不参与任何判定，也不改退出码；
覆盖率采集下数字整体偏大，不能与未采集的数字直接比。

指定模块仍走超时、桌面弹窗和用户数据保护；不代表完整回归、不检查覆盖率、不落关卡戳。
未知参数或零用例拒绝通过。SF_RERUN_FAILED=0 可保留首次失败为本轮失败。

退出码：0 全绿；1 存在失败/错误/桌面弹窗违规；2 用户数据被改动（最高优先级，不算"测试问题"）。
"""
import argparse
import contextlib
import faulthandler
import gc
import json
import os
import pathlib
import random
import re
import stat
import sys
import tempfile
import time
import unittest


def _support_dir():
    return os.path.expanduser(
        "~/Library/Application Support/StockFloatTargetPrice")


# 用户数据清单：这些是**用户的东西**，测试再脏也不能碰。
# debug.log 自 v1.3.67 起已由 SF_LOG_PATH 隔离，留在这里是双保险：
# 将来谁加了一条没隔离的写盘路径，这里立刻报警。
_USER_DATA = ["config.json", "securities.json", "debug.log"]
_GUI_APP: object | None = None


# 红线归因：测试期间真实 debug.log 被追加了哪些"有人在用正式版"的行。
# 只归因、不放宽 —— 判据仍是 exit 2，这里只是把"是谁改的"从零变成一句可读的数字。
# 只打印每类命中条数，**不打印日志正文**：里面可能有路径、股票代码和本地片段。
ACTIVITY_PHRASES = (
    ("启动", "STARTUP"),
    ("首帧", "first-frame painted"),
    ("点击", "btn action queued"),
    ("右键菜单", "ctx menu"),
    ("失活恢复", "reopen ca"),
)
ACTIVITY_READ_CAP = 2_000_000   # 只看本轮新增部分的前 2MB；再长也只报"超过"
WINDOW_HINT_SEC = 300           # 起跑前：真实日志 5 分钟内写过，就提示正式版可能刚用过


def _user_data_path(name: str, support_dir: str | None = None) -> str:
    """support_dir 只为用例而留：正式运行永远用 _support_dir()，解析不出真的用户目录。"""
    return os.path.join(support_dir or _support_dir(), name)


def user_log_state(name: str = "debug.log", support_dir: str | None = None):
    """真实日志的"起点"：inode、大小、mtime；不存在返回 None。"""
    try:
        st = os.stat(_user_data_path(name, support_dir))
    except OSError:
        return None
    return {"inode": st.st_ino, "size": st.st_size, "mtime": st.st_mtime}


def _iter_lines(path: str, offset: int = 0):
    """从 offset 逐行读，产出 (一行的正文, 读完后新的 offset)。

    手写而不是 for line in f：那样要额外维护 tell()，且不同版本的缓冲行为不一。
    只支持 '\n' 断行，这是本产品日志的写法（见 sf_logging）。
    """
    with open(path, "rb") as fh:
        fh.seek(offset)
        while True:
            raw = fh.readline()
            if not raw:
                return
            yield raw.rstrip(b"\r\n"), fh.tell()


def count_activity(start, name: str = "debug.log", support_dir: str | None = None) -> dict:
    """对比起点与现在，数出本轮真实日志新增了多少行"正式版在被人用"。

    返回 {"status": ..., "counts": {类别: 条数}, "total": 新增行数}：
      status="missing"  起点和现在都没有（没装过/从没跑过正式版）
      status="created"  本轮期间被新建（多半是正式版刚启动）
      status="rotated"  本轮期间被轮转或替换（inode 或大小回退），条数不可比
      status="ok"       正常，counts 有效
    读取失败一律记 "unreadable"，绝不让诊断把整轮回归弄崩。
    """
    end = user_log_state(name, support_dir)
    counts = {label: 0 for label, _ in ACTIVITY_PHRASES}
    if start is None and end is None:
        return {"status": "missing", "counts": counts, "total": 0}
    try:
        if start is None:
            lines = _iter_lines(_user_data_path(name, support_dir))
        elif end is None:
            return {"status": "rotated", "counts": counts, "total": 0}
        elif end["inode"] != start["inode"] or end["size"] < start["size"]:
            return {"status": "rotated", "counts": counts, "total": 0}
        else:
            lines = _iter_lines(_user_data_path(name, support_dir), start["size"])
        total, consumed = 0, 0
        for text, consumed in lines:
            total += 1
            for label, phrase in ACTIVITY_PHRASES:
                if phrase.encode("utf-8") in text:
                    counts[label] += 1
            if consumed >= ACTIVITY_READ_CAP:
                break
    except OSError:
        return {"status": "unreadable", "counts": counts, "total": 0}
    if start is None:
        status = "created"
    else:
        status = "ok"
    return {"status": status, "counts": counts, "total": total}


def activity_report(activity) -> str | None:
    """把归因结果写成一句人话；没有可归因的活动时返回 None（不刷屏）。"""
    if not activity:
        return None
    status, counts = activity.get("status"), activity.get("counts") or {}
    if status in ("missing", "unreadable"):
        return "归因：真实 debug.log %s，无法判断是谁写的" % (
            "不存在" if status == "missing" else "读不了")
    if status == "rotated":
        return "归因：真实 debug.log 在本轮期间被轮转或替换，新增条数不可比"
    hits = "、".join("%s %d 条" % (label, n) for label, n in counts.items() if n)
    opened = "（本轮期间被新建）" if status == "created" else ""
    if not hits:
        return ("归因：真实 debug.log 在本轮期间%s没有新增启动或点击行 —— 改动不是正式版写出来的"
                % ("被新建，但" if status == "created" else ""))
    return ("归因：本轮真实 debug.log 新增 %s%s（共 %d 行）。测试日志走 SF_LOG_PATH，"
            "真实日志只会由正式版写入 —— 多半是别的会话启动了正式版或点了它的按钮"
            % (hits, opened, activity.get("total", 0)))


def emit_activity_hint(out=print, support_dir: str | None = None) -> None:
    """起跑前的提示。抽成函数是为了让用例能直接调它并换掉输出，而不是只在整档里跑一次。"""
    hint = activity_hint(support_dir=support_dir)
    if hint:
        out(hint)


def emit_dirty_detail(dirty, log_start, out=print, support_dir: str | None = None) -> None:
    """红线触发时的说明：先列被改的文件，再给出归因。判据本身仍由 verdict 决定。"""
    out("!! 用户数据在测试期间被改动 —— 这不是测试问题，是红线：")
    for line in dirty:
        out("   - %s" % line)
    note = activity_report(count_activity(log_start, support_dir=support_dir))
    if note:
        out("   " + note)


def _activity_checks() -> dict:
    """自检用的判据表：{失败时的说明: 是否通过}。所有分支都会执行，便于改动行覆盖。"""
    import tempfile
    checks = {}
    with tempfile.TemporaryDirectory(prefix="sf_activity_") as directory:
        log = os.path.join(directory, "debug.log")
        missing = count_activity(None, support_dir=directory)
        checks["真实日志不存在时归因报告为空，会让人以为没有活动"] = activity_report(missing) is not None
        checks["没装过正式版时归因状态不是 missing"] = missing["status"] == "missing"
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("[old] INFO: STARTUP v1.0\n[old] INFO: btn action queued\n")
        start = user_log_state("debug.log", directory)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("[new] INFO: STARTUP v1.4.103\n[new] INFO: first-frame painted\n"
                     "[new] INFO: old line without activity\n")
        counts = count_activity(start, support_dir=directory)
        checks["归因把起点之前的旧行算进来了"] = counts["counts"] == {
            "启动": 1, "首帧": 1, "点击": 0, "右键菜单": 0, "失活恢复": 0} and counts["status"] == "ok"
        checks["归因报告没写出命中的类别与条数"] = "启动 1 条" in (activity_report(counts) or "")
        fresh = count_activity(None, support_dir=directory)
        checks["本轮期间新建的真实日志没按 created 统计"] = (
            fresh["status"] == "created" and fresh["counts"]["启动"] == 2)
        rotated = count_activity({"inode": start["inode"] + 1, "size": start["size"], "mtime": 0})
        checks["日志被轮转却按普通追加统计"] = rotated["status"] == "rotated"
        checks["轮转时没给出诚实说明"] = "轮转" in (activity_report(rotated) or "")
        checks["新增行数不是从起点算的"] = count_activity(start, support_dir=directory)["total"] == 3
        checks["日志很久没写却提示正式版刚用过"] = (
            activity_hint("debug.log", now=time.time() + WINDOW_HINT_SEC + 60,
                          support_dir=directory) is None)
        checks["日志刚写过却没有起跑前的提示"] = activity_hint(
            "debug.log", now=time.time(), support_dir=directory) is not None
        reports = [activity_report(x) or "" for x in
                   (counts, fresh, rotated, count_activity(start, support_dir=directory))]
        checks["归因说明里混进了日志正文"] = all(
            "STARTUP" not in r and "first-frame" not in r for r in reports)
    return checks


def activity_hint(name: str = "debug.log", now: float | None = None,
                  support_dir: str | None = None) -> str | None:
    """起跑前的便宜提示：真实日志最近有没有写过。只是提示，不拦跑。"""
    state = user_log_state(name, support_dir)
    if state is None:
        return None
    age = int((now if now is not None else time.time()) - state["mtime"])
    if age < 0 or age > WINDOW_HINT_SEC:
        return None
    return ("!! 提示：真实 debug.log %d 秒前刚有写入，正式版可能刚被用过。"
            "整档期间它再被启动或点击，会触发「用户数据被改动」红线（退出码 2）。" % age)


def _snapshot_user_data():
    snap: dict[str, tuple[int, int] | None] = {}
    for name in _USER_DATA:
        state = user_log_state(name)
        snap[name] = None if state is None else (state["size"], int(state["mtime"]))
    def visit_feedback(path, name):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            snap[name] = None
            return
        snap[name] = (st.st_size, st.st_mtime_ns)
        if stat.S_ISDIR(st.st_mode):
            with os.scandir(path) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
            for entry in children:
                visit_feedback(entry.path, name + entry.name + ("/" if entry.is_dir(follow_symlinks=False) else ""))
    visit_feedback(os.path.join(_support_dir(), "feedback"), "feedback/")
    return snap


def _diff_user_data(before, after):
    out = []
    names = set(_USER_DATA) | {name for name in before.keys() | after.keys() if name.startswith("feedback/")}
    for name in sorted(names):
        b, a = before.get(name), after.get(name)
        if b == a:
            continue
        if b is None:
            out.append("%s：测试期间被新建（%s）" % (name, a))
        elif a is None:
            out.append("%s：测试期间被删除（原 %s）" % (name, b))
        else:
            out.append("%s：大小 %d->%d，mtime %d->%d" % (name, b[0], a[0], b[1], a[1]))
    return out


# ---------- ⑤ 桌面弹窗拦截 ----------
# 由来（用户 2026-09-22 反馈）：桌面上"时不时"弹出一个「软件更新 / 正文 / 好」
# 系统窗口。来源不是 App —— 产品文案里没有"正文"这两个字，它是
# tests/test_v146_systemalert.py 里一条漏打桩的用例：SystemDialog 一构造就在
# 后台线程真的 Popen 了 `osascript -e '… display dialog …'`。
# 回归跑在用户的机器上（pre-commit 关卡、发版前全量），于是每次提交都可能
# 往用户桌面砸一个窗口。产品里 osascript 弹窗是真实功能，测试里它是污染 ——
# 这里把它拦下来：窗口不再出现，同时记为违规让关卡报红，逼后续用例补 mock。
UI_DIALOG_MARKERS = ("display dialog", "display notification", "display alert")

# 违规清单：[(用例名, 命令摘要)]
UI_DIALOG_VIOLATIONS = []
_current_case = ["<用例尚未开始>"]


class UiDialogBlocked(OSError):
    """测试期拦截异常。

    刻意继承 OSError：sf_system_alert / sf_notify 对"osascript 不可用"的既有
    处理就是吞掉 OSError 并降级，所以拦截不会把用例拖成看不懂的崩溃线程栈，
    真正的报红由 UI_DIALOG_VIOLATIONS 在汇总里给出。
    """


def describe_ui_command(cmd):
    """这条命令是否会在用户桌面弹出真实系统窗口？是则返回可读摘要，否则 None。

    只认"osascript + display dialog/notification/alert"这一组合：
    其他子进程（sys.executable 起的隔离子进程、bash run_tests.sh、git、hdiutil）
    一律放行，否则会把既有依赖真实子进程的用例全部误伤。
    """
    if isinstance(cmd, (list, tuple)):
        joined = " ".join(str(part) for part in cmd)
    elif isinstance(cmd, (bytes, bytearray)):
        joined = bytes(cmd).decode("utf-8", "replace")
    elif isinstance(cmd, str):
        joined = cmd
    else:
        return None
    if "osascript" not in joined:
        return None
    if not any(marker in joined for marker in UI_DIALOG_MARKERS):
        return None
    joined = joined.replace("\n", " ").replace("\t", " ")
    if len(joined) > 200:
        # 中段截断而不是尾部截断：osascript 的固定脚本源码在前，
        # 真正说明"弹的是什么"的标题/正文/按钮参数在尾部，必须留在摘要里。
        joined = joined[:90] + " …… " + joined[-100:]
    return joined


def install_ui_dialog_guard():
    """给 subprocess.Popen / run 套一层拦截（仅本运行器进程内生效）。

    局限要说清：NSAlert 走进程内 AppKit、不经过 subprocess，这里拦不住；
    需要观察弹窗的用例必须自己 patch sf_system_alert（test_v148 用假 AppKit，
    test_v146 用假 Popen），本关卡只兜住"osascript 降级通道被漏打桩"这一类。
    """
    import subprocess

    real_popen = subprocess.Popen
    real_run = subprocess.run

    def guarded_popen(cmd, *args, **kwargs):
        hit = describe_ui_command(cmd)
        if hit is None:
            return real_popen(cmd, *args, **kwargs)
        UI_DIALOG_VIOLATIONS.append((_current_case[0], hit))
        raise UiDialogBlocked("测试期间禁止弹出真实系统窗口：%s" % hit)

    def guarded_run(cmd, *args, **kwargs):
        hit = describe_ui_command(cmd)
        if hit is None:
            return real_run(cmd, *args, **kwargs)
        UI_DIALOG_VIOLATIONS.append((_current_case[0], hit))
        return subprocess.CompletedProcess(
            cmd, 1, b"", b"blocked by test_runner UI dialog guard")

    subprocess.Popen = guarded_popen
    subprocess.run = guarded_run


def _report_ui_dialog_violations():
    """打印被拦截的真实弹窗；返回是否发现违规。"""
    if not UI_DIALOG_VIOLATIONS:
        return False
    print("!! 有用例试图在桌面弹出真实系统窗口 —— 窗口已拦截，但用例必须自己")
    print("   patch subprocess.Popen（漏打桩会污染用户桌面，不是可以忽略的告警）：")
    seen = set()
    for case, hit in UI_DIALOG_VIOLATIONS:
        if (case, hit) in seen:
            continue
        seen.add((case, hit))
        print("   - %s\n     %s" % (case, hit))
    return True


def _ensure_gui_session():
    from PyQt6.QtWidgets import QApplication
    global _GUI_APP
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    elif not isinstance(app, QApplication):
        raise RuntimeError("测试会话已存在非GUI Qt应用，不能创建第二个 QApplication")
    app.setQuitOnLastWindowClosed(False)
    _GUI_APP = app
    return app


def _preimport_project_modules():
    """在 discover **之前**把项目自有模块导入一次。

    为什么必须这样：unittest 的 discover 会给每个测试模块套一层 `_ModuleCleanup`，
    它把"discover 期间新出现在 sys.modules 里的模块"在跑完后**移除**。
    于是 `sf_network` / `sf_quote` 这类被测试 import 的项目模块会被反复卸载重建 ——
    同一时刻进程里可能并存**两个同名模块对象**：先导入的测试模块持有旧对象 A，
    后导入的拿到新对象 B。两边对同一个全局变量的读写就落在了不同对象上，
    表现为"我刚赋的值，getter 读出来是 None"（随机序下 test_bugfix_148
    .test_getter_reflects_latest_status 就是这么红的，而固定顺序恰好躲开了）。
    提前导入后它们已存在于 sys.modules，cleanup 判定为"非新增"，不再卸载。
    """
    import importlib
    import pathlib
    # Keep one GUI owner alive across modules, before any Qt-dependent imports.
    _ensure_gui_session()
    root = pathlib.Path(__file__).resolve().parent.parent
    for p in sorted(root.glob("*.py")):
        name = p.stem
        if name.startswith("_") or name in ("tools",):
            continue
        try:
            importlib.import_module(name)
        except Exception:
            # 导入失败的模块不该拖垮整个运行器的准备阶段 —— 让它照常进测试，
            # 由具体用例去报错（错误信息比这里有用得多）
            pass


def _flatten(suite):
    """递归枚举 suite 内的单个测试，保留顶层 suite 的模块清理结构。"""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _shuffle_top(suite, rnd):
    """只在**顶层**洗牌：随机化各测试模块的执行先后。

    绝不能摊平到单个用例 —— unittest 的 discover 会把每个模块的 suite 包一层
    `_ModuleCleanup`，它负责该模块跑完后做模块级清理。实测摊平之后
    test_quit_lifecycle 会 SIGSEGV 11（Qt 对象在 cleanup 缺席时被提前回收）。
    顶层洗牌既能暴露跨模块的顺序依赖，又保留这层保护。
    """
    items = list(suite)
    rnd.shuffle(items)
    out = unittest.TestSuite()
    for it in items:
        out.addTest(it)
    return out


DURATIONS_SCHEMA = 1
DURATIONS_TOP = 12


def module_of(case_id):
    """用例 id 归到模块：`tests.test_x.Class.test_y` 与 `test_x.Class.test_y` 都归 `test_x`。"""
    parts = [p for p in str(case_id).split(".") if p]
    if parts and parts[0] == "tests" and len(parts) > 1:
        parts = parts[1:]
    return parts[0] if parts else "<未知>"


class DurationLedger:
    """按模块累计用例体耗时。只观测，不参与任何判定。

    时钟可注入，所以耗时数字能在测试里被精确构造，而不是靠真睡眠再断言。
    类级/模块级的准备与收尾（setUpClass 等）不在任何一条用例体里，
    所以这里的总和会小于主轮墙钟，差额由 render_durations 单独列出。
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._started = {}
        self.seconds = {}
        self.cases = {}

    def start(self, case_id):
        self._started[case_id] = self._clock()

    def stop(self, case_id):
        began = self._started.pop(case_id, None)
        if began is None:
            return
        module = module_of(case_id)
        self.seconds[module] = self.seconds.get(module, 0.0) + max(0.0, self._clock() - began)
        self.cases[module] = self.cases.get(module, 0) + 1

    def total(self):
        return sum(self.seconds.values())

    def ranked(self):
        return sorted(self.seconds.items(), key=lambda item: (-item[1], item[0]))


def coverage_active():
    """当前进程是否在 coverage 采集下跑：采集会让耗时整体偏大，不能与未采集的数字直接比。"""
    try:
        import coverage
    except ImportError:
        return False
    return coverage.Coverage.current() is not None


def render_durations(ledger, wall, under_coverage, top=DURATIONS_TOP):
    """人读的耗时摘要（只含主轮，不含失败重跑）。"""
    body = ledger.total()
    lines = ["== 用例耗时（主轮；%s）==" % ("覆盖率采集下，数字偏大" if under_coverage else "未采集覆盖率")]
    lines.append("用例体合计 %.1fs｜主轮墙钟 %.1fs｜差额 %.1fs（类/模块级准备与收尾 + 框架开销）"
                 % (body, wall, wall - body))
    ranked = ledger.ranked()
    for module, seconds in ranked[:top]:
        share = (100.0 * seconds / body) if body > 0 else 0.0
        lines.append("  %-44s %8.1fs  %5.1f%%  %d 条" % (module, seconds, share, ledger.cases.get(module, 0)))
    if len(ranked) > top:
        rest = sum(seconds for _, seconds in ranked[top:])
        lines.append("  其余 %d 个模块合计 %.1fs" % (len(ranked) - top, rest))
    return lines


def durations_report(ledger, wall, under_coverage):
    """落盘用的结构化明细，字段含义与上面的摘要一致。"""
    return {"schema": DURATIONS_SCHEMA, "under_coverage": bool(under_coverage),
            "wall_seconds": round(wall, 3), "case_seconds": round(ledger.total(), 3),
            "modules": [{"module": module, "seconds": round(seconds, 3), "cases": ledger.cases.get(module, 0)}
                        for module, seconds in ledger.ranked()]}


def write_durations(path, report):
    """先写同目录临时文件再整体替换。返回 ''，写失败返回原因：落盘失败不得改变测试结论。"""
    temp = None
    try:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        handle, temp = tempfile.mkstemp(prefix=".durations-", suffix=".tmp", dir=directory)
        with os.fdopen(handle, "w", encoding="utf-8") as sink:
            sink.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        os.replace(temp, path)
        return ""
    except OSError as exc:
        if temp is not None:
            with contextlib.suppress(OSError):
                os.unlink(temp)
        return "%s: %s" % (type(exc).__name__, exc)


def gc_at_test_boundary():
    """车道模式专用：关掉自动垃圾回收，只在每条用例收尾（已脱离事件循环）时手动回收一次。

    为什么：把测试模块分到不同进程后，模块之间的相邻关系变了；实测 test_bugfix_155 排在
    test_coverage_window_flows 前面时，自动回收恰好落在 Qt 切换激活窗口、应用级事件过滤器
    正在包装对象的那一刻，主线程 SIGSEGV（关闭自动回收后同一组合 4/4 通过，回收放到用例之间
    则 3/3 通过）。这是缓解而不是根治，所以只在显式设了 SF_GC_AT_TEST_BOUNDARY=1 时生效。
    """
    return os.environ.get("SF_GC_AT_TEST_BOUNDARY", "") == "1"


class _Result(unittest.TextTestResult):
    """给每个用例挂超时：到点 dump 全线程栈并退出进程。"""

    def __init__(self, *a, **kw):
        self._timeout = kw.pop("sf_timeout", 60.0)
        self._ledger = kw.pop("sf_ledger", None)
        self._gc_boundary = gc_at_test_boundary()
        super().__init__(*a, **kw)

    def startTest(self, test):
        _current_case[0] = test.id()
        if self._timeout > 0:
            # exit=True：超时后打印栈并 os._exit，避免整轮无限挂起
            faulthandler.dump_traceback_later(self._timeout, exit=True)
        if self._ledger is not None:
            self._ledger.start(test.id())
        super().startTest(test)

    def stopTest(self, test):
        if self._timeout > 0:
            faulthandler.cancel_dump_traceback_later()
        if self._ledger is not None:
            self._ledger.stop(test.id())
        super().stopTest(test)
        if self._gc_boundary:
            gc.collect()


def verdict(failures, flaky, ui_violations, dirty):
    """退出码的**唯一**来源：2 用户数据被改（红线）＞ 1 失败/错误/弹窗违规 ＞ 0 全绿。

    为什么抽出来：这条判定决定 run_tests.sh 那句"可进行打包发布"能不能打印，
    原先却埋在 main 一长串副作用里，没有任何东西证明它不会把红线读成绿灯
    （台账 G 族那类失效：一个永不失败的检查等于没有检查）。抽成纯函数后
    --self-test 能直接喂各种组合，包括"数据被改 + 同时有失败"这种优先级冲突。
    """
    if dirty:
        return 2
    if failures or ui_violations:
        return 1
    return 0


def parse_seed(raw):
    """SF_RANDOM_SEED 只接受整数，写错时给一句人话而不是 ValueError 栈。

    实测踩过（2026-10-09）：seed 写成 20261008b，int() 当场 ValueError，
    整轮以一句 traceback 收场，而后台任务通知仍然报 exit 0 —— 排查的人很容易
    把它当成"测试挂了"，其实是自己的参数敲错了。
    """
    text = str(raw if raw is not None else "").strip()
    if text in ("", "0"):
        return 0
    try:
        return int(text)
    except ValueError:
        raise SystemExit("SF_RANDOM_SEED 必须是整数（随机序要能复现），收到 %r" % (raw,))


def self_test():
    """运行器的判定层自检：每种"该红/该绿/该报警"的形状都必须落对。

    刻意不跑真用例（那会把自检变成第二次全量）：这里验的是**结论怎么算**，
    超时、随机序、覆盖率那些执行期机制由各自的用例与发版链关卡守。
    """
    bad = []
    # ---- verdict：优先级与语义 ----
    if verdict([], [], [], []) != 0:
        bad.append("全绿却给了非 0")
    if verdict([], [("t", "抖了一下")], [], []) != 0:
        bad.append("只有 flaky 应该给 0（重跑过了就不算失败），现在把它判成红了")
    if verdict([("t", "boom")], [], [], []) != 1:
        bad.append("有失败却给了 0 —— 这枚闸门废了")
    if verdict([], [], [("t", "osascript display dialog")], []) != 1:
        bad.append("桌面弹窗违规没算进失败（用户又会凭空看见一个框）")
    if verdict([], [], [], ["config.json：大小 93->93"]) != 2:
        bad.append("用户数据被改动必须给 2（红线），且优先级最高")
    if verdict([("t", "boom")], [], [], ["debug.log 变了"]) != 2:
        bad.append("数据红线被失败盖掉了 —— 优先级顺序错")
    # ---- _diff_user_data：AGENTS §6.11 那条真实事故 ----
    same = {"config.json": (93, 1000), "securities.json": (100, 2000), "debug.log": (5000, 3000)}
    if _diff_user_data(same, dict(same)):
        bad.append("什么都没变却报了用户数据被改动（红狼来了）")
    moved = {"config.json": (93, 1000), "securities.json": (100, 2000), "debug.log": (5000, 3000)}
    after = {"config.json": (93, 1777), "securities.json": (100, 2000), "debug.log": (5000, 3000)}
    diff = _diff_user_data(moved, after)
    if not diff:
        bad.append("mtime 变了但大小没变 —— 正是历史上被漏判的那次（x/y 639,348→100,100，"
                   "位数一样，肉眼看不出），必须报警")
    if _diff_user_data({"config.json": None}, {"config.json": (10, 20)}):
        pass
    if not _diff_user_data({"debug.log": None}, {"debug.log": (77, 88)}):
        bad.append("测试期间新建了用户日志文件却没报")
    if not _diff_user_data({"securities.json": (77, 88)}, {"securities.json": None}):
        bad.append("测试期间删掉了用户自选表却没报（最坏的一种污染）")
    extra = _diff_user_data({"other.json": (1, 1)}, {"other.json": (2, 2)})
    if extra:
        bad.append("清单外的文件被当成用户数据报红，会让人开始忽略这条红线")
    # ---- 桌面弹窗拦截的识别口径 ----
    if describe_ui_command(["osascript", "-e", 'display dialog "软件更新"']) is None:
        bad.append("osascript display dialog 没被认成弹窗违规（用户桌面会被砸窗口）")
    if describe_ui_command("osascript -e 'display notification body'") is None:
        bad.append("display notification 没被拦（同类污染）")
    if describe_ui_command([sys.executable, "-c", "print(1)"]) is not None:
        bad.append("把普通子进程误判成弹窗 —— 真链路子进程用例会全红")
    if describe_ui_command(["git", "status"]) is not None:
        bad.append("git 被误判成弹窗")
    if describe_ui_command(None) is not None:
        bad.append("None 命令应当返回 None，而不是崩或误报")
    long_cmd = describe_ui_command(["osascript", "-e", "display dialog " + "正文" * 200])
    if not long_cmd or len(long_cmd) > 260:
        bad.append("超长命令的摘要没截断（日志会被一条脚本源码灌满）")
    # ---- seed 解析：那条把自己坑过的 ValueError ----
    if parse_seed(None) != 0 or parse_seed("0") != 0 or parse_seed("") != 0:
        bad.append("空/0 的 seed 应当退化成 0（自动取时）")
    if parse_seed("20261009") != 20261009:
        bad.append("正常 seed 没解析出来")
    try:
        parse_seed("20261008b")
        bad.append("非法 seed 竟然安静通过")
    except SystemExit as exc:
        if "SF_RANDOM_SEED" not in str(exc):
            bad.append("非法 seed 的提示没说清是哪个环境变量")
    # ---- 红线归因：只数新增部分、只认固定类别，轮转/缺失不冒充 0 条 ----
    # 每条判据只产出一个布尔值，汇总时用一条 bad.extend：改动行覆盖关卡要求每行被执行，
    # 写成 12 个 "if …: bad.append(…)" 的话，失败分支在绿的那一轮里永远不执行，预判会如实报漏。
    bad.extend(label for label, ok in _activity_checks().items() if not ok)
    for line in bad:
        print("FAIL", line)
    if bad:
        return 1
    print("self-test 通过：退出码 5 种优先级、用户数据 5 种形状（含大小不变 mtime 变）、"
          "弹窗识别 6 种命令、seed 解析 4 种输入、红线归因 9 种形状全部判对")
    return 0


def module_name(value):
    name = value.removeprefix("tests.")
    if not re.fullmatch(r"test_[A-Za-z0-9_]+", name):
        raise argparse.ArgumentTypeError("expected test_module or tests.test_module")
    return name


@contextlib.contextmanager
def focused_environment():
    # Short paths also keep the single-instance Unix socket below macOS limits.
    with tempfile.TemporaryDirectory(prefix="sf_dt_", dir="/tmp" if os.path.isdir("/tmp") else None) as directory:
        overrides = {name: os.path.join(directory, filename) for name, filename in (
            ("SF_CONFIG_PATH", "config.json"), ("SF_LAST_QUOTE_PATH", "quote.json"),
            ("SF_UNIVERSE_PATH", "universe.json"), ("SF_LOG_PATH", "debug.log"),
            ("SF_SINGLE_SOCK", "single.sock"))}
        overrides.update(SF_UNIVERSE_DISABLE="1", SF_SEED_FIRST_SCREEN="0")
        previous = {name: os.environ.get(name) for name in overrides}
        os.environ.update(overrides)
        try:
            yield directory
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def list_modules(out_path, tests_dir=None):
    """只发现、不运行：把 {模块: 用例数} 写成 JSON，供车道总指挥分配与核对总数。

    导入失败的模块在 discover 里会变成 `unittest.loader._FailedTest`，模块名取它的
    `_testMethodName`（即出错的模块名），这样它仍按名字出现在清单里，由真正跑它的车道报红。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tests_dir = tests_dir or os.path.join(root, "tests")
    sys.path.insert(0, root)
    sys.path.insert(0, tests_dir)
    _preimport_project_modules()
    # 按**文件**逐个加载，与 `--module` 完全同一条加载路径。不能按用例 id 里的模块名归类：
    # 测试类被别的文件 import 进来时，同一批用例会在每个导入它的文件里各收集一遍，
    # 按 id 归类会把它们记到定义它的那个文件头上，车道按文件分配时账就对不上了。
    loader = unittest.TestLoader()
    counts = {}
    for path in sorted(pathlib.Path(tests_dir).glob("test_*.py")):
        counts[path.stem] = loader.loadTestsFromName(path.stem).countTestCases()
    problem = write_durations(out_path, {"schema": 1, "modules": counts, "total": sum(counts.values())})
    if problem:
        print("!! 模块清单没写成：" + problem)
        return 2
    print("模块清单已写入 %s（%d 个模块，%d 条用例）" % (out_path, len(counts), sum(counts.values())))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--self-test", action="store_true")
    action.add_argument("--module", action="append", type=module_name,
                        help="Run only this test module; repeat to combine (not the full gate)")
    action.add_argument("--list-json", metavar="PATH",
                        help="Discover every test module, write {module: case count} as JSON, run nothing")
    args = parser.parse_args(argv)
    if args.self_test:
        # 必须在任何发现/预导入之前分流，否则它会把整套用例再跑一遍（实测 88 秒）
        return self_test()
    if args.list_json:
        with focused_environment():
            return list_modules(args.list_json)
    if args.module:
        with focused_environment():
            return run_tests(args.module)
    return run_tests()


def run_tests(modules=None):
    boundary = gc_at_test_boundary()
    was_enabled = gc.isenabled()
    if boundary:
        gc.disable()
    try:
        return _run_tests(modules)
    finally:
        if boundary and was_enabled:
            gc.enable()


def _run_tests(modules=None):
    timeout = float(os.environ.get("SF_TEST_TIMEOUT", "60") or 0)
    random_order = os.environ.get("SF_RANDOM_ORDER", "0") == "1"
    rerun = os.environ.get("SF_RERUN_FAILED", "1") == "1"
    seed = parse_seed(os.environ.get("SF_RANDOM_SEED"))

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)
    tests_dir = os.path.join(root, "tests")
    sys.path.insert(0, tests_dir)

    emit_activity_hint()
    before = _snapshot_user_data()
    log_start = user_log_state("debug.log")
    install_ui_dialog_guard()
    _preimport_project_modules()

    loader = unittest.TestLoader()
    # top_level_dir 必须与原 `unittest discover -s tests` 保持一致（即 tests 自身），
    # 否则模块名会从 test_x 变成 tests.test_x，导致 `from sf_xxx import` 之类的
    # 顶层导入全部失败。tests/ 目录下没有 __init__.py，靠的就是这层目录在 sys.path 上。
    if modules:
        names = list(dict.fromkeys(modules))
        suite = loader.loadTestsFromNames(names)
        print("== 定向模块测试（不代表完整关卡，不写关卡戳）==")
    else:
        suite = loader.discover(start_dir="tests", pattern="test_*.py",
                                top_level_dir=tests_dir)
    cases = list(suite)
    if random_order:
        rnd = random.Random(seed or time.time_ns())
        suite = _shuffle_top(suite, rnd)
        cases = list(suite)
        # 随机序多为分mur诊断，下面不再重跑（顺序变了重跑结果不可比）
        rerun = False
        print("== 随机执行顺序（模块级，seed=%s）==" % (seed or "auto"))
    else:
        # Qt 退出生命周期用例会构造真实窗口与后台线程；coverage 会放大此前 UI
        # 模块留下的延迟销毁时序。将它固定放在全量的最前面，保证验证的是自身
        # 收口，而不是前序窗口残留。随机诊断模式保留原始洗牌语义。
        quit_cases = [case for case in cases if any(
            test.id().startswith("test_quit_lifecycle.")
            for test in _flatten(case))]
        other_cases = [case for case in cases if case not in quit_cases]
        if quit_cases:
            cases = quit_cases + other_cases
            print("== 退出生命周期用例优先执行（隔离 Qt 原生资源）==")
    total = sum(t.countTestCases() for t in cases)
    if total == 0:
        print("!! 没有加载到测试，拒绝将零用例记为通过")
        return 2

    ledger = DurationLedger()
    runner = unittest.TextTestRunner(
        verbosity=2, resultclass=lambda *a, **kw: _Result(
            *a, sf_timeout=timeout, sf_ledger=ledger, **kw))
    main_began = time.monotonic()
    result = runner.run(unittest.TestSuite(cases))
    main_wall = time.monotonic() - main_began
    failures = list(result.failures) + list(result.errors)

    flaky = []
    if failures and rerun:
        print("\n== 失败用例自动重跑一次（区分真失败与抖动）==")
        retry_cases = [t for t, _ in failures]
        # 逐个重跑：一条独立执行一次，避免彼此污染导致误判
        retry_result = unittest.TextTestRunner(
            verbosity=1,
            resultclass=lambda *a, **kw: _Result(*a, sf_timeout=timeout, **kw)
        ).run(unittest.TestSuite(retry_cases))
        still = {t.id() for t, _ in retry_result.failures} | \
                {t.id() for t, _ in retry_result.errors}
        survivors = [(t, tb) for t, tb in failures if t.id() in still]
        flaky = [t for t, tb in failures if t.id() not in still]
        failures = survivors
        for t in flaky:
            print("  [flaky] %s —— 首次失败、重跑通过，说明用例不稳定（需单独治理）" % t.id())
        if flaky:
            print("== flaky %d 条，不计入失败，但必须排期修 ==" % len(flaky))

    after = _snapshot_user_data()
    dirty = _diff_user_data(before, after)

    print("\n== 汇总 ==")
    print("用例总数 %d｜失败/错误 %d｜flaky %d" % (total, len(failures), len(flaky)))
    popped = _report_ui_dialog_violations()
    if dirty:
        emit_dirty_detail(dirty, log_start)
    else:
        print("用户数据未改动（config / securities / debug.log / feedback）")
    # 耗时只是观测：放在判定之前打印，但不参与 verdict，写盘失败也不改变结论。
    under_coverage = coverage_active()
    for line in render_durations(ledger, main_wall, under_coverage):
        print(line)
    durations_out = os.environ.get("SF_TEST_DURATIONS_OUT", "").strip()
    if durations_out:
        problem = write_durations(durations_out, durations_report(ledger, main_wall, under_coverage))
        if problem:
            print("!! 耗时明细没写成（不影响本轮结论）：" + problem)
        else:
            print("耗时明细已写入 " + durations_out)
    # 退出码只有一个来源（verdict），这里不再自己判一遍：两处各写一份"什么算红"
    # 迟早会漂移，而这条判定决定 run_tests.sh 那句"可进行打包发布"能不能打印。
    # 结论字符串由 run_tests.sh 统一打印，此处不重复。
    code = verdict(failures, flaky, popped, dirty)
    # 车道总指挥靠这份结构化结果判定，不去解析打印出来的汇总文字（文字一改就悄悄失效）。
    lane_out = os.environ.get("SF_LANE_RESULT_OUT", "").strip()
    if lane_out:
        problem = write_durations(lane_out, {
            "schema": 1, "rc": code, "total": total,
            "failures": [t.id() for t, _ in failures], "flaky": [t.id() for t in flaky],
            "ui_violations": 1 if popped else 0, "dirty": [str(item) for item in (dirty or [])]})
        if problem:
            print("!! 车道结果没写成（总指挥会按缺结果判红）：" + problem)
    return code


def _exit_cleanly(code):
    """保存覆盖率后绕过 Qt/AppKit 的解释器退出阶段。

    离屏全量 UI 回归完成后，部分 PyQt/AppKit 对象会在 Python 析构顺序中竞争释放，
    覆盖率追踪开启时会放大为 SIGSEGV。测试已经结束且结果、用户数据校验均已产出，
    因此先显式保存 coverage 数据，再用 os._exit 跳过无业务价值的解释器卸载阶段。
    真机应用不走本文件，产品退出逻辑不受影响。
    """
    try:
        import coverage
        current = coverage.Coverage.current()
        if current is not None:
            current.save()
    except Exception:
        pass
    # os._exit 不经过解释器正常退出，**管道场景下 stdout 是全缓冲的**：
    # 不显式 flush，上面 "== 汇总 ==" 与弹窗违规报告会被直接丢掉
    # （`./run_tests.sh | tail` 时看到"只有 Ran/OK、没有汇总"就是这个原因）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(int(code))


if __name__ == "__main__":
    _exit_cleanly(main())
