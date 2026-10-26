"""Private GitHub Release storage. Imported only by receiver/admin tools."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener

from sf_feedback import FeedbackProtocolError
from sf_feedback_capture import FEEDBACK_REPOSITORY, read_upload_archive, validate_capture_context, validate_upload_receipt
from sf_feedback_transport import MAX_UPLOAD_BYTES, NoRedirect

REPOSITORY = FEEDBACK_REPOSITORY
INBOX_TAG = "feedback-inbox-v1"
MAX_RELEASE_ASSETS = 1000
KEY_PATTERN = re.compile(r"[0-9a-f]{32}")


class GitHubRequestError(FeedbackProtocolError):
    def __init__(self, status):
        self.status = status
        super().__init__("GitHub HTTP %d" % status)


class GitHubAPI:
    def __init__(self, token: str, repository=REPOSITORY):
        if repository != REPOSITORY:
            raise FeedbackProtocolError("反馈只允许写入批准的私有仓库")
        if not token or any(c.isspace() for c in token):
            raise FeedbackProtocolError("GitHub 管理凭据不可用")
        self._token = token
        self.repository = repository

    def request(self, path, *, data=None, method="GET", binary=False, upload=False,
                limit=MAX_UPLOAD_BYTES):
        host = "uploads.github.com" if upload else "api.github.com"
        url = "https://%s/repos/%s/%s" % (host, self.repository, path)
        if not path:
            url = url.rstrip("/")
        body = data if isinstance(data, bytes) else (
            json.dumps(data).encode() if data is not None else None)
        headers = {"Authorization": "Bearer " + self._token,
                   "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Skyrocket-Feedback/1",
                   "Content-Type": "application/octet-stream" if upload else "application/json"}
        request = Request(url, data=body, headers=headers, method=method)
        opener = build_opener(NoRedirect())
        timeout = 120 if binary and limit > 256 * 1024 else 30
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as exc:
            if not binary or exc.code not in (301, 302, 307):
                raise GitHubRequestError(exc.code) from None
            location = exc.headers.get("Location", "")
            parts = urlsplit(location)
            if (parts.scheme != "https" or parts.username or parts.password
                    or parts.hostname != "release-assets.githubusercontent.com"):
                raise FeedbackProtocolError("拒绝未知资产下载重定向") from None
            # Signed asset URLs never receive the administrator Authorization header.
            response = opener.open(Request(location, headers={"User-Agent": headers["User-Agent"]}), timeout=timeout)
        with response:
            payload = response.read(limit + 1)
        if len(payload) > limit:
            raise FeedbackProtocolError("GitHub 响应超过大小限制")
        return payload if binary else (json.loads(payload) if payload else None)

    def assert_private(self):
        info = self.request("")
        if info.get("private") is not True or info.get("full_name") != self.repository:
            raise FeedbackProtocolError("接收仓库不再是批准的私有仓库，停止传输")

    def inbox(self):
        self.assert_private()
        for page in range(1, 101):
            rows = self.request("releases?per_page=100&page=%d" % page, limit=2 * 1024 * 1024)
            for row in rows:
                if row.get("tag_name") == INBOX_TAG:
                    return row
            if len(rows) < 100:
                break
        raise FeedbackProtocolError("私有反馈收件箱尚未初始化")

    def assets(self, release_id):
        result = []
        for page in range(1, 101):
            rows = self.request("releases/%d/assets?per_page=100&page=%d" % (release_id, page),
                                limit=2 * 1024 * 1024)
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise FeedbackProtocolError("反馈收件箱资产数量超过分页上限")

    def download(self, asset_id, *, limit=MAX_UPLOAD_BYTES):
        return self.request("releases/assets/%d" % asset_id, binary=True, limit=limit)

    def upload(self, release_id, name, data):
        return self.request("releases/%d/assets?name=%s" % (release_id, quote(name, safe="")),
                            method="POST", data=data, upload=True)


def validate_submission(envelope: dict, archive: bytes):
    if (not isinstance(envelope, dict) or envelope.get("schema_version") != 1
            or not KEY_PATTERN.fullmatch(str(envelope.get("idempotency_key", "")))):
        raise FeedbackProtocolError("invalid feedback identity")
    if (len(archive) > MAX_UPLOAD_BYTES or len(archive) != envelope.get("size_bytes")
            or hashlib.sha256(archive).hexdigest() != envelope.get("archive_sha256")):
        raise FeedbackProtocolError("反馈包与信封不一致")
    with tempfile.TemporaryDirectory(prefix="feedback-validate-") as directory:
        path = Path(directory) / "incoming.zip"
        path.write_bytes(archive)
        packet = read_upload_archive(path, envelope)
        validate_capture_context(packet["manifest"], packet["state"])
        return packet["manifest"]


class GitHubInbox:
    """Serialize publication; a receipt asset is the commit marker for admins."""
    def __init__(self, api, *, max_cases=1000):
        self.api = api
        self.max_cases = max_cases
        self.lock = threading.Lock()

    def accept(self, envelope, archive):
        validate_submission(envelope, archive)
        key = envelope["idempotency_key"]
        prefix = "feedback-" + key
        with self.lock:
            release = self.api.inbox()
            assets = {a["name"]: a for a in self.api.assets(release["id"])}
            archive_asset = assets.get(prefix + ".zip")
            receipt_asset = assets.get(prefix + ".receipt.json")
            if archive_asset is None and receipt_asset is not None:
                raise FeedbackProtocolError("案件对象缺失，不能返回已收件")
            needed_assets = int(archive_asset is None) + int(receipt_asset is None)
            if len(assets) + needed_assets > MAX_RELEASE_ASSETS:
                raise FeedbackProtocolError("反馈收件箱附件名额不足，暂不写入新资料")
            if archive_asset is None:
                if sum(a.endswith(".zip") for a in assets) >= self.max_cases:
                    raise FeedbackProtocolError("反馈收件箱已达到容量上限")
                archive_asset = self.api.upload(release["id"], prefix + ".zip", archive)
            stored = self.api.download(archive_asset["id"])
            if stored != archive:
                raise FeedbackProtocolError("同一反馈标识绑定了不同内容，或远端对象损坏")
            if receipt_asset is None:
                now = datetime.now(timezone.utc)
                receipt = {
                    "case_id": prefix, "archive_sha256": envelope["archive_sha256"],
                    "size_bytes": len(archive), "received_at": now.isoformat(),
                    "storage_status": "stored", "validation_status": "validated",
                    # No deletion/retention promise before an actual policy is deployed.
                    "retention_until": None, "retention_policy": "manual_deletion",
                    "repository": REPOSITORY, "asset_id": archive_asset["id"],
                    "consent_policy_version": envelope["consent_policy_version"],
                }
                if envelope["diagnostic_payload_version"] >= 2:
                    receipt["validation_scope"] = "container_sha256_and_member_catalog"
                receipt_asset = self.api.upload(release["id"], prefix + ".receipt.json",
                                                json.dumps(receipt, sort_keys=True).encode())
            receipt = json.loads(self.api.download(receipt_asset["id"], limit=64 * 1024))
            checked = validate_upload_receipt(receipt, envelope)
            if checked.get("asset_id") != archive_asset["id"]:
                raise FeedbackProtocolError("GitHub 回执与案件对象不匹配")
            return checked
