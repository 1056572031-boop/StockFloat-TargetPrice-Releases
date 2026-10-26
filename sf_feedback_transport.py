"""Explicit HTTPS submissions with persistent uncertainty and bound receipts."""

from __future__ import annotations

import contextlib
import json
import hashlib
import re
import struct
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sf_feedback import FeedbackProtocolError
from sf_feedback_capture import read_upload_archive, validate_capture_context, validate_upload_receipt

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
_REPLY_PEEK = 2048
# 服务端回的错误码/阶段只收这种形状，其余一律丢弃：日志、队列和诊断包里不许出现自由文本。
_TOKEN = re.compile(r"^[a-z][a-z0-9_]{1,47}$")


def server_reply(exc):
    """从 HTTPError 取一次服务端的回复：{"server_code", "server_phase", "retry_after_sec", "body"}。

    响应体只能读一次，所以结果缓存在异常对象上：队列里写失败原因和界面里弹提示都走这里，
    谁先读都不会让另一个拿到空。读不到时 body="unreadable"，不抛异常、不编内容。
    """
    cached = getattr(exc, "_sf_server_reply", None)
    if cached is not None:
        return cached
    reply: dict[str, Any] = {}
    try:
        wait = exc.headers.get("Retry-After") if getattr(exc, "headers", None) is not None else None
        if wait is not None and str(wait).isdigit() and 0 < int(wait) <= 86400:
            reply["retry_after_sec"] = int(wait)
    except Exception:
        reply["body"] = "unreadable"
    try:
        raw = exc.read(_REPLY_PEEK)
        body = json.loads(raw) if raw else {}
        for key, name in (("error", "server_code"), ("phase", "server_phase")):
            value = body.get(key) if isinstance(body, dict) else None
            if isinstance(value, str) and _TOKEN.match(value):
                reply[name] = value
    except Exception:
        reply["body"] = "unreadable"
    try:
        exc._sf_server_reply = reply
    except Exception as cache_exc:
        # 缓存不了：第二个读的人会拿到空回复，但状态码本身不受影响。不静默——留一条痕迹；
        # 本模块要能在没有 sf_logging 的无界面运行时里用，所以导入失败就算了（同 submit_pending）。
        with contextlib.suppress(ImportError):
            from sf_logging import log_swallow
            log_swallow("sf_feedback_transport.server_reply.cache", cache_exc)
    return reply


def error_label(exc):
    """写进队列 last_error 的失败标签：类名，HTTP 失败再带状态码和服务端错误码。

    之前只存类名，"HTTPError" 分不出是 409/429/422/503，事后（那次上传的诊断包早已打完）无处可查。
    """
    from urllib.error import HTTPError
    label = type(exc).__name__
    if isinstance(exc, HTTPError):
        label += ":%d" % int(exc.code)
        code = server_reply(exc).get("server_code")
        if code:
            label += ":" + code
    return label[:96]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint_url(url: str, *, allow_loopback=False) -> str:
    parts = urlsplit(url)
    local = allow_loopback and parts.scheme == "http" and parts.hostname == "127.0.0.1"
    if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
            or (parts.scheme != "https" and not local)):
        raise FeedbackProtocolError("反馈服务未配置有效的 HTTPS 地址")
    return url


def submit_https(url, envelope, archive_path, *, allow_loopback=False):
    url = endpoint_url(url, allow_loopback=allow_loopback)
    packet = read_upload_archive(archive_path, envelope)
    validate_capture_context(packet["manifest"], packet["state"])
    del packet
    archive = Path(archive_path).read_bytes()
    if len(archive) > MAX_UPLOAD_BYTES:
        raise FeedbackProtocolError("反馈包超过上传大小限制")
    if hashlib.sha256(archive).hexdigest() != envelope["archive_sha256"]:
        raise FeedbackProtocolError("读取后的反馈包与预览绑定不一致")
    header = json.dumps(envelope, sort_keys=True).encode()
    if len(header) > MAX_RESPONSE_BYTES:
        raise FeedbackProtocolError("反馈信封过大")
    request = Request(url, data=struct.pack("!I", len(header)) + header + archive,
                      headers={"Content-Type": "application/vnd.skyrocket.feedback-v1",
                               "User-Agent": "Skyrocket-Feedback/1", "Accept": "application/json"}, method="POST")
    timeout = 120 if envelope["diagnostic_payload_version"] >= 2 else 30
    with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        if response.status != 200:
            raise FeedbackProtocolError("反馈服务没有返回已收件回执")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise FeedbackProtocolError("反馈回执过大")
    receipt = validate_upload_receipt(json.loads(payload), envelope)
    return receipt


def submit_pending(outbox, outbox_id, url, *, sender=submit_https):
    row = outbox.get(outbox_id)
    if row["status"] not in ("locally_verified", "response_unknown", "receipt_persisted"):
        raise FeedbackProtocolError("反馈状态不允许提交")
    # Recompute the binding before any network access; corrupted disk state
    # must not be submitted under a previously accepted identity.
    from sf_feedback import sha256_file
    read_upload_archive(row["archive_path"], row["envelope"])
    if sha256_file(row["archive_path"]) != row["archive_sha256"]:
        raise FeedbackProtocolError("本地反馈包校验失败")
    if row["status"] == "receipt_persisted":
        return validate_upload_receipt(row["receipt"], row["envelope"])
    endpoint_url(url)
    claimed = outbox.claim(outbox_id, kind="submit", statuses=(row["status"],),
                           target_status="submitting", lease_seconds=300)
    token, fence = claimed["lease_token"], claimed["lease_fence"]
    try:
        receipt = sender(url, claimed["envelope"], claimed["archive_path"])
        checked = validate_upload_receipt(receipt, claimed["envelope"])
        outbox.persist_claimed_receipt(outbox_id, checked, token=token, fence=fence)
        return checked
    except Exception as exc:
        try:
            outbox.finish_claim(outbox_id, token=token, fence=fence,
                                status="response_unknown", error=error_label(exc))
        except Exception as finish_exc:
            # The original network error is more useful to the caller; finish_claim
            # can only fail because a newer worker already owns the row (normal
            # control flow).  Leave a trace when the logging layer exists, but the
            # GUI-free agent runtime snapshot intentionally has no sf_logging
            # (it drags in PyQt6), so a missing import degrades silently here.
            with contextlib.suppress(ImportError):
                from sf_logging import log_swallow
                log_swallow("sf_feedback_transport.submit_pending.finish_claim", finish_exc)
        raise
