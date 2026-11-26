#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原子发布更新资产：先验证版本 ZIP，再提交 raw 清单指针。

旧客户端继续使用固定 Release 中既有的 manifest 资产；本工具绝不删除
Release 资产。V1.3.96 起的客户端从 releases 仓库 main 分支固定文件读取清单。
"""

import argparse
import base64
import hashlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_identity import MANIFEST_PATH, REPO, TAG  # noqa: E402
from release_sequence import (load_ledger, mark_published,
                              verify as verify_release_sequence,
                              verify_rollback as verify_rollback_sequence)  # noqa: E402
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


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def newer_utc_now(live_published_at):
    candidate = datetime.now(timezone.utc).replace(microsecond=0)
    live = _utc_timestamp(live_published_at)
    if candidate <= live:
        candidate = live + timedelta(seconds=1)
    return candidate.isoformat().replace("+00:00", "Z")


def canonical(manifest):
    payload = dict(manifest)
    payload.pop("signature", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def resign_manifest(manifest, signing_key, published_at):
    stamped = dict(manifest)
    stamped["published_at"] = published_at
    stamped.pop("signature", None)
    stamped["signature"] = {
        "algorithm": "ed25519",
        "value": base64.b64encode(signing_key.sign(canonical(stamped)).signature).decode("ascii"),
    }
    return stamped


def ensure_published_at_forward(candidate, live):
    candidate_at = _utc_timestamp(candidate.get("published_at"))
    live_at = _utc_timestamp(live.get("published_at"))
    if candidate_at <= live_at:
        raise SystemExit("候选 published_at 必须晚于线上清单")


def compare_release_versions(left: str, right: str) -> int:
    """Compare strict X.Y.Z release versions without a packaging dependency."""
    try:
        left_parts = tuple(int(part) for part in left.split("."))
        right_parts = tuple(int(part) for part in right.split("."))
    except (AttributeError, TypeError, ValueError):
        raise SystemExit("版本号必须是 X.Y.Z：%r / %r" % (left, right)) from None
    if len(left_parts) != 3 or len(right_parts) != 3:
        raise SystemExit("版本号必须是 X.Y.Z：%r / %r" % (left, right))
    return (left_parts > right_parts) - (left_parts < right_parts)


def ensure_forward_version(candidate: str, live: str, *, resume: bool,
                           allow_same_version: bool = False) -> None:
    """Refuse to move the live pointer backwards or repeat a version unintentionally."""
    relation = compare_release_versions(candidate, live)
    if relation < 0:
        raise SystemExit("候选版本 %s 低于线上版本 %s，拒绝回退发布" % (candidate, live))
    if relation == 0 and not (resume or allow_same_version):
        raise SystemExit("候选版本 %s 与线上版本相同；只有 --resume 才允许续做" % candidate)


def rollback_ledger_path(value, source_root):
    default = Path(source_root) / "tools" / "release_sequence_ledger.json"
    if value is not None:
        raise SystemExit("回滚必须使用默认账本，不允许替换默认账本")
    return default


def ensure_release_sequence(candidate, live, *, resume: bool):
    """Reject a release sequence regression before any network write."""
    if not isinstance(candidate, dict) or not isinstance(live, dict):
        raise SystemExit("候选与线上清单必须是对象")
    candidate_sequence = candidate.get("release_sequence")
    live_sequence = live.get("release_sequence")
    if type(candidate_sequence) is not int or candidate_sequence < 1:
        raise SystemExit("候选清单缺少有效 release_sequence")
    if live_sequence is None:
        return
    if type(live_sequence) is not int or live_sequence < 1:
        raise SystemExit("线上清单缺少有效 release_sequence")
    if candidate_sequence < live_sequence:
        raise SystemExit("候选序号 %d 低于线上序号 %d，拒绝回退发布" %
                         (candidate_sequence, live_sequence))
    if candidate_sequence == live_sequence:
        same_version_promotion = (candidate.get("version") == live.get("version")
                                  and candidate.get("stage") == "stable"
                                  and live.get("stage") == "beta")
        candidate_digest = ((candidate.get("archive") or {}).get("sha256"))
        live_digest = ((live.get("archive") or {}).get("sha256"))
        if candidate.get("version") != live.get("version"):
            raise SystemExit("同序号候选版本必须与线上版本一致，拒绝覆盖")
        if candidate_digest != live_digest:
            raise SystemExit("同序号候选摘要不同，现有发布流程拒绝覆盖")
        if not (resume or same_version_promotion):
            raise SystemExit("候选序号 %d 与线上相同；只有同版本 Beta→Stable 或 --resume 才允许" %
                             candidate_sequence)


def _utc_timestamp(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SystemExit("published_at 必须是 UTC Z 时间戳")
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit("published_at 不是有效时间戳") from exc


def prepare_rollback(candidate, live, ledger, rollback_to, reason, *, now):
    if not isinstance(reason, str) or not reason.strip():
        raise SystemExit("--rollback-to 必须提供非空 --reason")
    if candidate.get("version") != rollback_to:
        raise SystemExit("候选版本必须与 --rollback-to 完全一致")
    records = ledger.get("releases") if isinstance(ledger, dict) else None
    exact = [record for record in records or []
             if isinstance(record, dict)
             and record.get("version") == candidate.get("version")
             and record.get("sequence") == candidate.get("release_sequence")]
    if len(exact) != 1:
        raise SystemExit("回滚候选必须有账本完全匹配记录")
    if _utc_timestamp(now) <= _utc_timestamp(live.get("published_at")):
        raise SystemExit("回滚 published_at 必须晚于线上清单")
    stamped = dict(candidate)
    stamped["published_at"] = now
    stamped["rollback_reason"] = reason.strip()
    return stamped


def verify_manifest_signature(candidate):
    """Verify the final manifest against the public key shipped to clients."""
    from nacl.signing import VerifyKey
    from sf_constants import UPDATE_PUBLIC_KEY_B64
    try:
        signature = candidate["signature"]
        if not isinstance(signature, dict) or signature.get("algorithm") != "ed25519":
            raise ValueError("unsupported signature")
        VerifyKey(base64.b64decode(UPDATE_PUBLIC_KEY_B64, validate=True)).verify(
            canonical(candidate), base64.b64decode(signature["value"], validate=True))
    except Exception as exc:
        raise SystemExit("更新清单签名无效") from exc


def validate_rollback_publication(candidate, live, ledger, rollback_to, reason):
    """Validate the final quality-bound rollback manifest without changing bytes."""
    expected = prepare_rollback(candidate, live, ledger, rollback_to, reason,
                                now=candidate.get("published_at"))
    if expected != candidate:
        raise SystemExit("回滚清单须先写入回滚原因、重新签名并绑定质量证据")
    from nacl.signing import VerifyKey
    from sf_constants import UPDATE_PUBLIC_KEY_B64
    try:
        signature = candidate["signature"]
        if signature.get("algorithm") != "ed25519":
            raise ValueError("unsupported signature")
        VerifyKey(base64.b64decode(UPDATE_PUBLIC_KEY_B64, validate=True)).verify(
            canonical(candidate), base64.b64decode(signature["value"], validate=True))
    except Exception as exc:
        raise SystemExit("回滚清单签名无效") from exc


def live_manifest_snapshot(token: str):
    """Read metadata and the checked Contents SHA in one response."""
    payload = read_contents_main(token)
    try:
        content = base64.b64decode(payload["content"])
        data = json.loads(content)
    except (KeyError, ValueError, TypeError) as exc:
        raise SystemExit("线上清单内容无法解码，拒绝发布") from exc
    if not isinstance(data, dict):
        raise SystemExit("线上清单必须是对象，拒绝发布")
    sha = payload.get("sha")
    if not isinstance(sha, str) or not sha:
        raise SystemExit("线上清单 SHA 无效，拒绝发布")
    return data, sha, content


def live_manifest_metadata(token: str) -> dict:
    """Read the authoritative current manifest before any release write."""
    return live_manifest_snapshot(token)[0]


def live_manifest_version(token: str) -> str:
    """Read the authoritative current manifest version before any release write."""
    data = live_manifest_metadata(token)
    version = str(data.get("version") or "")
    if not version:
        raise SystemExit("线上清单缺少有效 version，拒绝发布")
    return version


def verify_candidate_sequence(archive, manifest, metadata, *, ledger_path=None, source_root=None):
    """Verify source, frozen app, signed manifest, and ZIP before credentials."""
    try:
        source_root = Path(source_root or SOURCE_ROOT)
        ledger_path = Path(ledger_path or source_root / "tools" / "release_sequence_ledger.json")
        app = source_root / "dist" / "Skyrocket.app"
        if not app.is_dir():
            raise ValueError("dist/ 中没有冻结 Skyrocket.app，无法四处核验")
        result = verify_release_sequence(ledger_path, source_root / "sf_constants.py", app, manifest, archive)
        if result["version"] != metadata.get("version") or result["release_sequence"] != metadata.get("release_sequence"):
            raise ValueError("签名清单与四处核验结果不一致")
        return result
    except ValueError as exc:
        raise SystemExit("发布前序号核验失败：%s" % exc) from exc


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


def release_update_url(release_id):
    return API_ROOT + "/releases/" + str(release_id)


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
        published_identity: tuple[str, int] | None = None
        if (("--resume" in sys.argv[1:] or metadata.get("stage") == "stable")
                and isinstance(metadata.get("version"), str)
                and type(metadata.get("release_sequence")) is int
                and metadata["release_sequence"] > 0):
            published_identity = (metadata["version"], metadata["release_sequence"])
        errors = quality.verify_release_candidate(
            SOURCE_ROOT, record, record_path.parent, evidence_manifest,
            published_identity=published_identity)
        if errors:
            raise ValueError("正式发布资格未通过：" + "; ".join(errors))
        stored = quality.load(evidence_manifest)
        manifest_role = stored["release_artifacts"]["roles"]["ota_manifest"]
        if quality.resolve_reference(record_path.parent, manifest_role["path"]) != manifest.resolve():
            raise ValueError("更新清单路径未绑定到质量证据")
        if (manifest_role.get("kind") != "file" or
                manifest_role.get("content_sha256") != hashlib.sha256(manifest_bytes).hexdigest()):
            raise ValueError("更新清单字节与质量绑定不一致")
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


def required_public_asset_names(version, archive_name=None):
    """Return the exact four public assets required for a release version."""
    archive_name = archive_name or "Skyrocket-%s-macos.zip" % version
    return (
        "Skyrocket-V%s.dmg" % version,
        archive_name,
        "Skyrocket-%s-source.zip" % version,
        "Skyrocket-%s-SHA256SUMS.txt" % version,
    )


def public_asset_paths(directory, version, archive_name=None):
    """Resolve and require the four local public assets before any network call."""
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise SystemExit("公开资产目录不存在：%s" % directory)
    paths = {name: directory / name
             for name in required_public_asset_names(version, archive_name)}
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        raise SystemExit("缺少公开资产：%s" % ", ".join(missing))
    return paths


def public_asset_snapshot(directory, version, archive_name=None):
    """Read the local public assets into immutable bytes for the publication gate."""
    try:
        paths = public_asset_paths(directory, version, archive_name)
        return {name: path.read_bytes() for name, path in paths.items()}
    except OSError as exc:
        raise SystemExit("公开资产读取失败：%s" % exc) from exc


def public_asset_download_url(name):
    """Return the fixed public GitHub download URL for one release asset."""
    return "https://github.com/%s/releases/download/%s/%s" % (REPO, TAG, name)


def verify_public_release_assets(release, version, expected, token=None):
    """Verify the four public Release assets without changing remote state.

    ``expected`` maps required asset names to local raw bytes. The API URL is
    used for authenticated readback; when GitHub provides a browser URL, it
    must also match the fixed public download address.
    """
    if not isinstance(expected, dict):
        raise SystemExit("公开资产本地期望值必须是对象")
    fixed = (
        "Skyrocket-V%s.dmg" % version,
        "Skyrocket-%s-source.zip" % version,
        "Skyrocket-%s-SHA256SUMS.txt" % version,
    )
    archive_names = [name for name in expected
                     if name.endswith("-macos.zip") and name in expected]
    if len(archive_names) != 1:
        raise SystemExit("公开资产的 macOS ZIP 必须恰好一个")
    required = (fixed[0], archive_names[0], fixed[1], fixed[2])
    missing = [name for name in required if name not in expected]
    if missing:
        raise SystemExit("缺少公开资产本地期望值：%s" % ", ".join(missing))
    if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
        raise SystemExit("Release 缺少 assets 列表")

    verified = {}
    for name in required:
        local = expected[name]
        if not isinstance(local, bytes):
            raise SystemExit("公开资产 %s 的本地期望值必须是原始字节" % name)
        matches = [asset for asset in release["assets"]
                   if isinstance(asset, dict) and asset.get("name") == name]
        if len(matches) != 1:
            raise SystemExit("公开资产 %s 必须恰好一个，实际 %d 个" % (name, len(matches)))
        asset = matches[0]
        if type(asset.get("size")) is not int or asset["size"] != len(local):
            raise SystemExit("公开资产 %s 大小不匹配：线上 %r，期望 %d" %
                             (name, asset.get("size"), len(local)))
        browser_url = asset.get("browser_download_url")
        if browser_url and urllib.parse.unquote(browser_url) != public_asset_download_url(name):
            raise SystemExit("公开资产 %s 下载地址不匹配" % name)
        remote = download_asset(token or "", asset)
        if remote != local:
            raise SystemExit("公开资产 %s 原始字节不匹配" % name)
        verified[name] = {
            "name": name,
            "size": len(remote),
            "sha256": hashlib.sha256(remote).hexdigest(),
        }
    return verified


# 读回是只读 GET，重读不会改变线上任何东西；中途断线（v1.4.105 实测 IncompleteRead，
# 27MB 的包读到 5.6MB 与 0.3MB 就断）不该让一次已经上传成功的发布停在半路，
# 而重新运行本工具会因同名资产被拒。重读之后仍要逐字节比对，不放宽任何判据。
DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_RETRY_PAUSE = 5.0
_TRANSIENT_DOWNLOAD_ERRORS = (OSError, http.client.HTTPException)


def _download_retryable(exc):
    """服务端明确说“你错了”的状态码（401/403/404 等）重读没有意义，直接停。"""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 429) or exc.code >= 500
    return True


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
    last = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=resolve_timeouts()[0]) as response:
                return response.read()
        except _TRANSIENT_DOWNLOAD_ERRORS as exc:
            last = exc
            if not _download_retryable(exc) or attempt == DOWNLOAD_ATTEMPTS:
                break
            print("   资产读回第 %d 次中断（%r），%.0f 秒后重读" % (attempt, exc, DOWNLOAD_RETRY_PAUSE))
            time.sleep(DOWNLOAD_RETRY_PAUSE)
        except Exception as exc:
            raise SystemExit("上传后资产原始字节读回失败：%r" % (exc,)) from exc
    raise SystemExit("上传后资产原始字节读回失败（读了 %d 次）：%r" % (attempt, last)) from last


def reuse_uploaded_archive(token, release, archive, archive_bytes):
    """续做用：同名资产已在线上，且读回的字节与本地固定快照逐字节相同，才复用它；否则照旧拒绝。

    场景（v1.4.104、v1.4.105 连续两次）：ZIP 已上传成功，读回中途断线，发布工具停在清单切换之前；
    重新运行会因“同名资产”被拒，此前只能手写脚本接着做。这里不放宽“绝不覆盖”：字节不同、大小不同
    一律仍然拒绝，复用的资产照样要过读回比对。"""
    existing = [a for a in release.get("assets", []) if a.get("name") == archive.name]
    if len(existing) != 1:
        raise SystemExit("固定 Release 里有 %d 个同名版本 ZIP，无法确定复用哪一个，拒绝复用，也拒绝覆盖" % len(existing))
    if existing[0].get("size") != len(archive_bytes):
        raise SystemExit("固定 Release 里的同名版本 ZIP 大小与本地快照不同（线上 %s 字节，本地 %d 字节），"
                         "拒绝复用，也拒绝覆盖" % (existing[0].get("size"), len(archive_bytes)))
    try:
        remote = download_asset(token, existing[0])
    except SystemExit as exc:
        # download_asset 的措辞是给“刚上传完的读回”写的；续做读的是线上已有的资产，原因要说准。
        raise SystemExit("续做：读回线上已有的同名版本 ZIP 失败，无法确认它与本地快照一致，拒绝复用，"
                         "也拒绝覆盖。原因：%s" % (exc,)) from exc
    if remote != archive_bytes:
        raise SystemExit("固定 Release 里的同名版本 ZIP 与本地快照字节不同（读回 %d 字节，本地 %d 字节），"
                         "拒绝复用，也拒绝覆盖" % (len(remote), len(archive_bytes)))
    print("   续做：线上已有同名版本 ZIP，大小与字节都与本地快照一致，复用，不重新上传")
    return existing[0]


def upload_archive(token, release, archive, archive_bytes, resume=False, reuse_existing=False):
    if any(asset.get("name") == archive.name for asset in release.get("assets", [])):
        if not (resume or reuse_existing):
            raise SystemExit("固定 Release 已有同名版本 ZIP，拒绝覆盖")
        return reuse_uploaded_archive(token, release, archive, archive_bytes)
    request(upload_url(release["id"], archive.name), token, method="POST",
            data=archive_bytes, content_type="application/zip",
            timeout=resolve_timeouts()[0])
    asset = verified_asset(load_release(token), archive, len(archive_bytes))
    downloaded = download_asset(token, asset)
    if downloaded != archive_bytes:
        raise SystemExit("上传后 ZIP 原始字节与本地固定快照不一致，未更新线上清单")
    return asset


def update_release_body(token, release, notes):
    """Bind the public Release body to the same signed, current-version notes as OTA.

    The Release page is a user-facing distribution surface. Writing its body before the
    manifest pointer means a failed write cannot expose clients to a new update whose
    public description differs from the signed manifest.
    """
    payload = json.dumps({"body": notes}, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    request(release_update_url(release["id"]), token, method="PATCH", data=payload)
    read_back = load_release(token)
    if read_back.get("body") != notes:
        raise SystemExit("GitHub Release 发布说明读回不一致，未更新线上清单")
    return read_back


def manifest_commit_payload(manifest_bytes, existing_sha):
    payload = {
        "message": "发布更新清单指针",
        "content": base64.b64encode(manifest_bytes).decode("ascii"),
        "branch": "main",
    }
    if existing_sha:
        payload["sha"] = existing_sha
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def publish_manifest_pointer(token, manifest_bytes, *, expected_sha):
    """Use the SHA checked with the publication decision, never a newer unchecked SHA."""
    url = contents_url()
    if expected_sha is not None and (not isinstance(expected_sha, str) or not expected_sha):
        raise SystemExit("前置清单 SHA 无效，拒绝写入")
    request(url, token, method="PUT",
            data=manifest_commit_payload(manifest_bytes, expected_sha))
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


def readback_verdict(repo_manifest, expected_version, raw_manifest=None, expected=None):
    """Require repository readback to match version and supplied manifest fields.

    The raw endpoint may legitimately lag behind Contents because of CDN caching; it is
    reported but does not override the authoritative repository result.
    """
    lines, ok = [], True
    repo_ver = (repo_manifest or {}).get("version")
    if repo_ver != expected_version:
        ok = False
        lines.append("!! 仓库里的清单版本是 %r，期望 %r —— 发布没有真正生效，不要宣布成功"
                     % (repo_ver, expected_version))
    else:
        lines.append("   读回通过：仓库清单 version=%s（与本轮一致）" % repo_ver)
    if expected is not None:
        for key in ("release_sequence", "published_at"):
            if repo_manifest.get(key) != expected.get(key):
                ok = False
                lines.append("!! 仓库里的清单 %s=%r，期望 %r" %
                             (key, repo_manifest.get(key), expected.get(key)))
        expected_archive = expected.get("archive")
        repo_archive = repo_manifest.get("archive")
        if not isinstance(expected_archive, dict) or not isinstance(repo_archive, dict):
            ok = False
            lines.append("!! 仓库里的清单 archive 不完整")
        else:
            for key in ("sha256", "size"):
                if repo_archive.get(key) != expected_archive.get(key):
                    ok = False
                    lines.append("!! 仓库里的清单 archive.%s=%r，期望 %r" %
                                 (key, repo_archive.get(key), expected_archive.get(key)))
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
    expected = json.loads(expected_bytes.decode("utf-8")) if expected_bytes is not None else None
    ok, lines = readback_verdict(repo, expected_version, raw, expected)
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


def check_version_main(argv: list[str]) -> int:
    """Run the read-only version guard without requiring release notes."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-version", action="store_true", required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--notes", type=Path)
    args = parser.parse_args(argv)
    if args.manifest is None:
        raise SystemExit("--check-version 必须提供 --manifest")
    try:
        candidate = json.loads(args.manifest.read_text(encoding="utf-8"))
        candidate_version = str(candidate.get("version") or "")
        candidate_sequence = candidate.get("release_sequence")
        if type(candidate_sequence) is not int or candidate_sequence < 1:
            raise SystemExit("候选清单缺少有效 release_sequence")
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit("候选清单无法读取：%s" % exc) from exc
    token = github_token()
    live_metadata = live_manifest_metadata(token)
    live = str(live_metadata.get("version") or "")
    if not live:
        raise SystemExit("线上清单缺少有效 version，拒绝发布")
    ensure_forward_version(candidate_version, live, resume=False,
                           allow_same_version=(candidate_version == live
                                               and candidate.get("stage") == "stable"
                                               and live_metadata.get("stage") == "beta"))
    ensure_release_sequence(candidate, live_metadata, resume=False)
    ensure_published_at_forward(candidate, live_metadata)
    print("版本前置检查通过：候选 %s 高于线上 %s" % (candidate_version, live))
    return 0


