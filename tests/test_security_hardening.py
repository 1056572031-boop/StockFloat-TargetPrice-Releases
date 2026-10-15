"""Regression for URL privacy, private creation and cumulative OTA deadlines."""
import contextlib
from email.message import Message
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import stat
import socket
import tempfile
import threading
import time
from unittest import mock

import sf_config
import sf_credentials
import sf_diag
import sf_http
import sf_logging
import sf_privacy
import sf_update
from tests._base import SFCase
from tests.test_quality_credentials import MemoryKeychain
from tools import dependency_audit

# No public API exposes a range retry or config commit independently; observe files.
write_json = sf_config._atomic_write_json
download_range = sf_update._download_range


class URLPrivacyTests(SFCase):
    def test_all_custom_url_credential_locations_are_sealed(self):
        for suffix in ("/CANARY/q?code=%s", "/q?opaque=CANARY", "/q#CANARY"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as root:
                path = str(Path(root) / "config.json")
                url = "https://example.invalid" + suffix
                payload = {"api_urls": {"quote": url}, "alerts": ["keep"]}
                write_json(path, payload)
                write_json(path + ".bak", payload)
                vault = MemoryKeychain()
                resolved = sf_credentials.resolve_config_urls(path, payload, keychain=vault)
                self.assertEqual(resolved["quote"], url)
                for name in (path, path + ".bak"):
                    text = Path(name).read_text()
                    self.assertNotIn("CANARY", text)
                    self.assertIn("keychain://", text)

    def test_backup_is_migrated_when_primary_already_contains_a_reference(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "config.json")
            vault = MemoryKeychain()
            url = "https://example.invalid/CANARY"
            sealed = {"api_urls": {"quote": sf_credentials.seal_url(url, vault)}}
            write_json(path, sealed)
            write_json(path + ".bak", {"api_urls": {"quote": url}})
            sf_credentials.resolve_config_urls(path, sealed, keychain=vault)
            self.assertNotIn("CANARY", Path(path + ".bak").read_text())

    def test_migration_keeps_fields_saved_after_the_callers_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "config.json")
            old = {"api_urls": {"quote": "https://example.invalid/CANARY"}, "alerts": ["old"]}
            write_json(path, dict(old, alerts=["new"]))
            sf_credentials.resolve_config_urls(path, old, keychain=MemoryKeychain())
            self.assertEqual(json.loads(Path(path).read_text())["alerts"], ["new"])

    def test_future_backup_and_locked_keychain_keep_the_original_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "config.json")
            payload = {"api_urls": {"quote": "https://example.invalid/CANARY"}}
            future = dict(payload, version=sf_config.CONFIG_SCHEMA_VERSION + 1)
            write_json(path, payload)
            write_json(path + ".bak", future)
            original = [Path(name).read_bytes() for name in (path, path + ".bak")]
            with mock.patch.object(sf_credentials, "log_line") as logged:
                resolved = sf_credentials.resolve_config_urls(path, payload, keychain=MemoryKeychain(fail=True))
            self.assertEqual(resolved, payload["api_urls"])
            self.assertEqual([Path(name).read_bytes() for name in (path, path + ".bak")], original)
            self.assertTrue(logged.called)
            self.assertNotIn("CANARY", str(logged.call_args_list))

    def test_migration_does_not_rewrite_a_newer_schema_saved_during_keychain_io(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "config.json")
            payload = {"api_urls": {"quote": "https://example.invalid/CANARY"}}
            future = dict(payload, version=sf_config.CONFIG_SCHEMA_VERSION + 1)
            vault = MemoryKeychain()
            put = vault.put

            def store(account, value):
                write_json(path, future)
                put(account, value)

            write_json(path, payload)
            with mock.patch.object(vault, "put", side_effect=store):
                sf_credentials.resolve_config_urls(path, payload, keychain=vault)
            self.assertEqual(json.loads(Path(path).read_text()), future)

    def test_logs_and_exports_hide_unknown_url_fields_and_paths(self):
        for url in ("https://u:p@example.invalid/CANARY/q?opaque=CANARY#CANARY",
                    "https://example.invalid/q?unusual=CANARY", "https://example.invalid/#CANARY"):
            for scrub in (sf_privacy.redact_url, sf_privacy.scrub_secrets,
                          sf_config.redact_log_text, lambda value: sf_diag.scrub_text(value, ())):
                with self.subTest(url=url, scrub=scrub):
                    text = scrub(url)
                    self.assertNotIn("CANARY", text)
                    self.assertIn("example.invalid", text)
            self.assertNotIn("CANARY", json.dumps(sf_config.redact_config({"api_urls": {"quote": url}})))

    def test_repeated_scrubbing_does_not_grow_the_redaction(self):
        for marker in ("<REDACTED>", "***REDACTED***"):
            first = sf_privacy.redact_url("https://example.invalid/CANARY?code=%s", marker)
            self.assertEqual(sf_privacy.redact_url(first, marker), first)

    def test_both_log_destinations_receive_the_scrubbed_path(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(sf_logging, "LOG_PATH", str(Path(root) / "log")), \
                mock.patch.object(sf_logging, "_nslog_line") as native:
            sf_logging.log_line("failure https://example.invalid/CANARY", "WARNING")
            self.assertNotIn("CANARY", (Path(root) / "log").read_text())
            self.assertNotIn("CANARY", native.call_args.args[0])


class PrivateConfigTests(SFCase):
    def test_config_is_private_during_the_write_even_with_a_permissive_umask(self):
        original = json.dump
        modes = []

        def observe(value, stream, *args, **kwargs):
            modes.append(stat.S_IMODE(os.fstat(stream.fileno()).st_mode))
            return original(value, stream, *args, **kwargs)

        mask = os.umask(0)
        try:
            with tempfile.TemporaryDirectory() as root, mock.patch.object(sf_config.json, "dump", observe):
                path = Path(root) / "config.json"
                write_json(str(path), {"private": "CANARY"})
                self.assertEqual(modes, [0o600])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(json.loads(path.read_text()), {"private": "CANARY"})
        finally:
            os.umask(mask)

    def test_failed_write_preserves_old_config_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            write_json(str(path), {"alerts": ["keep"]})
            original = path.read_bytes()
            with mock.patch.object(sf_config.json, "dump", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write_json(str(path), {"alerts": []})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(sorted(p.name for p in Path(root).iterdir()), ["config.json"])

    def test_destination_symlink_is_replaced_without_writing_its_target(self):
        with tempfile.TemporaryDirectory() as root:
            victim = Path(root) / "victim"
            victim.write_text("KEEP")
            path = Path(root) / "config.json"
            path.symlink_to(victim)
            write_json(str(path), {"ok": True})
            self.assertEqual(victim.read_text(), "KEEP")
            self.assertFalse(path.is_symlink())


class ChunkResponse:
    def __init__(self, clock, *, cancel=None, step=0.4):
        self.clock, self.cancel, self.step = clock, cancel, step
        self.status = 206
        self.headers = {"Content-Range": "bytes 0-3/4"}
        self.closed = False
        self.count = 0

    def geturl(self):
        return "https://github.com/example/archive.zip"

    def read1(self, size):
        self.clock[0] += self.step
        self.count += 1
        if self.cancel is not None:
            self.cancel.set()
        return bytes([96 + self.count])[:size]

    def read(self, size):
        raise AssertionError("read(size) can wait for the whole buffer; use read1")

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class UpdateDeadlineTests(SFCase):
    def test_slow_http_headers_share_the_same_socket_deadline(self):
        reader, writer = socket.socketpair()
        reader.settimeout(2)
        stopped = threading.Event()

        def send():
            try:
                for value in b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx":
                    writer.sendall(bytes([value]))
                    if stopped.wait(0.02):
                        break
            except OSError:
                return

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        started = time.monotonic()
        response = sf_http.BudgetHTTPResponse(reader, deadline=started + 0.1)
        try:
            with self.assertRaises(TimeoutError):
                response.begin()
            self.assertLess(time.monotonic() - started, 0.75)
        finally:
            stopped.set()
            response.close()
            reader.close()
            writer.close()
            thread.join(2)

    def test_slow_manifest_read_is_rejected_and_closed(self):
        clock = [0.0]
        response = ChunkResponse(clock, step=2)
        with mock.patch.object(sf_update.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(sf_update, "_open_limited", return_value=response):
            with self.assertRaisesRegex(sf_update.UpdateError, "总时限"):
                sf_update.fetch_manifest(timeout=1)
        self.assertTrue(response.closed)

    def test_range_has_one_deadline_for_all_short_reads(self):
        clock = [0.0]
        response = ChunkResponse(clock)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "archive.part"
            path.write_bytes(b"0000")
            with mock.patch.object(sf_update.time, "monotonic", side_effect=lambda: clock[0]), \
                    mock.patch.object(sf_update, "safe_urlopen", return_value=response) as opened:
                with self.assertRaisesRegex(sf_update.UpdateError, "总时限"):
                    download_range(response.geturl(), 0, 3, path, threading.Event(), 1)
            self.assertEqual(path.read_bytes(), b"ab00", "an expired read must not write its bytes")
            self.assertEqual(opened.call_count, 1)
            self.assertTrue(response.closed)

    def test_cancellation_during_a_read_never_writes_or_retries(self):
        clock = [0.0]
        cancel = threading.Event()
        response = ChunkResponse(clock, cancel=cancel)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "archive.part"
            path.write_bytes(b"KEEP")
            with mock.patch.object(sf_update.time, "monotonic", side_effect=lambda: clock[0]), \
                    mock.patch.object(sf_update, "safe_urlopen", return_value=response) as opened:
                with self.assertRaisesRegex(sf_update.UpdateError, "取消"):
                    download_range(response.geturl(), 0, 3, path, cancel, 5)
            self.assertEqual(path.read_bytes(), b"KEEP")
            self.assertEqual(opened.call_count, 1)

    def test_connection_retries_cannot_reset_the_budget(self):
        clock = [0.0]

        def fail(request, timeout):
            clock[0] += 0.6
            raise OSError("connection reset")

        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(sf_update.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(sf_update, "safe_urlopen", side_effect=fail) as opened:
            with self.assertRaisesRegex(sf_update.UpdateError, "总时限"):
                download_range("https://github.com/example/a", 0, 3,
                               Path(root) / "part", threading.Event(), 1)
            self.assertEqual(opened.call_count, 2)

    def test_cancelled_cached_download_is_not_delivered(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(sf_update, "_file_matches", return_value=True) as cached:
            with self.assertRaisesRegex(sf_update.UpdateError, "取消"):
                sf_update.download_update(mock.Mock(), cancel_event=cancel)
        cached.assert_not_called()

    def test_real_http_body_cannot_keep_a_manifest_alive_by_trickling_bytes(self):
        stopped = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        if stopped.wait(0.02):
                            break
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            started = time.monotonic()
            with mock.patch.object(sf_update, "_open_limited", return_value=response):
                with self.assertRaises(sf_update.UpdateError):
                    sf_update.fetch_manifest(timeout=0.1)
            self.assertLess(time.monotonic() - started, 0.75)
            self.assertTrue(response.closed)
        finally:
            stopped.set()
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_redirect_body_is_closed_without_being_drained(self):
        fp = mock.Mock()
        request = sf_http.RestrictedRequest("https://github.com/a", deadline=time.monotonic() + 5)
        request.timeout = 5
        handler = sf_http.HTTPSRedirectHandler({"github.com"})
        handler.parent = mock.Mock()
        handler.parent.open.return_value = io.BytesIO(b"ok")
        headers = Message()
        headers["Location"] = "https://github.com/b"
        response = handler.http_error_302(request, fp, 302, "Found", headers)
        fp.close.assert_called_once()
        fp.read.assert_not_called()
        self.assertEqual(response.read(), b"ok")


class DependencyAuditTests(SFCase):
    def test_known_vulnerabilities_and_incomplete_results_fail_closed(self):
        packages = [("example", "1.0")]
        self.assertEqual(dependency_audit.findings(packages, {"results": [{}]}), [])
        issues = dependency_audit.findings(packages, {"results": [{"vulns": [{"id": "TEST-1"}]}]})
        self.assertEqual(issues, ["example==1.0: TEST-1"])
        for payload in ({}, {"results": []}, {"results": [{"error": "down"}]},
                        {"results": [{"vulns": [None]}]}, {"results": [{"next_page_token": "more"}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                dependency_audit.findings(packages, payload)
        with self.assertRaises(ValueError):
            dependency_audit.findings([], {"results": []})

    def test_lock_parser_rejects_empty_duplicate_and_unpinned_inputs(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "requirements.lock"
            for text in ("# empty", "example>=1", "example==1\nExample==1\n"):
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaises(ValueError):
                        dependency_audit.locked_packages(path)
            path.write_text("example==1 \\\n    --hash=sha256:fixture\n")
            self.assertEqual(dependency_audit.locked_packages(path), {"example": "1"})

    def test_lock_drift_and_database_failure_are_not_reported_as_success(self):
        with self.assertRaises(ValueError):
            dependency_audit.check_lock({"example": "1"}, {"example": "2"})
        with mock.patch.object(dependency_audit, "locked_packages", return_value={}), \
                mock.patch.object(dependency_audit, "query", side_effect=OSError("unavailable")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dependency_audit.main([]), 2)

    def test_dependency_audit_self_test(self):
        self.assertEqual(dependency_audit.self_test(), 0)
