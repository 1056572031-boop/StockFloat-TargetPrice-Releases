# -*- coding: utf-8 -*-
"""测试定义差集检查：这次要提交的内容相对 HEAD 不许悄悄丢测试定义。

成因：往测试文件插新用例时，编辑锚点取了旧用例的 def 行、新内容里没有重抄，
旧用例的函数头被顶掉，旧用例体并进上一条用例名下，旧用例名消失。旧体仍在跑，
整档照绿（2026-11-04、11-05 各出现一次），只有拿提交前后的测试定义清单做差集才看得出来。

判据：tests/ 下 test_*.py 里，HEAD 有、而即将提交的索引里没有的「路径::类.用例」，
一律拒绝并点名。确属有意删除、改名、搬家的，在 docs/test_removals.txt 新增一行
    路径::类.用例 | 理由          （整个文件写成  路径::* | 理由）
只认这次提交相对 HEAD 新增的行：旧声明不替以后的删除开脱，同一行文字重复使用也不算新增。
理由不足 MIN_REASON_CHARS 个字算格式错误。不按函数体相似度自动放行：放行必须是一句能被审阅的话。

失败从不当通过：git 起不来、读不出内容、暂存内容解析不了都判失败；
只有目录根本不是 git 仓库才跳过。合并提交按「两个父提交都丢了」才算丢。
--amend 只比较本次新增的那一段；撤销/摘取一个新增过用例的提交，需要写声明。
条件分支里的定义（if/try 下的 def）不收集，两边对称所以不会误报，也就拦不住。

钩子里必须继承钩子环境：GIT_INDEX_FILE 指向 git 此刻真要提交的那份索引。
洗掉 GIT_* 的读法在 commit -a 和路径提交上会错（探针实测 8 种场景只对 6 种）。
所以真实运行不洗环境；单测在临时仓库里跑时才传入洗过的 env。
"""
from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
import tempfile
from typing import NamedTuple

DECLARATION_FILE = "docs/test_removals.txt"   # docs/ 下未登记为对外的文件，公开快照导出时一律丢弃
TESTS_DIR = "tests"
MIN_REASON_CHARS = 6
GIT_TIMEOUT_SEC = 60


class Report(NamedTuple):
    status: str                      # ok | fail | skipped
    lost: tuple[str, ...] = ()       # 丢了且没有声明放行的「路径::类.用例」
    problems: tuple[str, ...] = ()   # 读不出 / 解析不了 / 声明格式错误
    released: tuple[str, ...] = ()   # 丢了但被本次新增的声明放行的
    note: str = ""


class GitError(Exception):
    """git 没起来、超时或返回非零：一律让上层判失败，绝不当成『没有差异』。"""


