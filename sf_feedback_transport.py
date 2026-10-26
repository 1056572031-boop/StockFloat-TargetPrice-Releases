"""Explicit HTTPS submissions with persistent uncertainty and bound receipts."""

from __future__ import annotations

import contextlib
import json
import hashlib
import struct
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sf_feedback import FeedbackProtocolError
from sf_feedback_capture import read_upload_archive, validate_capture_context, validate_upload_receipt

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024


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
                                status="response_unknown", error=type(exc).__name__)
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
