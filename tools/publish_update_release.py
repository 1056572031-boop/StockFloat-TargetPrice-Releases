#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原子发布更新资产：先验证版本 ZIP，再提交 raw 清单指针。

旧客户端继续使用固定 Release 中既有的 manifest 资产；本工具绝不删除
Release 资产。V1.3.96 起的客户端从 releases 仓库 main 分支固定文件读取清单。
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_identity import MANIFEST_PATH, REPO, TAG  # noqa: E402
# v1.4.55 改名后走新清单；旧清单 `StockFloatTargetPrice-update.json` 留在仓库里
# 冻结在 1.4.54 —— 已装旧版的客户端仍指它，删掉只会让旧版报错，留着不动最安全。
API_ROOT = "https://api.github.com/repos/" + REPO
# 资产上传走独立主机名；单列成常量是为了让端到端回归能把两个根都指向本地
# 假服务器（真实构造请求、真实解析响应），而不是把 request() 整个 mock 掉。
UPLOAD_ROOT = "https://uploads.github.com/repos/" + REPO

# ---- 超时：一律可配，不写死在代码里 ----
# 为什么必须可配：上传耗时由**上行带宽**决定，本机实测传一份 27MB 级版本 ZIP 要几分钟。
# v1.4.38 发版时两次在"写死的 60 秒"上抛 TimeoutError —— 而那时打包、产物核验、DMG、
# 装 /Applications 全部已经真完成，只能靠一个仓库外探针把超时放宽才把包发出去。
# 换一台机器、换一个网络就差一个数量级，钉死任何一个数都迟早变成"第 6 步必红"。
UPLOAD_TIMEOUT_ENV = "SF_PUBLISH_TIMEOUT_SEC"       # 只管版本 ZIP 那一条大请求
API_TIMEOUT_ENV = "SF_PUBLISH_API_TIMEOUT_SEC"      # 元数据/读回：本来就该快
UPLOAD_TIMEOUT_DEFAULT = 900.0
API_TIMEOUT_DEFAULT = 30.0


