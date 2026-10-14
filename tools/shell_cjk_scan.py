#!/usr/bin/env python3
"""闸门：shell 里 `$NAME` 后面紧跟中文（或任何非 ASCII）标点 —— 一律算 A 级硬零。

为什么这不是洁癖，而是"给用户跑的脚本会当场炸"（2026-10-13 实测）：
macOS 自带的 `/bin/sh` 与 `/bin/bash` 是同一份 **bash 3.2.57**。它在 **UTF-8 locale** 下
扫描变量名时，会把紧跟其后的多字节字符的**首字节当成名字的一部分**：

    DEST=/Applications/Skyrocket.app
    echo "旧版本来在 $DEST，但没能移到废纸篓"
    → /bin/sh: DEST\xEF: unbound variable      （set -u 下当场终止，rc 127）

吞掉的是全角逗号 `，`(U+FF0C) 的首字节 0xEF；`（`、`：`、`）` 同理。
不带 `set -u` 时更阴：变量值直接消失并吐出乱码（实测打印出 `C）`）。

而**这台开发机复现不了**：这里的 shell 没有设 LANG，走的是安全一侧；
用户机器的 AppleLocale 是 zh_CN，终端给的是 `LANG=zh_CN.UTF-8`，走的是必炸那一侧。
这与"来历标签"那一族是同一个结构：开发者永远站在机制的安全一侧。
`zsh` 免疫，但 `.command` 的执行由 shebang 决定（`#!/bin/sh`），躲不开。

正确写法只有一个：非 ASCII 紧跟变量时一律加花括号 —— `${DEST}，`（实测通过）。

判据侧的三条口径：
  · **单字符特殊变量不在此列**（`$1，` `$#，` `$?，` 实测都正常），所以只查 `[A-Za-z_][A-Za-z0-9_]*`；
  · **注释行不查**（不执行，且把"为什么有这条坑"写进注释恰恰是必要的）；
  · **扫描范围失效必须拒绝下结论**：shell 文件数少于 `MIN_SHELL_FILES` 就退 2 ——
    glob 对空目录与不存在的目录都安静返回空表（与 `tools/test_discipline_scan.py` 的
    `min_files` 同一族：零输入不等于全过）。

用法
    ./.venv/bin/python tools/shell_cjk_scan.py            # 有命中退 1
    ./.venv/bin/python tools/shell_cjk_scan.py --report    # 连上下文一起打
    ./.venv/bin/python tools/shell_cjk_scan.py --self-test # 种错必须被抓到
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# `$NAME` 之后紧跟一个非 ASCII 字符。`${NAME}，` 不在模式里（那是正确写法）。
HIT_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]")

# 要扫的文件形状：会进产物或进发版链的 shell，全部算，与 static_gate 同口径。
SHELL_SUFFIXES = (".sh", ".command")
SHELL_DIR = "git-hooks"
# git-hooks/ 里那一枚 `pre-commit` 没有扩展名，只能按目录收；但同目录的 README.md
# 不是 shell，得按"不是已知的非脚本扩展名"筛掉 —— 否则判据数出来的是文档，
# 而 MIN_SHELL_FILES 那条范围红线也跟着失真。
NON_SHELL_SUFFIXES = frozenset({".md", ".txt", ".json", ".yml", ".yaml", ".py", ".pyc"})
SKIP_PARTS = frozenset({".venv", "build", "node_modules", "__pycache__", ".git",
                        "dist", "backups", ".mypy_cache", ".ruff_cache"})

# 本仓当前实测 8 枚（2026-10-13：build_release / run_tests / update_helper /
# 两枚 tools/*.command / installer/*.command / git-hooks 两枚），下限留 2 枚余量。
MIN_SHELL_FILES = 6


class ScopeBroken(RuntimeError):
    """扫描范围失效 —— 这不是"通过"，是"没输入"。"""


def iter_shell_files(root: Path = ROOT) -> list[Path]:
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in SKIP_PARTS for part in rel_parts):
            continue
        rel = str(path.relative_to(root))
        if any(rel.startswith(prefix + "/") for prefix in ("tools/archive",)):
            continue
        if path.suffix in SHELL_SUFFIXES or (
                rel_parts[0] == SHELL_DIR and path.suffix not in NON_SHELL_SUFFIXES):
            found.append(path)
    return found


def find_hits(text: str) -> list[tuple[int, str, str]]:
    """返回 (行号, 命中片段, 整行) 。注释行跳过。"""
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for match in HIT_RE.finditer(line):
            hits.append((lineno, match.group(0), line.strip()))
    return hits


def scan(root: Path = ROOT, min_files: int = MIN_SHELL_FILES) -> list[tuple[str, int, str, str]]:
    """扫全仓 shell，返回 (相对路径, 行号, 命中片段, 整行)。范围塌了抛 ScopeBroken。"""
    files = iter_shell_files(root)
    if len(files) < min_files:
        raise ScopeBroken("只找到 %d 个 shell 文件（下限 %d）—— 扫描范围塌了，拒绝下结论"
                          % (len(files), min_files))
    out: list[tuple[str, int, str, str]] = []
    for path in files:
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            raise ScopeBroken("无法读取 shell 文件 %s：%s —— 扫描不完整，拒绝下结论"
                              % (path.relative_to(root), exc)) from exc
        for lineno, token, line in find_hits(body):
            out.append((str(path.relative_to(root)), lineno, token, line))
    return out


# ------------------------------------------------------------------ 变异自检

_SAMPLES = (
    # (说明, 文件内容, 期望命中条数)
    ("命名变量紧跟全角逗号", 'set -u\nDEST=/x\necho "旧版在 $DEST，但没能移走"\n', 1),
    ("命名变量紧跟全角括号", 'echo "   卷内脚本: $INSTALLER_BASE（已贴图标）"\n', 1),
    ("花括号写法必须放行", 'echo "旧版在 ${DEST}，但没能移走"\n', 0),
    ("位置参数不在此列", 'f(){ echo "参数是$1，结束"; }\n', 0),
    ("特殊变量不在此列", 'echo "计数=$#，"\n', 0),
    ("注释行不查", '# 坑在于 $DEST， 这种写法\n', 0),
    ("一行两处都要抓到", 'echo "$A，$B，"\n', 2),
)


def self_test() -> int:
    """种样例必须在**临时目录**里跑，绝不动仓库文件。"""
    bad: list[str] = []
    ok = 0
    with tempfile.TemporaryDirectory(prefix="sf_cjk_selftest_") as tmp:
        work = Path(tmp)
        # ① 逐条样例：命中数必须与期望一致
        for index, (label, body, want) in enumerate(_SAMPLES):
            got = find_hits(body)
            if len(got) != want:
                bad.append("样例「%s」期望 %d 条，实际 %d 条" % (label, want, len(got)))
            else:
                ok += 1
        # ② 整棵树：种一枚坏文件必须被 scan 抓到（scan 的范围判据同时得放行）
        tree = work / "tree"
        for name in ("good.sh", "bad.sh", "note.txt"):
            (tree / "git-hooks").mkdir(parents=True, exist_ok=True)
            (tree / name).write_text("echo hi\n", encoding="utf-8")
        (tree / "bad.sh").write_text('echo "$V，"\n', encoding="utf-8")
        hits = scan(tree, min_files=1)
        if [h[0] for h in hits] != ["bad.sh"]:
            bad.append("整棵树扫描结果不对：%s" % (hits,))
        else:
            ok += 1
        # ③ 空目录必须拒绝下结论（零输入不等于全过）
        try:
            scan(work / "empty", min_files=1)
            bad.append("空目录被当成通过：范围判据没生效")
        except ScopeBroken:
            ok += 1
        # ④ 好文件不该被 scan 看见（防"什么都算命中"的反向空转）
        clean = work / "clean"
        clean.mkdir()
        (clean / "a.sh").write_text('echo "${V}，"\n', encoding="utf-8")
        if scan(clean, min_files=1):
            bad.append("花括号写法被误报")
        else:
            ok += 1
        broken = work / "broken"
        broken.mkdir()
        (broken / "bad.sh").write_bytes(b'echo "$V\xff"\n')
        try:
            scan(broken, min_files=1)
            bad.append("不可解码的 shell 被静默跳过：扫描不完整却报告通过")
        except ScopeBroken:
            ok += 1
    print("== shell 中文标点自检：%d 项通过，%d 项失败 ==" % (ok, len(bad)))
    for one in bad:
        print("   · " + one, file=sys.stderr)
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    known = ("--report", "--self-test")
    unknown = [a for a in argv[1:] if a not in known]
    # 未知参数退 2，绝不落到默认动作里（AGENTS §3.1 v1.4.61 那条链上参数形状规矩）。
    if unknown:
        print("!! 不认识参数：%s\n   用法：shell_cjk_scan.py [--report|--self-test]"
              % " ".join(unknown), file=sys.stderr)
        return 2
    if "--self-test" in argv:
        return self_test()
    try:
        hits = scan()
    except ScopeBroken as exc:
        print("!! %s" % exc, file=sys.stderr)
        return 2
    files = iter_shell_files()
    if not hits:
        print("== shell 中文标点核验通过：%d 个文件，$NAME 紧跟非 ASCII 零命中 ==" % len(files))
        if "--report" in argv:
            for path in files:
                print("   · 扫过 %s" % path.relative_to(ROOT))
        return 0
    print("!! $NAME 紧跟中文标点，共 %d 处 —— bash 3.2 在用户终端的 UTF-8 locale 下" % len(hits))
    print("   会把多字节首字节吞进变量名，set -u 当场「unbound variable」终止：")
    for rel, lineno, token, line in hits:
        print("   · %s:%d  「%s」  %s" % (rel, lineno, token, line[:90]))
    print("   修法：写成 ${NAME}，（如 ${DEST}，）。扫过 %d 个文件，无基线、硬零。" % len(files))
    if "--report" in argv:
        for path in files:
            print("   · 扫过 %s" % path.relative_to(ROOT))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
