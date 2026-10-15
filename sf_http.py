"""HTTPS-only redirects and bounded response reads without global opener changes."""
import http.client
import io
import ssl
import time
import urllib.parse
import urllib.request
from typing import cast

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ResponsePolicyError(OSError):
    pass


class ResponseDeadlineError(TimeoutError):
    pass


class ResponseCancelledError(OSError):
    pass


def remaining_budget(deadline, cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise ResponseCancelledError("HTTP request cancelled")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ResponseDeadlineError("HTTP total deadline exceeded")
    return remaining


class RestrictedRequest(urllib.request.Request):
    def __init__(self, *args, allowed_hosts=None, deadline=None, cancel_event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.allowed_hosts = allowed_hosts
        self.deadline = deadline
        self.cancel_event = cancel_event


class BudgetReader(io.RawIOBase):
    """Check the same deadline for every socket fill, including HTTP headers."""

    def __init__(self, source, sock, deadline, cancel_event):
        self.source = source
        self._sock = sock
        self.deadline = deadline
        self.cancel_event = cancel_event

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = remaining_budget(self.deadline, self.cancel_event)
        idle = self._sock.gettimeout()
        self._sock.settimeout(min(remaining, idle) if idle is not None else remaining)
        data = self.source.read1(len(buffer))
        remaining_budget(self.deadline, self.cancel_event)
        buffer[:len(data)] = data
        return len(data)

    def close(self):
        try:
            self.source.close()
        finally:
            super().close()


class BudgetHTTPResponse(http.client.HTTPResponse):
    def __init__(self, sock, *args, deadline, cancel_event=None, **kwargs):
        super().__init__(sock, *args, **kwargs)
        self.fp = io.BufferedReader(BudgetReader(self.fp, sock, deadline, cancel_event))


class BudgetHTTPSHandler(urllib.request.HTTPSHandler):
    _context: ssl.SSLContext

    def https_open(self, req):
        deadline = getattr(req, "deadline", None)
        if deadline is None:
            return super().https_open(req)

        class RequestResponse(BudgetHTTPResponse):
            def __init__(self, sock, *args, **kwargs):
                super().__init__(sock, *args, deadline=deadline,
                                 cancel_event=getattr(req, "cancel_event", None), **kwargs)

        def connection(host, **kwargs):
            kwargs["timeout"] = min(kwargs["timeout"],
                                     remaining_budget(deadline, getattr(req, "cancel_event", None)))
            conn = http.client.HTTPSConnection(host, **kwargs)
            conn.response_class = RequestResponse
            return conn

        return self.do_open(connection, req, context=self._context)


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
        redirected = cast(RestrictedRequest | None,
                          super().redirect_request(req, fp, code, msg, headers, newurl))
        if redirected is not None:
            redirected.deadline = getattr(req, "deadline", None)
            redirected.cancel_event = getattr(req, "cancel_event", None)
        if redirected is not None and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
            redirected.remove_header("Cookie")
        return redirected

    def http_error_302(self, req, fp, code, msg, headers):
        fp.close()
        deadline = getattr(req, "deadline", None)
        if deadline is not None:
            req.timeout = min(req.timeout, remaining_budget(deadline, getattr(req, "cancel_event", None)))
        # No connection is pooled here; draining an untrusted redirect body can hang.
        return super().http_error_302(req, io.BytesIO(), code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def safe_urlopen(request, timeout):
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    hosts = getattr(request, "allowed_hosts", None)
    check_https(url, hosts)
    opener = urllib.request.build_opener(HTTPSRedirectHandler(hosts), BudgetHTTPSHandler())
    deadline = getattr(request, "deadline", None)
    cancel_event = getattr(request, "cancel_event", None)
    if deadline is not None:
        timeout = min(timeout, remaining_budget(deadline, cancel_event))
    response = opener.open(request, timeout=timeout)
    if deadline is not None:
        try:
            remaining_budget(deadline, cancel_event)
        except (ResponseDeadlineError, ResponseCancelledError):
            response.close()
            raise
    return response


def read_chunk(response, deadline, size, cancel_event=None):
    remaining = remaining_budget(deadline, cancel_event)
    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is not None:
        sock.settimeout(min(remaining, 1.0 if cancel_event is not None else 4.0))
    reader = response.read1 if callable(getattr(type(response), "read1", None)) else response.read
    chunk = reader(size)
    remaining_budget(deadline, cancel_event)
    if len(chunk) > size:
        raise ResponsePolicyError("HTTP chunk exceeds requested size")
    return chunk


def read_limited(response, deadline, max_bytes=MAX_RESPONSE_BYTES, cancel_event=None):
    remaining_budget(deadline, cancel_event)
    headers = getattr(response, "headers", {})
    declared = headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > max_bytes:
                raise ResponsePolicyError("HTTP response exceeds size limit")
        except (TypeError, ValueError) as exc:
            raise ResponsePolicyError("Invalid HTTP content length") from exc
    if not callable(getattr(type(response), "read1", None)):
        # Non-streaming adapters do not provide HTTPResponse.read1.
        body = read_chunk(response, deadline, max_bytes + 1, cancel_event)
        if len(body) > max_bytes:
            raise ResponsePolicyError("HTTP response exceeds size limit")
        return body
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = read_chunk(response, deadline, min(65536, max_bytes + 1 - total), cancel_event)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise ResponsePolicyError("HTTP response exceeds size limit")
        chunks.append(chunk)
