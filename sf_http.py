"""HTTPS-only redirects and bounded response reads without global opener changes."""
import time
import urllib.parse
import urllib.request

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ResponsePolicyError(OSError):
    pass


class RestrictedRequest(urllib.request.Request):
    def __init__(self, *args, allowed_hosts=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.allowed_hosts = allowed_hosts


def check_https(url, allowed_hosts=None):
    try:
        parts = urllib.parse.urlsplit(url)
        valid = parts.scheme == "https" and bool(parts.hostname)
        if allowed_hosts is not None:
            valid = valid and parts.hostname in allowed_hosts
        if not valid:
            raise ResponsePolicyError("HTTPS destination is not allowed")
    except ValueError as exc:
        raise ResponsePolicyError("Invalid HTTPS destination") from exc


class HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts=None):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_https(newurl, self.allowed_hosts)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
            redirected.remove_header("Cookie")
        return redirected


def safe_urlopen(request, timeout):
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    hosts = getattr(request, "allowed_hosts", None)
    check_https(url, hosts)
    opener = urllib.request.build_opener(HTTPSRedirectHandler(hosts))
    return opener.open(request, timeout=timeout)


def read_limited(response, deadline, max_bytes=MAX_RESPONSE_BYTES):
    if time.monotonic() >= deadline:
        raise TimeoutError("HTTP total deadline exceeded")
    headers = getattr(response, "headers", {})
    declared = headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > max_bytes:
                raise ResponsePolicyError("HTTP response exceeds size limit")
        except (TypeError, ValueError) as exc:
            raise ResponsePolicyError("Invalid HTTP content length") from exc
    # HTTPResponse.read1 performs at most one buffered/raw read, so a slow stream
    # cannot hold one read(size) indefinitely by sending a byte at a time.
    if callable(getattr(type(response), "read1", None)):
        chunks = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HTTP total deadline exceeded")
            raw = getattr(getattr(response, "fp", None), "raw", None)
            sock = getattr(raw, "_sock", None)
            if sock is not None:
                sock.settimeout(min(remaining, 4.0))
            chunk = response.read1(min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ResponsePolicyError("HTTP response exceeds size limit")
            chunks.append(chunk)
        return b"".join(chunks)
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ResponsePolicyError("HTTP response exceeds size limit")
    if time.monotonic() >= deadline:
        raise TimeoutError("HTTP total deadline exceeded")
    return body
