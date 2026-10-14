#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时目录残留副本清扫的命令行入口（实现见 sf_temp_sweep，唯一一份逻辑）。

为什么还需要命令行版
    产品侧 v1.4.23 起会在启动后自动清扫，但那只对**新产物**生效；已经躺在临时目录里
    的 ≤1.4.4 旧副本不会自己升级，发版机与实机仍需手动跑一次。本脚本就是那一次。

用法
    ./.venv/bin/python tools/sweep_temp_apps.py            # 只报告，不改动（默认）
    ./.venv/bin/python tools/sweep_temp_apps.py --apply    # 改名为 *.app.stale
    ./tools/sweep_temp_apps.py --apply                     # 已 chmod +x，走 shebang
    python3 tools/sweep_temp_apps.py --apply               # 不需要 .venv，但需要本仓库

依赖说明（v1.4.24 复核 F5 更正措辞）：本脚本只用标准库，系统自带 python3 即可运行
（实测 Python 3.9.6 通过），但它 `from sf_temp_sweep import ...` 依赖仓库根目录 ——
换机器必须连仓库一起拷。要在**没有本仓库**的机器（如 iMac 实机）上清理，用自包含的
`tools/cleanup_temp_copies.command`（双击运行，只用 PlistBuddy/find/mv）。

