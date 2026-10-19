#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二道防线：打包闭包核验 —— 产品代码 import 的每个模块，必须真的在 .app 里。

用法：
    ./.venv/bin/python tools/bundle_closure_check.py dist/实时股价-目标价版.app
    ./.venv/bin/python tools/bundle_closure_check.py --self-test      # 任何机器都能跑
    ./.venv/bin/python tools/bundle_closure_check.py <app> --self-test  # 再加验真产物层
    ./.venv/bin/python tools/bundle_closure_check.py <app> --json

为什么需要它（不是"打包成功就算过了"）：
    PyInstaller 靠静态分析决定把哪些模块装进包里。**函数里的惰性 import、
    只在某条分支上才走到的 import，它看不见**；补漏的手段是 StockFloat.spec 的
    hiddenimports —— 而那是一份**靠人记得写**的清单。
    真实事故：v1.3.74 漏打包 `_cffi_backend`（PyNaCl 的 Ed25519 验签要它），
    表现为已安装程序点「检查更新」直接中断；同类问题 V1.4.7 的 LOAD_SMALL_INT、
    v1.3.83 的弹窗模块也各踩过一次。每次的修复都是"往 spec 里补一行"，
    但**没有任何一关会问一句：现在代码里 import 的东西，包内齐不齐**。
    本工具就是把这一问问成自动的。

