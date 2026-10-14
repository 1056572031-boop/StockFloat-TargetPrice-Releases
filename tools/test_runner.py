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

退出码：0 全绿；1 存在失败/错误/桌面弹窗违规；2 用户数据被改动（最高优先级，不算"测试问题"）。
"""
import faulthandler
import os
import random
import sys
import time
import unittest


def _support_dir():
    return os.path.expanduser(
        "~/Library/Application Support/StockFloatTargetPrice")


# 用户数据清单：这些是**用户的东西**，测试再脏也不能碰。
# debug.log 自 v1.3.67 起已由 SF_LOG_PATH 隔离，留在这里是双保险：
# 将来谁加了一条没隔离的写盘路径，这里立刻报警。
_USER_DATA = ["config.json", "securities.json", "debug.log"]


def _snapshot_user_data():
    snap = {}
    for name in _USER_DATA:
        p = os.path.join(_support_dir(), name)
        try:
            st = os.stat(p)
        except OSError:
            snap[name] = None
        else:
            snap[name] = (st.st_size, int(st.st_mtime))
    return snap


def _diff_user_data(before, after):
    out = []
    for name in _USER_DATA:
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


class _Result(unittest.TextTestResult):
    """给每个用例挂超时：到点 dump 全线程栈并退出进程。"""

    def __init__(self, *a, **kw):
        self._timeout = kw.pop("sf_timeout", 60.0)
        super().__init__(*a, **kw)

    def startTest(self, test):
        _current_case[0] = test.id()
        if self._timeout > 0:
            # exit=True：超时后打印栈并 os._exit，避免整轮无限挂起
            faulthandler.dump_traceback_later(self._timeout, exit=True)
        super().startTest(test)

    def stopTest(self, test):
        if self._timeout > 0:
            faulthandler.cancel_dump_traceback_later()
        super().stopTest(test)


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
    for line in bad:
        print("FAIL", line)
    if bad:
        return 1
    print("self-test 通过：退出码 5 种优先级、用户数据 5 种形状（含大小不变 mtime 变）、"
          "弹窗识别 6 种命令、seed 解析 4 种输入全部判对")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        # 必须在任何发现/预导入之前分流，否则它会把整套用例再跑一遍（实测 88 秒）
        return self_test()
    timeout = float(os.environ.get("SF_TEST_TIMEOUT", "60") or 0)
    random_order = os.environ.get("SF_RANDOM_ORDER", "0") == "1"
    rerun = os.environ.get("SF_RERUN_FAILED", "1") == "1"
    seed = parse_seed(os.environ.get("SF_RANDOM_SEED"))

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)
    tests_dir = os.path.join(root, "tests")
    sys.path.insert(0, tests_dir)

    install_ui_dialog_guard()
    _preimport_project_modules()

    loader = unittest.TestLoader()
    # top_level_dir 必须与原 `unittest discover -s tests` 保持一致（即 tests 自身），
    # 否则模块名会从 test_x 变成 tests.test_x，导致 `from sf_xxx import` 之类的
    # 顶层导入全部失败。tests/ 目录下没有 __init__.py，靠的就是这层目录在 sys.path 上。
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

    before = _snapshot_user_data()
    runner = unittest.TextTestRunner(
        verbosity=2, resultclass=lambda *a, **kw: _Result(*a, sf_timeout=timeout, **kw))
    result = runner.run(unittest.TestSuite(cases))
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
        print("!! 用户数据在测试期间被改动 —— 这不是测试问题，是红线：")
        for line in dirty:
            print("   - %s" % line)
    else:
        print("用户数据未改动（config / securities / debug.log）")
    # 退出码只有一个来源（verdict），这里不再自己判一遍：两处各写一份"什么算红"
    # 迟早会漂移，而这条判定决定 run_tests.sh 那句"可进行打包发布"能不能打印。
    # 结论字符串由 run_tests.sh 统一打印，此处不重复。
    return verdict(failures, flaky, popped, dirty)


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
