#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为已核验的 .app 生成自动更新 ZIP 与签名 manifest。

私钥仅从工程 .private_tools/update_signing_key.txt 读取，该目录被 git 忽略，绝不写入产物。
发布说明只从工程根目录 release_notes.txt 读取，供清单、应用弹窗与 GitHub 发布页共用。
"""

import argparse
import base64
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from nacl.signing import SigningKey


sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_identity import NOTES_MAX_CHARS, REPO, TAG  # noqa: E402
# v1.4.55 全局改名：产物与清单里的身份字段一起换（REPO 名不动 —— 那是外部仓库地址，
# 改它要连 GitHub 仓库一起搬，客户端里也有它）。
APP_NAME = "Skyrocket.app"
BUNDLE_ID = "com.skyrocket.app"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def canonical(data):
    copy = dict(data)
    copy.pop("signature", None)
    return json.dumps(copy, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")



def own_section_text(raw_notes, version):
    """取某一版自己的说明文本（到下一版标题为止）。"""
    pat = re.compile(r"^(V%s\s+发布说明.*?)(?=^V\d+\.\d+\.\d+\s|^\Z)"
                     % re.escape(version), re.M | re.S)
    m = pat.search(raw_notes)
    return m.group(1) if m else ""


def require_own_section(raw_notes, version):
    """硬闸门：本次版本号必须在 release_notes.txt 里有自己的一节标题。

    为什么必须是函数（而不是埋在 main 里的一段正则）：这条判据管着
    "更新弹窗只展示本次版本说明"这条用户可见规则 —— 缺了标题就会把整份
    按版本堆叠的历史一次塞进弹窗（v1.4.20 实测一屏显示 4 个版本）。
    那种事不该靠肉眼在弹窗里发现，打包时就得红。抽出来才能被 --self-test
    逐种形状验一遍（包括"标题里有空格/大小写不同"这类容易写歪的边界）。
    """
    if not re.search(r"^V%s\s+发布说明" % re.escape(version), raw_notes, re.M):
        raise SystemExit(
            "release_notes.txt 里找不到 v%s 这一节（应有一行 \"V%s 发布说明:\"），"
            "拒绝把整份历史变更写进更新弹窗。" % (version, version))
    # 第二道判据（用户定的永久规则）：本版说明**不超过 NOTES_MAX_CHARS 字**（数值见
    # release_identity；2026-10-23 起从 100 字放宽为 200 字）。
    # 为什么也做成闸门而不是靠自觉：这条规则是"永久"的，而人会忘 —— 更新说明
    # 是弹在用户屏幕上的，长文等于没人看（v1.4.50~53 那几版都是 5~8 行）。
    # 计数口径：去掉所有空白后的字符数（中文按字、英文按字母，标题行一起算）。
    body = own_section_text(raw_notes, version)
    n = len("".join(body.split()))
    if n > NOTES_MAX_CHARS:
        raise SystemExit(
            "v%s 这一节说明 %d 字，超过 %d 字上限 —— 用户定的永久规则："
            "OTA 更新日志必须用最少的字说清更新内容。请删到 %d 字以内再打包。"
            % (version, n, NOTES_MAX_CHARS, NOTES_MAX_CHARS))
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    import sf_update
    try:
        sf_update.require_numbered_notes(sf_update.notes_for_version(raw_notes, version))
    except sf_update.UpdateError as exc:
        raise SystemExit("拒绝生成 OTA 资产：%s" % exc) from exc


def self_test():
    """资产/清单侧的变异自检：签名前提与两条硬闸门都必须能失败。

    三条判据各自的来由：
      · canonical —— 签名算的是这份规范化字节；键序变了签名就该仍然有效，
        而 signature 字段本身混进去会自指（签自己）。
      · sha256 —— 清单里的 sha 与资产不符时客户端会拒绝更新；改一个字节必须变。
      · require_own_section —— 见其 docstring（v1.4.20 一屏 4 个版本那次）。
    """
    bad = []
    a = {"version": "1.4.46", "size": 12, "notes": "甲"}
    b = {"notes": "甲", "size": 12, "version": "1.4.46"}
    if canonical(a) != canonical(b):
        bad.append("canonical 不认键序（换一行 JSON 排版就把有效签名判成失效）")
    if canonical({**a, "signature": "zzz"}) != canonical(a):
        bad.append("signature 字段混进了被签名的载荷（签自己，验签必然漂移）")
    if canonical({**a, "size": 13}) == canonical(a):
        bad.append("改了值却得到同一份字节 —— 那签名和清单就对不上了")
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "asset.zip"
        f.write_bytes(b"hello")
        if sha256(f) != hashlib.sha256(b"hello").hexdigest():
            bad.append("sha256 与标准实现不一致（清单里的摘要会是另一个东西）")
        f.write_bytes(b"hellp")
        if sha256(f) == hashlib.sha256(b"hello").hexdigest():
            bad.append("改了一个字节摘要却没变（空转的核验器）")
        empty = Path(td) / "empty.bin"
        empty.write_bytes(b"")
        if sha256(empty) != hashlib.sha256(b"").hexdigest():
            bad.append("空文件读不出正确摘要（分块循环的边界）")
    notes = "V1.4.46 发布说明（Beta）— 本版\n1. 甲\n\nV1.4.45 发布说明（Beta）— 上一版\n2. 乙\n"
    try:
        require_own_section(notes, "1.4.46")
    except SystemExit as exc:
        bad.append("本版有标题却被拒：%s" % exc)
    for missing in ("1.4.99", "1.4.4"):
        try:
            require_own_section(notes, missing)
            bad.append("v%s 没有自己的标题却被放行（弹窗会把整份历史塞进去）" % missing)
        except SystemExit:
            pass
    try:
        require_own_section("v1.4.46 发布说明（小写 v 也算缺标题）", "1.4.46")
        bad.append("小节标题用了小写 v 却被放行 —— 约定是大写 V，弹窗侧按同一个正则裁")
    except SystemExit:
        pass
    # 字数上限（用户永久规则，2026-10-23 起 200 字）：这条判据自己也必须能红。
    # 下面的边界样本按 NOTES_MAX_CHARS 推算，不写死数字：标题行 "V1.4.46 发布说明:" 与
    # 编号 "1." 共 14 个非空白字符，所以刚好在限的正文是 NOTES_MAX_CHARS - 14 个“甲”。
    short = "V1.4.46 发布说明（Beta）— 短\n1. 修了甲。\n\nV1.4.45 发布说明（Beta）— 长\n1. " + ("乙" * 300) + "\n"
    try:
        require_own_section(short, "1.4.46")
    except SystemExit as exc:
        bad.append("短说明被误拒：%s" % exc)
    global NOTES_MAX_CHARS
    original_limit = NOTES_MAX_CHARS
    try:
        # Positive control: temporarily widen the limit so the self-test proves
        # the "would have been accepted" branch is observable before restoring
        # the production rule and checking that 300 characters are rejected.
        NOTES_MAX_CHARS = 400
        require_own_section(short, "1.4.45")
    except SystemExit:
        bad.append("扩大上限后的正向对照未通过")
    finally:
        NOTES_MAX_CHARS = original_limit
    try:
        require_own_section(short, "1.4.45")
        bad.append("300 字的说明被放行 —— 字数上限成了没有牙齿的文档")
    except SystemExit:
        pass
    at_limit = "V1.4.46 发布说明:\n1. " + ("甲" * (NOTES_MAX_CHARS - 14)) + "\n"
    try:
        require_own_section(at_limit, "1.4.46")
    except SystemExit:
        bad.append("刚好 %d 字的说明被误拒" % NOTES_MAX_CHARS)
    just_over = "V1.4.46 发布说明:\n1. " + ("甲" * (NOTES_MAX_CHARS - 13)) + "\n"
    try:
        require_own_section(just_over, "1.4.46")
        bad.append("%d 字的说明被放行 —— 字数上限没有收紧成功" % (NOTES_MAX_CHARS + 1))
    except SystemExit:
        pass
    for broken in ("甲；乙。", "1. 甲\n3. 乙", "1. 甲\n1. 乙", "1. 甲\n乙", "1. "):
        try:
            require_own_section("V1.4.46 发布说明:\n" + broken, "1.4.46")
            bad.append("未按连续编号逐行列出的更新说明被放行：" + repr(broken))
        except SystemExit:
            pass
    for line in bad:
        print("FAIL", line)
    if bad:
        return 1
    print("self-test 通过：canonical 3 类（键序/signature 排除/改值变字节）、"
          "sha256 3 类（一致/改字节必须变/空文件）、本版说明闸门 6 类各判对"
          "（含 %d 字上限能拦下 300 字），编号闸门 5 类非法正文均拒绝" % NOTES_MAX_CHARS)
    return 0


def main():
    if "--self-test" in sys.argv[1:]:
        return self_test()
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    app = args.app.resolve()
    if app.name != APP_NAME or not app.is_dir():
        raise SystemExit("不是目标应用包：%s" % app)
    info = app / "Contents" / "Info.plist"
    with open(info, "rb") as handle:
        plist = plistlib.load(handle)
    version = str(plist.get("CFBundleShortVersionString", ""))
    if plist.get("CFBundleIdentifier") != BUNDLE_ID or not version:
        raise SystemExit("应用标识或版本无效")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    notes_file = root / "release_notes.txt"
    try:
        raw_notes = notes_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit("未找到发布说明：%s" % notes_file) from exc
    # 只把**本次版本**那一段写进清单：release_notes.txt 是按版本往上堆的完整
    # 变更史，整份塞进清单会让更新弹窗一屏显示 4 个版本（v1.4.20 实测）。
    # 显示侧 stock_float_window 也会再裁一次，兜住线上已有的旧清单。
    import sf_update
    # 硬闸门：本次版本号必须在发布说明里有自己的一节标题。缺了就会把整份历史
    # 变更原样塞进清单（notes_for_version 取不到块时是"宁多勿空"），弹窗又会
    # 一屏显示好几个版本 —— 那种事不该靠肉眼在弹窗里发现，打包时就得红。
    require_own_section(raw_notes, version)
    notes = sf_update.notes_for_version(raw_notes, version)
    if not notes:
        raise SystemExit("发布说明为空：%s" % notes_file)
    if len(notes) > 4000:
        raise SystemExit(
            "本次版本 v%s 的发布说明过长（%d 字）：%s\n"
            "   多半是文件里缺了 \"V%s 发布说明\" 这一节标题，"
            "导致没能从堆叠的历史里裁出来。" % (version, len(notes), notes_file, version))
    key_file = root / ".private_tools" / "update_signing_key.txt"
    if not key_file.is_file():
        raise SystemExit("未找到本机更新私钥")
    signing_key = SigningKey(base64.b64decode(key_file.read_text(encoding="ascii").strip()))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    archive = output / ("Skyrocket-%s-macos.zip" % version)
    if archive.exists():
        archive.rename(output / (archive.name + ".previous"))
    subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app), str(archive)], check=True)
    digest = sha256(archive)
    size = archive.stat().st_size
    # 发版阶段标签：单一来源仍是 sf_constants.RELEASE_STAGE（工程根已在 sys.path 上）。
    # channel 保持 "stable" 不变（所有客户端照常自动更新），stage 只是给用户看的标签。
    try:
        import sf_constants
        release_stage = getattr(sf_constants, "RELEASE_STAGE", "stable")
    except Exception:
        release_stage = "stable"
    if release_stage not in ("beta", "stable"):
        release_stage = "stable"
    manifest = {
        "schema": 1,
        "channel": "stable",
        "stage": release_stage,
        "version": version,
        "published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "bundle_id": BUNDLE_ID,
        "app_name": APP_NAME,
        "notes": notes,
        "archive": {
            "url": "https://github.com/%s/releases/download/%s/%s" % (REPO, TAG, archive.name),
            "size": size,
            "sha256": digest,
        },
    }
    manifest["signature"] = {
        "algorithm": "ed25519",
        "value": base64.b64encode(signing_key.sign(canonical(manifest)).signature).decode("ascii"),
    }
    manifest_path = output / "Skyrocket-update.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    print("archive=%s" % archive)
    print("manifest=%s" % manifest_path)
    print("sha256=%s" % digest)
    print("notes_file=%s" % notes_file)


if __name__ == "__main__":
    main()