安全边界见 sf_temp_sweep 模块 docstring：只扫系统临时目录、只认本程序包标识、
跳过正在运行的副本、一律改名不删除。
"""

from __future__ import annotations

import argparse
import os
import plistlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sf_temp_sweep import (APP_DIR_NAME, BUNDLE_ID, DEFAULT_MAX_DEPTH,  # noqa: E402
                           OWN_APP_NAMES, SUFFIX, bundle_identifier,
                           find_stale_apps, is_launchable, neutralize,
                           running_bundle_paths, temp_roots, version_of)

__all__ = ["BUNDLE_ID", "SUFFIX", "OWN_APP_NAMES", "APP_DIR_NAME", "temp_roots",
           "running_bundle_paths", "bundle_identifier", "version_of",
           "is_launchable", "find_stale_apps", "neutralize", "main"]




def _mk_app(root, name, bundle_id=None, version="1.3.97"):
    """造一个看起来能启动的假 .app（Info.plist + MacOS 下的可执行文件占位）。"""
    app = Path(root) / name
    (app / "Contents" / "MacOS").mkdir(parents=True)
    if bundle_id is not None:
        with open(app / "Contents" / "Info.plist", "wb") as fh:
            plistlib.dump({"CFBundleIdentifier": bundle_id,
                           "CFBundleShortVersionString": version,
                           "CFBundleExecutable": "Skyrocket"}, fh)
    exe = app / "Contents" / "MacOS" / "StockFloatTargetPrice"
    exe.write_bytes(b"#!/bin/sh\n")
    exe.chmod(0o755)
    return str(app)


def self_test():
    """清扫侧边界自检：AGENTS 写死的那五条里，能用临时目录验的全验一遍。

    重点是两条**已经被抓到过**的失效：
      · 重叠根把同一份副本扫两遍 → 第二次改名拿到 FileNotFoundError，却被记成
        "改名失败"写进用户真实 debug.log（复核 F1）。现在按 realpath 去重，
        这里直接喂重叠根，要求一份副本只出现一次。
      · 别人的 .app 绝不能被改名：只认本程序包标识；读不出标识时还要包名匹配。
    "一律改名不删文件"这条也必须可证：改名后目录仍在，只是换个名字 ——
    用户的东西丢了不可回退。
    """
    bad = []
    with tempfile.TemporaryDirectory() as td:
        mine = _mk_app(td, APP_DIR_NAME, BUNDLE_ID)
        foreign = _mk_app(td, "别的程序.app", "com.somebody.else")
        no_plist_wrong_name = _mk_app(td, "无关副本.app", None)
        already = Path(td) / (APP_DIR_NAME + SUFFIX)
        (already / "Contents" / "MacOS").mkdir(parents=True)

        if bundle_identifier(mine) != BUNDLE_ID:
            bad.append("自家副本的包标识读不出来（那 find_stale_apps 会整体扫不到）")
        if bundle_identifier(foreign) == BUNDLE_ID:
            bad.append("别人的包标识被读成自家")
        if bundle_identifier(no_plist_wrong_name) is not None:
            bad.append("没有 Info.plist 的目录不该读出标识")
        if bundle_identifier(str(Path(td) / "根本没有这个.app")) is not None:
            bad.append("不存在的路径应当返回 None，而不是猜一个")
        if version_of(mine) != "1.3.97":
            bad.append("版本号读不出来（报告里是空串，人看不出那是哪一份）")
        if not is_launchable(mine):
            bad.append("能启动的副本被判成不可启动（会漏扫，旧副本继续被拉起崩溃）")
        if is_launchable(str(already)):
            bad.append("已改名的残留仍被判为可启动 —— 它会每轮被重复处理")

        found = [path for path, _r in find_stale_apps([str(td)], BUNDLE_ID)]
        if mine not in found:
            bad.append("自家副本没被找到（清扫等于没做）")
        if foreign in found:
            bad.append("别人的 .app 被列进清扫名单（越界就是破坏）")
        if no_plist_wrong_name in found:
            bad.append("缺 plist 且包名不是自家名的目录被误扫")
        if str(already) in found:
            bad.append("已改名的 .stale 残留被反复处理")
        dup = [path for path, _r in
               find_stale_apps([str(td), str(Path(td)), str(td.rstrip("/") + "/")], BUNDLE_ID)]
        if dup.count(mine) != 1:
            bad.append("重叠根让同一份副本被扫到 %d 次 —— 复核 F1 那条假「改名失败」会复发"
                       % dup.count(mine))
        if not isinstance(running_bundle_paths(), set):
            bad.append("running_bundle_paths 返回的不是集合（跳过运行中副本的逻辑会静默失效）")

        renamed = neutralize(mine)
        if os.path.exists(mine):
            bad.append("改名后原路径还在（LaunchServices 仍能把它当应用拉起）")
        if not os.path.isdir(renamed):
            bad.append("改名后目录不见了 —— 清扫只许改名不许删除")
        if mine in [path for path, _r in find_stale_apps([str(td)], BUNDLE_ID)]:
            bad.append("改名之后还能被扫到（下一轮会重复处理同一份）")
        if neutralize(renamed) == renamed:
            bad.append("同一路径第二次改名没有追加序号（会撞名报错）")
    for line in bad:
        print("FAIL", line)
    if bad:
        return 1
    print("self-test 通过：只认自家包标识、别人的 .app 不动、缺 plist 不误扫、"
          "已改名残留不重复处理、重叠根去重（复核 F1）、改名不删文件且可再改名")
    return 0

def main(argv: list[str] | None = None) -> int:
    if "--self-test" in list(argv if argv is not None else sys.argv[1:]):
        return self_test()
    parser = argparse.ArgumentParser(description="清扫临时目录里可启动的本程序旧副本")
    parser.add_argument("--apply", action="store_true",
                        help="真正改名（默认只列出，不改动）")
    parser.add_argument("--bundle-id", default=BUNDLE_ID,
                        help="要中性化的 CFBundleIdentifier（默认 %s）" % BUNDLE_ID)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH,
                        help="临时目录内最大扫描深度（默认 %d）" % DEFAULT_MAX_DEPTH)
    args = parser.parse_args(argv)

    roots = temp_roots()
    installed = os.path.join("/Applications", APP_DIR_NAME)
    print("扫描根目录: %s" % ", ".join(roots))
    if os.path.isdir(installed):
        print("正式版: %s (v%s)" % (installed, version_of(installed)))
    else:
        print("正式版: 未安装在 /Applications")

    skip = running_bundle_paths()
    hits = find_stale_apps(roots, args.bundle_id, args.max_depth)
    if not hits:
        print("未发现可启动的残留副本。")
        return 0

    changed = 0
    for path, reason in hits:
        if os.path.realpath(path) in skip:
            print("跳过（正在运行）: %s" % path)
            continue
        tag = "v%s" % version_of(path)
        note = "" if reason == "bundle-id" else " [%s]" % reason
        if not args.apply:
            print("待中性化: %s (%s)%s" % (path, tag, note))
            continue
        try:
            new = neutralize(path)
        except OSError as exc:
            print("改名失败: %s -> %s" % (path, exc))
            continue
        changed += 1
        print("已中性化: %s -> %s (%s)%s" % (path, os.path.basename(new), tag, note))
    if args.apply:
        print("本轮改名 %d 个（未删除任何文件，去掉 .stale 后缀即可还原）。" % changed)
    else:
        print("以上为预览，未做任何改动；加 --apply 执行改名。")
    return 0



if __name__ == "__main__":
    sys.exit(main())