def _timeout_from_env(name, default, floor):
    """读一个正数超时；写错**当场失败**，绝不静默退回默认值。

    静默退回是这类工具最典型的说谎方式：`SF_PUBLISH_TIMEOUT_SEC=abc` 被当成"没设"，
    发版人以为自己放宽了，实际还在原来的短超时上跑，红的时候完全找不到原因。
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        raise SystemExit("%s 必须是秒数，现在=%r（拒绝静默退回默认值）" % (name, raw))
    if val < floor:
        raise SystemExit("%s 不得低于 %.0f 秒，现在=%r —— 要缩短请明确改小，"
                         "不要靠设一个不合理的值" % (name, floor, raw))
    return val


def resolve_timeouts():
    """(上传超时, 元数据与读回超时)。每次调用都现读环境变量，便于用例覆盖。"""
    return (_timeout_from_env(UPLOAD_TIMEOUT_ENV, UPLOAD_TIMEOUT_DEFAULT, 60.0),
            _timeout_from_env(API_TIMEOUT_ENV, API_TIMEOUT_DEFAULT, 1.0))


def request(url, token, method="GET", data=None, content_type="application/json",
            timeout=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Basic " + base64.b64encode(
            ("x-access-token:" + token).encode("utf-8")).decode("ascii"),
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    if timeout is None:
        # 没显式给的一律走"元数据档"：清单指针、读回这类请求本来就该快，
        # 跟着上传一起放宽只会把"GitHub 挂了"这种故障拖成十几分钟才报出来。
        timeout = resolve_timeouts()[1]
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def github_token():
    """从 git credential 获取 GitHub 凭据，避免令牌出现在参数或日志中。"""
    result = subprocess.run(
        ["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
        text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise SystemExit("无法读取 GitHub 发布凭据")
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    token = fields.get("password", "")
    if not token:
        raise SystemExit("未找到 GitHub 发布凭据")
    return token


def release_url():
    return API_ROOT + "/releases/tags/" + urllib.parse.quote(TAG, safe="")


def upload_url(release_id, asset_name):
    query = urllib.parse.urlencode({"name": asset_name})
    return "%s/releases/%s/assets?%s" % (UPLOAD_ROOT, release_id, query)


def contents_url():
    return API_ROOT + "/contents/" + urllib.parse.quote(MANIFEST_PATH, safe="/")


def contents_ref_url(ref="main"):
    return contents_url() + "?" + urllib.parse.urlencode({"ref": ref})


def read_contents_main(token):
    """Read the same explicit main branch that manifest_commit_payload writes."""
    return json.loads(request(contents_ref_url("main"), token))


def load_release(token):
    return json.loads(request(release_url(), token))


SOURCE_ROOT = Path(__file__).resolve().parents[1]


def quality_module():
    # 独立 CLI 和测试按路径加载时都使用同一份真实核验器。
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "publish_quality_evidence", Path(__file__).with_name("quality_evidence.py"))
    if spec is None or spec.loader is None:
        raise ValueError("无法加载质量证据核验器")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def publication_snapshot(archive, manifest, notes):
    """凭据及网络之前核验；返回之后发布所需的不可变字节。"""
    try:
        manifest_bytes = manifest.read_bytes()
        metadata = json.loads(manifest_bytes)
        if not isinstance(metadata, dict):
            raise ValueError("更新清单必须是对象")
        check_release_notes(notes, manifest, metadata)
        record_ref = os.environ.get("SF_QUALITY_RECORD", "").strip()
        if not record_ref:
            raise ValueError("缺少 SF_QUALITY_RECORD")
        record_path = Path(record_ref).resolve()
        quality = quality_module()
        record = quality.load(record_path)
        evidence_manifest = record_path.with_suffix(".manifest.json")
        errors = quality.verify_release_candidate(
            SOURCE_ROOT, record, record_path.parent, evidence_manifest)
        if errors:
            raise ValueError("正式发布资格未通过：" + "; ".join(errors))
        stored = quality.load(evidence_manifest)
        matches = [item for item in stored["artifacts"]
                   if quality.resolve_reference(record_path.parent, item["path"]) == archive.resolve()]
        if len(matches) != 1:
            raise ValueError("ZIP 必须唯一绑定质量 manifest 中的产物")
        archive_bytes = archive.read_bytes()
        # quality 摘要包含权限与目录结构，不是 OTA 的原始文件 SHA-256。
        mode = oct(archive.stat().st_mode & 0o777)
        raw_hash = hashlib.sha256(archive_bytes).hexdigest()
        entries = [[".", mode, raw_hash]]
        identity = hashlib.sha256(json.dumps(
            entries, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        if (matches[0].get("sha256") != identity or matches[0].get("kind") != "file"
                or matches[0].get("entries") != 1):
            raise ValueError("ZIP 字节与质量 manifest 不一致")
        info = metadata.get("archive")
        if not isinstance(info, dict):
            raise ValueError("更新清单缺少 archive 对象")
        if type(info.get("size")) is not int or info["size"] != len(archive_bytes):
            raise ValueError("ZIP 原始大小与更新清单不一致")
        if not isinstance(info.get("sha256"), str) or info["sha256"] != raw_hash:
            raise ValueError("ZIP 原始 SHA-256 与更新清单不一致")
        url = info.get("url")
        expected_url = "https://github.com/%s/releases/download/%s/%s" % (REPO, TAG, archive.name)
        if not isinstance(url, str) or urllib.parse.unquote(url) != expected_url:
            raise ValueError("ZIP 下载地址与发布目标不一致")
        return archive_bytes, manifest_bytes, metadata["version"]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SystemExit("发布前核验失败：%s" % exc) from exc


def verified_asset(release, archive, expected_size=None):
    """返回已上传且大小匹配的版本唯一资产，否则拒绝切换清单。"""
    if expected_size is None:
        expected_size = archive.stat().st_size
    for asset in release.get("assets", []):
        if asset.get("name") == archive.name and asset.get("size") == expected_size:
            return asset
    raise SystemExit("上传后未验证到版本 ZIP，未更新线上清单")


def download_asset(token, asset):
    """读回资产原始字节；名称和大小都相同也不能替代内容核对。"""
    url = asset.get("url") or asset.get("browser_download_url")
    if not isinstance(url, str) or not url:
        raise SystemExit("上传后资产缺少可读回地址，未更新线上清单")
    headers = {
        "Accept": "application/octet-stream",
        "Authorization": "Basic " + base64.b64encode(
            ("x-access-token:" + token).encode("utf-8")).decode("ascii"),
        "X-GitHub-Api-Version": "2022-11-28",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=resolve_timeouts()[0]) as response:
            return response.read()
    except Exception as exc:
        raise SystemExit("上传后资产原始字节读回失败：%r" % (exc,)) from exc


def upload_archive(token, release, archive, archive_bytes):
    if any(asset.get("name") == archive.name for asset in release.get("assets", [])):
        raise SystemExit("固定 Release 已有同名版本 ZIP，拒绝覆盖")
    request(upload_url(release["id"], archive.name), token, method="POST",
            data=archive_bytes, content_type="application/zip",
            timeout=resolve_timeouts()[0])
    asset = verified_asset(load_release(token), archive, len(archive_bytes))
    downloaded = download_asset(token, asset)
    if downloaded != archive_bytes:
        raise SystemExit("上传后 ZIP 原始字节与本地固定快照不一致，未更新线上清单")
    return asset


def manifest_commit_payload(manifest_bytes, existing_sha):
    payload = {
        "message": "发布更新清单指针",
        "content": base64.b64encode(manifest_bytes).decode("ascii"),
        "branch": "main",
    }
    if existing_sha:
        payload["sha"] = existing_sha
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def publish_manifest_pointer(token, manifest_bytes):
    """通过 Contents API 单次提交固定清单，提交前的 ZIP 已可下载并经验证。"""
    url = contents_url()
    try:
        existing_sha = read_contents_main(token).get("sha")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        existing_sha = None
    request(url, token, method="PUT",
            data=manifest_commit_payload(manifest_bytes, existing_sha))
    payload = read_contents_main(token)
    try:
        read_back = base64.b64decode(payload["content"])
    except (KeyError, ValueError, TypeError) as exc:
        raise SystemExit("Contents 清单写入后无法读回原始字节") from exc
    if read_back != manifest_bytes:
        raise SystemExit("Contents 清单写入后原始字节不一致，未替换旧客户端清单")


def publish_legacy_manifest(token, release, manifest_bytes):
    """迁移期仅替换旧客户端唯一依赖的 manifest；版本 ZIP 绝不删除。"""
    old = next((asset for asset in release.get("assets", [])
                if asset.get("name") == MANIFEST_PATH), None)
    if old is not None:
        request(API_ROOT + "/releases/assets/" + str(old["id"]), token, method="DELETE")
    request(upload_url(release["id"], MANIFEST_PATH), token, method="POST",
            data=manifest_bytes, content_type="application/json")
    refreshed = load_release(token)
    asset = next((asset for asset in refreshed.get("assets", [])
                  if asset.get("name") == MANIFEST_PATH), None)
    if asset is None or asset.get("size") != len(manifest_bytes):
        raise SystemExit("旧客户端清单上传后未验证，停止发布")
    if download_asset(token, asset) != manifest_bytes:
        raise SystemExit("旧客户端清单上传后原始字节不一致，停止发布")


RAW_MANIFEST_URL = ("https://raw.githubusercontent.com/" + REPO + "/main/" + MANIFEST_PATH)


def readback_verdict(repo_manifest, expected_version, raw_manifest=None):
    """发布后读回的判定（纯函数，可离线自检）：仓库里的清单必须是本版。

    为什么需要这一步（2026-10-09 实测）：发布器原先只 `print("published_...")` 就算完，
    而**用户侧那条 URL 是 raw.githubusercontent，有 CDN 缓存**。v1.4.47 发完我直读 raw
    仍是 1.4.46，连着读了三次都一样，一度判成"报了成功其实没生效"；绕开 CDN 用 API 看仓库，
    里面早就已经是 1.4.47。也就是说：没有权威读回时，**成功与失败看起来一模一样**，
    复核的人只能靠猜 —— 这正是本仓反复在防的那类"关卡自己说不清话"。
    所以这里分两件事说：仓库侧决定发没发成（必须等于本版）；raw 滞后只影响"用户几秒/几分钟
    后才看得见"，如实报出来但不算失败。

    返回 (是否成功, 要打印的行)。
    """
    lines, ok = [], True
    repo_ver = (repo_manifest or {}).get("version")
    if repo_ver != expected_version:
        ok = False
        lines.append("!! 仓库里的清单版本是 %r，期望 %r —— 发布没有真正生效，不要宣布成功"
                     % (repo_ver, expected_version))
    else:
        lines.append("   读回通过：仓库清单 version=%s（与本轮一致）" % repo_ver)
    if ok and raw_manifest is not None and raw_manifest.get("version") != expected_version:
        lines.append("   注：raw 侧暂时还是 %r —— 是 CDN 缓存滞后，通常几分钟内自愈；"
                     "客户端下一次检查更新就会拿到新版，不需要重发"
                     % raw_manifest.get("version"))
    return ok, lines


def _fetch_manifest_bytes(url, token=None):
    """读取清单原始字节；API Contents 与公开 raw 都走同一条判定。"""
    try:
        if url.startswith(API_ROOT):
            data = json.loads(request(url, token))
            return base64.b64decode(data["content"])
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=resolve_timeouts()[1]) as response:
            return response.read()
    except Exception as exc:
        print("   （读回拉取失败 %s：%r）" % (url, exc), file=sys.stderr)
        return None


def _fetch_manifest_json(url, token=None):
    """拉一份清单 JSON；拉不到返回 None（调用方按"没读到"处理，绝不当成成功）。"""
    try:
        data = _fetch_manifest_bytes(url, token)
        return json.loads(data.decode("utf-8")) if data is not None else None
    except Exception as exc:
        # 本工具是独立 CLI，没有产品那套日志通道；读不到就说读不到（stderr + 返回 None，
        # 调用方按「没读到」判失败，绝不当成成功）。
        print("   （读回拉取失败 %s：%r）" % (url, exc), file=sys.stderr)
        return None


def verify_live_manifest(token, expected_version, expected_bytes=None):
    """发布后必须读回：仓库侧不过关就直接非零退出（发版链据此中止）。"""
    repo_bytes = _fetch_manifest_bytes(contents_ref_url("main"), token)
    raw_bytes = _fetch_manifest_bytes(RAW_MANIFEST_URL)
    repo = json.loads(repo_bytes.decode("utf-8")) if repo_bytes is not None else None
    raw = json.loads(raw_bytes.decode("utf-8")) if raw_bytes is not None else None
    if expected_bytes is not None and repo_bytes != expected_bytes:
        raise SystemExit("发布读回未通过：Contents 清单原始字节不一致，不宣布发版成功")
    ok, lines = readback_verdict(repo, expected_version, raw)
    for line in lines:
        print(line)
    if not ok:
        raise SystemExit("发布读回未通过：不宣布发版成功")
    return True


def check_release_notes(notes_path, manifest_path, metadata=None):
    """对拍"release_notes.txt 的本次版本一节"与"即将签发的清单 notes"。

    原来这里只有一句 `notes = read(); if not notes or len(notes) > 4000: 拒绝`，
    而 notes 读出来**就丢掉** —— 本工具从不用它更新 GitHub Release 正文，发布物
    只有 ZIP 与清单。于是它守的是一个没人看的数字：release_notes.txt 是按版本往上
    堆的完整变更史，v1.4.33 那次它长到 4288 字，把一次正常发版拦死在最后一步，
    而真正进弹窗的本次说明只有 322 字（已签名在清单里）。

    现在守的是 build_release.sh 第 6 步注释里那句承诺 —— "清单和 GitHub 发布页
    强制读取同一份 release_notes.txt" —— 这道对拍就是它一直缺的 enforcement：
      · 本次版本号必须在清单里，且能在 release_notes.txt 里裁出**非空**的一节；
      · 裁出来的那节必须与清单 notes **逐字相同**（防止改了文件却没重签清单，
        或反过来；也防把整份历史塞进弹窗 —— 那是 v1.4.20 的真实事故）；
      · 长度上限按**本次版本说明**计，与客户端 sf_update.verify_manifest 同口径。
    """
    manifest = metadata if metadata is not None else json.loads(manifest_path.read_text(encoding="utf-8"))
    version = str(manifest.get("version") or "")
    signed_notes = str(manifest.get("notes") or "")
    if not version or not signed_notes:
        raise SystemExit("清单缺少 version 或 notes，无法核验发布说明")
    raw = notes_path.read_text(encoding="utf-8").strip()
    if not raw:
        raise SystemExit("发布说明为空：%s" % notes_path)
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    import sf_update
    section = sf_update.notes_for_version(raw, version)
    if not section:
        raise SystemExit("发布说明为空：%s" % notes_path)
    if len(section) > 4000:
        raise SystemExit(
            "本次版本 v%s 的发布说明过长（%d 字）：%s\n"
            "   多半是文件里缺了 \"V%s 发布说明\" 这一节标题，导致没能从堆叠的"
            "历史里裁出来。" % (version, len(section), notes_path, version))
    if section.strip() != signed_notes.strip():
        raise SystemExit(
            "release_notes.txt 的 v%s 一节与清单里已签名的 notes 不一致：\n"
            "   清单是 %d 字、文件裁出 %d 字。发版说明改过就必须重跑 "
            "make_update_assets 重新签名，否则线上清单与仓库文件说的不是"
            "同一件事。" % (version, len(signed_notes), len(section)))
    return section



def self_test():
    """发布侧的变异自检：URL 编码与"未验证到资产就拒绝切清单"这两条必须能失败。

    为什么是这两条（都来自台账里的真实逃逸）：
      · 中文发布标签 / 中文资产名 —— v1.3.72~1.3.79 的外机事故：固定标签是中文，
        Python 3.14 的 urllib 在**未编码**时抛 UnicodeEncodeError，用户点下载就报
        "下载或校验失败"（清单能下能验签，界面先显示有更新再统一报失败）。
        所以这里不问"能不能跑通"，只问"发出去的每一段 URL 是不是纯 ASCII"。
      · verified_asset —— 清单指针切到一个没验证过大小/名字的资产，等于把用户
        指向半截上传。它必须拒绝，而且拒绝要走 SystemExit（非零退出）。
    """
    bad = []
    for name, url in (("release_url", release_url()), ("contents_url", contents_url())):
        if not url.isascii():
            bad.append("%s 里还有非 ASCII 字符（%r）—— 就是当年让外机下载即失败的那种 URL"
                       % (name, url))
    enc = upload_url("123", "实时股价-1.4.46.zip")
    if not enc.isascii():
        bad.append("资产名没被编码就拼进 query（非 ASCII 的 URL 会在 urllib 里炸）")
    if "123" not in enc or "/assets?" not in enc:
        bad.append("upload_url 形状不对（release id 或 query 分隔丢了）")
    if upload_url("1", "a.zip") == upload_url("2", "a.zip"):
        bad.append("不同 release id 得到同一个上传地址（会传到别版本上去）")
    real = Path(tempfile.mkdtemp()) / "StockFloatTargetPrice-1.4.46-macos.zip"
    real.write_bytes(b"x" * 1234)
    ok_release = {"assets": [{"name": real.name, "size": 1234, "browser_download_url": "u"}]}
    if verified_asset(ok_release, real) is None:
        bad.append("名字与大小都匹配的资产被判成不存在（正常发版会被自己拦死）")
    for broken, why in (
        ({"assets": [{"name": real.name, "size": 1233}]}, "大小不符必须拒绝切清单"),
        ({"assets": [{"name": "别版.zip", "size": 1234}]}, "只有别版本的资产在场必须拒绝"),
        ({"assets": []}, "空资产列表必须拒绝"),
        ({}, "release 里根本没有 assets 字段必须拒绝"),
    ):
        try:
            verified_asset(broken, real)
            bad.append(why + "，却被放行")
        except SystemExit:
            pass
    ok3, lines3 = readback_verdict({"version": "1.4.47"}, "1.4.47", {"version": "1.4.46"})
    if not ok3:
        bad.append("仓库已是本版、raw 还在 CDN 滞后却被判失败 —— 会把一次正常发版误中止")
    if not any("CDN" in x for x in lines3):
        bad.append("raw 滞后没有如实说出来；下次复核的人又会把它当成『发布器说谎』")
    ok4, lines4 = readback_verdict({"version": "1.4.46"}, "1.4.47")
    if ok4:
        bad.append("仓库里还是上一版却被判通过 —— 这正是读回要拦的那种假成功")
    if not any("不要宣布成功" in x for x in lines4):
        bad.append("读回失败却没给出可行动的文字")
    ok5, _l5 = readback_verdict(None, "1.4.47")
    if ok5:
        bad.append("读回拿到 None（拉取失败）却被当成发布成功")
    # ---- 超时不许写死：默认值、覆盖、两个坏值、两档互不干扰 ----
    saved = {k: os.environ.get(k) for k in (UPLOAD_TIMEOUT_ENV, API_TIMEOUT_ENV)}
    try:
        for k in saved:
            os.environ.pop(k, None)
        d_up, d_api = resolve_timeouts()
        if d_up <= 60.0:
            bad.append("上传默认超时只有 %.0f 秒 —— 本机实测 27MB 资产 60 秒传不完，"
                       "写死它等于让第 6 步必红" % d_up)
        if d_api > 60.0:
            bad.append("元数据档被跟着放宽到 %.0f 秒：GitHub 故障要等十几分钟才报出来"
                       % d_api)
        os.environ[UPLOAD_TIMEOUT_ENV] = "1234"
        os.environ[API_TIMEOUT_ENV] = "7"
        got_up, got_api = resolve_timeouts()
        if (got_up, got_api) != (1234.0, 7.0):
            bad.append("环境变量没被读到（%r, %r）—— 放宽超时的那句话成了空话"
                       % (got_up, got_api))
        for name, key, junk in (("非数字", UPLOAD_TIMEOUT_ENV, "abc"),
                                ("低于下限", UPLOAD_TIMEOUT_ENV, "5"),
                                ("非数字", API_TIMEOUT_ENV, "x"),
                                ("低于下限", API_TIMEOUT_ENV, "0.2")):
            os.environ[key] = junk
            try:
                resolve_timeouts()
                bad.append("%s的 %s=%r 被静默放过（当成没设就退回默认值，"
                           "发版人会以为自己放宽成功了）" % (name, key, junk))
            except SystemExit:
                pass
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    for line in bad:
        print("FAIL", line)
    if bad:
        return 1
    print("self-test 通过：两个 URL 构造器纯 ASCII、资产名编码、release id 参与地址、"
          "超时两档（默认值 + 覆盖 + 两种坏值当场拒绝）、"
          "verified_asset 匹配放行 + 4 种坏 release 全部拒绝、发布读回 3 类判定（含 CDN 滞后）")
    return 0


def main():
    if "--self-test" in sys.argv[1:]:
        return self_test()
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--notes", type=Path, required=True)
    args = parser.parse_args()
    up, api = resolve_timeouts()
    print("   超时：版本 ZIP 上传 %.0f 秒（%s，默认 %.0f）/ 元数据与读回 %.0f 秒（%s）"
          % (up, UPLOAD_TIMEOUT_ENV, UPLOAD_TIMEOUT_DEFAULT, api, API_TIMEOUT_ENV))
    assets = args.assets.resolve()
    # v1.4.55 改名：资产前缀跟着变成 Skyrocket-*。两代通配都扫，扫到多于一个仍判
    # "不唯一" —— 那正是这条判据要拦的（一个目录里两个版本的包，发出去就是发错包）。
    archives = (list(assets.glob("Skyrocket-*-macos.zip"))
                + list(assets.glob("StockFloatTargetPrice-*-macos.zip")))
    manifest = assets / MANIFEST_PATH
    if len(archives) != 1 or not manifest.is_file():
        raise SystemExit("更新资产不完整或版本 ZIP 不唯一")
    archive = archives[0]
    archive_bytes, manifest_bytes, expected_version = publication_snapshot(archive, manifest, args.notes)
    token = github_token()
    release = load_release(token)
    asset = upload_archive(token, release, archive, archive_bytes)
    # 新客户端的 raw 清单先切换：一旦它可见，ZIP 已存在且已验证。
    publish_manifest_pointer(token, manifest_bytes)
    # 旧客户端仍从固定 Release 取清单；最后才替换这一枚小清单，不触碰历史 ZIP。
    publish_legacy_manifest(token, load_release(token), manifest_bytes)
    print("published_archive=%s" % asset["name"])
    print("published_manifest_pointer=main/%s" % MANIFEST_PATH)
    print("published_legacy_manifest=%s" % MANIFEST_PATH)
    # 打印不等于生效：必须绕开 CDN 读回仓库里的那一份，版本对不上就中止发版。
    verify_live_manifest(token, expected_version, manifest_bytes)
    print("   线上清单读回一致，发版可以宣布成功")


if __name__ == "__main__":
    main()
