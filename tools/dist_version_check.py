#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_release.sh 版本闸门：确认 dist/ 里的 .app 打包版本 == 期望版本。

背景（v1.2.95 第8轮走查）：build_release.sh 支持 `--skip-build`，此时 dist/Skyrocket.app
是上一轮**残留产物**。若它来自旧版本却仍被 DMG 打包并安装到 /Applications，用户会拿到一个
"版本号对（DMG 由 APP_VERSION 命名）、内容却是旧的"安装包，且脚本全程零报错 —— 没有任何
闸门拦住这件事。在已发布的成品上这是危险的（旧代码、旧目标价逻辑可能被当新版发出去）。

本脚本作为硬闸门：读取 .app/Contents/Info.plist 的 CFBundleShortVersionString，与
sf_constants.APP_VERSION 比对；不符直接非零退出，build_release.sh 据此中止。

即使走完整构建（DO_BUILD=1），本闸门也照常校验：刚打出来的包版本必然与 APP_VERSION 一致，
故不影响正常流程，只拦住"skip-build + 陈旧 dist"这一类误发。

用法：
    tools/dist_version_check.py <app路径> <期望版本>
返回码：0=版本匹配，1=不匹配，2=参数/读不到
"""
import plistlib
import sys
import tempfile
from pathlib import Path


def dist_version(app_path):
    """读取 .app/Contents/Info.plist 的 CFBundleShortVersionString；读不到返回 None。"""
    plist = Path(app_path) / "Contents" / "Info.plist"
    if not plist.exists():
        return None
    try:
        with open(plist, "rb") as f:
            data = plistlib.load(f)
        return data.get("CFBundleShortVersionString")
    except Exception:
        return None


def dist_version_ok(app_path, expected):
    """dist/ 内 .app 的打包版本是否 == 期望版本（忽略首尾空白）。"""
    v = dist_version(app_path)
    if v is None:
        return False
    return str(v).strip() == str(expected).strip()


def main(argv):
    if argv and argv[0] == "--self-test":
        return self_test()
    if len(argv) < 2:
        print("usage: dist_version_check.py <app> <expected_version> | --self-test",
              file=sys.stderr)
        return 2
    app, expected = argv[0], argv[1]
    got = dist_version(app)
    ok = dist_version_ok(app, expected)
    print("dist version: %r (expected %r) -> %s" %
          (got, expected, "OK" if ok else "MISMATCH"))
    return 0 if ok else 1


def _mk_app(root, version):
    """造一个只有 Info.plist 的假 .app，用来喂自检用例。"""
    app = Path(root) / "假包.app"
    (app / "Contents").mkdir(parents=True, exist_ok=True)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleShortVersionString": version}, f)
    return app


def self_test():
    """变异自检：这道闸门必须**能失败**。

    为什么要专门写它（2026-10-09）：本仓为"一个永不失败的检查等于没有检查"栽过至少
    四次（`verify_dmg_pyz` 无脑 PASS 约 54 个版本、`release_checks` 伪特征、探针把全屏
    前置做成永真、位图缓存核验空转却报"36 组零差异"）。此前全仓对这道版本闸门的唯一
    引用是 `tests/test_core.py` 里的一句**注释** —— 也就是说它一次也没有被证明过会红。
    它红不了意味着：`--skip-build` 拿上一轮残留的 dist/ 打成新 DMG 时，
    用户会拿到"版本号对、内容是旧的"安装包且全程零报错（v1.2.95 第 8 轮的原事故）。

    两类断言各守一头，缺一类都会留下会说假话的空间：
      · 坏输入必须被拒（旧版本 / 缺 Info.plist / 坏 plist / 只带 V 前缀）；
      · 好输入必须放行（含首尾空白），否则闸门只会一路红，下一轮就被人类绕过去。
    """
    bad = []
    with tempfile.TemporaryDirectory() as td:
        # 1) 好包：版本一致必须放行
        app = _mk_app(Path(td) / "ok", "1.4.46")
        if not dist_version_ok(app, "1.4.46"):
            bad.append("同版本被判成不匹配（闸门会把每次正常发版都拦死）")
        # 2) 首尾空白必须容忍 —— 这是它现有的语义，锁住别悄悄变
        if not dist_version_ok(_mk_app(Path(td) / "ws", " 1.4.46 "), "1.4.46"):
            bad.append("带首尾空白的同版本被判红（口径漂移）")
        # 3) 旧产物必须被拒：这条就是这道闸门存在的全部理由
        if dist_version_ok(_mk_app(Path(td) / "old", "1.4.45"), "1.4.46"):
            bad.append("旧版本 1.4.45 对上期望 1.4.46 竟然放行")
        # 4) V 前缀必须被拒（不剥前缀是既有口径；悄悄剥掉就会让"版本号对内容旧"过关）
        if dist_version_ok(_mk_app(Path(td) / "vt", "V1.4.46"), "1.4.46"):
            bad.append("V1.4.46 被当成等于 1.4.46（前缀口径漂移）")
        # 5) 缺 Info.plist 必须判不匹配，而不是抛异常或返回 None 被当成通过
        no_plist = Path(td) / "nopl" / "假包.app"
        (no_plist / "Contents").mkdir(parents=True)
        if dist_version_ok(no_plist, "1.4.46"):
            bad.append("没有 Info.plist 的目录被判成版本匹配")
        # 6) 坏 plist（垃圾字节）必须被兜住并判不匹配
        junk = Path(td) / "junk" / "假包.app"
        (junk / "Contents").mkdir(parents=True)
        (junk / "Contents" / "Info.plist").write_bytes(b"not a plist at all\xff\x00")
        if dist_version_ok(junk, "1.4.46"):
            bad.append("损坏的 plist 被判成版本匹配")
        # 7) 路径根本不存在时也不能"通过"
        if dist_version_ok(Path(td) / "不存在的.app", "1.4.46"):
            bad.append("路径不存在却判成匹配")
        # 8) main() 的退出码：不匹配必须是 1（build_release.sh 据此中止）
        rc = main([str(app), "1.4.99"])
        if rc != 1:
            bad.append("版本不匹配时 main() 返回 %s，不是 1" % rc)
        rc = main([str(app), "1.4.46"])
        if rc != 0:
            bad.append("版本匹配时 main() 返回 %s，不是 0" % rc)
        # 9) 参数不足必须是 2（别把用法错误伪装成"版本不符"）
        if main(["只给一个参数"]) != 2:
            bad.append("参数不足时退出码不是 2")
    for line in bad:
        print("FAIL", line)
    if bad:
        return 1
    print("self-test 通过：9 类输入各判对（旧包/坏 plist/缺 plist/不存在路径/V 前缀 "
          "全部被拒，同版本与带空白同版本全部放行，退出码 0/1/2 各归各位）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
