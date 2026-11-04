#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""改坏必红的唯一入口：锚点必须在文件里恰好出现 1 次，才允许落盘改坏。

为什么需要：手写 `src.replace(old, new, 1)` 时，`old` 若在文件里出现多次，换中的是
第一处，而目标用例碰的可能是另一处——"改坏之后仍然全绿"，把没有牙的判据误当成有牙
（2026-11-04 实测：`os.unlink(path)` 在 sf_config.py 里有 3 处，换中的不是被测那一处）。
本工具把"先数次数"做进入口：不是恰好 1 次就拒绝，源码一个字节都不动，命令也不跑。

它还替人做三件容易漏的事：改前先跑一遍命令确认基线是绿的（基线就红则"改坏后红"
毫无意义）；改坏与复原之后都清掉该文件的旧字节码（同一秒内的旧 .pyc 会让改坏被悄悄
绕过）；复原后逐字节读回核对，复原不了就大声喊并留下备份。备份放在系统临时目录，
外部超时杀（SIGTERM）时也会走到复原。

用法：
    tools/mutation_probe.py <文件> <原文> <改成> [--timeout 秒] -- <命令> [参数...]

退出码：0 改坏后命令失败（判据抓住了）；1 改坏后命令仍通过（判据没牙）；
2 参数错误、锚点不是恰好 1 次、改前命令就失败、或超时（都是"没得出结论"）；
3 复原失败（源码可能还停在被改坏的状态，备份路径会打印出来）。
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_TIMEOUT = 300.0
TAIL_CHARS = 800
EXIT = {"caught": 0, "survived": 1, "refused": 2, "control_failed": 2, "timeout": 2,
        "restore_failed": 3}


def clear_bytecode(path):
    """删掉这个源文件对应的全部 .pyc（按解释器版本命名的那一批）。"""
    path = Path(path)
    for stale in (path.parent / "__pycache__").glob(path.stem + ".*.pyc"):
        stale.unlink(missing_ok=True)


def run_command(command, timeout):
    """跑一次验证命令。超时时 returncode 为 None。"""
    try:
        done = subprocess.run(command, capture_output=True, text=True, errors="replace",
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"returncode": None, "tail": ""}
    return {"returncode": done.returncode, "tail": (done.stdout + done.stderr)[-TAIL_CHARS:]}


def restore(path, original):
    """写回原始字节、清旧字节码，并读回核对。成功返回 True。"""
    try:
        Path(path).write_bytes(original)
        clear_bytecode(path)
        return Path(path).read_bytes() == original
    except OSError:
        return False


def terminate(signum, frame):
    """外部超时杀（SIGTERM）也要走到 finally 去复原源码。"""
    raise SystemExit(128 + signum)


def install_terminate_handler():
    """装上 SIGTERM 处理函数，返回需要装回去的旧处理函数；装不上（非主线程）返回 None。"""
    try:
        return signal.signal(signal.SIGTERM, terminate) or signal.SIG_DFL
    except ValueError:
        return None


def probe(path, old, new, command, timeout=DEFAULT_TIMEOUT):
    """对 `path` 做一次改坏验证，返回带 status 的字典。"""
    path = Path(path)
    original = path.read_bytes()
    text = original.decode("utf-8")
    count = text.count(old) if old else 0
    if count != 1:
        return {"status": "refused", "count": count}
    control = run_command(command, timeout)
    if control["returncode"] is None:
        return {"status": "timeout", "phase": "control"}
    if control["returncode"] != 0:
        return {"status": "control_failed", **control}
    with tempfile.NamedTemporaryFile(prefix="mutation_probe_", suffix=".bak", delete=False) as handle:
        handle.write(original)
    backup = handle.name
    previous = install_terminate_handler()
    restored = False
    try:
        path.write_bytes(text.replace(old, new, 1).encode("utf-8"))
        clear_bytecode(path)
        mutated = run_command(command, timeout)
    finally:
        restored = restore(path, original)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        if restored:
            os.unlink(backup)
        else:
            sys.stderr.write("复原失败：%s 可能还停在被改坏的状态，原始字节在备份 %s\n" % (path, backup))
    if not restored:
        return {"status": "restore_failed", "backup": backup}
    if mutated["returncode"] is None:
        return {"status": "timeout", "phase": "mutated", "backup": backup}
    status = "caught" if mutated["returncode"] != 0 else "survived"
    return {"status": status, "backup": backup, **mutated}


def report(result, path):
    """把结果翻成一句人话。"""
    status = result["status"]
    if status == "caught":
        return ("抓住了：改坏后命令退出码 %d（判据有牙）。输出尾部：\n%s"
                % (result["returncode"], result["tail"]))
    if status == "survived":
        return "没牙：改坏之后命令仍然通过，这条判据管不到被改的那处。输出尾部：\n%s" % result["tail"]
    if status == "refused":
        return ("拒绝改坏：锚点在 %s 里出现了 %d 次，必须恰好 1 次（源码未动，命令未跑）"
                % (path, result["count"]))
    if status == "control_failed":
        return ("没得出结论：改前命令就失败了（退出码 %d），基线不绿时\"改坏后红\"没有意义，源码未动。"
                "输出尾部：\n%s" % (result["returncode"], result["tail"]))
    if status == "timeout":
        where = "改前（源码未动）" if result["phase"] == "control" else "改坏后（源码已复原）"
        return "没得出结论：命令在%s超时。" % where
    return ("复原失败！原始字节的备份在：\n%s\n请手工把它拷回 %s 再继续。"
            % (result["backup"], path))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    head, separator, command = _split_at_separator(argv)
    parser = argparse.ArgumentParser(description="锚点恰好出现 1 次才许落盘的改坏验证")
    parser.add_argument("file", help="要改坏的源文件")
    parser.add_argument("old", help="锚点原文，必须在文件里恰好出现 1 次")
    parser.add_argument("new", help="改成什么")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="命令超时秒数")
    args = parser.parse_args(head)
    if not separator or not command:
        parser.error("缺验证命令：在三个位置参数之后写 `-- 命令 [参数...]`")
    result = probe(args.file, args.old, args.new, command, args.timeout)
    print(report(result, args.file))
    return EXIT[result["status"]]


def _split_at_separator(argv):
    if "--" not in argv:
        return argv, False, []
    index = argv.index("--")
    return argv[:index], True, argv[index + 1:]


if __name__ == "__main__":
    sys.exit(main())
