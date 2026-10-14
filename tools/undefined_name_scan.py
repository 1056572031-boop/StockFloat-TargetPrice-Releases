# -*- coding: utf-8 -*-
"""tools/undefined_name_scan.py —— 静态查出"用了但本文件根本没有这个名字"。

为什么需要它
------------
本工程大量使用 `except Exception: pass` 兜底（2026-09-24 盘点：产品模块 206 处）。
这是刻意的韧性设计，但有一个致命副作用：**NameError 会被静默吞掉**，于是"某个功能
从写下起一次也没执行过"可以潜伏几十个版本没人发现。

真实案例（v1.4.29 复核）：`stock_float_window._raise_tool_window` 裸调 `_objc_deps()`，
而这个名字只存在于 sf_icons（sf_glass 导入了它，本模块没有）→ 每次调用都在真正动作
之前抛 NameError → 被同一个方法里的 `except: pass` 吞掉 → 工具窗层级提升从未生效。

与"给 206 处都加日志"相比，本工具查的是**当下正在发生的静默失效**，且零依赖
（只用标准库 ast），可以进提交钩子当关卡。

判定口径（刻意保守：宁可漏报，不误报）
--------------------------------------
  · 先扁平收集本文件里**所有**被绑定的名字（def/class/赋值/for/with/except 名/
    推导式目标/函数与 lambda 形参/import/global/nonlocal/del），
    再看"以 Load 方式出现的名字"里有哪些从来没绑定过。
  · 因此它抓的是"**这个名字根本不存在**"（= NameError 的来源），
    不抓"取到了另一个同名变量"那种作用域遮蔽问题 —— 那类靠测试。
  · 含 `exec`/`eval`/`globals()`/`from x import *` 的文件，名字可能在运行时才出现：
    照常报，但在结论里标"判定不完备"，由人确认，不假装干净。

用法
----
    ./.venv/bin/python tools/undefined_name_scan.py                 # 人读摘要
    ./.venv/bin/python tools/undefined_name_scan.py --strict        # 有命中则退出码 1
    ./.venv/bin/python tools/undefined_name_scan.py --json /tmp/u.json
"""
import argparse
import ast
import builtins
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 扫描范围：根目录下的产品模块。历史一次性打包脚本不在范围内。
PRODUCT_SKIP = {"build_v1240.py", "build_v1241.py", "build_v1242.py", "build_v1243.py"}

BUILTIN_NAMES = set(dir(builtins)) | {
    "__name__", "__file__", "__doc__", "__package__", "__spec__", "__loader__",
    "__builtins__", "__debug__", "__class__", "__path__", "__qualname__",
}

DYNAMIC_CALLS = {"exec", "eval", "globals", "locals", "vars", "__import__"}


def _bound_in_target(node, out):
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            _bound_in_target(e, out)
    elif isinstance(node, ast.Starred):
        _bound_in_target(node.value, out)
    # Attribute / Subscript 目标不是名字绑定


def _args_names(a):
    out = set()
    for arg in (list(getattr(a, "posonlyargs", [])) + list(a.args) + list(a.kwonlyargs)
                + ([a.vararg] if a.vararg else []) + ([a.kwarg] if a.kwarg else [])):
        out.add(arg.arg)
    return out


def scan(path):
    return analyze(path.name, path.read_text(encoding="utf-8", errors="replace"))


def analyze(name, src):
    """对一份源码字符串做判定。**测试直接调这个**，不必碰磁盘。

    留这个入口是为了关卡自身可被"变异测试"：往源码里种一个未定义名，
    `analyze` 必须报出来 —— 否则关卡哪天退化成"永远通过"就白设了
    （本工程为这种检查吃过好几次亏）。
    """
    try:
        tree = ast.parse(src, filename=name)
    except SyntaxError as exc:
        return {"file": name, "error": "SyntaxError: %s" % exc,
                "hits": [], "dynamic": False, "import_star": False}
    bound = set(BUILTIN_NAMES)
    loaded = {}
    dynamic = False
    import_star = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound |= _args_names(node.args)
        elif isinstance(node, ast.Lambda):
            bound |= _args_names(node.args)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                _bound_in_target(t, bound)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            _bound_in_target(node.target, bound)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _bound_in_target(node.target, bound)
        elif isinstance(node, ast.comprehension):
            _bound_in_target(node.target, bound)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    _bound_in_target(item.optional_vars, bound)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.name == "*":
                    import_star = True
                else:
                    bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound |= set(node.names)
        elif isinstance(node, ast.MatchAs):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node.ctx, ast.Load):
                loaded.setdefault(node.id, node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in DYNAMIC_CALLS:
                dynamic = True
    hits = [{"name": n, "line": line} for n, line in sorted(loaded.items(),
                                                            key=lambda kv: kv[1])
            if n not in bound]
    return {"file": name, "hits": hits, "dynamic": dynamic,
            "import_star": import_star, "error": None}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args(argv)
    root = pathlib.Path(args.root)
    files = sorted(p for p in root.glob("*.py") if p.name not in PRODUCT_SKIP)
    results = [scan(p) for p in files]
    total = sum(len(r["hits"]) for r in results)

    print("== 未定义名扫描（产品模块 %d 个文件）==" % len(files))
    for r in results:
        if r["error"]:
            print("  [跳过] %-30s %s" % (r["file"], r["error"]))
            continue
        flags = []
        if r["dynamic"]:
            flags.append("含 exec/eval/globals → 判定不完备")
        if r["import_star"]:
            flags.append("含 import * → 判定不完备")
        if r["hits"]:
            print("  [命中] %-30s %d 处  %s" % (r["file"], len(r["hits"]),
                                                 "；".join(flags)))
            for h in r["hits"]:
                print("        %s:%d  %s" % (r["file"], h["line"], h["name"]))
        elif flags:
            print("  [OK*]  %-30s %s" % (r["file"], "；".join(flags)))
    print("未定义名合计：%d 处" % total)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(results, ensure_ascii=False,
                                                      indent=2), encoding="utf-8")
        print("明细已写入", args.json)
    return 1 if (args.strict and total) else 0


if __name__ == "__main__":
    sys.exit(main())
