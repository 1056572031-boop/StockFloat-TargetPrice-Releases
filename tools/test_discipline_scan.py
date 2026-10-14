#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AGENTS §6「测试书写纪律」9 条的自动关卡（v1.4.33 补）。

为什么需要：这 9 条写在 AGENTS.md 里靠人记，实际只有第 9、10 条有自动关卡。
纪律没有关卡就等于没有 —— 本仓为「一个永不失败的检查等于没有检查」反复付过学费。

用法：
    ./.venv/bin/python tools/test_discipline_scan.py                # 判定（超基线即红）
    ./.venv/bin/python tools/test_discipline_scan.py --report       # 看各条分布
    ./.venv/bin/python tools/test_discipline_scan.py --update       # 下调基线后同步
    ./.venv/bin/python tools/test_discipline_scan.py --self-test    # 变异自检

立场与 static_gate 一致：**存量不批量改造**（AGENTS §6 明令），所以每条规则
按「规则名 → 当前违规数」记基线，只降不升；新增一条就红。

各条判据（刻意保守，宁可漏报也不误报，误报多了真信号就没人看）：
    R1 断言版本号        —— assert* 里出现 x.y.z 字面量或引用 APP_VERSION
    R2 读源码做字符串断言 —— inspect.getsource / getsourcefile，或读 .py 文件后紧接断言
    R3 sleep 后立刻断言   —— time.sleep(...) 的**下一条语句**就是 assert*
    R4 依赖当下时钟       —— 用 datetime.now()/today() 判交易时段，却没走 fixed_clock
    R5 访问私有成员       —— 读 obj._xxx（self/cls 自身的除外，魔术方法除外）
    R6 布局用裸数字       —— assert*(<int>, obj.height()) 这类没有语义的断言
    R7 未继承 SFCase      —— 测试类直接继承 unittest.TestCase 而非登记式基类
