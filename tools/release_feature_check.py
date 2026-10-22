#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二道防线：发版链上的"本轮特征"闸门 —— 逼产物核验真的核到本轮改动。

用法：
    ./.venv/bin/python tools/release_feature_check.py <app路径>
    ./.venv/bin/python tools/release_feature_check.py <app> --checks release_checks.txt
    ./.venv/bin/python tools/release_feature_check.py <app> --self-test

它只是 release_checks.txt 的**读表员 + 硬闸门**，字节码核验仍由
tools/verify_dmg_pyz.py 做（单一实现，不在这里复制一遍）。

为什么必须有这一层（否则等于没有）：
    verify_dmg_pyz.py 设计得很好：不传 --check/--forbid 时它会**如实降级结论**
    （"本轮未提供特征，不能证明本次修复在包内"）。但降级结论仍是 exit 0，
    放进脚本里就是一道永远过去的关。v1.4.17 之前它写死一批 v1.2.63 的特征，
    十几个版本每次无脑 PASS —— 一个永不失败的检查等于没有检查。
    本工具把这一步变成**必须有内容**：
      · release_checks.txt 里没有 `version` 行，或版本号不是本轮
        `sf_constants.APP_VERSION` → 拒绝出包（防止"上一轮的特征被当成本轮验收"）；
      · 一条 check/forbid 都没有 → 拒绝出包；
      · 特征没命中 → 非零退出（打包失败）。
    于是"发版前把本轮特征填进去"从人的记性变成了脚本的强制。

填什么特征（写清单时的判据）：
    · `check 模块::符号` —— 本轮**新加或改名**的函数/常量名。注意只认
      `co_names`：模块级赋值、def、被调用的名字都算；**纯字符串字面量不算**
      （`feature_enabled("system_drag")` 里的开关名查不到，要查它的
      使用点符号如 `_sys_drag`）。
    · `forbid 模块::符号` —— 本轮删掉的东西，用来证明"旧实现确实不在了"。
    · 至少一条要落在**本轮改动所在的那个文件**上：只填一个到处都有的通用符号，
      等于把闸门又变回永远通过。
    · `testref tests/文件.py::用例名`（或 `::类名::用例名`）—— 登记"覆盖这处改动的测试"。
      用在包内拿不出新名字的模块（只改了函数体或字面量）：以前只能写成清单里的注释，
      闸门读不到，测试改名或删掉后注释照样好看。它只证明用例**仍在源码里**、以 test 开头、
      没被无条件 skip；不证明通过（通过由同轮完整回归证明），也**不算**本轮特征，
      不能替 check/forbid 过关或充当新名字。
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import verify_dmg_pyz as VD          # noqa: E402

DEFAULT_CHECKS = ROOT / "release_checks.txt"


def parse_checks(path: pathlib.Path) -> dict:
    """读清单 -> {"version", "check", "forbid", "checkfile", "forbidfile", "errors", "path"}。"""
    # 显式标 dict[str, Any]：不标的话 mypy 会把值类型并成 `str | list | None`，
    # 于是 out["check"].append(...) 全被判成"在 str 上调 append"（tools/ 进类型层
    # 后实测 10 条）。这不是误报要豁免，是这里本来就该写清楚。
    out: dict = {"version": None, "check": [], "forbid": [],
                 "checkfile": [], "forbidfile": [], "stale": [], "testref": [],
                 "errors": [], "path": str(path)}
    # 取成局部具名列表再 append：直接写 out["check"].append(...) 时，
    # mypy 会把字典值类型并成 `str | list | None`，于是每条 append 都成报错。
    check: list[str] = out["check"]
    forbid: list[str] = out["forbid"]
    checkfile: list[str] = out["checkfile"]
    forbidfile: list[str] = out["forbidfile"]
    stale: list[str] = out["stale"]
    testref: list[str] = out["testref"]
    errors: list[str] = out["errors"]
    if not path.exists():
        errors.append("特征清单不存在：%s（发版必须提供本轮特征）" % path)
        return out
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        key = parts[0].lower()
        val = parts[1].strip() if len(parts) > 1 else ""
        if key == "version":
            if out["version"] is not None:
                errors.append("%s:%d 出现第二个 version 行" % (path.name, lineno))
            out["version"] = val or None
        elif key in ("check", "forbid", "checkfile", "forbidfile"):
            if "::" not in val:
                errors.append("%s:%d `%s` 需要 `模块::符号` 或 `包内路径::字串` 形式，收到 %r"
                                     % (path.name, lineno, key, val))
                continue
            bucket = {"check": check, "forbid": forbid,
                      "checkfile": checkfile, "forbidfile": forbidfile}[key]
            bucket.append(val)
        elif key == "testref":
            if "::" not in val:
                errors.append("%s:%d `testref` 需要 `tests/文件.py::用例名` 或 "
                              "`tests/文件.py::类名::用例名` 形式，收到 %r" % (path.name, lineno, val))
                continue
            testref.append(val)
        elif key == "stale-feature":
            stale.append(val)
        else:
            errors.append("%s:%d 不认识指令 %r（只支持 version/check/forbid/checkfile/"
                          "forbidfile/testref/stale-feature）" % (path.name, lineno, key))
    out["stale"] = stale
    return out


