"""Reject redirects before contact, and terminate oversized/slow response reads."""
import http.client
import io
import socket
import threading
import time
import urllib.request
from unittest import mock

import sf_http
from tests._base import SFCase


class HTTPQualityTests(SFCase):
    def test_redirects_cannot_downgrade_or_escape_update_hosts(self):
        handler = sf_http.HTTPSRedirectHandler({"github.com"})
        request = urllib.request.Request("https://github.com/a")
        for destination in ("http://github.com/a", "https://untrusted.invalid/a", "file:///tmp/a"):
            with self.subTest(destination=destination), self.assertRaises(sf_http.ResponsePolicyError):
                handler.redirect_request(request, None, 302, "Found", {}, destination)

    def test_valid_redirect_drops_credentials_when_host_changes(self):
        request = urllib.request.Request("https://a.invalid/q", headers={"Authorization": "Bearer secret", "Cookie": "s=x"})
        redirected = sf_http.HTTPSRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "https://b.invalid/q")
        self.assertFalse(redirected.has_header("Authorization"))
        self.assertFalse(redirected.has_header("Cookie"))

    def test_initial_destination_is_checked_before_opener_exists(self):
        with mock.patch.object(urllib.request, "build_opener") as opened:
            with self.assertRaises(sf_http.ResponsePolicyError):
                sf_http.safe_urlopen("http://example.invalid/q", timeout=1)
        opened.assert_not_called()

    def test_declared_and_actual_size_limits(self):
        response = io.BytesIO(b"123456")
        with self.assertRaises(sf_http.ResponsePolicyError):
            sf_http.read_limited(response, time.monotonic() + 5, max_bytes=5)
        response = io.BytesIO(b"ok")
        response.headers = {"Content-Length": "6"}
        with self.assertRaises(sf_http.ResponsePolicyError):
            sf_http.read_limited(response, time.monotonic() + 5, max_bytes=5)

    def test_response_at_limit_is_read_successfully(self):
        self.assertEqual(sf_http.read_limited(io.BytesIO(b"12345"), time.monotonic() + 5, 5), b"12345")

    def test_total_deadline_applies_to_streamed_chunks(self):
        response = io.BytesIO(b"12345")
        with mock.patch.object(sf_http.time, "monotonic", side_effect=[1, 1, 3]):
            with self.assertRaises(TimeoutError):
                sf_http.read_limited(response, deadline=2)


class TruncatedBodyTests(SFCase):
    """2026-11-02 走查 F-A-05：响应头声明了 Content-Length 而实收更少，必须判失败。

    http.client 的 read1 在对端提前 FIN 时只返回空串、不抛 IncompleteRead，于是旧实现把
    "声明 300 字节、只收到 60 字节"当成一次成功读取，截断的行情文本继续往下解析。
    """

    def _serve_once(self, declared, body):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        def serve():
            conn, _ = listener.accept()
            try:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % declared + body)
            finally:
                conn.close()

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 3)
        client = http.client.HTTPConnection("127.0.0.1", listener.getsockname()[1], timeout=3)
        self.addCleanup(client.close)
        client.request("GET", "/")
        return client.getresponse()

    def test_body_shorter_than_declared_content_length_is_rejected(self):
        response = self._serve_once(300, b"x" * 60)
        with self.assertRaises(sf_http.ResponsePolicyError):
            sf_http.read_limited(response, time.monotonic() + 3)

    def test_body_matching_declared_content_length_is_returned(self):
        response = self._serve_once(60, b"x" * 60)
        self.assertEqual(sf_http.read_limited(response, time.monotonic() + 3), b"x" * 60)

    def test_missing_content_length_is_not_judged(self):
        self.assertEqual(sf_http.read_limited(io.BytesIO(b"abc"), time.monotonic() + 5), b"abc")

    def test_chunked_response_ignores_a_stale_content_length_header(self):
        response = io.BytesIO(b"abc")
        response.headers = {"Content-Length": "100", "Transfer-Encoding": "chunked"}
        self.assertEqual(sf_http.read_limited(response, time.monotonic() + 5), b"abc")

    def test_non_streaming_adapter_is_checked_too(self):
        class Adapter:
            headers = {"Content-Length": "10"}

            def read(self, size):
                return b"abcd"

        with self.assertRaises(sf_http.ResponsePolicyError):
            sf_http.read_limited(Adapter(), time.monotonic() + 5)
