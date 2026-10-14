# -*- coding: utf-8 -*-
"""临时目录旧副本清扫：把系统临时目录里"还能被启动的本程序旧副本"改名中性化。

为什么需要（产品侧自愈）
    2026-09-22 / 09-23 两份"程序意外退出"报告（均为 v1.3.97）的崩溃路径都是
    `/private/tmp/*/实时股价-目标价版.app` —— 不是「应用程序」里的正式版，而是历史
    验证与发版过程遗留在临时目录的旧副本被系统重新拉起。崩溃发生在 dlopen
    PyQt6/QtCore.abi3.so 的静态初始化里（QLibraryInfo → CFBundleCopyBundleURL 取到
    NULL bundle → __CFCheckCFInfoPACSignature 读 0x8），**早于本程序任何 Python 代码**。
    v1.4.5 的 sf_bootstrap 守卫只存在于新产物内，对 ≤1.4.4 的残留副本无效，
    旧副本自身也无法再打补丁，所以唯一手段是让那些目录不再被 LaunchServices
    识别为应用：改名去掉 .app 后缀即可，不删任何文件。

安全边界（改动本模块时不要放宽）
    1. 只扫系统临时目录（/tmp、/var/tmp、$TMPDIR、/private/var/folders），
       永不进入 /Applications 与工程目录；
    2. 只处理 CFBundleIdentifier 等于本程序包标识的目录；Info.plist 已被系统回收
       而读不到时，退化为按本程序产物名识别，且要求 Contents/MacOS 确有可执行文件
       （测试夹具造的空壳 .app 因此不会被误伤）；
    3. 正在运行的副本、以及本进程自己所在的 bundle 一律跳过；
    4. 一律 os.rename 加 .stale 后缀，不删除任何文件 —— 去掉后缀即可还原。

依赖：只用标准库。调用方（stock_float_window）在后台线程里跑，失败静默降级。
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile

from sf_constants import (APP_DISPLAY_NAME, BUNDLE_ID, LEGACY_APP_DISPLAY_NAMES,
                          LEGACY_BUNDLE_IDS)

SUFFIX = ".stale"
APP_DIR_NAME = APP_DISPLAY_NAME + ".app"
# Info.plist 缺失时的兜底识别名单（见模块 docstring 第 2 条）。
# v1.4.55 起把**旧包名**也算进来：改名前留在 /tmp 里的那些完整可启动副本同样会被
# LaunchServices 拉起并在 PyQt6 dlopen 期 SIGSEGV（v1.4.5 事故），漏扫就是留着那颗雷。
# 这不是放宽范围 —— 仍然是「只认自己这一款产品」，只是同一款产品的两代名字。
OWN_APP_NAMES = frozenset({APP_DIR_NAME}
                          | {n + ".app" for n in LEGACY_APP_DISPLAY_NAMES})
# 扫描深度：/private/tmp/xxx/App.app == 2，/private/var/folders/a/b/T/xxx/App.app ≈ 5
DEFAULT_MAX_DEPTH = 6


def temp_roots() -> list[str]:
    """本机所有"系统临时目录"根，realpath 后去重。

    为什么 $TMPDIR 与 /private/var/folders 要**同时保留**（v1.4.24 端到端实测结论）：
    扫描按"相对根的层数"截断（DEFAULT_MAX_DEPTH）。若因为包含关系丢掉 $TMPDIR，
    临时目录内的可用层数就从 6 层被 /private/var/folders/<u>/<hash>/T 这段前缀吃掉 4 层、
    只剩 2 层，实测会把 `$TMPDIR/ovl2/wy/T/实时股价-目标价版.app` 这类嵌套副本**漏掉**。
    重叠根导致的"同一副本命中两次"改由 find_stale_apps 按 realpath 去重解决
    （realpath 后两条根产生完全相同的绝对路径，去重有效），既保住覆盖面又不写假故障日志。
    """
    candidates = ["/tmp", "/var/tmp", tempfile.gettempdir(), "/private/var/folders"]
    roots: list[str] = []
    for item in candidates:
        try:
            real = os.path.realpath(item)
        except OSError:
            continue
        if os.path.isdir(real) and real not in roots:
            roots.append(real)
    return roots


PS_MARKER = ".app/Contents/"


def bundle_paths_from_ps(ps_output: str) -> set[str]:
    """从 `ps -Ao comm=` 的文本里解析出所有 .app bundle 路径（纯函数，可单测）。

    拆成纯函数是 v1.4.24 复核 F3 的要求：原来只有"把 running_bundle_paths 整个
    patch 成假数据再断言会跳过"的用例，等于只测了"如果认得出来就会跳过"，没测
    "到底认不认得出来"。本应用 bundle 名是中文，一旦 ps 的输出格式或编码处理变化，
    解析会静默返回空集，那条"跳过正在运行副本"的保护网就消失了且没人报警。
    """
    paths: set[str] = set()
    for line in ps_output.splitlines():
        line = line.strip()
        if PS_MARKER not in line:
            continue
        try:
            paths.add(os.path.realpath(line.split(PS_MARKER, 1)[0] + ".app"))
        except OSError:
            continue
    return paths


def running_bundle_paths() -> set[str]:
    """当前正在运行的进程所持有的 .app 路径，用于避免误伤正在使用的副本。"""
    try:
        out = subprocess.run(["/bin/ps", "-Ao", "comm="],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    paths = bundle_paths_from_ps(out)
    # 本进程自己所在的 bundle（源码运行时不含 .app，自然为空）
    try:
        exe = os.path.realpath(getattr(sys, "executable", ""))
    except OSError:
        exe = ""
    if PS_MARKER in exe:
        paths.add(os.path.realpath(exe.split(PS_MARKER, 1)[0] + ".app"))
    return paths


def bundle_identifier(app_dir: str) -> str | None:
    """读取 .app 的 CFBundleIdentifier；读不到返回 None（视为"Info.plist 已缺失"）。"""
    plist = os.path.join(app_dir, "Contents", "Info.plist")
    try:
        with open(plist, "rb") as handle:
            data = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    value = data.get("CFBundleIdentifier") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def version_of(app_dir: str) -> str:
    """读一份 .app 的 CFBundleShortVersionString，只为了清扫日志能说清"中性化的是哪一版"。

    读不到（没有 Info.plist / 解析失败 / 字段缺失）一律返回 "?"：这个值**不参与任何判定** ——
    归属看的是 bundle id（BUNDLE_ID + LEGACY_BUNDLE_IDS 两代标识），运行中与否看的是进程，
    版本号只出现在"本轮改名 N 个"那行日志里。所以 "?" 不代表异常，别拿它当缺失来报警。
    """
    plist = os.path.join(app_dir, "Contents", "Info.plist")
    try:
        with open(plist, "rb") as handle:
            data = plistlib.load(handle)
        value = data.get("CFBundleShortVersionString")
        return str(value) if value else "?"
    except (OSError, ValueError, plistlib.InvalidFileException):
        return "?"


def is_launchable(app_dir: str) -> bool:
    """Contents/MacOS 下有真实可执行文件才算"还能被启动"。"""
    macos = os.path.join(app_dir, "Contents", "MacOS")
    try:
        with os.scandir(macos) as entries:
            return any(e.is_file(follow_symlinks=False) for e in entries)
    except OSError:
        return False


def find_stale_apps(roots: list[str], bundle_id: str = BUNDLE_ID,
                    max_depth: int = DEFAULT_MAX_DEPTH) -> list[tuple[str, str]]:
    """在临时目录里找出本程序的残留 .app，返回 (路径, 识别方式) 列表。

    认 `bundle_id` **以及**改名的前代 id（`LEGACY_BUNDLE_IDS`）—— 同一款产品的
    两代标识；第三方 app 仍然一个都不碰（AGENTS §3.1 那五条边界原样保留）。
    """
    own_ids = {bundle_id} | set(LEGACY_BUNDLE_IDS)
    found: list[tuple[str, str]] = []
    for root in roots:
        for base, dirs, _files in os.walk(root, followlinks=False):
            depth = base.rstrip(os.sep).count(os.sep) - root.rstrip(os.sep).count(os.sep)
            if depth >= max_depth:
                dirs[:] = []
                continue
            for name in list(dirs):
                if not name.endswith(".app"):
                    continue
                # 任何 .app 都不再深入（Frameworks 目录很大，且包内不会再有应用包）
                dirs.remove(name)
                path = os.path.join(base, name)
                try:
                    identifier = bundle_identifier(path)
                except OSError:
                    continue
                if identifier in own_ids:
                    if is_launchable(path):
                        found.append((path, "bundle-id"))
                elif identifier is None and name in OWN_APP_NAMES and is_launchable(path):
                    found.append((path, "Info.plist 缺失，按包名识别"))
    # 调用方传入重叠根时同一副本会被扫到多次；这里按 realpath 去重，保证一份副本
    # 只被改名一次，第二次的 FileNotFoundError 不会被误记成"改名失败"（复核 F1）。
    unique: dict[str, tuple[str, str]] = {}
    for path, reason in found:
        unique.setdefault(os.path.realpath(path), (path, reason))
    return sorted(unique.values())


def neutralize(app_dir: str) -> str:
    """改名去掉 .app 后缀；重名时追加序号。返回新路径。"""
    target = app_dir + SUFFIX
    index = 1
    while os.path.exists(target):
        target = "%s%s.%d" % (app_dir, SUFFIX, index)
        index += 1
    os.rename(app_dir, target)
    return target


def sweep_temporary_copies(roots: list[str] | None = None, log=None) -> int:
    """执行一次清扫，返回被改名的副本数。任何单项失败都不影响其余项。

    `log` 传入可写一行文本的回调（如 sf_logging.log_line）用于留痕；
    本函数自身不抛异常，调用方无需兜底。
    """

    def note(message: str) -> None:
        if log is None:
            return
        try:
            log(message)
        except Exception:
            pass

    changed = 0
    try:
        hits = find_stale_apps(roots if roots is not None else temp_roots())
        skip = running_bundle_paths()
    except OSError as exc:
        note("temp-sweep scan failed: %r" % (exc,))
        return 0
    for path, reason in hits:
        try:
            if os.path.realpath(path) in skip:
                continue
            new = neutralize(path)
        except OSError as exc:
            note("temp-sweep rename failed: %s (%r)" % (path, exc))
            continue
        changed += 1
        note("临时目录旧副本已中性化: %s -> %s (v%s, %s)"
             % (path, os.path.basename(new), version_of(new), reason))
    return changed