def _is_unconditional_skip(node: ast.AST) -> bool:
    """装饰器是不是无条件跳过（skip）。skipIf/skipUnless 是有条件的，不在此列。"""
    target = node.func if isinstance(node, ast.Call) else node
    name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
    return name == "skip"


def testref_problems(parsed: dict, root: "pathlib.Path | None" = None) -> list[str]:
    """核对 `testref` 登记的测试**仍然存在**，且不是占位或无条件跳过。

    为什么需要它：本轮有些模块只改了函数体或字面量，包内拿不出新名字，证据只能写成
    `release_checks.txt` 里的注释（"判据是 tests/xxx.py::test_yyy"）。注释闸门读不到，
    测试改名或删掉之后注释照样好看——一句不会过期报错的话，不是证据。
    `testref` 把它变成可读的一行，闸门去源码里确认那个用例还在。

    刻意收窄的边界（写在这里，免得被当成更强的证明）：
      · 只证明"登记的用例在源码里存在、是 test 开头的函数、没被无条件 skip"，
        **不证明它通过** —— 通过与否由同一轮的完整回归证明；
      · 它**不算**本轮特征：gate() 的"至少一条 check/forbid"和 judge_newness 的"新增名字"
        都不看它，所以不能拿来绕过那两道判据；
      · 只认 `tests/` 目录内的 .py，路径不许绝对、不许含 `..`。
    """
    base = (root or ROOT).resolve()
    tests_dir = base / "tests"
    problems: list[str] = []
    for spec in parsed.get("testref", []):
        parts = spec.split("::")
        if len(parts) not in (2, 3) or not all(p.strip() for p in parts):
            problems.append("testref %s：形状应为 `tests/文件.py::用例名` 或 `…::类名::用例名`" % spec)
            continue
        rel, names = parts[0].strip(), [p.strip() for p in parts[1:]]
        rel_path = pathlib.PurePosixPath(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts or rel_path.suffix != ".py" \
                or not rel_path.parts or rel_path.parts[0] != "tests":
            problems.append("testref %s：路径必须是 tests/ 下的相对 .py 文件，不含 `..`" % spec)
            continue
        target = (base / rel).resolve()
        if not target.is_relative_to(tests_dir) or not target.is_file():
            problems.append("testref %s：没有这个测试文件（%s）" % (spec, rel))
            continue
        try:
            tree = ast.parse(target.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            problems.append("testref %s：测试文件无法解析（%s）" % (spec, type(exc).__name__))
            continue
        found: "ast.FunctionDef | ast.AsyncFunctionDef | None" = None
        owner_skipped = False
        if len(names) == 1:
            scopes: list = [(tree, False)]
            scopes += [(c, any(_is_unconditional_skip(d) for d in c.decorator_list))
                       for c in tree.body if isinstance(c, ast.ClassDef)]
        else:
            scopes = [(c, any(_is_unconditional_skip(d) for d in c.decorator_list))
                      for c in tree.body if isinstance(c, ast.ClassDef) and c.name == names[0]]
        for scope, skipped in scopes:
            for node in scope.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == names[-1]:
                    found, owner_skipped = node, skipped
                    break
            if found is not None:
                break
        if found is None:
            problems.append("testref %s：文件在，但里面找不到这个用例（改名或删了？）" % spec)
        elif not names[-1].startswith("test"):
            problems.append("testref %s：%s 不是测试用例（名字须以 test 开头），辅助函数不算证据"
                            % (spec, names[-1]))
        elif owner_skipped or any(_is_unconditional_skip(d) for d in found.decorator_list):
            problems.append("testref %s：该用例被无条件 skip，跑不到的用例不是证据" % spec)
    return problems


_STALE_VER_RE = re.compile(r"^(\d+\.\d+\.\d+)\s+(.+)$", re.S)


def stale_declaration(parsed: dict, version: str) -> tuple[list[str], list[str], list[str]]:
    """这一轮有没有**书面申报**"包内拿不出新名字"，以及申报本身的问题。

    为什么需要这条通道：判据要求"至少一行是本轮的新名字"，拦的是"抄上一轮特征糊过去"。
    但确实存在正当的一轮 —— 改动全在打包资产与链上工具里，包内只动了版本号/阶段标签
    这类**字符串字面量**（co_names 天生看不见）。这时只有两种走法：
      · 编一条与本轮无关的旧符号填进去 —— 闸门当场变回"永远通过"，正是它当初要治的病；
      · 把"本轮没有包内新名字"写在受版本控制的清单里并附理由，运行时大声打 WARNING。
    这里选后者，且**理由不许为空**：不带理由的开关和 --skip-tests 一样，迟早被滥用。

    v1.4.64 补的一刀：申报必须写明**对哪一版**。release_checks.txt 是堆叠历史（和
    release_notes.txt 一样，旧版本那一段留在原地不动），于是不写版本的申报会被以后
    每一版继承 —— 1.4.64 这一轮实测撞上：包内五条新特征全部命中，闸门却照样打
    "本轮拿不出新名字"的 WARNING；而 main 里是 `not declared → 才跑新增特征判据`，
    等于清单里只要躺着一条无主申报，**以后任何一版都可以不填新名字过闸**。
    返回 (本轮生效的申报理由, 问题列表, 属于别的版本因而本轮不生效的申报)。
    """
    mine: list[str] = []
    other: list[str] = []
    problems: list[str] = []
    for idx, raw in enumerate(parsed.get("stale", []), 1):
        text = str(raw).strip()
        m = _STALE_VER_RE.match(text)
        if not m:
            problems.append(
                "第 %d 条 `stale-feature` 没写明对哪一版：%s —— 这份清单是堆叠历史，"
                "不写版本等于给以后每一版都免检。请写成 "
                "`stale-feature <版本号> <理由>`。" % (idx, text[:48]))
            continue
        ver, reason = m.group(1), m.group(2).strip()
        if not reason:
            problems.append("第 %d 条 `stale-feature` 后面没有理由：这一关可以绕，"
                            "但绕了必须写下为什么，否则它只是开关不是申报。" % idx)
            continue
        (mine if ver == version else other).append("%s：%s" % (ver, reason))
    # 返回清单本身而不是布尔：调用方要把理由原样打进发版日志
    return mine, problems, other


def current_version() -> str:
    """本轮版本号唯一来源：sf_constants.APP_VERSION（不硬编码）。"""
    import sf_constants
    return sf_constants.APP_VERSION


def gate(parsed: dict, want_version: str) -> list[str]:
    """返回**必须先解决**的问题清单；空表示可以进产物核验。"""
    problems = list(parsed["errors"])
    if not parsed["version"]:
        problems.append("特征清单缺 `version` 行：无法判断这份特征是为哪一轮写的。")
    elif parsed["version"] != want_version:
        problems.append(
            "特征清单的 version=%s，本轮 APP_VERSION=%s。"
            "要么清单还是上一轮的，要么版本号忘了同步 —— 两者都不能出包。"
            % (parsed["version"], want_version))
    if not (parsed["check"] or parsed["forbid"] or parsed["checkfile"] or parsed["forbidfile"]):
        problems.append(
            "特征清单里一条 check/forbid 都没有：没有本轮特征，"
            "产物核验就只能证明『结构存在』，证明不了『本次修复在包内』。")
    return problems


def resource_problems(app: str, parsed: dict) -> list[str]:
    """核验**包内资源文件**里的字串 —— shell 助手这类改动进不了字节码，只能这样查。

    为什么补这一层：3.6 这道闸门此前只会查 PYZ 字节码的 co_names。而
    `updater/update_helper.sh` 是随包带出去的**资源文件**（`StockFloat.spec` 的 datas），
    改得再关键也查不到符号；v1.4.58 修的正是它。没有这一层，本轮就只能填一条
    与改动无关的旧符号来糊过闸门 —— 那等于把这道防线又变回"每次无脑 PASS"。
    路径写的是 .app 内的相对路径，所以清单同时证明"改的是被打包的那一份"。
    """
    problems: list[str] = []
    root = pathlib.Path(app)
    for spec in parsed["checkfile"]:
        rel, _, needle = spec.partition("::")
        target = root / rel
        if not target.is_file():
            problems.append("checkfile %s：包内没有这个文件（%s）" % (spec, rel))
            continue
        if needle not in target.read_text(encoding="utf-8", errors="replace"):
            problems.append("checkfile %s：文件在包里，但本轮改动不在里面" % spec)
    for spec in parsed["forbidfile"]:
        rel, _, needle = spec.partition("::")
        target = root / rel
        if not target.is_file():
            problems.append("forbidfile %s：包内没有这个文件，无法证明旧写法已消失" % spec)
            continue
        if needle in target.read_text(encoding="utf-8", errors="replace"):
            problems.append("forbidfile %s：旧写法仍在包里" % spec)
    return problems
def _git_show_prev(rev: str, rel_path: str) -> "str | None":
    """读上一轮的同名文件；读不到返回 None（不抛）。"""
    import subprocess
    try:
        r = subprocess.run(["git", "show", "%s:%s" % (rev, rel_path)],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=20)
    except Exception as exc:
        print("   （git show 调用失败：%r）" % (exc,))
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def judge_newness(parsed: dict, prev_lines: set, prev_sources: dict) -> list[str]:
    """纯判据（可被 --self-test 直接喂样本）：新增行必须存在，且至少一行是新名字。

    `prev_lines` = 基线清单里的非注释行集合（基线怎么取见 prev_release_rev）；
    `prev_sources` = {模块名: 基线那一笔的该模块源码 or None（None=当时还没有这个文件）}。
    """
    cur = (["check " + c for c in parsed["check"]]
           + ["forbid " + f for f in parsed["forbid"]]
           # v1.4.59 合并：包内资源那一层（checkfile/forbidfile）也算本轮特征，
           # 否则"本轮只改了 shell 助手"那种正当发版会被这条判据拦死。
           + ["checkfile " + c for c in parsed.get("checkfile", [])]
           + ["forbidfile " + f for f in parsed.get("forbidfile", [])])
    added = [c for c in cur if c not in prev_lines]
    if not added:
        return ["特征清单与上一轮逐行相同：一条本轮新增都没有。"
                "填不出新增特征 = 这次改动没有任何可核验的产物证据。"]
    for line in added:
        spec = line.split(None, 1)[1]
        mod, _, sym = spec.partition("::")
        if not sym:
            continue
        src = prev_sources.get(mod, "")
        if src is None:
            return []          # 模块本身就是本轮新建的 → 它的任何符号都是新名字
        if sym not in src:
            return []
    return ["新增的行全是**旧符号**：%s —— co_names 对「只改了函数体」天生无感，"
            "这道关要的是本轮真实存在的新名字（新函数/新常量/改名后的旧函数）。"
            "确实拿不出新名字时，用 --allow-stale-feature 显式放行并说明原因。"
            % ", ".join(added[:3])]


def _git_lines(args: list[str]) -> "list[str] | None":
    """跑一条只读 git 命令，按行返回；失败返回 None（宁可判"读不到"，不猜一个值）。"""
    import subprocess
    try:
        r = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                           text=True, timeout=20)
    except Exception as exc:
        print("   （git %s 调用失败：%r）" % (" ".join(args), exc))
        return None
    if r.returncode != 0:
        return None
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def version_of(text: str) -> str:
    """从一份清单正文里取 `version X.Y.Z`；没有或读不懂一律返回空串。"""
    for ln in (text or "").splitlines():
        body = ln.split("#", 1)[0].strip()
        if body.startswith("version"):
            parts = body.split(None, 1)
            return parts[1].strip() if len(parts) > 1 else ""
    return ""


def prev_release_rev(rel: str, cur_version: str,
                     history=None, show=None) -> "str | None":
    """本轮的对照基线 = 清单里 version 与本轮**不同**的那一笔最近提交。

    为什么不是 HEAD~1（2026-10-11 实测：v1.4.62 发版被它连拦两次）：
    发版人先改清单（把本轮特征写进去），随后还要补逃逸台账、补交付记录 ——
    每多一笔提交，HEAD~1 就恰好是"已经含本轮特征的那一笔"，判据于是报
    "特征清单与上一轮逐行相同，本轮没有任何可核验证据"。**这句是假话**：
    特征在，只是不在最后一笔里。一道会说假话的红闸门，和一道永远绿的假闸门
    是同一种东西，只是方向相反。
    按 version 行往回找上一版清单，基线才和"本轮"这个概念对齐；
    找不到（清单是新文件、或不在 git 树里）返回 None，由调用方退回 HEAD~1
    并照旧如实打印"读不到"。
    """
    if not cur_version:
        return None
    if history is None:
        history = _git_lines(["log", "--format=%H", "--", rel])
        if history is None:
            return None
    if show is None:
        show = _git_show_prev
    for sha in history:
        text = show(sha, rel)
        if text is None:
            continue
        v = version_of(text)
        if v and v != cur_version:
            return sha
    return None


def newness_problems(parsed: dict, checks_path: pathlib.Path,
                     rev: str | None = None) -> list[str]:
    """S7 第一处：清单必须有**本轮新增**的一行，且那一行得真是新名字。

    旧判据只要求"version 对 + 至少一条 check"，于是把上一轮、上上轮的特征抄下来
    照样放行 —— 实测只填 1.4.46/1.4.48 的旧符号 + 本轮 version 就能过。
    清单累积到 60 条（58 唯一）时，这道关的证明力已经是 0。
    两条判据见 judge_newness（拆开是为了能被自检喂样本验"它真的会红"）。
    基线由 prev_release_rev 按"上一版清单"取，不是 HEAD~1 —— 理由写在那里面。
    """
    rel = "release_checks.txt"
    try:
        rel = str(checks_path.resolve().relative_to(ROOT))
    except Exception:
        pass
    cur_version = str(parsed.get("version") or "").strip()
    base = rev or prev_release_rev(rel, cur_version) or "HEAD~1"
    prev_text = _git_show_prev(base, rel)
    if prev_text is None:
        return ["读不到基线 %s 的 %s —— 无法判断本轮特征是否新增。"
                "（在无 git 的树里打包请显式 --allow-stale-feature，别让它静默放行）"
                % (base, rel)]
    prev_lines = {ln.split("#", 1)[0].strip() for ln in prev_text.splitlines()}
    mods = {spec.partition("::")[0]
            for spec in list(parsed["check"]) + list(parsed["forbid"])}
    prev_sources = {mod: _git_show_prev(base, "%s.py" % mod) for mod in mods}
    problems = judge_newness(parsed, prev_lines, prev_sources)
    if problems:
        # 把基线是谁写进结论：红的时候要知道它比的是哪一笔，否则又一次从头猜。
        problems = ["（特征基线 = %s，本轮 version=%s）%s"
                    % (base[:9], cur_version, problems[0])]
    return problems


def to_argv(app: str, parsed: dict, want_version: str) -> list[str]:
    argv = [app, "--version", want_version]
    for item in parsed["check"]:
        argv += ["--check", item]
    for item in parsed["forbid"]:
        argv += ["--forbid", item]
    return argv


def self_test() -> int:
    """闸门自己的负向验证：三种"没填对"必须全部被拒，填对了要放行到核验步骤。"""
    bad = []
    with tempfile.TemporaryDirectory() as td:
        # 不重新赋值 td：它是 TemporaryDirectory 给的 str，改名成 Path 会让
        # mypy 把后面每一次 `td / "x"` 都判成 str 上的除法（本轮就是这么新增的）。
        tmp_root = pathlib.Path(td)
        # 每条用例自带"本轮期望版本"，不再在循环里猜（复核 F5：
        # 原来这里写的是 `"9.9.9" if "0.0.1" not in body else "9.9.9"`，
        # 两个分支同一个值，读起来像在切换期望版本，实际什么都没做）。
        cases = [
            ("空文件", "", "9.9.9", ["缺 `version` 行", "一条 check/forbid 都没有"]),
            ("只有旧版本号", "version 0.0.1\ncheck sf_quote::fetch_quote\n", "9.9.9",
             ["本轮 APP_VERSION"]),
            ("有版本没特征", "version 9.9.9\n", "9.9.9", ["一条 check/forbid 都没有"]),
            ("指令写错", "version 9.9.9\nchek sf_quote::fetch_quote\n", "9.9.9",
             ["不认识指令"]),
            ("特征格式错", "version 9.9.9\ncheck sf_quote\n", "9.9.9",
             ["需要 `模块::符号`"]),
            ("版本号写对了但那是上一轮的", "version 1.4.32\ncheck sf_quote::fetch_quote\n",
             "1.4.33", ["version=1.4.32"]),
        ]
        for title, body, want, expect in cases:
            f = tmp_root / "checks.txt"
            f.write_text(body, encoding="utf-8")
            problems = " \n".join(gate(parse_checks(f), want))
            missing = [e for e in expect if e not in problems]
            if missing:
                bad.append("用例「%s」没被拒，缺少这些拒绝理由：%s（实际：%s）"
                           % (title, missing, problems or "无"))
        good = tmp_root / "good.txt"
        good.write_text("version 9.9.9\ncheck sf_quote::fetch_quote\n"
                        "forbid sf_glass::no_such_old_symbol\n", encoding="utf-8")
        parsed = parse_checks(good)
        if gate(parsed, "9.9.9"):
            bad.append("合规清单被误拒：%s" % gate(parsed, "9.9.9"))
        argv = to_argv("/tmp/x.app", parsed, "9.9.9")
        if argv != ["/tmp/x.app", "--version", "9.9.9",
                    "--check", "sf_quote::fetch_quote",
                    "--forbid", "sf_glass::no_such_old_symbol"]:
            bad.append("传给 verify_dmg_pyz 的参数拼装有误：%s" % argv)

        # ---- checkfile/forbidfile：包内资源文件那一层（v1.4.59 补）----
        # 种错必须被抓到，否则"本轮改的是 shell 资源"这句话又是空话。
        app = tmp_root / "Fake.app"
        helper = app / "Contents" / "Resources" / "updater" / "update_helper.sh"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text('for _name in "New.app" "Old.app"; do\n  echo "$_name"\ndone\n',
                          encoding="utf-8")
        p = tmp_root / "res_checks.txt"

        # 只填 checkfile（一条 PYZ 符号都没有）必须被认成"有本轮特征"：
        # v1.4.58 改的就是包内 shell，若这里还判"没有特征"，闸门会把一次正当发版拦死。
        p.write_text("version 9.9.9\ncheckfile Contents/Resources/updater/"
                     "update_helper.sh::New.app\n", encoding="utf-8")
        only_resource = parse_checks(p)
        if gate(only_resource, "9.9.9"):
            bad.append("只填 checkfile 被误判成『没有本轮特征』：%s" % gate(only_resource, "9.9.9"))

        for title, spec, expect_problem in (
                ("包内没有那个文件", "Contents/Resources/updater/missing.sh::New.app",
                 ["包内没有这个文件"]),
                ("文件在但字串不在", "Contents/Resources/updater/update_helper.sh::Nope",
                 ["本轮改动不在里面"])):
            p.write_text("version 9.9.9\ncheckfile %s\n" % spec, encoding="utf-8")
            found = " \n".join(resource_problems(str(app), parse_checks(p)))
            missing = [e for e in expect_problem if e not in found]
            if missing:
                bad.append("资源核验用例「%s」没被拒，缺理由：%s（实际：%s）"
                           % (title, missing, found or "无"))
        # forbidfile 指到旧字串仍在的位置 → 必须报
        p.write_text("version 9.9.9\ncheckfile Contents/Resources/updater/update_helper.sh::New.app\n"
                     "forbidfile Contents/Resources/updater/update_helper.sh::Old.app\n",
                     encoding="utf-8")
        found = " \n".join(resource_problems(str(app), parse_checks(p)))
        if "forbidfile" not in found:
            bad.append("forbidfile 抓不到『旧写法仍在包里』（实际：%s）" % (found or "无"))
        # 合规：新字串在、旧写法不在 → 必须全绿
        p.write_text("version 9.9.9\n"
                     "checkfile Contents/Resources/updater/update_helper.sh::for _name\n"
                     "forbidfile Contents/Resources/updater/update_helper.sh::candidate=\n",
                     encoding="utf-8")
        parsed_ok = parse_checks(p)
        if gate(parsed_ok, "9.9.9") or resource_problems(str(app), parsed_ok):
            bad.append("合规的资源特征被误拒：%s %s"
                       % (gate(parsed_ok, "9.9.9"),
                          resource_problems(str(app), parsed_ok)))
        # ---- S7 第一处：新增特征判据自己也必须能失败 --------------------------
        # 四种形状各判一次。第二种就是 v1.4.49 那轮的真实情况：填的是上一轮
        # 已经存在的 `_build_session`，旧闸门照样放行。
        prev_lines = {"check sf_a::old_sym"}   # sf_b 那一行是本轮新加的
        cases2: list = [
            ("整份清单照抄上一轮",
             {"check": ["sf_a::old_sym"], "forbid": []},
             prev_lines, {"sf_a": "def old_sym(): pass\n"},
             True, "一条本轮新增都没有"),
            ("新增了行、但查的是旧符号（只改了函数体）",
             {"check": ["sf_b::body_only"], "forbid": []},
             prev_lines, {"sf_b": "def body_only(): pass  # 本轮只改了这里\n"},
             True, "全是**旧符号**"),
            ("新增行 + 真新名字",
             {"check": ["sf_c::brand_new_helper"], "forbid": []},
             prev_lines, {"sf_c": "def old(): pass\n"},
             False, ""),
            ("新增行落在本轮新建的模块上",
             {"check": ["sf_new::anything"], "forbid": []},
             prev_lines, {"sf_new": None},
             False, ""),
        ]
        for title, parsed2, lines2, srcs2, expect_red, expect_msg in cases2:
            problems2 = judge_newness(parsed2, lines2, srcs2)
            if expect_red and not problems2:
                bad.append("用例「%s」应该被拒，实际放行" % title)
            if expect_red and problems2 and expect_msg not in problems2[0]:
                bad.append("用例「%s」拒了，但理由不对：%s" % (title, problems2))
            if not expect_red and problems2:
                bad.append("用例「%s」被误拒：%s" % (title, problems2))
        if not any("读不到" in x for x in newness_problems(
                {"check": ["x::y"], "forbid": []},
                tmp_root / "not-in-repo.txt", rev="heads/does-not-exist-anywhere")):
            bad.append("读不到上一轮清单时没有报红 —— 无 git 的树会静默放行")

        # ---- stale-feature：绕判据这件事必须写在清单里，而且必须带理由（v1.4.63 补）----
        sf = tmp_root / "stale_checks.txt"
        sf.write_text("version 9.9.9\ncheck sf_a::old_sym\n"
                      "stale-feature 9.9.9 本轮只改打包资产与链上工具\n",
                      encoding="utf-8")
        parsed_sf = parse_checks(sf)
        mine, decl_problems, other = stale_declaration(parsed_sf, "9.9.9")
        if not (mine and not decl_problems and not other):
            bad.append("写明版本且带理由的申报没被判为本轮有效：%s / %s（errors=%s）"
                       % (mine, decl_problems, parsed_sf["errors"]))
        if parsed_sf["stale"] != ["9.9.9 本轮只改打包资产与链上工具"]:
            bad.append("stale-feature 的理由没被完整读到：%s" % parsed_sf["stale"])
        # ① 不写版本 = 无效申报。这一条是 v1.4.64 实测补的：清单是堆叠历史，
        #    无主申报会被以后每一版继承，等于给整条"新增特征"判据永久免检。
        sf_nov = tmp_root / "stale_nov.txt"
        sf_nov.write_text("version 9.9.9\ncheck sf_a::old_sym\n"
                          "stale-feature 本轮只改打包资产\n", encoding="utf-8")
        mine_nv, prob_nv, _o_nv = stale_declaration(parse_checks(sf_nov), "9.9.9")
        if mine_nv or not any("没写明对哪一版" in x for x in prob_nv):
            bad.append("不带版本的 stale-feature 被当成有效申报 —— 它会被下一版白拿：%s"
                       % prob_nv)
        # ② 上一版的申报不许被下一版继承（本轮真实撞上的形状）
        sf_prev = tmp_root / "stale_prev.txt"
        sf_prev.write_text("version 9.9.10\ncheck sf_a::old_sym\n"
                           "stale-feature 9.9.9 上一版只改打包资产\n", encoding="utf-8")
        mine_pv, prob_pv, other_pv = stale_declaration(parse_checks(sf_prev), "9.9.10")
        if mine_pv or len(other_pv) != 1 or prob_pv:
            bad.append("上一版的申报被本轮继承了（本轮=%s 不生效=%s 问题=%s）"
                       " —— 一旦继承，以后任何一版都能不填新名字过闸"
                       % (mine_pv, other_pv, prob_pv))
        # ③ 空理由仍然无效
        sf_empty = tmp_root / "stale_empty.txt"
        sf_empty.write_text("version 9.9.9\ncheck sf_a::old_sym\n"
                            "stale-feature 9.9.9\n", encoding="utf-8")
        mine_2, problems2b, _o2 = stale_declaration(parse_checks(sf_empty), "9.9.9")
        if mine_2 or not problems2b:
            bad.append("不带理由的 stale-feature 被当成有效申报 —— 那是开关，不是申报")
        # 申报只影响 main 里"要不要跑这条判据"，判据本身不许因此变瞎
        if not judge_newness(parse_checks(sf_empty), {"check sf_a::old_sym"},
                             {"sf_a": "def old_sym(): pass\n"}):
            bad.append("清单里有一行 stale-feature 就让「全是旧符号」判绿了 —— 越权")

        # ---- 基线怎么取：必须是"上一版清单"，不是"上一笔提交"（v1.4.62 实测坑）----
        # 不碰真仓库：history/show 都可注入，这里喂的是形状，不是 git 事实。
        def _show_for(mapping):
            def _show(rev, path):
                return mapping.get(rev)
            return _show

        v98 = "version 9.9.8\ncheck sf_a::old_sym\n"
        v99 = "version 9.9.9\ncheck sf_a::old_sym\ncheck sf_c::brand_new_helper\n"
        cases3: list = [
            ("清单改完又补了一笔无关提交（台账/交付记录）",
             ["c3", "c2", "c1"],
             {"c3": v99, "c2": v99, "c1": v98},
             "c1", "本轮特征在 c2 里，基线必须回到上一版清单 c1，"
                   "否则判据会说『本轮没有新增特征』这句假话"),
            ("最近一笔就是改清单那笔（正常形状）",
             ["c2", "c1"], {"c2": v99, "c1": v98}, "c1", "同上"),
            ("整条历史里只有本轮版本号（清单是新文件）",
             ["c1"], {"c1": v99}, None, "找不到上一版 → 返回 None，由调用方退回 HEAD~1"),
            ("历史为空", [], {}, None, "无历史可比 → None"),
        ]
        for title, hist, mapping, expect_rev, why in cases3:
            got = prev_release_rev("release_checks.txt", "9.9.9",
                                   history=hist, show=_show_for(mapping))
            if got != expect_rev:
                bad.append("基线选取用例「%s」判成 %s，应为 %s（%s）"
                           % (title, got, expect_rev, why))
        # 反向：基线取错一笔（取成"已含本轮特征的那一笔"）必须让判据红 ——
        # 这一条证明"基线选取"不是走过场，它直接决定判据会不会说谎。
        wrong_base = {ln.split("#", 1)[0].strip() for ln in v99.splitlines()}
        if not judge_newness({"check": ["sf_c::brand_new_helper"], "forbid": []},
                             wrong_base, {"sf_c": "def old(): pass\n"}):
            bad.append("基线取成含本轮特征的那一笔时，判据竟然不红 —— "
                       "说明它无法识破『本轮没有新增』这种情况")

        # ---- testref：登记的测试必须真在源码里（只证明存在，不证明通过）----
        fake_root = tmp_root / "fake_root"
        (fake_root / "tests").mkdir(parents=True)
        (fake_root / "tests" / "test_demo.py").write_text(
            "import unittest\n\n\ndef test_module_level():\n    pass\n\n\n"
            "def helper_not_a_test():\n    pass\n\n\n"
            "class DemoCase(unittest.TestCase):\n"
            "    def test_inside_class(self):\n        pass\n\n"
            "    @unittest.skip('placeholder')\n"
            "    def test_always_skipped(self):\n        pass\n\n"
            "    @unittest.skipIf(False, 'conditional')\n"
            "    def test_conditionally_skipped(self):\n        pass\n\n\n"
            "@unittest.skip('whole class')\nclass SkippedCase(unittest.TestCase):\n"
            "    def test_in_skipped_class(self):\n        pass\n", encoding="utf-8")
        (fake_root / "tests" / "test_broken.py").write_text("def (:\n", encoding="utf-8")
        tr_cases: list = [
            ("模块级用例在", "tests/test_demo.py::test_module_level", False, ""),
            ("类内用例在（只写用例名）", "tests/test_demo.py::test_inside_class", False, ""),
            ("类内用例在（类名::用例名）", "tests/test_demo.py::DemoCase::test_inside_class", False, ""),
            ("有条件 skip 不算无条件", "tests/test_demo.py::DemoCase::test_conditionally_skipped", False, ""),
            ("文件不存在", "tests/test_nope.py::test_x", True, "没有这个测试文件"),
            ("用例被改名或删了", "tests/test_demo.py::test_renamed_away", True, "找不到这个用例"),
            ("类名写错", "tests/test_demo.py::WrongCase::test_inside_class", True, "找不到这个用例"),
            ("辅助函数冒充用例", "tests/test_demo.py::helper_not_a_test", True, "不是测试用例"),
            ("无条件 skip 的用例", "tests/test_demo.py::DemoCase::test_always_skipped", True, "无条件 skip"),
            ("整个类被 skip", "tests/test_demo.py::SkippedCase::test_in_skipped_class", True, "无条件 skip"),
            ("文件解析不了", "tests/test_broken.py::test_x", True, "无法解析"),
            ("路径逃出 tests/", "tests/../tools/x.py::test_x", True, "路径必须是 tests/"),
            ("绝对路径", "/etc/passwd.py::test_x", True, "路径必须是 tests/"),
            ("不在 tests/ 下", "tools/x.py::test_x", True, "路径必须是 tests/"),
            ("段数过多", "tests/test_demo.py::A::B::C", True, "形状应为"),
            ("某一段为空", "tests/test_demo.py::::test_x", True, "形状应为"),
        ]
        for title, spec, expect_red, expect_msg in tr_cases:
            found_tr = " \n".join(testref_problems({"testref": [spec]}, fake_root))
            if expect_red and expect_msg not in found_tr:
                bad.append("testref 用例「%s」没被拒或理由不对：%s（实际：%s）"
                           % (title, expect_msg, found_tr or "放行"))
            if not expect_red and found_tr:
                bad.append("testref 用例「%s」被误拒：%s" % (title, found_tr))
        tr_file = tmp_root / "tr_checks.txt"
        tr_file.write_text("version 9.9.9\ntestref tests/test_demo.py::test_module_level\n"
                           "testref noseparator\n", encoding="utf-8")
        parsed_tr = parse_checks(tr_file)
        if parsed_tr["testref"] != ["tests/test_demo.py::test_module_level"] \
                or not any("testref" in e and "形式" in e for e in parsed_tr["errors"]):
            bad.append("testref 行的解析有误（合规的该读到、缺 `::` 的该报形状错）：%s / %s"
                       % (parsed_tr["testref"], parsed_tr["errors"]))
        # 越权检查：只有 testref、没有任何 check/forbid 的清单，必须仍被判成"没有本轮特征"。
        if not any("一条 check/forbid 都没有" in x for x in gate(parsed_tr, "9.9.9")):
            bad.append("testref 被当成了本轮特征 —— 它只是补充证据，不能替 check/forbid 过关")
        if judge_newness(parse_checks(tr_file), {"check sf_a::old_sym"}, {}) == []:
            bad.append("testref 被算进『新增特征』—— 那会让它成为绕过新名字判据的通道")
    if bad:
        print("!! 本轮特征闸门自检失败（它对『没填特征』不再报警，结论不可信）：")
        for b in bad:
            print("   -", b)
        return 1
    print("   本轮特征闸门自检通过：%d 种『没填对/填错』+ %d 种『新增特征』+ %d 种『基线取哪一笔』"
          "+ %d 种『testref』的形状全部判对" % (len(cases), len(cases2), len(cases3), len(tr_cases)))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="发版链：本轮产物特征闸门")
    ap.add_argument("app", nargs="?", help="待核验的 .app 路径")
    ap.add_argument("--checks", default=str(DEFAULT_CHECKS),
                    help="特征清单路径（默认 release_checks.txt）")
    ap.add_argument("--version", help="期望版本号，默认取 sf_constants.APP_VERSION")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--allow-stale-feature", action="store_true",
                    help="放行「本轮没有新增特征」这一条判据（会打印 WARNING 留痕）")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.app:
        print("!! 需要 .app 路径（或用 --self-test 只验闸门本身）")
        return 2

    want = args.version or current_version()
    parsed = parse_checks(pathlib.Path(args.checks))
    problems = gate(parsed, want)
    declared, decl_problems, stale_other = stale_declaration(parsed, want)
    problems += decl_problems          # 申报了却没写理由 = 直接拒，不给"空开关"留路
    # v1.4.66 复核（2026-10-12）：--allow-stale-feature 的适用范围是**且只是**"新增特征"
    # 这一条判据（AGENTS §3.1 一直这么写，旧实现却把 problems 整表清空 —— 清单只剩一行
    # version 时连 .app 不存在都退 0）。现在按文档口径执行：其余判据照常红。
    newness_skipped = bool(declared) or args.allow_stale_feature
    if not problems and not newness_skipped:
        problems = newness_problems(parsed, pathlib.Path(args.checks))
    # testref 只是补充证据，排在上面所有判据之后，不替它们过关，也不被它们的结果遮住。
    problems += testref_problems(parsed)
    print("== 本轮特征闸门（清单 %s，期望版本 %s）==" % (args.checks, want))
    print("   check %d 条 / forbid %d 条 / checkfile %d 条 / forbidfile %d 条"
          % (len(parsed["check"]), len(parsed["forbid"]),
             len(parsed["checkfile"]), len(parsed["forbidfile"])))
    if parsed["testref"]:
        print("   testref %d 条（只证明登记的测试仍在源码里，不证明它通过，也不算本轮特征）"
              % len(parsed["testref"]))
    if declared:
        # 跳过判据这件事必须出现在发版日志里，而不是只存在于清单的一行注释里
        print("   WARNING 本轮按清单申报跳过「新增特征」判据（包内拿不出新名字）：")
        for one in declared:
            print("     - %s" % one)
        print("   产物层的本轮证据因此只剩：version 三方比对 + 包内资源核验。")
    if stale_other:
        # 堆叠清单里属于别版的申报：本轮不生效，但要说出来，否则读日志的人
        # 会以为它们也算过（而"为什么这一版没被免检"正是需要看得见的事）
        print("   清单里另有 %d 条 stale-feature 属于别的版本，本轮不生效：" % len(stale_other))
        for one in stale_other:
            print("     · %s" % one)
    if args.allow_stale_feature and not declared:
        # 显式跳过也要在发版日志里留下"这轮产物核验证明力有限"。
        # 注意不清 problems：清单形状/版本对不上这类问题与"有没有新名字"无关，照常红。
        print("   WARNING 已用 --allow-stale-feature 跳过新增特征判据"
              "（只跳过这一条；version 与清单形状照常判）。")
    if problems:
        print("!! 拒绝进入产物核验，因为这份清单不足以证明本轮改动：")
        for p in problems:
            print("   -", p)
        print("   怎么办：把本轮修动的特征符号填进 %s（至少一条落在改动文件上；"
              "改的是包内 shell/字体这类资源就用 checkfile），并把 version 改成本轮版本号。"
              % args.checks)
        return 1
    # 资源文件的核验在这里就地做（不塞进 verify_dmg_pyz 的 argv：那个工具解的是字节码归档，
    # 两件事的读法完全不同，混在一个参数拼装里会让 to_argv 的自检失去意义）。
    rc = 0
    resource = resource_problems(args.app, parsed)
    if resource:
        print("!! 包内资源文件没通过本轮特征核验：")
        for p in resource:
            print("   -", p)
        rc = 1
    if parsed["check"] or parsed["forbid"]:
        rc = VD.main(to_argv(args.app, parsed, want)) or rc
    elif not resource:
        print("   本轮没有字节码特征，只核验包内资源文件（如实说明：字节码层本轮无新增可查）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
