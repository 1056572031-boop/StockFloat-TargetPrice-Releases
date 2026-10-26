# -*- coding: utf-8 -*-
"""瘦身前置扫描：把「确定无用」与「有风险不能动」分开。

只做只读分析，不修改任何文件。输出三档结论：

  SAFE    全仓（含测试、spec、字符串常量）都找不到任何引用 —— 可直接删
  RISKY   疑似无用但存在反射/动态调用/外部契约风险 —— 保留，交人工确认
  USED    有引用，不动

判定"引用"时特意把**字符串常量**也算进去：本工程大量使用
`feature_enabled("anim_params")`、`getattr(self, "_x")`、`self.timer.setInterval(...)`
这类按名字间接调用，只看 `ast.Name` 会把活代码误判成死代码。

用法：
    ./.venv/bin/python tools/deadcode_scan.py [--json out.json]
"""
import argparse
import ast
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 不参与瘦身、但会引用符号的目录（只读来建引用语料）
REF_DIRS = ("tests",)
SKIP_DIRS = {"backups", "build", "dist", "__pycache__", ".git", ".venv",
             ".private_tools", "outputs", "fonts", "git-hooks"}


def iter_py(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                out.append(pathlib.Path(dirpath) / fn)
    return sorted(out)


def target_files():
    """被瘦身的对象：工程根下的源码 + tools/。"""
    out = [p for p in ROOT.glob("*.py")]
    out += [p for p in (ROOT / "tools").glob("*.py")]
    return sorted(out)


def ref_files():
    """引用语料：所有目标文件 + tests/ + tools/archive/。"""
    out = list(target_files())
    out += [p for p in (ROOT / "tools" / "archive").glob("*.py")]
    for d in REF_DIRS:
        out += iter_py(ROOT / d)
    return sorted(set(out))


def read(p):
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


# ---------------------------------------------------------------- 语料
class Corpus:
    """全仓出现过的标识符 / 字符串常量 / 属性名。"""

    def __init__(self, files):
        self.names = set()          # ast.Name(Load)
        self.attrs = set()          # ast.Attribute.attr
        self.strings = set()        # 字符串常量（动态调用会走这里）
        self.from_imports = {}      # 模块名 -> {被导入的名字}
        self.texts = {}
        for f in files:
            src = read(f)
            self.texts[f] = src
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    self.names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    self.attrs.add(node.attr)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.strings.add(node.value)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.from_imports.setdefault(node.module.split(".")[0], set()).update(
                        a.asname or a.name for a in node.names)
        # 纯文本语料（兜底：注释、文档字符串、spec 里的引用）
        # 注意**不能**把被分析文件自身算进来 —— 否则任何名字都会命中自己的定义处，
        # 结果全部被判成"有引用"，扫描就失去意义了。
        self.external_text = ""
        for extra in ("StockFloat.spec", "run_tests.sh", "build_release.sh"):
            fp = ROOT / extra
            if fp.exists():
                self.external_text += "\n" + read(fp)
        for d in REF_DIRS:
            for fp in iter_py(ROOT / d):
                self.external_text += "\n" + read(fp)

    def mentions(self, name, exclude=None):
        if name in self.names or name in self.attrs or name in self.strings:
            return True
        return self.mentioned_outside(name, exclude)

    def mentioned_outside(self, name, exclude=None):
        """在**除 exclude 之外**的所有文本里是否出现（含测试与打包脚本）。"""
        text = self.external_text
        for fp, src in self.texts.items():
            if exclude is not None and fp == exclude:
                continue
            text += "\n" + src
        return re.search(r"\b%s\b" % re.escape(name), text) is not None


# ---------------------------------------------------------------- 单文件分析
def module_stem(path):
    return path.stem


def imported_names(tree):
    """返回 [(绑定名, 原始全名, 节点行号)]。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bind = a.asname or a.name.split(".")[0]
                out.append((bind, a.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    continue
                bind = a.asname or a.name
                out.append((bind, (node.module or "") + "." + a.name, node.lineno))
    return out


def local_loads(tree):
    s = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            s.add(node.id)
        elif isinstance(node, ast.Attribute):
            cur = node
            while isinstance(cur, ast.Attribute):
                cur = cur.value
            if isinstance(cur, ast.Name):
                s.add(cur.id)
    return s


def unused_imports(path, tree, corpus):
    loads = local_loads(tree)
    src = read(path)
    used_text = {n.id for n in ast.walk(tree)
                 if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    stem = module_stem(path)
    reexported = corpus.from_imports.get(stem, set())
    out = []
    for bind, full, lineno in imported_names(tree):
        if bind in loads or bind in used_text:
            continue
        if bind in reexported:
            continue
        # 以字符串形式出现在同文件（getattr / __all__ / 文档）
        if re.search(r"""["']%s["']""" % re.escape(bind), src):
            continue
        out.append({"kind": "unused_import", "name": bind, "full": full,
                    "line": lineno})
    return out


def unused_module_level(path, tree, corpus):
    """模块级函数/类/变量：全仓无引用才算死代码。"""
    out = []
    stem = module_stem(path)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
            if name.startswith("__"):
                continue                       # dunder 属外部契约
            if corpus.mentions(name):
                continue
            out.append({"kind": "unused_def", "name": name, "line": node.lineno,
                        "type": "class" if isinstance(node, ast.ClassDef) else "def"})
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    # 全大写常量常被别的模块 from ... import 或按名取用
                    if corpus.mentions(t.id):
                        continue
                    if t.id in corpus.from_imports.get(stem, set()):
                        continue
                    out.append({"kind": "unused_const", "name": t.id,
                                "line": node.lineno, "type": "assign"})
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if not corpus.mentions(node.target.id):
                out.append({"kind": "unused_const", "name": node.target.id,
                            "line": node.lineno, "type": "annassign"})
    return out


def unused_locals(tree):
    """函数内赋值后再未读取的局部变量（纯死值）。"""
    out = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        stores, loads = {}, set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Store):
                    stores.setdefault(node.id, node.lineno)
                else:
                    loads.add(node.id)
            elif isinstance(node, ast.Attribute):
                cur = node
                while isinstance(cur, ast.Attribute):
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    loads.add(cur.id)
            elif isinstance(node, ast.arg):
                loads.add(node.arg)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                loads.update(node.names)
        for name, lineno in stores.items():
            # 下划线开头的一律跳过：那是"故意只写不读"的占位（解包位、回调签名位）
            if name in loads or name.startswith("_"):
                continue
            out.append({"kind": "unused_local", "name": name, "line": lineno,
                        "func": fn.name})
    return out


_COMMENT_CODE_HINT = re.compile(
    r"^\s*#\s*(def |class |if |for |while |return |import |from |try:|except|else:|"
    r"[A-Za-z_][A-Za-z0-9_]*\s*[=(]|\})")
# 说明性注释的特征词：含这些就不是"被注释掉的代码"，而是版本变更说明，必须保留
_NOTE_HINT = re.compile(r"(v1\.\d|第\d轮|修复|原因|注意|背景|为什么|已知|TODO|FIXME|历史|此前|原实现)")


def _looks_like_code(text):
    """去注释符 + 统一缩进后能否真的按 Python 解析 —— 能解析才算"注释掉的代码"。"""
    body = text.strip()
    if not body or len(body) > 400:
        return False
    try:
        ast.parse(body)
    except SyntaxError:
        # 整块解析失败时，逐行按"单条语句"再试一次（常见：注释掉了几行语句但缺上下文）
        ok = False
        for ln in body.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                ast.parse(ln)
                ok = True
            except SyntaxError:
                pass
        if not ok:
            return False
    # 必须是"代码感"强的行：赋值 / 调用 / 控制流 / self. 访问
    return bool(re.search(r"(self\.|=|\(|return |def |if |for |import )", body))


def commented_code_blocks(path, min_lines=2):
    """连续注释行、去注释符后能按 Python 解析、且不像说明文字 —— 才算注释掉的代码。

    刻意保守：本工程大量注释是"版本变更说明"（含 v1.2.x / 第N轮 / 修复 / 原因 等
    关键词），那是**知识资产**，删了以后没人知道这段逻辑为什么长这样。宁可漏报。
    """
    lines = read(path).splitlines()
    out, buf, start = [], [], 0
    for i, ln in enumerate(lines):
        if _COMMENT_CODE_HINT.match(ln) and not _NOTE_HINT.search(ln):
            if not buf:
                start = i + 1
            buf.append(ln)
        else:
            if len(buf) >= min_lines:
                body = "\n".join(re.sub(r"^\s*#\s?", "", x) for x in buf)
                if _looks_like_code(body):
                    out.append({"kind": "commented_code", "line": start,
                                "lines": len(buf), "name": "-"})
            buf = []
    if len(buf) >= min_lines:
        body = "\n".join(re.sub(r"^\s*#\s?", "", x) for x in buf)
        if _looks_like_code(body):
            out.append({"kind": "commented_code", "line": start,
                        "lines": len(buf), "name": "-"})
    return out


def _norm_stmts(fn):
    """把函数体归一化成语句指纹，用于找重复逻辑（忽略变量名与常量字面量）。"""
    out = []

    class V(ast.NodeVisitor):
        def generic_visit(self, node):
            if isinstance(node, ast.Name):
                out.append("N")
            elif isinstance(node, ast.Attribute):
                out.append("." + node.attr)
            elif isinstance(node, ast.Constant):
                out.append("C")
            else:
                out.append(type(node).__name__)
            super().generic_visit(node)

    V().visit(fn)
    return "|".join(out)


def duplicate_functions(files, min_len=60):
    """跨文件找函数体指纹完全相同的重复实现。"""
    seen = {}
    dups = []
    for f in files:
        src = read(f)
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            sig = _norm_stmts(fn)
            if len(sig) < min_len:
                continue
            key = sig
            if key in seen:
                dups.append({"kind": "duplicate_func",
                             "name": fn.name, "line": fn.lineno,
                             "file": str(f), "same_as": str(seen[key][0]),
                             "same_as_line": seen[key][1]})
            else:
                seen[key] = (f, fn.lineno, fn.name)
    return dups


def debug_prints(tree):
    """裸 print / pprint —— 生产程序里的调试输出。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("print", "pprint"):
                out.append({"kind": "debug_print", "name": node.func.id,
                            "line": node.lineno})
    return out


# ---------------------------------------------------------------- 全局项
def env_vars(files):
    used, defined = set(), set()
    pat_get = re.compile(r"""os\.environ(?:\.get\(|\[|\.setdefault\()\s*["']([A-Z0-9_]+)["']""")
    pat_pop = re.compile(r"""os\.environ\.pop\(\s*["']([A-Z0-9_]+)["']""")
    pat_def = re.compile(r"""["']([A-Z0-9_]{3,})["']\s*[:,]\s*os\.environ""")
    for f in files:
        src = read(f)
        used.update(pat_get.findall(src))
        used.update(pat_pop.findall(src))
        defined.update(pat_def.findall(src))
    return sorted(used), sorted(defined - used)


def requirement_deps():
    """requirements.txt 里声明但全仓从未 import 的依赖（PyInstaller 钩子除外）。"""
    p = ROOT / "requirements.txt"
    if not p.exists():
        return []
    deps = []
    for ln in read(p).splitlines():
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        deps.append(re.split(r"[=<>!~\[]", ln)[0].strip())
    corpus = "\n".join(read(f) for f in ref_files())
    spec = read(ROOT / "StockFloat.spec")
    for sh in ("run_tests.sh", "build_release.sh"):
        sp = ROOT / sh
        if sp.exists():
            corpus += "\n" + read(sp)
    # 发行包名 ≠ import 名，不加映射会把 pyobjc-* 全误报成未使用。
    #
    # 这一族为什么容易说谎：判定用的是「候选名在语料/spec 里出现过没有」，而语料全是 .py ——
    # 于是"某人在注释里写过 pyobjc-framework-AVFoundation"就能让一个包算"已用"，
    # 而只在 spec 的 hiddenimports 里以裸模块名出现的包（CoreMedia / CoreAudio）反被报成
    # "疑似未使用"。上一版就是这样：AVFoundation 侥幸过关，另外两个每次发版都被点名，
    # 而下一个人照这个结论去删 requirements.txt，装机版第一次开摄像头就弹不出授权框
    # （AGENTS §1：`requestAccessForMediaType:completionHandler:` 的第三个参数是 ObjC block，
    # 缺框架元数据时 PyObjC 直接 TypeError；三条"不加依赖"的绕法实测都不通）。
    #
    # 所以这三个显式映射到**模块名**：模块名在 `StockFloat.spec` 的 hiddenimports 里，
    # 那才是惰性 import 的唯一权威落点（惰性 import PyInstaller 静态分析看不见）。
    # 配套判据见 tests/test_dependency_alias_gate.py —— 含"requirements 新增一枚框架却
    # 没声明模块名落点"就必须红，别让同一个假阳性再长回来。
    alias = {
        "PyQt6-Qt6": ["PyQt6"],
        "PyQt6-sip": ["PyQt6"],
        "pyobjc-core": ["objc", "Foundation"],
        "pyobjc-framework-Cocoa": ["AppKit", "Cocoa"],
        "pyobjc-framework-Quartz": ["Quartz", "CoreGraphics"],
        "pyobjc-framework-UserNotifications": ["UserNotifications"],
        "pyobjc-framework-AVFoundation": ["AVFoundation"],
        "pyobjc-framework-CoreMedia": ["CoreMedia"],
        "pyobjc-framework-CoreAudio": ["CoreAudio"],
        "pyobjc-framework-Security": ["Security"],
        "pyinstaller": ["PyInstaller", "pyinstaller"],
        # setuptools 是 PyInstaller 的构建时依赖（Requires-Dist setuptools>=42）；
        # 显式锁版本供 OSV 审计，产品源码不会直接 import 它。
        "setuptools": ["PyInstaller", "pyinstaller"],
        "coverage": ["coverage"],
    }
    out = []
    for d in deps:
        for cand in alias.get(d, [d, d.replace("-", "_")]):
            if re.search(r"\b%s\b" % re.escape(cand), corpus) or \
               re.search(r"\b%s\b" % re.escape(cand), spec):
                break
        else:
            out.append(d)
    return out


# ---------------------------------------------------------------- 风险标注
# ---------------------------------------------------------------- 再导出契约
# stock_float_window.py 顶部有 9 个「本文件正文零使用」的符号，它们**不是死代码**，
# 而是对外的**再导出契约**：tests/ 下以 `m.符号` 直接访问（例如 test_render.py 的
# m.RED / m.GREEN / m.FG_DIM、test_migrate.py 的 m._migrate_legacy_config、
# test_core.py 的 m._drop_flash_due 与 m.fmt_pct、test_v165.py 的 m.POLL_DEGRADED /
# m.POLL_INTERVAL_DEGRADED、test_v123.py 的 m.POLL_INTERVAL_NORMAL）。
# 从 stock_float_window.py 删掉这些 import，测试会立刻变红。
#
# 为什么要显式白名单：v1.3.46 之前它们被判成 RISKY 只是**碰巧** —— 靠
# `tools/archive/verify_v1346.py` 里的字符串元组命中了上面的「名字出现在字符串常量」
# 规则。一旦那个核验脚本被改写或清理，这 9 个就会降级成 SAFE「可直接删」，
# 契约就没人守了。所以这里固化成白名单，命中即 RISKY，且 reason 写明禁止删除。
#
# 维护约定：增删/改名这 9 个符号，**必须同步** tests/test_reexport_contract.py。
# 第 9 个 `fmt_pct` 是 2026-10-12 清理批**发现**、2026-10-13 才并进白名单的：ruff 报它 F401
# （正文命中的是方法内第二条同名 import，判定没错），而 test_core.py 连用 6 次 m.fmt_pct 取值。
# 注意并进白名单**并不会让它出现在扫描报告里** —— 本扫描器的"未用导入"判定只看名字在文件中
# 是否出现、不区分作用域，所以它压根没把这条报成 unused_import。真正在守它的是两层：
# 本白名单（命中即 RISKY、禁止删除）+ tests 里的存在性判据（`m.fmt_pct` 取不到就红），
# 外加那条形状锁 —— 凡 ruff 报未用的顶层 import 都必须落在名单内，**漏登记下一个当场红**。
#
# 已知限制（刻意为之，请勿误当成精确匹配）：本白名单**按符号名生效、不区分文件** ——
# 只要名字命中就一律判成契约、不再看它属于哪个文件。当前这 9 个名字足够特异，
# 仓库内不存在同名的真死代码，按名匹配的收益远大于在 classify() 里传递文件上下文
# 的改动成本。若将来别处真的出现同名的死代码被本白名单包庇，就需要改成按文件路径
# （module_stem(path) == "stock_float_window" 之类的门槛）收口。
REEXPORT_CONTRACT = frozenset({
    "RED",
    "GREEN",
    "FG_DIM",
    "_migrate_legacy_config",
    "_drop_flash_due",
    "fmt_pct",
    "POLL_DEGRADED",
    "POLL_INTERVAL_DEGRADED",
    "POLL_INTERVAL_NORMAL",
})

RISKY_PATTERNS = (
    "feature_enabled", "getattr", "setattr", "globals(", "locals(",
    "__all__", "importlib", "eval(", "exec(",
)


GENERIC_NAMES = {"print", "pprint", "app", "io", "sys", "os", "-",
                 "commented_code", "debug_print", "unused_local",
                 "unused_import", "unused_const", "unused_def"}


def classify(path, item, corpus):
    """给出 SAFE / RISKY。"""
    name = item.get("name") or item.get("kind") or ""
    stem = module_stem(path)
    # 0) 通用名（print/app/io...）不参与"动态调用"推断，否则全是假阳性
    if name in GENERIC_NAMES:
        return ("SAFE" if item["kind"] != "debug_print"
                else "RISKY", "工具/CLI 自身的正常输出" if item["kind"] == "debug_print"
                else "通用名，无动态调用风险")
    # 0.5) 再导出契约白名单：测试以 m.符号 直访，正文零使用亦禁止删除
    if name in REEXPORT_CONTRACT:
        return "RISKY", "再导出契约（白名单），测试直访，禁止删除"
    # 1) 名字出现在任何字符串常量里 → 极可能按名动态取用
    if name in corpus.strings:
        return "RISKY", "名字以字符串形式出现（getattr/feature_enabled 类动态调用）"
    # 2) 被别的模块 from ... import（含 tests）
    if name in corpus.from_imports.get(stem, set()):
        return "RISKY", "被其他模块 from %s import %s" % (stem, name)
    # 3) 在其他文件 / spec / 脚本 / 测试里按名出现（排除本文件自身的定义）
    if corpus.mentioned_outside(name, exclude=path):
        return "RISKY", "在其他文件/spec/脚本/测试里出现，可能属外部契约"
    return "SAFE", "全仓无引用"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    tgts = target_files()
    refs = ref_files()
    corpus = Corpus(refs)

    report = {"files": [], "summary": {}}
    for path in tgts:
        src = read(path)
        if not src.strip():
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            report["files"].append({"file": str(path.relative_to(ROOT)),
                                    "error": "SyntaxError: %s" % e})
            continue
        items = []
        items += unused_imports(path, tree, corpus)
        items += unused_module_level(path, tree, corpus)
        items += [i for i in unused_locals(tree) if i["name"] != "_"]
        items += commented_code_blocks(path)
        if "tools" not in path.parts:
            items += debug_prints(tree)
        # 去重（同名同 kind 只保留第一条）
        seen, uniq = set(), []
        for it in items:
            key = (it["kind"], it.get("name"), it.get("line"))
            if key in seen:
                continue
            seen.add(key)
            lvl, why = classify(path, it, corpus)
            it["level"] = lvl
            it["why"] = why
            uniq.append(it)
        report["files"].append({"file": str(path.relative_to(ROOT)),
                                "items": sorted(uniq, key=lambda x: x["line"])})

    dups = duplicate_functions(tgts)
    report["duplicates"] = dups

    used_env, unused_env = env_vars(refs)
    report["env"] = {"used": used_env, "declared_not_used": unused_env}
    report["requirements_unused"] = requirement_deps()

    n_safe = sum(1 for f in report["files"] for i in f.get("items", [])
                 if i["level"] == "SAFE")
    n_risky = sum(1 for f in report["files"] for i in f.get("items", [])
                  if i["level"] == "RISKY")
    report["summary"] = {"files": len(report["files"]),
                         "safe": n_safe, "risky": n_risky,
                         "env_used": used_env,
                         "env_unused": unused_env,
                         "req_unused": report["requirements_unused"]}

    print("== 瘦身扫描：%s ==" % ROOT.name)
    print("   扫描文件 %d 个 ｜ SAFE %d 项 ｜ RISKY %d 项"
          % (report["summary"]["files"], n_safe, n_risky))
    if report["requirements_unused"]:
        print("   requirements 疑似未使用：%s" % ", ".join(report["requirements_unused"]))
    if used_env:
        print("   环境变量（在用）：%s" % ", ".join(used_env))
    if unused_env:
        print("   环境变量（声明未用）：%s" % ", ".join(unused_env))
    if dups:
        print("   疑似重复实现 %d 处：" % len(dups))
        for d in dups:
            print("      %s:%d %s  ≡  %s:%d %s"
                  % (pathlib.Path(d["file"]).name, d["line"], d["name"],
                     pathlib.Path(d["same_as"]).name, d["same_as_line"], d["name"]))
    print()
    for f in report["files"]:
        items = f.get("items")
        if not items:
            continue
        print("-- %s" % f["file"])
        for it in items:
            print("   [%-5s] L%-5d %-16s %-24s %s"
                  % (it["level"], it["line"], it["kind"], it.get("name", "-"),
                     it.get("why", "")))

    if a.json:
        pathlib.Path(a.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n明细已写入 %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