判定口径（宁可漏报噪声，不可误报正常）：
    · 只比**顶层模块名**（`import a.b.c` 记 a）—— 打包缺失的粒度就是顶层；
    · 可用集合 = 归档条目（CArchive + PYZ）∪ Frameworks/Resources 里的顶层目录名
      与 *.so/*.dylib 的模块名。必须并上磁盘那一半：实测 `_cffi_backend` 与
      `PyQt6/QtCore.so` 都以**扩展文件**形式存在，只查归档条目会把它们误判成缺失；
    · 标准库用 `sys.stdlib_module_names` 排除，不进包也不报。

诚实边界：
    · 它证明的是"包内 import 闭包完整"，**不证明运行时一定不崩**（真跑一遍由
      tools/smoke_frozen_app.py 负责）；
    · 动态拼出来的模块名（`importlib.import_module(var)`）取不到，这类站点会漏判 ——
      脚本会把它们单独列出来提示人工确认，不静默跳过。
"""
from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_dmg_pyz as VD          # noqa: E402  复用同一个归档读取实现

# 这些名字由解释器/打包器在运行时提供，不该进包也不算缺失
RUNTIME_PROVIDED = {
    "__main__", "__future__", "builtins", "sys", "os", "time", "threading",
}
# 确认过的"由 rthook / 引导注入"的名字（新增要写清为什么）
ALLOW_MISSING: set[str] = set()

DYNAMIC_IMPORT_FUNCS = {"import_module", "__import__"}
# Maintenance-only modules are kept in the source checkout for the private
# feedback workflow but are deliberately excluded from the user application.
# Scanning them as application roots would make an admin-only GitHub adapter
# look like a missing frozen-app dependency.
DEVELOPER_ONLY_SOURCE_MODULES = {"sf_feedback_admin.py"}


def collect_imports(product_dir: pathlib.Path) -> tuple[set[str], list[str]]:
    """AST 扫产品根模块，返回 (顶层导入名集合, 动态导入站点说明)。

    覆盖：`import a.b`、`from a.b import c`（含函数内惰性导入 —— 正是静态打包
    最容易漏的那类）、`__import__("a")` / `importlib.import_module("a")` 的字符串量。
    """
    tops: set[str] = set()
    dynamic: list[str] = []
    for path in sorted(product_dir.glob("*.py")):
        if path.name in DEVELOPER_ONLY_SOURCE_MODULES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            dynamic.append("%s 解析失败：%s（本工具对它的判定无效）" % (path.name, exc))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    tops.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:          # 相对导入不产生顶层名
                    tops.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else (
                    fn.attr if isinstance(fn, ast.Attribute) else "")
                if name in DYNAMIC_IMPORT_FUNCS and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        tops.add(arg.value.split(".")[0])
                    else:
                        dynamic.append("%s:%d 模块名是运行时算出来的，无法静态核验"
                                       % (path.name, node.lineno))
    return tops, dynamic


def _ext_module_name(fname: str) -> str:
    """`_cffi_backend.cpython-314-darwin.so` -> `_cffi_backend`；`QtCore.abi3.so` -> `QtCore`。"""
    stem = fname.split(".")[0]
    return stem


def judge_missing(tops, avail, stdlib, allow_missing):
    """纯判定：哪些顶层导入在包里找不到。抽出来的理由是它得能在**任何机器**上被验。

    原来这段判断写在 `check()` 里，而 `check()` 需要一个真 .app —— 于是自检也只能
    在打包机上跑（CI 上 dist/ 不存在就整段 skipTest，等于这条闸门在没有产物的环境里
    从不执行）。本仓为同一件事修过 release_feature_check（复核 F8），这里补上第二处。
    """
    ok, missing = [], []
    for name in sorted(tops):
        if name in allow_missing:
            continue
        if name in avail:
            ok.append(name)
            continue
        if name in stdlib:
            continue
        missing.append(name)
    return ok, missing


def bundle_available(app: pathlib.Path) -> set[str]:
    """包内"能被 import 到的顶层名"：归档条目 + 磁盘扩展/包目录。"""
    bundle = VD.Bundle(app)
    try:
        entries = bundle.entries()
    finally:
        bundle.dispose()
    avail: set[str] = set()
    for e in entries:
        e = str(e).replace("\\", "/")
        avail.add(e.split("/")[0])
        if "." in e and not e.endswith((".so", ".dylib", ".py", ".txt")):
            avail.add(e.split(".")[0])
    for sub in ("Frameworks", "Resources", "MacOS"):
        d = app / "Contents" / sub
        if not d.exists():
            continue
        for child in d.iterdir():
            avail.add(child.stem)
            if child.is_dir():
                avail.add(child.name)
            elif child.suffix in (".so", ".dylib"):
                avail.add(_ext_module_name(child.name))
    avail |= RUNTIME_PROVIDED
    return avail


def check(app: pathlib.Path,
          product_dir: pathlib.Path | None = None) -> dict:
    product_dir = product_dir or ROOT
    tops, dynamic = collect_imports(product_dir)
    avail = bundle_available(app)
    ok, missing = judge_missing(tops, avail, set(sys.stdlib_module_names), ALLOW_MISSING)
    return {
        "app": str(app),
        "checked": len(tops),
        "present": ok,
        "missing": missing,
        "dynamic_sites": dynamic,
        "allow_missing": sorted(ALLOW_MISSING),
    }


def self_test(app: pathlib.Path | None = None) -> int:
    """两层自检：离线判定层**任何机器都必须跑**，真产物层只在有 .app 时加验。

    刻意不因为"没有产物"就跳过全部：一条只在打包机上生效的自检，等于在其余环境
    不存在（本仓同类事故：清单核验器写死旧特征连续 PASS 约 54 个版本；
    以及"看到红就改数字"那批被证伪的门）。
    """
    bad = []
    # ---- 离线层：收集器 + 判定 ----
    tops, dynamic = collect_imports(ROOT)
    if "PyQt6" not in tops or "sf_constants" not in tops:
        bad.append("连产品代码自己的 import 都没收集到（tops=%d）→ 收集器已失效" % len(tops))
    if not isinstance(dynamic, list):
        bad.append("动态导入站点没有以列表返回（这类站点最易漏判，必须单独列出来）")
    std = set(sys.stdlib_module_names)
    ok, missing = judge_missing({"sf_constants", "PyQt6", "json", "_cffi_backend"},
                                {"sf_constants", "PyQt6"}, std, set())
    if "_cffi_backend" not in missing:
        bad.append("包里没有 _cffi_backend 却没被判成缺失 —— v1.3.74 那类漏打包"
                   "（已安装程序点「检查更新」就中断）今天照样过得去")
    if "json" in missing or "sf_constants" in missing:
        bad.append("标准库/已在包内的名字被判成缺失（会把每次正常发版拦死）")
    ok2, missing2 = judge_missing({"nacl"}, set(), std, {"nacl"})
    if missing2:
        bad.append("ALLOW_MISSING 名单没生效：确认过由引导注入的名字仍被判缺失")
    if ok2:
        bad.append("ALLOW_MISSING 的名字被当成「已在包内」（放行≠命中，两者不能混，"
                   "否则白名单会变成一条永不失败的通路）")
    # ---- 真产物层（可选，但跑了就必须严格）----
    layer = "离线判定层"
    if app is not None and app.exists():
        layer += " + 真产物层"
        avail = bundle_available(app)
        for known in ("sf_constants", "PyQt6", "_cffi_backend", "nacl"):
            if known not in avail:
                bad.append("包内确实存在的 %s 被判成不可用 → 可用集合构造有误" % known)
        if "definitely_not_packaged_xyz" in avail:
            bad.append("凭空出现的名字出现在可用集合里 → 判定不可信")
        fake = check(app, product_dir=ROOT)
        if fake["checked"] < 15:
            bad.append("只核验了 %d 个顶层模块，疑似扫描范围塌缩" % fake["checked"])
        if fake["missing"]:
            bad.append("当前 dist 产物缺模块：%s" % fake["missing"])
    else:
        if app is not None:
            print("   （未给产物路径或产物不存在，只跑离线判定层：真产物那一层本轮没有验）")
    for line in bad:
        print("!! 打包闭包核验自检失败：", line)
    if bad:
        return 1
    print("   打包闭包自检通过（%s）：收集器有效、缺失/放行判定正确、无凭空命中" % layer)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="核验 .app 的 import 闭包完整性")
    ap.add_argument("app", nargs="?", help="已打包的 .app 路径")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    app = pathlib.Path(args.app) if args.app else None
    if args.self_test:
        # 不传产物也要能跑：离线判定层在任何机器上都验，真产物层有就跑、没有就明说没验
        return self_test(app)
    if app is None:
        ap.error("需要一个 .app 路径；只想验判定本身用 --self-test")
    if not app.exists():
        print("!! 产物不存在：%s" % app)
        return 2

    r = check(app)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("== 打包闭包核验：%s ==" % app)
        print("   代码 import 的顶层模块 %d 个，包内可用 %d 个"
              % (r["checked"], len(r["present"])))
        for site in r["dynamic_sites"]:
            print("   [需人工确认] %s" % site)
        if r["missing"]:
            print("!! 包内缺失 %d 个：" % len(r["missing"]))
            for m in r["missing"]:
                print("   -", m)
            print("   修法二选一：① 加进 StockFloat.spec 的 hiddenimports 重新打包；"
                  "② 确认这模块本就不该进包，则从产品代码里去掉该 import。")
            return 1
        print("   import 闭包完整")
    return 1 if r["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