def collect_ids(source):
    """源码里的测试定义集合：顶层 test* 函数、类（含嵌套类）里的 test* 方法。

    解析失败返回 None（与『没有用例』的空集合区分开）。函数里再嵌套的 def 不算。
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    found: set[str] = set()

    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test"):
                    found.add(prefix + node.name)
            elif isinstance(node, ast.ClassDef):
                walk(node.body, prefix + node.name + ".")

    walk(tree.body, "")
    return found


def _git(root, args, env=None, check=True):
    """在 root 下跑一条 git，返回 (退出码, 标准输出字节)。env 为 None 时继承当前环境。"""
    child = dict(os.environ if env is None else env)
    child["GIT_OPTIONAL_LOCKS"] = "0"      # 只读检查不去顺手刷新索引
    try:
        done = subprocess.run(["git", *args], cwd=root, env=child, capture_output=True,
                              timeout=GIT_TIMEOUT_SEC, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError("git %s 没能运行：%s" % (" ".join(args[:2]), exc)) from exc
    if check and done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise GitError("git %s 退出码 %d：%s" % (" ".join(args[:2]), done.returncode, detail))
    return done.returncode, done.stdout


def _base_refs(root, env):
    """本次提交的比较基线：[HEAD]、合并中的 [HEAD, MERGE_HEAD]、或还没有提交的 []。"""
    code, _ = _git(root, ["rev-parse", "--verify", "-q", "HEAD^{commit}"], env, check=False)
    if code == 1:
        # 1 有两种：分支上还没有提交（正常），或 HEAD 指向的提交读不出来（仓库坏了）
        sym, _ = _git(root, ["symbolic-ref", "-q", "HEAD"], env, check=False)
        if sym != 0:
            raise GitError("HEAD 指向的提交读不出来，仓库可能已损坏")
        return []
    if code != 0:
        raise GitError("git rev-parse HEAD 退出码 %d，无法确定比较基线" % code)
    # MERGE_HEAD 缺失、乱码、指向不存在的对象，rev-parse 都只返回 1（实测）。读不出就只比 HEAD：
    # 合并时的丢失集合是「对 HEAD 的丢失」的子集，所以只比 HEAD 只会更严，不会放过丢失。
    merge, _ = _git(root, ["rev-parse", "--verify", "-q", "MERGE_HEAD^{commit}"], env, check=False)
    return ["HEAD", "MERGE_HEAD"] if merge == 0 else ["HEAD"]


def _changed_tests(root, base, env):
    """索引相对 base 有改动的测试文件：[(状态 A/D/M/T, 路径)]。关掉改名检测，改名按删+增算。"""
    _, out = _git(root, ["diff", "--cached", "--name-status", "--no-renames", "-z",
                         "--diff-filter=ADMT", base, "--", TESTS_DIR], env)
    tokens = out.split(b"\0")
    pairs = []
    for status, path in zip(tokens[0::2], tokens[1::2]):
        name = os.fsdecode(path)
        if name.endswith(".py") and os.path.basename(name).startswith("test_"):
            pairs.append((status.decode("ascii", "replace")[:1], name))
    return pairs


def _losses_against(root, base, env, problems):
    """索引相对 base 丢掉的「路径::用例」。暂存内容解析不了记入 problems，基线本身解析不了就不比。"""
    lost: set[str] = set()
    for status, path in _changed_tests(root, base, env):
        if status == "D":
            fresh = set()
        else:
            _, staged = _git(root, ["cat-file", "blob", ":" + path], env)
            fresh = collect_ids(staged)
            if fresh is None:
                problems.append("%s 暂存的内容解析不了（语法错误），无法核对测试定义" % path)
                continue
        if status == "A":
            continue
        _, old_source = _git(root, ["cat-file", "blob", "%s:%s" % (base, path)], env)
        old = collect_ids(old_source)
        if old is not None:
            lost.update("%s::%s" % (path, ident) for ident in old - fresh)
    return lost


def _declared_lines(root, ref, env):
    """某个提交里 test_removals.txt 的行（去首尾空白）；那个提交里没有该文件就是空集合。"""
    _, listed = _git(root, ["ls-tree", "--name-only", "-z", ref, "--", DECLARATION_FILE], env)
    if not listed:
        return set()
    _, text = _git(root, ["cat-file", "blob", "%s:%s" % (ref, DECLARATION_FILE)], env)
    return {line.strip() for line in text.decode("utf-8", "replace").splitlines()}


def _read_declarations(root, env, bases):
    """(放行的『路径::用例』或『路径::*』集合, 格式问题列表)。只收相对所有基线新增的行。"""
    _, listed = _git(root, ["ls-files", "--cached", "-z", "--", DECLARATION_FILE], env)
    if not listed:
        return set(), []
    _, staged = _git(root, ["cat-file", "blob", ":" + DECLARATION_FILE], env)
    old = set()
    for base in bases:
        old |= _declared_lines(root, base, env)
    allowed, problems = set(), []
    for number, raw in enumerate(staged.decode("utf-8", "replace").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line in old:
            continue
        key, bar, reason = (part.strip() for part in line.partition("|"))
        if bar and "::" in key and len(reason) >= MIN_REASON_CHARS:
            allowed.add(key)
        else:
            problems.append("%s 第 %d 行格式不对（要写『路径::类.用例 | 至少 %d 个字的理由』）：%s"
                            % (DECLARATION_FILE, number, MIN_REASON_CHARS, line))
    return allowed, problems


def _is_declared(allowed, lost_id):
    return lost_id in allowed or lost_id.split("::", 1)[0] + "::*" in allowed


def find_losses(root, env=None):
    """核对 root 仓库里『即将提交的索引』相对 HEAD 丢了哪些测试定义。env 为 None 时继承当前环境。"""
    root = os.fspath(root)
    routed = (os.environ if env is None else env).get("GIT_DIR")
    if not routed and not os.path.exists(os.path.join(root, ".git")):
        return Report("skipped", note="%s 不是 git 仓库（没有 .git），跳过测试定义差集检查" % root)
    problems: list[str] = []
    try:
        bases = _base_refs(root, env)
        if not bases:
            return Report("ok", note="分支上还没有提交，没有可比较的基线")
        lost = set.intersection(*[_losses_against(root, base, env, problems) for base in bases])
        allowed, declaration_problems = _read_declarations(root, env, bases)
    except GitError as exc:
        return Report("fail", problems=(str(exc),))
    released = sorted(ident for ident in lost if _is_declared(allowed, ident))
    remaining = sorted(lost.difference(released))
    problems = list(dict.fromkeys(problems + declaration_problems))
    status = "fail" if remaining or problems else "ok"
    return Report(status, tuple(remaining), tuple(problems), tuple(released))


def _render(report, out):
    if report.status == "skipped":
        print("== 测试定义差集：跳过（%s）==" % report.note, file=out)
        return
    if report.released:
        print("== 测试定义差集：按 %s 新增的声明放行 %d 条 ==" % (DECLARATION_FILE, len(report.released)),
              file=out)
        for ident in report.released:
            print("   已声明移除：%s" % ident, file=out)
    if report.status == "ok":
        print("== 测试定义差集：通过（即将提交的内容没有悄悄丢测试定义）==", file=out)
        return
    print("!! 测试定义差集：未通过", file=out)
    for ident in report.lost:
        print("   丢失：%s" % ident, file=out)
    if report.lost:
        print("   常见成因：编辑锚点取了旧用例的 def 行、新内容里没有重抄，旧用例名被顶掉而旧体仍在跑。", file=out)
        print("   不是有意的：把这些用例的定义补回去。", file=out)
        print("   是有意删除/改名/搬家：在 %s 新增一行『路径::类.用例 | 至少 %d 个字的理由』，"
              % (DECLARATION_FILE, MIN_REASON_CHARS), file=out)
        print("   整个文件写『路径::* | 理由』，并把该文件一起提交；只认本次相对 HEAD 新增的行。", file=out)
    for problem in report.problems:
        print("   问题：%s" % problem, file=out)


def self_test(out=None):
    """自检：在临时 git 仓库里种事故形状，本工具必须报出来。返回 0 通过、1 未通过。"""
    out = sys.stdout if out is None else out
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    path = TESTS_DIR + "/test_sample.py"
    lost = (path + "::S.test_b",)
    base = "class S:\n    def test_a(self):\n        pass\n\n    def test_b(self):\n        pass\n"
    eaten = "class S:\n    def test_a(self):\n        pass\n\n        pass\n"
    ident = ["-c", "user.email=gate@example.invalid", "-c", "user.name=gate", "-c", "commit.gpgsign=false"]
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="sf_def_diff_selftest_") as root:
        def stage(rel, text):
            target = os.path.join(root, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(text)
            _git(root, ["add", "--", rel], env)

        def check(label, status, want_lost=(), want_released=()):
            got = find_losses(root, env=env)
            if (got.status, got.lost, got.released) != (status, want_lost, want_released):
                problems.append("%s：得到 %s，应为 %s" % (label, (got.status, got.lost, got.released),
                                                      (status, want_lost, want_released)))

        try:
            _git(root, ["init", "-q"], env)
            stage(path, base)
            _git(root, [*ident, "commit", "-qm", "base"], env)
            check("没动测试", "ok")
            stage(path, eaten)
            check("def 行被顶掉", "fail", lost)
            stage(DECLARATION_FILE, "%s | 自检：有意移除并写明理由\n" % lost[0])
            check("新增声明放行", "ok", (), lost)
            stage(path, base)
            _git(root, [*ident, "commit", "-qm", "declare"], env)
            stage(path, eaten)
            check("旧声明不替新删除开脱", "fail", lost)
        except GitError as exc:
            problems.append("自检仓库没能建起来：%s" % exc)
    if problems:
        print("!! 测试定义差集自检未通过", file=out)
        for line in problems:
            print("   %s" % line, file=out)
        return 1
    print("== 测试定义差集：自检通过（没动、事故形状、新增声明、旧声明共 4 种场景）==", file=out)
    return 0


def main(argv=None, out=None, env=None):
    parser = argparse.ArgumentParser(
        prog="test_def_diff.py",
        description="核对即将提交的索引相对 HEAD 有没有悄悄丢掉 tests/ 下的测试定义。"
                    "退出码：0 通过或跳过，1 未通过，2 参数错误。")
    parser.add_argument("--root", default=".", help="仓库工作树根目录（默认当前目录；钩子里就是提交所在目录）")
    parser.add_argument("--self-test", action="store_true", help="变异自检（只碰临时目录）")
    args = parser.parse_args(argv)
    out = sys.stdout if out is None else out
    if args.self_test:
        return self_test(out)
    report = find_losses(args.root, env=env)
    _render(report, out)
    return 1 if report.status == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
