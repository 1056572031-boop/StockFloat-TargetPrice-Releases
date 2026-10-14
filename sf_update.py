# -*- coding: utf-8 -*-
"""安全的软件更新协议：签名清单、受限下载与外部安装助手交接。"""

import base64
import concurrent.futures
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from sf_logging import log_line
from sf_http import RestrictedRequest, safe_urlopen

from sf_constants import (APP_DISPLAY_NAME, APP_VERSION,
                          BUNDLE_ID, STAGING_PRUNE_GRACE_SEC, UPDATE_ARCHIVE_MAX_BYTES,
                          UPDATE_MANIFEST_ATTEMPTS, UPDATE_MANIFEST_MAX_BYTES,
                          UPDATE_MANIFEST_TIMEOUT_SEC, UPDATE_MANIFEST_URL,
                          UPDATE_PUBLIC_KEY_B64, UPDATE_SIGNING_CERT_SHA1,
                          UPDATE_SIGNING_TEAM_ID, UPDATE_STAGING_DIR)


_VERSION_RE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
_ALLOWED_HOSTS = frozenset({"github.com", "raw.githubusercontent.com",
                            "objects.githubusercontent.com",
                            "github-releases.githubusercontent.com",
                            "release-assets.githubusercontent.com"})
_APP_NAME = APP_DISPLAY_NAME + ".app"


class UpdateError(Exception):
    """更新检查、下载或交接失败；调用方不应影响行情主流程。"""


@dataclasses.dataclass(frozen=True)
class UpdateInfo:
    """一份**已验签且已校验过**的线上更新清单，字段全部来自清单 JSON。

    frozen：构造后不改写，避免下载途中被就地改坏（同一份对象会同时喂给下载、校验与助手）。
    各字段的来源与口径（校验发生在解析清单时，不在这里）：
        version        三段数字版本号（`parse_version` 只收 X.Y.Z，预发布/歧义串一律拒）
        notes          本版更新说明，≤4000 字符；更新弹窗只展示本版这一段
        archive_url    版本 zip 的下载地址（Release 资产 URL）
        archive_size   压缩包字节数（int，1 ≤ 它 ≤ UPDATE_ARCHIVE_MAX_BYTES）——
                       分段下载的区间、进度上限、落盘后的大小核对都取这个值
        archive_sha256 64 位小写十六进制摘要，逐段下载完成后整包核对
        stage          'beta' | 'stable'，**纯展示**：只影响弹窗措辞，不改"是否提示更新"，
                       channel 仍恒为 stable，所有客户端照常更新；缺失或伪造值按 stable
    """
    version: str
    notes: str
    archive_url: str
    archive_size: int
    archive_sha256: str
    # 发版阶段标签（纯展示，不改变更新链路）：'beta' | 'stable'。
    # 缺省 stable，兼容不含该字段的旧清单；channel 仍恒为 stable，所有客户端照常更新。
    stage: str = "stable"


def parse_version(value):
    """只接受三段数字版本，防止预发布/歧义版本进入稳定更新链。"""
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
        raise UpdateError("版本号格式无效")
    return tuple(int(part) for part in value.split("."))


def is_newer(candidate, current=APP_VERSION):
    """candidate 是否**严格新于** current。同版本返回 False —— 不重复提示已装的这一版。

    比较按三段数字逐位比大小（(1,4,10) > (1,4,9)），不是字符串比较，所以 1.4.10 不会被
    判成比 1.4.9 旧。格式不合法直接抛 UpdateError（由 parse_version 把关），
    调用方不许把它当 False 吞掉 —— "版本号写法不对"和"没有新版"是两件事。
    """
    return parse_version(candidate) > parse_version(current)


def offer_headline(version, stage="stable"):
    """更新弹窗主文案：Beta 版显式标出 "Beta"，稳定版保持原有措辞。

    纯函数，便于单测；不改变"是否提示更新"的判定，只影响给用户看的一句话。"""
    if stage == "beta":
        return "发现新 Beta 版本 v%s。" % version
    return "发现新版本 v%s。" % version


# release_notes.txt 里每个版本块的标题（"V1.4.20 发布说明（Beta）:" 等）。
_NOTES_HEADING_RE = re.compile(r"^V(\d+\.\d+\.\d+)\s+发布说明")


