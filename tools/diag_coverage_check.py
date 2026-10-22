#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断覆盖闸门 —— 发版链第 3.9 步（2026-10-13 新增）。

要治的那件事
------------
`outputs/诊断导出逐版核查-v1.4.27至v1.4.66-2026-10-12.md` 逐版对账的结论：39 个实发
版本里 29 个引入了当时远程读不出来的状态，而全量回归两年全绿 —— 因为"接线"这件事
从来没有一道关卡看过。负责人那句要求是：**每次发版都必须检查，别让他隔几版手动对齐
一次**。所以这里把"检查"做成硬拦：本轮改动过的产品模块，诊断包必须答得上来，
答不上来就必须在申报清单里逐条写理由，否则**拒绝出包**。

判据分三档（宽严按可复核性排，不按方便排）
----------------------------------------
1. **curated**：模块在 `_export_diagnostics` 的符号闭包里（含再导出别名与被当引用
   传出去的 getter）。这是"有人为它写过一句'坏了用户会说什么'"。
2. **auto**：模块被主程序静态 import（装机版里真的加载了），且模块级有至少一条
   名字命中 `sf_diag` 状态词表的变量。这类模块的运行态由 `state.json` 自动带走 ——
   属于"数据在包里、立意还没接"，闸门放它过，但会在结论里单独点名。
3. **skip 申报**：两档都不沾（例如纯绘制、纯函数工具层），必须在
   `diag_coverage.txt` 当前版本那一段里写一行 `skip <模块> <理由>`。理由为空即拒。

三档全部**实测过形状**，不是看着像就行：`--self-test` 里种了八处错，包括"闭包算成
空集还判通过""用上一版的申报块蒙本版""申报一个本轮根本没改的模块"。

范围口径（决定它到底拦得住什么）
------------------------------
- 本轮 = **上一个 APP_VERSION 变动的提交 .. HEAD**，再并上工作区未提交的产品文件
  （未提交的改动同样进包，这是 gate_stamp 早就定过的口径）。
- 找不到版本锚点（浅克隆、历史被压平）**拒绝下结论并退 2**，绝不退化成"没改动 = 通过"
  —— 台账 G24 那一族：空输入当通过。
- 扫描到的产品模块少于 `MIN_PRODUCT_MODULES` 同样拒答（`glob` 对空目录静默返回空表，
  与 `test_discipline_scan.min_files` 同族）。

用法
----
    ./.venv/bin/python tools/diag_coverage_check.py [--report|--self-test]
                                    [--range <A..B>] [--baseline <SHA>] [--version <X.Y.Z>]
                                    [--dir <仓库>]