def prepare_rollback_main(argv):
    """Create final rollback bytes locally before quality evidence is bound."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-rollback", action="store_true", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rollback-to", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--published-at", required=True)
    args = parser.parse_args(argv)
    ledger_path = SOURCE_ROOT / "tools" / "release_sequence_ledger.json"
    verify_rollback_sequence(ledger_path, args.manifest, args.archive)
    candidate = json.loads(args.manifest.read_bytes())
    if candidate.get("version") != args.rollback_to or not args.reason.strip():
        raise SystemExit("回滚版本或原因无效")
    _utc_timestamp(args.published_at)
    key_path = SOURCE_ROOT / ".private_tools" / "update_signing_key.txt"
    key = SigningKey(base64.b64decode(key_path.read_text(encoding="ascii").strip(), validate=True))
    from sf_constants import UPDATE_PUBLIC_KEY_B64
    if bytes(key.verify_key) != base64.b64decode(UPDATE_PUBLIC_KEY_B64, validate=True):
        raise SystemExit("回滚签名密钥与客户端公钥不一致")
    candidate["rollback_reason"] = args.reason.strip()
    signed = resign_manifest(candidate, key, args.published_at)
    with args.output.open("x", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(signed, ensure_ascii=False, indent=2) + "\n")
    print("回滚清单已本地准备；发布前必须用质量工具绑定该最终文件")
    return 0


def main():
    if "--prepare-rollback" in sys.argv[1:]:
        return prepare_rollback_main(sys.argv[1:])
    if "--self-test" in sys.argv[1:]:
        return self_test()
    if "--check-version" in sys.argv[1:]:
        return check_version_main(sys.argv[1:])
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--notes", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--resume", action="store_true",
                        help="线上已有同名版本 ZIP 且字节与本地快照一致时复用它接着做；不一致仍拒绝覆盖")
    parser.add_argument("--rollback-to",
                        help="显式回滚到已分配的版本；必须同时提供 --reason")
    parser.add_argument("--reason", help="显式回滚原因")
    parser.add_argument("--ledger", type=Path,
                        help="发布序号账本路径（默认使用源码树 tools/release_sequence_ledger.json）")
    parser.add_argument("--public-assets", type=Path,
                        help="已上传公开资产的本地快照目录；用于 OTA 指针切换前的四资产读回")
    args = parser.parse_args()
    if args.rollback_to and not args.reason:
        raise SystemExit("--rollback-to 必须同时提供 --reason")
    if args.reason and not args.rollback_to:
        raise SystemExit("--reason 只能与 --rollback-to 一起使用")
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
    try:
        manifest_metadata = json.loads(manifest.read_text(encoding="utf-8"))
        expected_version = str(manifest_metadata.get("version") or "")
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit("发布前核验失败：更新清单无法读取：%s" % exc) from exc
    public_assets = None
    if args.public_assets is not None:
        public_assets = public_asset_snapshot(args.public_assets, expected_version, archives[0].name)
    archive = archives[0]
    archive_bytes, manifest_bytes, expected_version = publication_snapshot(archive, manifest, args.notes)
    metadata = json.loads(manifest_bytes)
    verify_manifest_signature(metadata)
    if type(metadata.get("release_sequence")) is not int or metadata["release_sequence"] < 1:
        raise SystemExit("候选清单缺少有效 release_sequence")
    if args.rollback_to:
        try:
            verify_rollback_sequence(rollback_ledger_path(args.ledger, SOURCE_ROOT), manifest, archive)
        except ValueError as exc:
            raise SystemExit("回滚前身份核验失败：%s" % exc) from exc
    else:
        verify_candidate_sequence(archive, manifest, metadata, ledger_path=args.ledger,
                                  source_root=SOURCE_ROOT)
    if public_assets is not None and archive_bytes != public_assets[archive.name]:
        raise SystemExit("公开资产快照与版本 ZIP 核验字节不一致")
    default_ledger = SOURCE_ROOT / "tools" / "release_sequence_ledger.json"
    if args.ledger is not None:
        raise SystemExit("发布必须使用默认账本，不允许替换默认账本")
    identity_records = [item for item in load_ledger(default_ledger)["releases"]
                        if item["version"] == expected_version and
                        item["sequence"] == metadata["release_sequence"]]
    if len(identity_records) != 1 or identity_records[0].get("status") not in ("allocated", "published"):
        raise SystemExit("发布账本身份不存在或已撤回")
    published_identity = identity_records[0].get("status") == "published"
    notes = metadata["notes"]
    token = github_token()
    release = load_release(token)
    live_metadata, checked_live_sha, live_bytes = live_manifest_snapshot(token)
    live_version = str(live_metadata.get("version") or "")
    if not live_version:
        raise SystemExit("线上清单缺少有效 version，拒绝发布")
    if args.rollback_to:
        ledger_path = rollback_ledger_path(args.ledger, SOURCE_ROOT)
        validate_rollback_publication(metadata, live_metadata, load_ledger(ledger_path),
                                      args.rollback_to, args.reason)
    else:
        ensure_forward_version(expected_version, live_version, resume=args.resume,
                               allow_same_version=(
                                   metadata.get("version") == live_metadata.get("version")
                                   and metadata.get("stage") == "stable"
                                   and live_metadata.get("stage") == "beta"))
        if "release_sequence" in metadata:
            ensure_release_sequence(metadata, live_metadata, resume=args.resume)
            if not (args.resume and metadata == live_metadata):
                ensure_published_at_forward(metadata, live_metadata)
    reuse_existing = bool(args.rollback_to or (
        metadata.get("version") == live_metadata.get("version")
        and metadata.get("stage") == "stable"
        and live_metadata.get("stage") == "beta"))
    if published_identity:
        if reuse_existing and not args.rollback_to:
            immutable_candidate = {key: value for key, value in metadata.items()
                                   if key not in ("stage", "published_at", "signature")}
            immutable_live = {key: value for key, value in live_metadata.items()
                              if key not in ("stage", "published_at", "signature")}
            if immutable_candidate != immutable_live:
                raise SystemExit("Beta 转稳定只允许改变阶段、发布时间和签名")
        if not (args.resume or reuse_existing):
            raise SystemExit("已发布身份只允许续做或 Beta 转稳定")
        if (metadata.get("version") != live_metadata.get("version") or
                metadata.get("release_sequence") != live_metadata.get("release_sequence") or
                (metadata.get("archive") or {}).get("sha256") !=
                (live_metadata.get("archive") or {}).get("sha256")) and not args.rollback_to:
            raise SystemExit("已发布身份与线上清单不一致")
        if not any(item.get("name") == archive.name for item in release.get("assets", [])):
            raise SystemExit("已发布身份的 ZIP 缺失，拒绝重传")
    if (args.resume and not args.rollback_to and not reuse_existing and
            metadata.get("version") == live_metadata.get("version") and
            metadata.get("release_sequence") == live_metadata.get("release_sequence") and
            manifest_bytes != live_bytes):
        raise SystemExit("已发布续做的清单必须与线上清单完全一致")
    current_records = [item for item in load_ledger(default_ledger)["releases"]
                       if item["version"] == expected_version and
                       item["sequence"] == metadata["release_sequence"]]
    if len(current_records) != 1 or current_records[0].get("status") not in ("allocated", "published"):
        raise SystemExit("发布账本身份在写入前已变化，拒绝发布")
    asset = upload_archive(token, release, archive, archive_bytes,
                           resume=args.resume, reuse_existing=reuse_existing)
    if public_assets is not None:
        release = load_release(token)
        verify_public_release_assets(release, expected_version, public_assets, token)
    # Bind the public release page before exposing the new manifest to clients.
    release = update_release_body(token, release, notes)
    # 新客户端的 raw 清单先切换：一旦它可见，ZIP 与公开说明已存在且已验证。
    publish_manifest_pointer(token, manifest_bytes, expected_sha=checked_live_sha)
    # 旧客户端仍从固定 Release 取清单；最后才替换这一枚小清单，不触碰历史 ZIP。
    publish_legacy_manifest(token, load_release(token), manifest_bytes)
    print("published_archive=%s" % asset["name"])
    print("published_manifest_pointer=main/%s" % MANIFEST_PATH)
    print("published_legacy_manifest=%s" % MANIFEST_PATH)
    # 打印不等于生效：必须绕开 CDN 读回仓库里的那一份，版本对不上就中止发版。
    verify_live_manifest(token, expected_version, manifest_bytes)
    mark_published(SOURCE_ROOT / "tools" / "release_sequence_ledger.json",
                   expected_version, metadata["release_sequence"])
    print("   线上清单读回一致，发版可以宣布成功")


if __name__ == "__main__":
    main()