"""
from __future__ import annotations

import argparse
import ast
import collections
import json
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = pathlib.Path(__file__).with_name("test_discipline_baseline.json")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+")
GEOMETRY_ATTRS = ("height", "width", "size", "geometry", "margin", "pad",
                  "gap", "x", "y")
RULES = ("R1", "R2", "R3", "R4", "R5", "R6", "R7")


def _is_assert(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith("assert"))


def _stmt_is_assert(stmt: ast.AST) -> bool:
    """语句位上的断言：assert* 是表达式语句，外面还包着一层 ast.Expr。"""
    return isinstance(stmt, ast.Expr) and _is_assert(stmt.value)


def _has_version_literal(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and VERSION_RE.match(sub.value.strip()):
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == "APP_VERSION":
            return True
        if isinstance(sub, ast.Name) and sub.id == "APP_VERSION":
            return True
    return False


# 显式豁免标记：在违规那一行末尾写  # discipline-ok(R5): 为什么必须这样
# 目的是让"有意访问内部状态"这件事**留在代码里可被 review**，而不是靠
# 调基线默默吸收 —— 调基线只该用于"整条规则在这里不合理"的场合。
EXEMPT_RE = re.compile(r"discipline-ok\((R\d)\)\s*[:：]\s*(\S.*)$")


def exempt_lines(src: str) -> dict:
    """{行号: 规则码}：从源码注释里读出显式豁免（AST 拿不到注释，只能按行扫）。"""
    out = {}
    for i, line in enumerate(src.splitlines(), 1):
        m = EXEMPT_RE.search(line)
        if m:
            out[i] = m.group(1)
    return out


class _Walker(ast.NodeVisitor):
    """一个文件一次遍历，产出 {规则: [行号, ...]}。"""

    def __init__(self, src: str):
        self.src = src
        self.hits: dict[str, list[int]] = collections.defaultdict(list)
        self.uses_fixed_clock = False

    # ---- 先扫一遍文件级事实（R4 需要）----
    def pre(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "fixed_clock":
                self.uses_fixed_clock = True
            if isinstance(node, ast.Attribute) and node.attr == "fixed_clock":
                self.uses_fixed_clock = True

    # ---- R3/R4/R5/R6/R7 都在函数级判 ----
    def visit_FunctionDef(self, node):
        self._fn(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _fn(self, node):
        body = list(node.body)
        for i, stmt in enumerate(body):
            # R3：time.sleep 的下一条语句就是断言
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                f = stmt.value.func
                fname = (f.attr if isinstance(f, ast.Attribute) else
                         getattr(f, "id", ""))
                if fname == "sleep" and i + 1 < len(body) \
                        and _stmt_is_assert(body[i + 1]):
                    self.hits["R3"].append(stmt.lineno)
        for sub in ast.walk(node):
            # R1：断言里写死版本号
            if _is_assert(sub) and _has_version_literal(sub):
                self.hits["R1"].append(sub.lineno)
            # R4：判"现在几点"却没注入时钟
            if isinstance(sub, ast.Attribute) and sub.attr in ("now", "today",
                                                              "utcnow"):
                self.hits["R4"].append(sub.lineno)
            # R5：读别人的私有成员
            if isinstance(sub, ast.Attribute) and sub.attr.startswith("_") and not sub.attr.startswith("__"):
                base = sub.value
                if not (isinstance(base, ast.Name) and base.id in ("self", "cls")):
                    self.hits["R5"].append(sub.lineno)
            # R6：assert*(<int>, 几何量)
            if _is_assert(sub):
                ints = [a for a in sub.args
                        if isinstance(a, ast.Constant) and isinstance(a.value, int)]
                geoms = [a for a in sub.args
                         if isinstance(a, (ast.Attribute, ast.Call))
                         and _looks_geometry(a)]
                if ints and geoms:
                    self.hits["R6"].append(sub.lineno)

    def visit_Call(self, node):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name in ("getsource", "getsourcefile"):
            self.hits["R2"].append(node.lineno)
        self.generic_visit(node)


def _looks_geometry(node) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    attr = getattr(target, "attr", None) or getattr(target, "id", "") or ""
    return any(attr == g or attr.endswith("_" + g) or g in attr.lower()
               for g in GEOMETRY_ATTRS)


def scan_file(path: pathlib.Path) -> dict:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        return {"error": "%s 解析失败：%s" % (path.name, exc)}
    src = path.read_text(encoding="utf-8", errors="replace")
    w = _Walker(src)
    w.pre(tree)
    w.visit(tree)
    ex = exempt_lines(src)
    if ex:
        w.hits = {rule: [ln for ln in lns if ex.get(ln) != rule]
                  for rule, lns in w.hits.items()}
    # R4 的豁免：整个文件用了 fixed_clock 注入，说明作者知道这条规矩
    hits = {k: v for k, v in w.hits.items()}
    if w.uses_fixed_clock:
        hits.pop("R4", None)
    # R7：类级判据单独走
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        bases = [getattr(b, "id", None) or getattr(b, "attr", None)
                 for b in cls.bases]
        if "TestCase" in [b for b in bases if b] and "SFCase" not in [b for b in bases if b]:
            hits.setdefault("R7", []).append(cls.lineno)
    return {"hits": hits}


def scan(tests_dir: pathlib.Path,
         min_files: int = 100) -> tuple[collections.Counter, list[str]]:
    counts: collections.Counter = collections.Counter()
    errors: list[str] = []
    files = [p for p in sorted(tests_dir.glob("*.py"))
             if p.name != pathlib.Path(__file__).name]
    # 范围红线：`glob` 对**不存在的目录**和空目录都安静地返回空表，于是"零输入零违规"
    # 变成"整关通过"。实测把 --dir 指到空目录/不存在的目录都是 rc=0。
    # 用例一旦被挪进子目录（tests/unit/）也会整体脱账 —— 同样必须当场暴露。
    # `min_files` 只为变异自检的临时目录存在（那里只放 8 个种样例）。
    if len(files) < min_files:
        errors.append("扫描目录 %s 只取到 %d 个 .py（tests/ 现有 100+ 条用例文件），"
                      "关卡范围疑似失效，本关拒绝给出\"通过\"结论"
                      % (tests_dir, len(files)))
    for path in files:
        r = scan_file(path)
        if "error" in r:
            errors.append(r["error"])
            continue
        for rule, lines in r["hits"].items():
            counts["%s:%s" % (rule, path.name)] += len(lines)
    return counts, errors


def diff(counts: collections.Counter, baseline: dict):
    old = baseline.get("counts", {})
    worse, better = [], []
    for key, n in sorted(counts.items()):
        cap = int(old.get(key, 0))
        if n > cap:
            worse.append("%s：%d → %d（新增 %d 条）" % (key, cap, n, n - cap))
    for key, cap in sorted(old.items()):
        n = int(counts.get(key, 0))
        if n < cap:
            better.append("%s：基线 %d，实际 %d → 跑 --update 下调" % (key, cap, n))
    return worse, better


# ---- 变异自检：每条规则各喂一个违规样例，必须被抓到 ----
PLANTED = {
    "R1": "import unittest\n\n\nclass T(unittest.TestCase):\n"
          "    def test_v(self):\n        self.assertEqual('1.4.33', '1.4.33')\n",
    "R2": "import inspect\n\n\ndef f():\n    return inspect.getsource(f)\n",
    "R3": "import time\nimport unittest\n\n\nclass T(unittest.TestCase):\n"
          "    def test_s(self):\n        time.sleep(0.1)\n"
          "        self.assertTrue(1)\n",
    "R4": "import datetime\nimport unittest\n\n\nclass T(unittest.TestCase):\n"
          "    def test_c(self):\n        h = datetime.datetime.now().hour\n"
          "        self.assertGreater(h, 0)\n",
    "R5": "def f(win):\n    return win._hidden_thing\n",
    "R6": "import unittest\n\n\nclass T(unittest.TestCase):\n"
          "    def test_g(self):\n        self.assertEqual(224, self.win.height())\n",
    "R7": "import unittest\n\n\nclass T(unittest.TestCase):\n    pass\n",
}


def self_test() -> int:
    bad = []
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        for rule, src in PLANTED.items():
            (tdp / ("planted_%s.py" % rule.lower())).write_text(src, encoding="utf-8")
        counts, errors = scan(tdp, min_files=1)
        if errors:
            bad.extend(errors)
        for rule in RULES:
            got = sum(n for k, n in counts.items() if k.startswith(rule + ":"))
            if not got:
                bad.append("种下的 %s 违规没被抓到 → 该条关卡已失效" % rule)
        # 豁免标记必须真的生效（否则这条逃生门就是假的）
        (tdp / "planted_exempt.py").write_text(
            "def f(win):\n    return win._hidden_thing  "
            "# discipline-ok(R5): 该属性没有公开读取口\n", encoding="utf-8")
        counts2, _ = scan(tdp, min_files=1)
        if sum(n for k, n in counts2.items()
               if k.startswith("R5:planted_exempt")):
            bad.append("写了 discipline-ok 标记的行仍然被计违规 → 逃生门不生效")
        # 反向：干净文件不该报 —— 此前这一步只 write_text、**从不重新 scan 也不断言**，
        # 于是"恒真的判据"照样能让自检通过（实测把 R6 判据改成永远命中，self_test
        # 仍 rc=0，而 test_arch_constraints.py 正把这条自检当成整关的可信度来源）。
        # 现在把 R1 的种样例换成同结构的干净写法，重扫后必须一条都不剩。
        # 现在把 R1 的种样例换成**同结构**的干净写法（版本串→普通串、基类→SFCase），
        # 重扫后必须一条都不剩。
        (tdp / "planted_r1.py").write_text(
            "import _base\n\n\nclass T(_base.SFCase):\n"
            "    def test_v(self):\n        self.assertEqual('a', 'a')\n",
            encoding="utf-8")
        counts3, errors3 = scan(tdp, min_files=1)
        bad.extend(errors3)
        leftover = {k: n for k, n in counts3.items() if k.endswith(":planted_r1.py")}
        if leftover:
            bad.append("干净文件仍被判违规（%s）→ 该条关卡会误伤，重构会被误拦"
                       % ", ".join("%s×%d" % (k, n) for k, n in sorted(leftover.items())))
    if bad:
        print("!! 书写纪律关卡自检失败：")
        for b in bad:
            print("   -", b)
        return 1
    print("   书写纪律关卡自检通过：R1~R7 七条各种一处违规，全部被抓到")
    return 0


def load_baseline() -> dict:
    if not BASELINE.exists():
        raise RuntimeError("基线缺失：%s（用 --update 生成并一起提交）"
                           % BASELINE.relative_to(ROOT))
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    if not isinstance(data.get("counts"), dict):
        raise RuntimeError("基线格式已损坏：%s" % BASELINE)
    return data


def write_baseline(counts: collections.Counter, allow_rise: bool = False) -> None:
    # 棘轮的"棘轮"部分此前只存在于文档（2026-10-12 全方位复核 F3）：这一版 `--update`
    # 不与旧值比较，任何上调都被无条件写进新基线 —— 与 static_gate 当年被 df0bb0d
    # 把 attr-defined 从 22 抬到 28 时是同一个形状。三枚基线工具里只有这一枚漏了
    # 比较分支，于是"只降不升"对它不成立。现在与 static_gate 同口径：
    # 上调必须显式带 --allow-rise，且当场把每条差值打出来。
    if BASELINE.exists():
        old = load_baseline().get("counts", {}) or {}
        rises = ["  %s：%d → %d（+%d）" % (k, int(old.get(k, 0)), int(v),
                                          int(v) - int(old.get(k, 0)))
                 for k, v in sorted(counts.items()) if int(v) > int(old.get(k, 0))]
        if rises and not allow_rise:
            raise RuntimeError(
                "拒绝写基线：下列 %d 个键比现基线**更高**，棘轮只降不升。\n%s\n"
                "确认合理（例：规则本身改了口径）时，带 --allow-rise 重跑，"
                "并把理由写进提交说明。" % (len(rises), "\n".join(rises)))
    payload = collections.OrderedDict([
        ("_note", "AGENTS §6 测试书写纪律的棘轮基线（只降不升）。"
                  "各条判据与保守取舍见 tools/test_discipline_scan.py 的 docstring。"),
        ("generator", "tools/test_discipline_scan.py --update"),
        ("counts", {k: int(v) for k, v in sorted(counts.items())}),
        ("total", int(sum(counts.values()))),
    ])
    BASELINE.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="测试书写纪律棘轮关卡")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--allow-rise", action="store_true",
                    help="与 --update 同用：确需上调基线时才允许，且当场打印每条差值")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--dir", default=str(ROOT / "tests"))
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    counts, errors = scan(pathlib.Path(args.dir))
    if errors:
        print("!! 有测试文件解析失败，关卡对它无效（**不算通过**）：")
        for e in errors:
            print("   -", e)
        return 2
    by_rule = collections.Counter()
    for key, n in counts.items():
        by_rule[key.split(":", 1)[0]] += n
    if args.report:
        for rule in RULES:
            print("%s 合计 %d 条" % (rule, by_rule.get(rule, 0)))
        for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]:
            print("   %5d  %s" % (n, key))
        return 0
    if args.update:
        try:
            write_baseline(counts, allow_rise=args.allow_rise)
        except RuntimeError as exc:
            print("!! %s" % exc)
            return 1
        print("已写入基线：%d 条 / %d 个组合 → %s"
              % (sum(counts.values()), len(counts), BASELINE.relative_to(ROOT)))
        return 0

    try:
        baseline = load_baseline()
    except (RuntimeError, ValueError) as exc:
        print("!! 书写纪律关卡无法执行（**不算通过**）：%s" % exc)
        return 2
    worse, better = diff(counts, baseline)
    if not args.quiet:
        print("== 测试书写纪律：R1~R7 共 %d 条（基线 %d 条）=="
              % (sum(counts.values()), baseline.get("total", 0)))
    if worse:
        print("!! 新增违反 AGENTS §6 书写纪律的用例：")
        for w in worse:
            print("   -", w)
        print("   改法见 AGENTS.md §6；确认规则本身不合理才允许 --update 并写明理由。")
        return 1
    if better and not args.quiet:
        print("   （存量已减少，建议下调基线）")
        for b in better[:10]:
            print("   -", b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