区间优先级：`--range` > `--baseline`（或环境变量 `SF_RELEASE_BASELINE`）> 按版本号推断。
推断只是估计（见 `resolve_range`），发版前应显式给出上一版发布记录里的 `source.head`。
未知参数一律退 2（§3.1「链上工具的参数形状规矩」：这工具在发版链上，会被逐个自证）。
"""

from __future__ import annotations

import argparse
import ast
import os
import pathlib
import re
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import sf_diag            # 状态词表只有一份实现，闸门不许自己再抄一份（AGENTS §7）
from diagnostic_closure import diagnostic_modules

MIN_PRODUCT_MODULES = 15      # 少于这个数＝扫描范围失效，拒答（与 test_discipline_scan.min_files 同族）
MIN_CURATED = 3               # 导出面一个模块都读不到＝尺子坏了，拒答（主窗必然读 sf_constants）
EXPORT_FN_NAMES = ("_export_diagnostics",)
DECL_FILE = "diag_coverage.txt"


def run_git(args: list[str], root: pathlib.Path) -> str:
    """只读 git 查询。失败返回空串，由调用方判"取不到就不许下结论"。"""
    proc = subprocess.run(["git", *args], cwd=str(root),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return ""
    return proc.stdout


def product_modules(root: pathlib.Path) -> list[str]:
    """仓库里的产品模块名：工程根目录下的全部 `.py`（tests/tools 在子目录，天然不在内）。

    此前是 `sf_*.py` 加两个点名的文件。点名清单回答不了「还有第几个没进来」，v1.4.101 实测
    漏了 `target_panel` 和 `targets`：本轮 `target_panel` 有真实改动，却从来不在闸门的视野里，
    手动跑完整区间也只是「通过」而没有核对到它。按目录取，新增的根目录模块自动进视野。
    """
    return sorted(p.stem for p in root.glob("*.py") if not p.stem.startswith("_"))


def module_tree(root: pathlib.Path, name: str) -> ast.Module | None:
    path = root / ("%s.py" % name)
    if not path.exists():
        return None
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None


def alias_table(tree: ast.Module) -> dict[str, str]:
    """本文件里 `from X import y as z` / `import X` 得到的符号 -> 源模块名。

    必须别名感知：本轮的接线写法是 `from sf_notify import delivery_stats as
    _notify_delivery_stats` 再把**函数本身**当引用传给 `_snapshot`。第一版按
    `sf_notify.` 属性访问找，实测把 13 个已包覆模块误判成 4 个 —— 尺子错一格，
    闸门就说一整轮的假话。
    """
    table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for al in node.names:
                head = al.name.split(".")[0]
                table[al.asname or head] = head
        elif isinstance(node, ast.ImportFrom) and node.module:
            head = node.module.split(".")[0]
            for al in node.names:
                table[al.asname or al.name] = head
    return table


def curated_modules(root: pathlib.Path) -> set[str]:
    """Resolve actual diagnostic helpers, callbacks and FloatWindow methods."""
    sources = {}
    for name in product_modules(root):
        path = root / (name + ".py")
        try:
            source = path.read_text(encoding="utf-8")
            ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):
            continue
        sources[name] = source
    return diagnostic_modules(sources)


def imported_modules(root: pathlib.Path) -> set[str]:
    """从主程序静态可达的 import 闭包（决定 auto 档能不能真在装机版里被采集到）。"""
    names = set(product_modules(root))
    reached: set[str] = set()
    frontier = ["stock_float_window"]
    seen: set[str] = set()
    while frontier:
        current = frontier.pop(0)
        if current in seen:
            continue
        seen.add(current)
        tree = module_tree(root, current)
        if tree is None:
            continue
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [al.name.split(".")[0] for al in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                targets = [node.module.split(".")[0]]
            for target in targets:
                if target in names and target not in reached and target != "sf_diag":
                    reached.add(target)
                    frontier.append(target)
    # 函数内的惰性 import 也算：ast.walk 覆盖方法体，产品里确有"用时再 import"的写法
    # （sf_universe/sf_diag 那几处），漏掉就等于把真在包里读的模块判成读不到。
    reached.add("sf_diag")
    return reached


def _written_names(tree: ast.Module) -> set[str]:
    """函数体里被写过（重新赋值 / 自增 / `global` 声明 / 下标写入）的名字。

    这是"常量"与"状态位"的静态分界：`_TOP_WINDOW_LEVEL = 25` 只在模块顶层出现过一次，
    永远不会变；`_DELIVER_COUNTS` 会在函数里被写。第一版只看名字是否命中词表，
    实测把 `sf_glass._TOP_WINDOW_LEVEL`、`sf_icons._BUTTON_ICON_PX`、
    `sf_system_alert.FALLBACK_SCRIPT` 全算成"有状态"，于是闸门在**宽松那一侧**说谎
    —— 与设计常量对齐的模块会被判成"运行态已经在包里了"。
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Global):
                    out.update(sub.names)
                elif isinstance(sub, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    targets: list[ast.expr] = list(sub.targets) if isinstance(sub, ast.Assign) else [sub.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            out.add(target.id)
                        elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                            out.add(target.value.id)
                elif isinstance(sub, ast.Delete):
                    for target in sub.targets:
                        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                            out.add(target.value.id)
    return out


def _mutable_literal(node: ast.expr | None) -> bool:
    """模块顶层的值是不是容器字面量/推导式（dict/list/set 天生是会被写的状态袋）。"""
    if node is None:
        return False
    if isinstance(node, (ast.Dict, ast.List, ast.Set)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in {"dict", "list", "set", "defaultdict", "Counter",
                                "OrderedDict", "OrderedDict"}
    return False


def state_bearing_modules(root: pathlib.Path) -> set[str]:
    """模块级存在**真运行态**的模块（auto 档判据：命中词表 且 会被改写）。"""
    out: set[str] = set()
    for name in product_modules(root):
        tree = module_tree(root, name)
        if tree is None:
            continue
        written = _written_names(tree)
        for node in tree.body:
            pairs: list[tuple[str, ast.expr | None]] = []
            if isinstance(node, ast.Assign):
                pairs = [(t.id, node.value) for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                pairs = [(node.target.id, node.value)]
            for var, value in pairs:
                if not sf_diag.is_state_name(var) or sf_diag.is_secret_name(var):
                    continue
                if var in written or _mutable_literal(value):
                    out.add(name)
                    break
        if name in out:
            continue
    return out


def classes_with_state(trees: dict[str, ast.Module]) -> dict[str, set[str]]:
    """每个模块里"实例上写过状态名"的类名（自动层的对象爬取正是靠这个才收得到）。

    `sf_diag.collect(self)` 会从主窗实例往里爬一层自有对象，所以**运行态住在实例里**
    的模块也可能已经在包里 —— 只看模块级变量会漏判它们，于是闸门向人要一条申报。
    注意这条判据同时要求"属性名命中词表"：实测 `sf_runtime.RuntimeState` 就被排除
    （它的属性名一条都不命中），所以它的申报必须老实写"这一层确实没进包"，
    而不是写"由 objects 段带走"。
    """
    out: dict[str, set[str]] = {}
    for stem, tree in trees.items():
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                            and sub.value.id == "self" and sf_diag.is_state_name(sub.attr)):
                        names.add(node.name)
        if names:
            out[stem] = names
    return out


def instance_held_modules(trees: dict[str, ast.Module],
                          class_index: dict[str, set[str]]) -> set[str]:
    """在 `FloatWindow.__init__` 里 `self.X = 某个自有类()` 建起来的实例所属模块。

    只认 `__init__`：自动层的爬取发生在"导出那一下"，所以只有**进程活着就一直持有**的
    对象才真的在包里。第一版按全文扫赋值，实测把 `sf_editor` 判成"状态已在包里"，
    而 `self._editor` 在编辑器关着时是 `None` —— 报假平安。编辑器的拒绝计数走
    universe.json，那条由 curated 档负责。
    """
    entry = trees.get("stock_float_window")
    if entry is None:
        return set()
    table = alias_table(entry)
    init: ast.FunctionDef | None = None
    for node in ast.walk(entry):
        if isinstance(node, ast.ClassDef) and node.name == "FloatWindow":
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "__init__":
                    init = sub
            break
    if init is None:
        return set()
    out: set[str] = set()
    for node in ast.walk(init):
        if not isinstance(node, ast.Call):
            continue
        cls = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if not cls:
            continue
        origin = table.get(cls)
        if origin is None and cls in class_index.get("stock_float_window", set()):
            origin = "stock_float_window"
        if origin and cls in class_index.get(origin, set()):
            out.add(origin)
    return out


def trees_iter(root: pathlib.Path):
    """(模块名, 是否为产品模块) 生成器：只为让上面的字典推导式保持一行可读。"""
    for name in product_modules(root):
        yield name


def version_at(root: pathlib.Path, rev: str) -> str:
    """某个版本位上 `sf_constants.APP_VERSION` 的字面值（从 git 取，不 import）。"""
    proc = subprocess.run(["git", "show", "%s:sf_constants.py" % rev], cwd=str(root),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return ""
    match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', proc.stdout, re.MULTILINE)
    return match.group(1) if match else ""


def head_version(root: pathlib.Path) -> str:
    """当前版本号：**先读工作区文件**，读不到再退回 git HEAD。

    为什么必须先读磁盘：发版时 `APP_VERSION` 那一笔常常还没提交（或刚提交但顺序不一），
    只读 HEAD 会拿到上一版的号，于是本轮的申报段（`version 1.4.69`）永远匹配不上，
    闸门红在一件与代码无关的事上。`build_release.sh` 第 1 步也是按磁盘取的，两边同口径。
    """
    path = root / "sf_constants.py"
    if path.exists():
        match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']',
                          path.read_text(encoding="utf-8"), re.MULTILINE)
        if match:
            return match.group(1)
    return version_at(root, "HEAD")


def previous_bump(root: pathlib.Path) -> str:
    """上一个把 APP_VERSION 改成了别的值的提交（= 上一轮发版的位置）。

    从 HEAD 往回走：只要版本号与当前不同，那一提交就是上一轮的收尾。找到即返回，
    找不到返回空串（调用方必须拒答，不能当成"没改动"）。
    """
    current = head_version(root)
    if not current:
        return ""
    log = run_git(["log", "--format=%H", "--", "sf_constants.py"], root).split()
    for rev in log:
        if version_at(root, rev) == current:
            continue
        return rev
    return ""


def changed_modules(root: pathlib.Path, rev_range: str) -> set[str]:
    """本轮改过的产品模块：提交区间 ∪ 工作区未提交（未提交的改动同样进包）。"""
    names = set(product_modules(root))
    out: set[str] = set()
    if rev_range:
        for line in run_git(["diff", "--name-only", rev_range], root).splitlines():
            stem = pathlib.Path(line).stem
            if stem in names:
                out.add(stem)
    for line in run_git(["status", "--porcelain"], root).splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:                    # 重命名取"改后"那一侧
            path = path.split(" -> ", 1)[1]
        stem = pathlib.Path(path).stem
        if stem in names:
            out.add(stem)
    return out


def resolve_range(root: pathlib.Path, override: str, version: str,
                  baseline: str = "") -> tuple[str, str]:
    """返回 (rev_range, 说明)。取不到锚点时 rev_range 为空串 —— 调用方必须拒答。

    优先级：`--range` > 显式基线（`--baseline` / `SF_RELEASE_BASELINE`）> 按版本号推断。
    显式基线应取自上一版发布记录里的 `source.head`，必须是 HEAD 的祖先，否则拒答。
    推断不可靠：它找「上一个把 APP_VERSION 改成别的值的提交」，而发布后主线上的修复提交
    不改版本号。v1.4.101 实测两次都不准：并线前取到更早的版本位（区间偏大，14 笔 17 个模块），
    并线后取到旁支上的 `bf48875`（区间偏小，漏掉它自己带来的 `sf_glass` 改动）。
    所以推断结果必须把提交数说出来，并明确它只是估计。
    """
    if override:
        return override, "命令行指定 --range"
    if baseline:
        sha = run_git(["rev-parse", "--verify", "--quiet", "%s^{commit}" % baseline], root).strip()
        if not sha:
            return "", "基线 %s 不是仓库里的提交，拒绝下结论" % baseline
        if subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=str(root),
                          capture_output=True).returncode != 0:
            return "", "基线 %s 不是 HEAD 的祖先，拒绝下结论" % sha[:9]
        return "%s..HEAD" % sha, "显式基线 %s 到 HEAD" % sha[:9]
    prev = previous_bump(root)
    if not prev:
        return "", "找不到上一个版本变动的提交（历史被压平/浅克隆？）"
    short = run_git(["log", "--format=%h", "-1", prev], root).strip() or prev[:7]
    count = run_git(["rev-list", "--count", "%s..HEAD" % prev], root).strip() or "?"
    return "%s..HEAD" % prev, (
        "按版本号推断的上一个版本位 %s（当前 %s）到 HEAD，共 %s 笔；这只是估计，可能偏大也可能偏小，"
        "发版前用 --baseline <上一版发布记录的 source.head> 或环境变量 SF_RELEASE_BASELINE 指定"
        % (short, version, count))


def parse_declarations(root: pathlib.Path, version: str) -> tuple[dict[str, str], list[str]]:
    """读 `diag_coverage.txt`，**只取当前版本那一段**（堆叠历史，见台账 G26）。

    形状：
        version 1.4.69
        skip sf_utils 纯函数工具层，无运行态可采
    返回 ({模块: 理由}, 问题行)。段外的申报一律不生效 —— 否则上一版的"图省事"
    会被下一版自动继承，那正是 G26 那条"无主 stale-feature 被下一版继承"的形状。
    """
    path = root / DECL_FILE
    if not path.exists():
        return {}, []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: dict[str, str] = {}
    problems: list[str] = []
    in_block = False
    for index, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("version"):
            in_block = line.split(None, 1)[1].strip() == version if " " in line else False
            continue
        if not in_block:
            continue
        parts = line.split(None, 2)
        if parts[0] != "skip":
            problems.append("第 %d 行：只认 skip，读到 %r" % (index, parts[0]))
            continue
        module = parts[1] if len(parts) > 1 else ""
        reason = parts[2] if len(parts) > 2 else ""
        if not module:
            problems.append("第 %d 行：skip 少了模块名" % index)
            continue
        if not reason.strip():
            problems.append("第 %d 行：skip %s 没写理由（理由为空即拒）" % (index, module))
            continue
        out[module] = reason.strip()
    return out, problems


def check(root: pathlib.Path, rev_range: str = "", version: str = "",
          changed: set[str] | None = None, baseline: str = "") -> dict[str, Any]:
    """跑一次对账，返回结构化结果（`main` 与判据共用同一份实现）。

    `changed` 由判据显式传入（模拟「本轮改了哪些模块」），生产路径不传，由 git 取。
    刻意不给它一个「传了就跳过尺子自检」的副作用：尺子的失效与改动的来源是两件事。

    返回的键（值类型见名字）：`ok` bool、`refuse` str（非空即"闸门无法下结论"，
    调用方退 2）、`empty_round` bool（本轮零改动，**不算通过也不算红**）、
    `version`/`range`/`range_note` str、`changed`/`curated`/`auto_only`/
    `undeclared`/`stale_declarations`/`redundant_declarations`/
    `declaration_problems` 均为 list[str]、`counts` dict[str,int]。
    用 `Any` 而不是 TypedDict：这张表就是"一次核验的全部输出"，写成 TypedDict
    会让每个字段多两行，而它没有任何跨模块的契约要钉（判据与 main 都只读它）。
    """
    result: dict[str, Any] = {
        "ok": False, "refuse": "", "version": version, "range": "", "range_note": "",
        "changed": [], "curated": [], "auto_only": [], "undeclared": [],
        "stale_declarations": [], "declaration_problems": [],
        "redundant_declarations": [],
        "counts": {"product_modules": 0, "curated": 0, "stateful": 0,
                   "changed": 0, "declared": 0},
    }
    mods = product_modules(root)
    if len(mods) < MIN_PRODUCT_MODULES:
        result["refuse"] = ("只扫到 %d 个产品模块（下限 %d）：扫描范围失效，拒绝下结论"
                            % (len(mods), MIN_PRODUCT_MODULES))
        return result
    version = version or head_version(root)
    if not version:
        result["refuse"] = "读不到 sf_constants.APP_VERSION，无法界定本轮"
        return result

    curated = curated_modules(root)
    # 尺子自己失效就不许给结论：这正是 S6 那一族（"闭包算成空集，于是全都未包覆"
    # 看着像严格，实际是把闸门变成了橡皮图章的反面 —— 空集什么也拦不住）。
    if not curated:
        result["refuse"] = ("导出面的符号闭包算出空集：`_export_diagnostics` 改名/消失，"
                            "或闭包解析失效 —— 尺子坏了，拒绝下结论")
        return result
    if len(curated) < MIN_CURATED:
        result["refuse"] = "导出面只读到 %d 个模块（下限 %d）：闭包解析可疑，拒绝下结论" % (
            len(curated), MIN_CURATED)
        return result

    if changed is None:
        resolved, note = resolve_range(root, rev_range, version, baseline)
        if not resolved:
            result["refuse"] = note or "取不到本轮提交区间"
            return result
        rev_range = resolved
        changed_set = changed_modules(root, rev_range)
    else:
        note = "调用方显式指定改动模块（判据用）"
        changed_set = set(changed)

    loaded = imported_modules(root)
    trees = {n: t for n in trees_iter(root) if (t := module_tree(root, n)) is not None}
    class_index = classes_with_state(trees)
    stateful = state_bearing_modules(root) | instance_held_modules(trees, class_index)
    declared, problems = parse_declarations(root, version)

    auto_only: list[str] = []
    undeclared: list[str] = []
    for module in sorted(changed_set):
        if module in curated or module in declared:
            continue
        if module in loaded and module in stateful:      # stateful 含"实例被主窗持有"
            auto_only.append(module)
            continue
        undeclared.append(module)
    # 本轮零产品模块改动（只改了文档/构建件，或有人在落代码之前提前跑了一次）：
    # 这不是失败。把它判红会把人推向"改清单"而不是"看懂结论"，而清单那条申报本来没错。
    # 复核 F1 实测：`--range HEAD..HEAD` 时旧写法报的是「申报了本轮没改的模块」。
    if not changed_set:
        result.update({
            "ok": not problems,
            "empty_round": True,
            "version": version, "range": rev_range, "range_note": note,
            "changed": [], "curated": [], "auto_only": [], "undeclared": [],
            "stale_declarations": [], "redundant_declarations": [],
            "declaration_problems": problems,
            "counts": {"product_modules": len(mods), "curated": len(curated),
                       "stateful": len(stateful), "changed": 0, "declared": len(declared)},
        })
        return result
    stale = sorted(m for m in declared if m not in changed_set and m not in curated)
    # 规则③：**已经包覆的模块不许申报**。申报的语义是"这一项不需要进包"，把它用在
    # 已经在包里的模块上，等于给清单攒下一条"下一轮可以顺手继承"的免检牌 ——
    # 版本作用域能挡住跨轮继承，但挡不住同一轮里"反正已经报了"的懒得接线。
    redundant = sorted(m for m in declared
                       if m in changed_set and (m in curated
                                                or (m in loaded and m in stateful)))
    result.update({
        "ok": not undeclared and not problems and not stale and not redundant,
        "empty_round": False,
        "version": version, "range": rev_range, "range_note": note,
        "changed": sorted(changed_set), "curated": sorted(curated & changed_set),
        "auto_only": auto_only, "undeclared": undeclared,
        "stale_declarations": stale, "declaration_problems": problems,
        "redundant_declarations": redundant,
        "counts": {"product_modules": len(mods), "curated": len(curated),
                   "stateful": len(stateful), "changed": len(changed_set),
                   "declared": len(declared)},
    })
    return result


def report(root: pathlib.Path, version: str = "") -> str:
    """人读的分布：三档各有哪些模块（--report 用，不参与判定）。"""
    curated = sorted(m for m in curated_modules(root))
    loaded = imported_modules(root)
    stateful = sorted(state_bearing_modules(root))
    auto_only = [m for m in stateful if m not in curated and m in loaded]
    invisible = [m for m in product_modules(root)
                 if m not in curated and m not in stateful and m != "sf_diag"]
    del version
    return ("\n产品模块 %d 个\n"
            "  curated（导出面符号闭包读得到）  %d：%s\n"
            "  auto  （只有自动采集的模块级状态）%d：%s\n"
            "  两档都不沾（须逐条申报）        %d：%s\n"
            % (len(product_modules(root)), len(curated), ", ".join(curated),
               len(auto_only), ", ".join(auto_only),
               len(invisible), ", ".join(invisible)))


def fixture_repo(work: pathlib.Path, index: int, files: dict[str, str]) -> pathlib.Path:
    target = work / ("r%d" % index)
    target.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (target / name).write_text(body, encoding="utf-8")
    return target


def base_fixture() -> dict[str, str]:
    """一个"刚好够判据跑"的微型仓库：产品模块数过下限，导出面引用三个模块。"""
    files: dict[str, str] = {"sf_constants.py": 'APP_VERSION = "9.9.9"\n'}
    for index in range(MIN_PRODUCT_MODULES):
        files["sf_m%02d.py" % index] = "POLL_INTERVAL_M%02d = 1\n" % index
    files["sf_notify.py"] = "POLL_INTERVAL_N = 1\n"
    files["sf_glass.py"] = "POLL_INTERVAL_G = 1\n"
    files["sf_calendar.py"] = "POLL_INTERVAL_C = 1\n"
    files["sf_utils.py"] = "VALUE = 3\n"            # 无状态形状：两档都不沾
    files["stock_float_window.py"] = (
        'from sf_notify import delivery_stats as _stats_ref\n'
        "import sf_glass\nimport sf_calendar\n\n\n"
        "class FloatWindow:\n"
        "    def _export_diagnostics(self):\n"
        "        _snapshot(\"notify.json\", _stats_ref)\n"
        "        sf_glass.glass_diagnostics()\n"
        "        return sf_calendar.sync()\n\n"
        "    def _helper(self):\n"
        "        import sf_calendar\n"
        "        return sf_calendar.sync()\n")
    return files


def self_test(root: pathlib.Path | None = None) -> int:
    # `root` 可以不传：`tests/test_gate_selftests_in_suite.py` 那套同进程自检的调用约定是
    # `fn()` 无参（本工具的种错全在临时目录里造微型仓库，用不到真仓库根）。
    # 签名不一致的后果实测过——它抛 TypeError，而同进程清单把"抛异常"记成失败，
    # 于是新工具进清单这件事本身先红一次。
    """八处种错必须全部被抓到，外加三条正向对照（防"闸门只会红"）。"""
    import shutil
    import tempfile
    failures = 0
    work = pathlib.Path(tempfile.mkdtemp(prefix="sf_diagcov_"))
    counter = 0

    def build(files: dict[str, str]) -> pathlib.Path:
        nonlocal counter
        counter += 1
        return fixture_repo(work, counter, files)

    def note(text: str) -> None:
        nonlocal failures
        failures += 1
        print("   !! " + text)

    try:
        base = base_fixture()
        # 正向 A：改动三个 curated 模块 -> 通过
        r = check(build(base), version="9.9.9", changed={"sf_notify", "sf_glass"})
        if not r["ok"]:
            note("正向 A 误判：%s" % r["undeclared"])
        # 正向 B：本轮零产品改动 -> 如实标 empty_round 并放行（复核 F1：
        # 旧写法在这里判红，还把原因写成"申报了本轮没改的模块"，指错了地方）
        r = check(build(base), version="9.9.9", changed=set())
        if not (r["empty_round"] and r["ok"]):
            note("正向 B 失效：空轮次应当如实标注并放行（%s/%s）" % (r["empty_round"], r["ok"]))

        # S1 改动两档都不沾的模块且不申报 -> 必须判红
        r = check(build(base), version="9.9.9", changed={"sf_utils"})
        if "sf_utils" not in r["undeclared"]:
            note("S1 未抓到：无状态、未接线、未申报的改动模块被放过")

        # S2 同一处改动 + 当前版本段写了理由 -> 必须转绿
        files = dict(base)
        files[DECL_FILE] = "version 9.9.9\nskip sf_utils 纯函数工具层，无运行态可采\n"
        r = check(build(files), version="9.9.9", changed={"sf_utils"})
        if r["undeclared"] or not r["ok"]:
            note("S2 正当申报被误判：%s" % r["undeclared"])

        # S3 申报写在上一版段里 -> 必须仍然红（版本作用域）
        files = dict(base)
        files[DECL_FILE] = "version 9.9.8\nskip sf_utils 上一轮已说明\n"
        r = check(build(files), version="9.9.9", changed={"sf_utils"})
        if not r["undeclared"]:
            note("S3 未抓到：上一版的申报被本版继承了")

        # S4 申报没有理由 -> 必须红
        files = dict(base)
        files[DECL_FILE] = "version 9.9.9\nskip sf_utils\n"
        r = check(build(files), version="9.9.9", changed={"sf_utils"})
        if not r["declaration_problems"]:
            note("S4 未抓到：空理由被当成合法申报")

        # S5 申报了本轮没改的模块 -> 必须红（清单只增不减＝没人读）
        files = dict(base)
        files[DECL_FILE] = "version 9.9.9\nskip sf_m02 与本轮无关\n"
        r = check(build(files), version="9.9.9", changed={"sf_utils", "sf_notify"})
        if "sf_m02" not in r["stale_declarations"]:
            note("S5 未抓到：过期申报没人管")

        # S6 导出函数被改名（闭包取到空集）-> 必须拒绝下结论，不许"全都未包覆"当通过
        files = dict(base)
        files["stock_float_window.py"] = base["stock_float_window.py"].replace(
            "_export_diagnostics", "_export_diagnostics_renamed")
        r = check(build(files), version="9.9.9", changed={"sf_notify"})
        if not r["refuse"]:
            note("S6 未抓到：闭包算成空集时闸门还敢下结论")

        # S7 产品模块少于下限 -> 必须拒答（空输入当通过那一族）
        r = check(build({"sf_constants.py": 'APP_VERSION = "9.9.9"\n'}),
                  version="9.9.9", changed={"sf_notify"})
        if not r["refuse"]:
            note("S7 未抓到：只扫到几个模块就敢下结论")

        # S8 改动模块有状态、被主程序 import，但没人工接线 -> auto 档必须认下来并通过
        files = dict(base)
        files["sf_m03.py"] = ("POLL_INTERVAL_M03 = 1\n_last_fail_count = 0\n"
                              "def bump():\n    global _last_fail_count\n"
                              "    _last_fail_count += 1\n")
        files["stock_float_window.py"] = base["stock_float_window.py"].replace(
            "import sf_glass", "import sf_glass\nimport sf_m03")
        r = check(build(files), version="9.9.9", changed={"sf_m03"})
        if "sf_m03" not in r["auto_only"]:
            note("S8 未抓到：自动采集档没认出'状态在包里'的模块（%s）" % r["auto_only"])

        # S9 改动模块没被主程序 import（只在测试里用）-> auto 档不许算它通过
        files = dict(base)
        files["sf_m04.py"] = ("POLL_INTERVAL_M04 = 1\n_last_fail_count = 0\n"
                              "def bump():\n    global _last_fail_count\n"
                              "    _last_fail_count += 1\n")
        r = check(build(files), version="9.9.9", changed={"sf_m04"})
        if "sf_m04" in r["auto_only"] or "sf_m04" not in r["undeclared"]:
            note("S9 未抓到：装机版里根本没加载的模块被算成已被采集")
        # S11 空轮次里清单写成空理由 -> 仍然必须红（放行的是"没改动"，不是"没解析"）
        files = dict(base)
        files[DECL_FILE] = "version 9.9.9\nskip sf_utils\n"
        r = check(build(files), version="9.9.9", changed=set())
        if r["ok"] or not r["declaration_problems"]:
            note("S11 未抓到：空轮次把清单解析错误一起放过了")

        # S10 申报一个已经被导出面读到的模块 -> 必须红（攒免检牌那一族）
        files = dict(base)
        files[DECL_FILE] = "version 9.9.9\nskip sf_notify 顺手报一条\n"
        r = check(build(files), version="9.9.9", changed={"sf_notify"})
        if "sf_notify" not in r["redundant_declarations"]:
            note("S10 未抓到：已经包覆的模块可以随便申报")

    finally:
        shutil.rmtree(work, ignore_errors=True)
    return failures


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="diag_coverage_check.py",
        description="本轮改动的产品模块是否都被诊断包覆住（发版链 3.9 关）")
    parser.add_argument("--report", action="store_true", help="只看分布，不判定")
    parser.add_argument("--self-test", action="store_true", help="十一处种错必须全被抓到")
    parser.add_argument("--range", dest="rev_range", default="", help="覆盖本轮区间 A..B")
    parser.add_argument("--baseline", dest="baseline", default="",
                        help="上一版已发布的源码提交（取自发布记录的 source.head），区间=它..HEAD；"
                             "未给时读环境变量 SF_RELEASE_BASELINE，仍无才按版本号推断")
    parser.add_argument("--version", dest="version", default="", help="覆盖版本（判据用）")
    parser.add_argument("--dir", dest="root", default="", help="仓库根（默认按脚本位置解析）")
    known, unknown = parser.parse_known_args(argv)
    if unknown:
        print("!! 未知参数：%s（拒绝执行，绝不落到默认动作）" % " ".join(unknown))
        return 2
    root = pathlib.Path(known.root) if known.root else pathlib.Path(
        __file__).resolve().parent.parent

    if known.self_test:
        failures = self_test(root)
        if failures:
            print("诊断覆盖闸门自检失败 %d 处" % failures)
            return 1
        print("诊断覆盖闸门自检通过：11 处种错全部抓到，3 条正向对照成立")
        return 0

    if known.report:
        print(report(root))
        return 0

    baseline = known.baseline or os.environ.get("SF_RELEASE_BASELINE", "").strip()
    result = check(root, known.rev_range, known.version, baseline=baseline)
    if result["refuse"]:
        print("!! 诊断覆盖闸门无法下结论：%s" % result["refuse"])
        return 2
    if result["empty_round"]:
        print("   ⚠ 本轮没有产品模块改动（只改了文档/构建件，或代码还没落地）："
              "没有需要答复的现场，放行")
        if result["declaration_problems"]:
            for line in result["declaration_problems"]:
                print("   !! 申报清单：%s" % line)
            return 1
        return 0
    counts = result["counts"]
    print("== 诊断覆盖核对（本轮 %s｜区间 %s｜%s）=="
          % (result["version"], result["range"] or "-", result["range_note"]))
    print("   产品模块 %s ｜ curated %s ｜ 有状态的模块 %s ｜ 本轮改动 %s 个"
          % (counts["product_modules"], counts["curated"], counts["stateful"],
             counts["changed"]))
    if result["changed"]:
        print("   本轮改动：%s" % ", ".join(result["changed"]))
    if result["auto_only"]:
        print("   ⚠ 只靠自动采集进包（状态在包里、立意还没接）：%s"
              % ", ".join(result["auto_only"]))
    for line in result["declaration_problems"]:
        print("   !! 申报清单：%s" % line)
    if result["stale_declarations"]:
        print("   !! 申报了本轮没改的模块（清单只增不减＝没人读）：%s"
              % ", ".join(result["stale_declarations"]))
    if result["redundant_declarations"]:
        print("   !! 申报了**已经在包里**的模块（申报=这项不需要进包，别拿它当免检牌）：%s"
              % ", ".join(result["redundant_declarations"]))
    if result["undeclared"]:
        print("   !! 诊断包答不上来、且没申报理由的模块：%s" % ", ".join(result["undeclared"]))
        print("   处理：接进 _export_diagnostics（人工立意），或在 %s 当前版本段写" % DECL_FILE)
        print("         「skip <模块> <为什么不需要包>」；两者都不做就别出包。")
        return 1
    if not result["ok"]:
        return 1
    print("   诊断覆盖核对通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
