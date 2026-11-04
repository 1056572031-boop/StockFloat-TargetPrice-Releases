#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复 PyQt6 6.12.0 官方存根里的非法位置参数标记（上游打包缺陷）。

背景（2026-10-31 实测确证，不是推断）：
  - PyQt6 6.12.0 的 `site-packages/PyQt6/QtGui.pyi` 有 16 处、`QtDBus.pyi` 有 3 处写成
    `def f(self, a, *args: T, /)`，即 `/` 出现在 `*args` 之后。这不是合法的 PEP 570
    语法：本机 CPython 的 `ast.parse` 就直接报 "`/` must be ahead of `*`"。
  - 影响只落在**类型层**。mypy 必须解析被跟进的存根文件，遇到语法错误即以
    exit=2 终止并打印 "errors prevented further checking"，于是
    `tools/static_gate.py` 判"不算通过"，`run_tests.sh` 第一步就断。
    运行时与 PyInstaller 打包都不读 .pyi，产品行为与二进制不受影响。
  - 配置层面绕不过去，三条都实测过：`[mypy-PyQt6.*] ignore_errors = True`、
    全局 `follow_imports = skip`、`--exclude ^(...|\\.venv)/` —— 全部压不住。

做法：把 `/` 挪回 `*args` 之前（`def f(self, a, /, *args: T)`），语义正是存根想要
表达的"首参仅限位置 + 可变位置参数"，签名含义不变、类型信息不丢。

边界与规矩：
  - **幂等**：先用 `ast.parse` 判断，只有确实因这条规则解析失败的文件才改写；
    上游（Riverbank）发新版修好之后，本脚本自动变成 no-op。
  - **只认这一种错误**：改写后仍解析失败的，原样回滚该文件并报错退出，
    绝不留下半改状态，也绝不把别的语法问题一并"修掉"。
  - site-packages 属可再生构件：`uv pip install --require-hashes -r requirements.lock`
    重装即可回到上游原始状态，改动不进入 git、不进入 DMG。
  - 修复失败时闸门必须失败（沿用本闸门"检查器跑不起来＝不通过"的规矩）。

用法：
    .venv/bin/python tools/repair_pyqt6_stubs.py          # 检查并按需修复
    .venv/bin/python tools/repair_pyqt6_stubs.py --check   # 只报告，不写盘
退出码：0 = 存根可用（已修或本来就合法）；1 = 仍有问题，闸门应失败。
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import pathlib
import re
import sys

# `*args: <不含逗号与括号的类型>, /)` → `/, *args: <同一类型>)`
_PATTERN = re.compile(r"(\*args: [^,()]+?),\s*/\)")
_MSG = "/ must be ahead of *"


def pyqt6_stub_dir() -> pathlib.Path:
    spec = importlib.util.find_spec("PyQt6")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit("找不到 PyQt6 包，解释器 = %s" % sys.executable)
    return pathlib.Path(list(spec.submodule_search_locations)[0])


def repair_file(path: pathlib.Path, *, write: bool) -> int:
    """返回本次改写掉的行数；文件本来就合法时返回 0。"""
    text = path.read_text(encoding="utf-8")
    try:
        ast.parse(text)
        return 0
    except SyntaxError as exc:
        if _MSG not in str(exc.msg):
            print("  跳过 %s：语法错误与 PEP 570 无关（%s，line %s）"
                  % (path.name, exc.msg, exc.lineno))
            return 0

    patched, n = _PATTERN.sub(r"/, \1)", text), 0
    n = len(_PATTERN.findall(text))
    if not n:
        print("  !! %s：报 `%s` 却匹配不到可改写模式，需人工看" % (path.name, _MSG))
        return -1
    try:
        ast.parse(patched)
    except SyntaxError as exc:
        print("  !! %s：改写后仍解析失败（%s，line %s），保持原文件不动"
              % (path.name, exc.msg, exc.lineno))
        return -1
    if write:
        path.write_text(patched, encoding="utf-8")
        print("  已修 %s：%d 处 `%s` → 合法" % (path.name, n, "*args: T, /)"))
    else:
        print("  待修 %s：%d 处（--check 模式未写盘）" % (path.name, n))
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="只报告，不写盘")
    args = ap.parse_args()

    root = pyqt6_stub_dir()
    total = 0
    bad = 0
    for path in sorted(root.rglob("*.pyi")):
        r = repair_file(path, write=not args.check)
        if r < 0:
            bad += 1
        else:
            total += r

    # 复核：确认没有别的文件仍在非法状态
    remaining = []
    for path in sorted(root.rglob("*.pyi")):
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            remaining.append("%s(line %s: %s)" % (path.name, exc.lineno, exc.msg))

    if args.check:
        print("检查：%s 下待修 %d 处，仍非法 %d 个文件" % (root, total, len(remaining)))
        return 1 if remaining or bad else 0
    if bad or remaining:
        print("!! 存根仍不可被 mypy 解析：%s" % ("; ".join(remaining) or "见上方逐条"))
        return 1
    print("PyQt6 存根可解析（本次改写 %d 处，文件 %s）" % (total, root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