def notes_for_version(notes, version):
    """从"多版本堆叠"的发布说明里只取本次版本那一段（标题行不含）。

    为什么要它：弹窗正文此前直接把整份 release_notes.txt 搬上来，v1.4.20 那次
    一屏塞了 4 个版本的说明，用户只想看"这次更新了什么"。发版侧（
    tools/make_update_assets.py）已改为只签名本次版本那一段，但**线上已有清单
    是全文**，且旧客户端拿到的也是全文，所以显示侧必须自己会裁。

    取不到本次版本那一段时**原样返回**：宁可文字多，也不能把更新说明显示成空白。
    纯函数，无 IO，便于单测。
    """
    text = (notes or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    headings = []
    for index, line in enumerate(lines):
        match = _NOTES_HEADING_RE.match(line.strip())
        if match:
            headings.append((index, match.group(1)))
    if not headings:
        return text                 # 不是分块格式（例如只写了一句话）：不裁
    for order, (start, ver) in enumerate(headings):
        if ver != version:
            continue
        end = headings[order + 1][0] if order + 1 < len(headings) else len(lines)
        block = "\n".join(lines[start + 1:end]).strip()
        return block or text        # 该版本块是空的：退回全文，别显示空白
    return text


def _canonical_payload(manifest):
    payload = dict(manifest)
    payload.pop("signature", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _check_release_url(value):
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise UpdateError("更新地址不受信任")
    if parsed.username or parsed.password or parsed.fragment:
        raise UpdateError("更新地址格式无效")


def verify_manifest(raw):
    """验证线上清单。签名失败前不会向界面暴露任何下载地址。"""
    if not isinstance(raw, bytes) or len(raw) > UPDATE_MANIFEST_MAX_BYTES:
        raise UpdateError("更新清单大小异常")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("更新清单无法读取") from exc
    if not isinstance(manifest, dict):
        raise UpdateError("更新清单格式无效")
    if manifest.get("schema") != 1 or manifest.get("channel") != "stable":
        raise UpdateError("更新清单版本不受支持")
    if manifest.get("bundle_id") != BUNDLE_ID or manifest.get("app_name") != _APP_NAME:
        raise UpdateError("更新包不属于当前应用")
    signature = manifest.get("signature")
    if not isinstance(signature, dict) or signature.get("algorithm") != "ed25519":
        raise UpdateError("更新清单缺少有效签名")
    try:
        key = VerifyKey(base64.b64decode(UPDATE_PUBLIC_KEY_B64, validate=True))
        key.verify(_canonical_payload(manifest),
                   base64.b64decode(signature.get("value", ""), validate=True))
    except (ValueError, TypeError, BadSignatureError) as exc:
        raise UpdateError("更新清单签名校验失败") from exc
    archive = manifest.get("archive")
    if not isinstance(archive, dict):
        raise UpdateError("更新包信息缺失")
    version = manifest.get("version")
    parse_version(version)
    url = archive.get("url")
    _check_release_url(url)
    size = archive.get("size")
    digest = archive.get("sha256")
    if (not isinstance(size, int) or size < 1 or size > UPDATE_ARCHIVE_MAX_BYTES
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise UpdateError("更新包校验信息无效")
    notes = manifest.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 4000:
        raise UpdateError("更新说明无效")
    # 发版阶段标签（纯展示）：只认 beta/stable，异常/缺失一律按 stable，
    # 保证不含该字段的旧清单、或伪造值都不会改变"照常更新"的行为。
    stage = manifest.get("stage")
    if stage not in ("beta", "stable"):
        stage = "stable"
    return UpdateInfo(version, notes, url, size, digest, stage)


def _request_url(url):
    """把签名清单中的 Unicode 路径转为可发出的 HTTPS 请求地址。

    GitHub 标签可使用中文；清单验签仍使用原始文本，只有实际 HTTP 请求使用
    百分号编码，避免 Python 3.14 的 urllib 在写请求行时因非 ASCII 路径中止。
    """
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(parsed._replace(
        path=urllib.parse.quote(parsed.path, safe="/%"),
        params=urllib.parse.quote(parsed.params, safe=";%="),
        query=urllib.parse.quote(parsed.query, safe="=&;%"),
    ))


def _open_limited(url, max_bytes, timeout):
    _check_release_url(url)
    request = RestrictedRequest(_request_url(url), allowed_hosts=_ALLOWED_HOSTS,
                                headers={"User-Agent": "Skyrocket-Updater"})
    try:
        response = safe_urlopen(request, timeout=timeout)
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError("无法连接更新服务") from exc
    # 初始 URL 和重定向终点都必须是 GitHub 官方下载域；签名可防篡改，
    # 域名限制同时防止被异常重定向到第三方站点泄漏网络元数据。
    final_url = urllib.parse.urlparse(response.geturl())
    if (final_url.scheme != "https" or final_url.hostname not in _ALLOWED_HOSTS
            or final_url.username or final_url.password):
        response.close()
        raise UpdateError("更新下载重定向不安全")
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > max_bytes:
                response.close()
                raise UpdateError("下载文件超过安全上限")
        except ValueError as exc:
            response.close()
            raise UpdateError("下载文件长度无效") from exc
    return response


def fetch_manifest(timeout=UPDATE_MANIFEST_TIMEOUT_SEC):
    """取线上清单的**原始字节**，返回 bytes；这里不验签、不解析、不判版本。

    签名校验与字段解析都在调用方（`check_for_update`）—— 分开的理由是"取回什么"与
    "能不能信它"是两步：取回失败可以重试（见 check_for_update 的额度说明），
    验签失败绝不能重试（内容不对，重取一遍只是白跑一次网络）。
    体积上限走 UPDATE_MANIFEST_MAX_BYTES：读多 1 字节就整包判为超限并拒绝，
    避免被人塞一个巨型清单把客户端拖死。
    """
    with _open_limited(UPDATE_MANIFEST_URL, UPDATE_MANIFEST_MAX_BYTES, timeout) as response:
        raw = response.read(UPDATE_MANIFEST_MAX_BYTES + 1)
    if len(raw) > UPDATE_MANIFEST_MAX_BYTES:
        raise UpdateError("更新清单超过安全上限")
    return raw


def check_for_update(current_version=APP_VERSION, attempts=UPDATE_MANIFEST_ATTEMPTS):
    """取线上清单并回答"有没有新版"；**只给取清单这一步**留重试额度。

    v1.4.43 的两处放宽，数字来源见 `sf_constants.UPDATE_MANIFEST_TIMEOUT_SEC`
    旁的实测说明（本机 6 次首字节 7.38~8.25 秒、另有 1 次 12 秒无响应）：
      · 单次超时 5 秒 → 12 秒：5 秒在这条链路上等于**每次检查必失败**，
        客户端从来没被告知过新版本，自动更新形同不存在；
      · 允许重试一次：吃掉"单条连接被 CDN 提前掐断"这类瞬时故障。
    **验签不重试**：签名/格式失败是"内容不对"，重取一遍只会白跑一次网络，
    而且失败必须原样上抛，让界面如实说"暂时无法检查更新"，绝不静默当成没新版。
    """
    raw = None
    last_error = None
    for _ in range(max(1, attempts)):
        try:
            raw = fetch_manifest(timeout=UPDATE_MANIFEST_TIMEOUT_SEC)
            break
        except UpdateError as exc:
            last_error = exc
    if raw is None:
        raise last_error or UpdateError("无法连接更新服务")
    info = verify_manifest(raw)
    return info if is_newer(info.version, current_version) else None


def _file_matches(path, expected_size, expected_sha256):
    try:
        if path.stat().st_size != expected_size:
            return False
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest() == expected_sha256
    except OSError:
        return False


# 分片数与单片重试次数（v1.4.22 提速，依据 2026-09-23 真实更新包实测）：
#   8 路 1.1 MB/s、12 路 2.3 MB/s、16 路 2.1 MB/s，单路只有 1.5 MB/s —— 更多的
#   分片能从同一条窄管道里挤出更多吞吐；而 8 路里几乎每次都有个别连接被 CDN
#   提前掐断（实测最慢分片 0.0 MB/s），一处失败会让整轮并行报废、一路路降到
#   单路 —— 所以分片要更多、且**每个分片能从断点续传重试**，把一次坏连接的代价
#   从"整轮重来"降到"补一小段"。
_PART_COUNT = 16
_PART_ATTEMPTS = 4


def _download_ranges(size, parts=_PART_COUNT):
    """把包切为不重叠的有效 Range；极小测试包不生成 0--1 之类非法范围。"""
    if not isinstance(size, int) or size < 1:
        raise UpdateError("更新包大小无效")
    parts = max(1, min(parts, size))
    base, remainder = divmod(size, parts)
    ranges = []
    start = 0
    for index in range(parts):
        length = base + (1 if index < remainder else 0)
        end = start + length - 1
        ranges.append((start, end))
        start = end + 1
    return ranges


def resolve_download_url(info, cancel_event, timeout):
    """Resolve the official asset once; unavailable HEAD falls back to the signed URL."""
    if cancel_event is not None and cancel_event.is_set():
        raise UpdateError("下载已取消")
    _check_release_url(info.archive_url)
    if urllib.parse.urlparse(info.archive_url).hostname != "github.com":
        return info.archive_url
    request = RestrictedRequest(
        _request_url(info.archive_url), method="HEAD",
        allowed_hosts=_ALLOWED_HOSTS,
        headers={"User-Agent": "Skyrocket-Updater", "Cache-Control": "no-cache"})
    try:
        response = safe_urlopen(request, timeout=min(timeout, UPDATE_MANIFEST_TIMEOUT_SEC))
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        if cancel_event is not None and cancel_event.is_set():
            raise UpdateError("下载已取消") from exc
        log_line("update asset resolution unavailable; using signed origin (%s)" %
                 type(exc).__name__, "WARNING")
        return info.archive_url
    with response:
        resolved = response.geturl()
        _check_release_url(resolved)
        declared = response.headers.get("Content-Length")
        if declared:
            try:
                if int(declared) != info.archive_size:
                    raise UpdateError("更新包大小与签名清单不一致")
            except ValueError as exc:
                raise UpdateError("下载文件长度无效") from exc
        if cancel_event is not None and cancel_event.is_set():
            raise UpdateError("下载已取消")
        return resolved


class _DownloadSource:
    """A per-download URL shared by workers, with at most one expiry refresh."""

    def __init__(self, info, cancel_event, timeout):
        self.info = info
        self.cancel_event = cancel_event
        self.timeout = timeout
        self.lock = threading.Lock()
        self.url = resolve_download_url(info, cancel_event, timeout)
        self.refreshed = False

    def get_url(self):
        with self.lock:
            return self.url

    def refresh(self, failed_url):
        with self.lock:
            if self.url != failed_url:
                return True
            if self.url == self.info.archive_url:
                return False
            if self.refreshed:
                self.url = self.info.archive_url
                log_line("update asset rejected after refresh; falling back to signed origin",
                         "WARNING")
                return True
            self.refreshed = True
            self.url = resolve_download_url(self.info, self.cancel_event, self.timeout)
            return True


def _download_range(url, start, end, part_path, cancel_event, timeout,
                    progress_callback=None, attempts=_PART_ATTEMPTS, source=None):
    """下载一个分片，中断时从断点续传重试（最多 attempts 次）。

    重试必须从 offset 续传而不是从头来：分片越小、续传越准，一次坏连接的代价
    才是"补一小段"而不是"整个分片重来"。连接失败与**读取中断**都算可重试 ——
    后者此前会作为裸 OSError 逃出本函数，让整轮并行下载直接报废（实测就是它
    把 8 路打到只剩单路）。取消请求不属于失败，立即上抛。"""
    offset = start
    last_error = None
    for _ in range(max(1, attempts)):
        if cancel_event is not None and cancel_event.is_set():
            raise UpdateError("下载已取消")
        if source is not None:
            url = source.get_url()
        _check_release_url(url)
        request = RestrictedRequest(
            _request_url(url),
            allowed_hosts=_ALLOWED_HOSTS,
            headers={"User-Agent": "Skyrocket-Updater",
                     "Range": "bytes=%d-%d" % (offset, end)},
        )
        try:
            response = safe_urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in (401, 403) and source is not None:
                if source.refresh(url):
                    continue
                raise UpdateError("更新下载地址已失效") from exc
            last_error = UpdateError("无法连接更新服务: HTTP %s" % exc.code)
            continue
        except (urllib.error.URLError, OSError) as exc:
            last_error = UpdateError("无法连接更新服务: %r" % (exc,))
            continue
        with response:
            _check_release_url(response.geturl())
            if response.status != 206:
                raise UpdateError("更新服务不支持分段下载")
            content_range = response.headers.get("Content-Range", "")
            if not content_range.startswith("bytes %d-%d/" % (offset, end)):
                raise UpdateError("更新分段响应无效")
            try:
                with open(part_path, "r+b", buffering=0) as out:
                    out.seek(offset)
                    while offset <= end:
                        if cancel_event is not None and cancel_event.is_set():
                            raise UpdateError("下载已取消")
                        chunk = response.read(min(1024 * 1024, end - offset + 1))
                        if not chunk:
                            break
                        out.write(chunk)
                        offset += len(chunk)
                        if progress_callback is not None:
                            progress_callback(len(chunk))
            except OSError:
                # 读到一半连接断了/超时：换一条连接从断点接着来。
                last_error = UpdateError("更新分段读取中断")
                continue
        if offset != end + 1:
            last_error = UpdateError("下载文件大小异常")
            continue
        return
    raise last_error or UpdateError("下载分段失败")


def _load_resume_state(state_path, info):
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if (data.get("version") == info.version
                and data.get("size") == info.archive_size
                and data.get("sha256") == info.archive_sha256):
            max_parts = len(_download_ranges(info.archive_size))
            return {int(index) for index in data.get("completed", [])
                    if isinstance(index, int) and 0 <= index < max_parts}
    except (OSError, ValueError, TypeError):
        pass
    return set()


def _save_resume_state(state_path, info, completed):
    data = {"version": info.version, "size": info.archive_size,
            "sha256": info.archive_sha256, "completed": sorted(completed)}
    temp = state_path.with_suffix(".json.tmp-%d-%d" % (os.getpid(), time.time_ns()))
    temp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, state_path)


def _worker_ladder(parts):
    """并发阶梯：随分片数推导、按分片数封顶、去重。

    16 分片就该有 16 路并发（实测 12-16 路吞吐约为 8 路的两倍）；分片比阶梯
    少时按分片数封顶并去掉重复档，避免小包出现 8、8、4、1 这种空转档位。
    """
    ladder = []
    for workers in (_PART_COUNT, 8, 4, 1):
        workers = min(workers, max(1, parts))
        if workers not in ladder:
            ladder.append(workers)
    return ladder


def _download_adaptive(info, part_path, state_path, cancel_event, timeout,
                       progress_callback=None):
    ranges = _download_ranges(info.archive_size)
    completed = _load_resume_state(state_path, info) if part_path.is_file() else set()
    if not part_path.is_file() or part_path.stat().st_size != info.archive_size:
        with open(part_path, "wb") as out:
            out.truncate(info.archive_size)
        os.chmod(part_path, 0o600)
        completed.clear()
        _save_resume_state(state_path, info, completed)
    completed_bytes = sum(ranges[index][1] - ranges[index][0] + 1 for index in completed)
    progress_lock = threading.Lock()
    covered = [end - start + 1 if index in completed else 0
               for index, (start, end) in enumerate(ranges)]

    def part_progress(index):
        received = 0

        def report(delta):
            nonlocal received
            received += delta
            with progress_lock:
                # A retried part overwrites bytes already counted, rather than adding new coverage.
                covered[index] = max(covered[index], min(received, ranges[index][1] - ranges[index][0] + 1))
                if progress_callback is not None:
                    progress_callback(sum(covered), info.archive_size)
        return report

    if progress_callback is not None:
        progress_callback(completed_bytes, info.archive_size)
    last_error = None
    source = None
    for workers in _worker_ladder(len(ranges)):
        pending = [(index, *ranges[index]) for index in range(len(ranges)) if index not in completed]
        if not pending:
            return
        if cancel_event is not None and cancel_event.is_set():
            raise UpdateError("下载已取消")
        if source is None:
            source = _DownloadSource(info, cancel_event, timeout)
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(workers, len(pending)), thread_name_prefix="update-part") as pool:
            future_to_index = {
                pool.submit(_download_range, source.get_url(), start, end,
                            part_path, cancel_event, timeout, part_progress(index), source=source): index
                for index, start, end in pending
            }
            for future in concurrent.futures.as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    future.result()
                except UpdateError as exc:
                    last_error = exc
                else:
                    completed.add(index)
                    _save_resume_state(state_path, info, completed)
        if cancel_event is not None and cancel_event.is_set():
            raise UpdateError("下载已取消")
        if len(completed) == len(ranges):
            return
    raise last_error or UpdateError("下载未完成")


def download_update(info, cancel_event=None, timeout=60, progress_callback=None):
    """多路分片优先、单片断点重试、失败自动降级；完整校验后才原子交付。"""
    root = Path(UPDATE_STAGING_DIR)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    final_path = root / ("Skyrocket-%s.zip" % info.version)
    if _file_matches(final_path, info.archive_size, info.archive_sha256):
        if progress_callback is not None:
            progress_callback(info.archive_size, info.archive_size)
        return str(final_path)
    part_path = final_path.with_suffix(".zip.part")
    state_path = final_path.with_suffix(".zip.resume.json")
    local_cancel = cancel_event or threading.Event()
    try:
        _download_adaptive(info, part_path, state_path, local_cancel, timeout,
                           progress_callback)
        if not _file_matches(part_path, info.archive_size, info.archive_sha256):
            part_path.unlink(missing_ok=True)
            state_path.unlink(missing_ok=True)
            raise UpdateError("下载文件校验失败")
        os.replace(part_path, final_path)
        state_path.unlink(missing_ok=True)
        return str(final_path)
    except UpdateError:
        raise


def _parse_install_result(raw):
    """update-result.json 的公共解析与校验（consume 与 peek 共用一份口径）。"""
    if len(raw) > 2048:
        return None
    data = json.loads(raw.decode("utf-8"))
    if (not isinstance(data, dict) or data.get("status") not in {
            "installed", "rolled_back", "rollback_failed", "failed"}
            or not isinstance(data.get("message"), str)):
        return None
    return {"status": data["status"], "message": data["message"]}


def peek_install_result():
    """只读版 consume_install_result：读结果但**绝不删除**。

    为什么必须单独存在（S38/S48 诊断）：consume 在 finally 里 unlink，诊断导出
    若复用它，会把用户下次启动本该看到的那次"装好了/装失败"提示抢掉 ——
    助手三态（installed/rolled_back/rollback_failed/failed）此前因此在诊断包里
    一次都拿不到。判据与 consume 完全一致（同一份解析函数），差别只有删不删。
    """
    path = Path(UPDATE_STAGING_DIR) / "update-result.json"
    try:
        raw = path.read_bytes()
        return _parse_install_result(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None




def residue_reason(name, mtime_age_sec, installed_version, grace_sec=STAGING_PRUNE_GRACE_SEC):
    """这个文件名是不是**可以挪走**的更新残留；不是就返回 None。

    只认这五种有明确来源的形状，认不出的一个都不碰：
      · `*.zip.part`          现行分片下载的中转件
      · `*.part-<pid>-<ns>`   老命名的分片件（本机实测躺在 updates/ 里的那一份）
      · `*.zip.resume.json`   断点续传状态
      · `*.json.tmp-<pid>-<ns>`  写状态时的临时件
      · `Skyrocket-<旧版>.zip` / `StockFloatTargetPrice-*.zip`
                            **版本段不等于当前装机版本**的完整包（当前那一份留着，
                            那是"装失败还能手动重装"的唯一凭据）
    `update_helper-*.sh` 与 `update-result.json` **不在名单里**：前者可能正被 /bin/sh
    逐行读取，后者是用户下次启动该看到的那一次提示。
    每条都还要过 `grace_sec` 这一关 —— 时间不够的一律当作"正在写"。
    """
    if mtime_age_sec < grace_sec:
        return None
    if name.endswith(".zip.part"):
        return "part"
    if ".part-" in name:
        return "legacy-part"
    if name.endswith(".zip.resume.json"):
        return "resume"
    if ".json.tmp-" in name:
        return "state-tmp"
    if name.endswith(".zip"):
        stem = name[:-4]
        if stem.startswith("Skyrocket-") and stem.endswith(installed_version):
            return None      # 当前版本的包：留着
        if stem.startswith("Skyrocket-") or stem.startswith("StockFloatTargetPrice-"):
            return "old-zip"
    return None


def prune_staging_residue(installed_version=None, grace_sec=STAGING_PRUNE_GRACE_SEC,
                          dest_root=None):
    """把 updates/ 里没人认领的残留**挪走**（一律 mv，一个文件都不删）。

    目的地用系统临时目录下的一个专用文件夹，与 `updater/update_helper.sh` 处理回退包
    同法：同卷 rename 瞬间完成，剩下的交给 macOS 自己回收。刻意不直接删 —— 这一族
    文件里唯一"可能还有用"的是旧版本完整包，万一用户要手动回滚，挪走还来得及找回。

    返回一份可外发的报告（进诊断包 `storage.json.updates.prune`）：挪走了哪些、
    多少字节、跳过了哪些、错在哪。**只写日志不算告知**（AGENTS §5.2 那条口径：
    日志会被 5MB×3 轮转挤掉），所以末态同时留在 `staging_prune_report()` 里。
    """
    version = str(installed_version or APP_VERSION)
    report: dict = {"moved": [], "moved_bytes": 0, "held_back": [],
                    "errors": [], "at": time.time()}
    dest = Path(dest_root or (os.path.join(tempfile.gettempdir(),
                                           "Skyrocket-更新残留回收")))
    try:
        root = Path(UPDATE_STAGING_DIR)
        if not root.is_dir():
            return _remember_prune(report)
        dest.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for entry in sorted(root.iterdir()):
            try:
                if entry.is_symlink():
                    continue
                age = now - entry.stat().st_mtime
                if entry.is_dir():
                    if not re.fullmatch(r"unpack\.[A-Za-z0-9]{6}", entry.name):
                        continue
                    if age < grace_sec or _unpack_owner_alive(entry):
                        report["held_back"].append({"name": entry.name, "why": "active-or-recent-unpack"})
                        continue
                    reason = "orphan-unpack"
                elif entry.is_file():
                    reason = residue_reason(entry.name, age, version, grace_sec)
                else:
                    continue
                if reason is None:
                    # 名字像残留却没被挪走，只有两种可能：年龄不够（可能正在写）、
                    # 或它就是当前版本那一份包。这两种都要**报出来**而不是默默消失 ——
                    # "为什么还占着 80MB"是这个问题真正的答案。
                    if _looks_like_residue_name(entry.name):
                        report["held_back"].append({
                            "name": entry.name,
                            "why": ("current-version" if entry.name.endswith(".zip")
                                    and entry.name[:-4].endswith(version)
                                    else "within-grace")})
                    continue
                target = dest / entry.name
                if target.exists():
                    target = dest / ("%s.%d" % (entry.name, int(now)))
                try:
                    os.replace(entry, target)      # 同卷 rename，瞬间完成
                except OSError:
                    shutil.move(str(entry), str(target))   # 跨卷时退化成搬移
                report["moved"].append({"name": entry.name, "why": reason,
                                        "size": _residue_bytes(target)})
                report["moved_bytes"] += report["moved"][-1]["size"]
            except Exception as exc:
                report["errors"].append("%s: %r" % (entry.name, exc))
    except Exception as exc:
        report["errors"].append("staging: %r" % (exc,))
    return _remember_prune(report)


def _unpack_owner_alive(path):
    try:
        with path.with_name(path.name + ".owner").open("r", encoding="ascii") as stream:
            text = stream.read(32).strip()
        if not text.isdigit() or int(text) <= 1:
            return False
        os.kill(int(text), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


def _residue_bytes(path):
    if path.is_file():
        return path.stat().st_size
    total = 0
    for entry in path.rglob("*"):
        if not entry.is_symlink() and entry.is_file():
            total += entry.stat().st_size
    return total


_RESIDUE_HINTS = (".zip.part", ".part-", ".resume.json", ".json.tmp-", ".zip")


def _looks_like_residue_name(name):
    return any(h in name for h in _RESIDUE_HINTS)


_LAST_PRUNE: dict = {}


def _remember_prune(report):
    global _LAST_PRUNE
    _LAST_PRUNE = {"at": report["at"], "moved": len(report["moved"]),
                   "moved_bytes": report["moved_bytes"],
                   "held_back": list(report["held_back"])[:8],
                   "errors": list(report["errors"])[:8]}
    return report


def staging_prune_report():
    """最近一次 staging 清理的末态；这个进程还没扫过就是 {}。"""
    return dict(_LAST_PRUNE)


def update_storage_inventory(apps_dir="/Applications"):
    """诊断导出用：更新通道的落盘现场。只统计名字与字节数，不读内容。

    两块，都对应台账上真实存在过的盲区：
      · staging 目录残留 —— `UPDATE_STAGING_DIR` 此前不在 storage.json 的清单里，
        下载中断留下的半包没有任何一项能看见；
      · /Applications 里的隐藏回退包（S38）—— update_helper.sh 把旧版 mv 成
        `.<名>.rollback-<epoch>`，Finder 看不见、用户清不掉，一份约 100MB。
    `apps_dir` 只是参数化：测试把 /Applications 换成夹具目录，判据落在目录树上。
    任何一步失败如实记进 errors，不假装为空 —— "读不到"和"没有"不许同形。
    """
    out: dict = {"staging_files": [], "staging_bytes": 0,
                 "rollback_apps": [], "rollback_bytes": 0, "errors": []}
    try:
        root = Path(UPDATE_STAGING_DIR)
        if root.is_dir():
            for p in sorted(root.iterdir()):
                try:
                    if p.is_file():
                        size = p.stat().st_size
                        out["staging_files"].append({"name": p.name, "size": size})
                        out["staging_bytes"] += size
                except OSError as exc:
                    out["errors"].append("staging/%s: %r" % (p.name, exc))
    except Exception as exc:
        out["errors"].append("staging: %r" % (exc,))
    try:
        apps = Path(apps_dir)
        if apps.is_dir():
            for p in sorted(apps.iterdir()):
                name = p.name
                if not (name.startswith(".") and ".rollback-" in name):
                    continue
                try:
                    total = 0
                    for f in p.rglob("*"):
                        try:
                            if f.is_file():
                                total += f.stat().st_size
                        except OSError:
                            continue
                    out["rollback_apps"].append({"name": name, "size": total})
                    out["rollback_bytes"] += total
                except OSError as exc:
                    out["errors"].append("rollback/%s: %r" % (name, exc))
    except Exception as exc:
        out["errors"].append("applications: %r" % (exc,))
    return out


def consume_install_result():
    """读取并移除上一轮安装结果；结果只在下次成功启动时提示一次。"""
    path = Path(UPDATE_STAGING_DIR) / "update-result.json"
    try:
        raw = path.read_bytes()
        result = _parse_install_result(raw)
        return result
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def bundled_helper_path():
    """随包 OTA 助手 `update_helper.sh` 在**当前运行形态**下的路径（只给路径，不校验存在）。

    两种形态分开取，取错就等于"助手找不到"：
        冻结产物 → sys._MEIPASS 下的 updater/（PyInstaller 的 datas 落点，运行时解包目录）
        源码运行 → 本文件同级的 updater/（仓库里那份）
    为什么要把助手**拷到 App 外**再执行见 launch_installer 的说明：助手要在运行中替换
    它自己所属的那个 bundle，留在包内会边跑边被换掉。
    """
    base = getattr(__import__("sys"), "_MEIPASS", None)
    if base:
        return os.path.join(base, "updater", "update_helper.sh")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "updater", "update_helper.sh")


def installed_bundle_path():
    """从当前可执行文件反推所在 .app 的绝对路径；推不出 bundle 时返回 None。

    取的是 `sys.executable` 的 realpath 再按 ".app/Contents/MacOS/" 切 —— 用 realpath 是因为
    从替身/符号链接启动时原路径不指向真正的 bundle；名字里那段就是打包形状（可执行文件名
    由 Info.plist 的 CFBundleExecutable 决定，所以这里不写死任何一代产品名）。
    返回 None 表示"这次不是从 .app 里跑的"（源码运行、或产物形状不对），
    调用方必须当成"不能自更新"处理，而不是拿 None 去拼路径。
    """
    executable = os.path.realpath(__import__("sys").executable)
    marker = ".app/Contents/MacOS/"
    if marker not in executable:
        return None
    return executable.split(marker, 1)[0] + ".app"


def launch_installer(archive_path, expected_sha256, expected_version, target_app=None):
    """把助手拷到 App 外再启动；主程序随后调用既有 quit() 优雅退出。"""
    target_app = target_app or installed_bundle_path()
    if not target_app or not os.path.isdir(target_app):
        raise UpdateError("仅支持从“应用程序”中的正式应用更新")
    helper = bundled_helper_path()
    if not os.path.isfile(helper):
        raise UpdateError("更新助手不可用")
    root = Path(UPDATE_STAGING_DIR)
    root.mkdir(parents=True, exist_ok=True)
    copied = root / ("update_helper-%d.sh" % time.time_ns())
    shutil.copy2(helper, copied)
    os.chmod(copied, 0o700)
    result = root / "update-result.json"
    parse_version(expected_version)
    if bool(UPDATE_SIGNING_TEAM_ID) != bool(UPDATE_SIGNING_CERT_SHA1):
        raise UpdateError("更新签名身份配置不完整")
    command = ["/bin/sh", str(copied), "--pid", str(os.getpid()), "--archive", archive_path,
               "--sha256", expected_sha256, "--version", expected_version,
               "--target", target_app, "--result", str(result),
               "--team-id", UPDATE_SIGNING_TEAM_ID,
               "--cert-sha1", UPDATE_SIGNING_CERT_SHA1]
    try:
        subprocess.Popen(command, close_fds=True, start_new_session=True)
    except OSError as exc:
        copied.unlink(missing_ok=True)
        raise UpdateError("无法启动更新助手") from exc
    return command
