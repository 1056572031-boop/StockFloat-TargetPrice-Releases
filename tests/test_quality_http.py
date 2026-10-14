"""Reject redirects before contact, and terminate oversized/slow response reads."""
import io
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
