"""Download reuse, mixed-result retries, URL expiry, and end-to-end integrity."""
import concurrent.futures
import hashlib
import io
import json
import tempfile
import threading
import urllib.error
from pathlib import Path
from unittest import mock

import sf_update
import sf_update_cache
from sf_constants import APP_VERSION
from tests._base import SFCase

_range = sf_update._download_range  # discipline-ok(R5): exercise the transport boundary without contacting the network
_Source = sf_update._DownloadSource  # discipline-ok(R5): inspect the shared expiry refresh contract

ORIGIN = "https://github.com/o/r/releases/download/v/a.zip"
ASSET = "https://release-assets.githubusercontent.com/a.zip?token=first"
FRESH = "https://release-assets.githubusercontent.com/a.zip?token=second"
BODY = bytes(range(160))


def info(digest=None, url=ORIGIN, sequence=None, published_at=None, version=APP_VERSION):
    return sf_update.UpdateInfo(version, "", url, len(BODY),
                                digest or hashlib.sha256(BODY).hexdigest(),
                                "stable", sequence, published_at)


class Response:
    def __init__(self, url=ASSET, payload=b"", headers=None, status=200):
        self.url = url
        self.body = io.BytesIO(payload)
        self.headers = headers or {}
        self.status = status
        self.closed = False

    def geturl(self):
        return self.url

    def read(self, count):
        return self.body.read(count)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class TestDownloadReuse(SFCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory(prefix="sf-download-reuse-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.track_patch(mock.patch.object(sf_update, "UPDATE_STAGING_DIR", str(self.root)))
        self.requests = []
        self.track_patch(mock.patch.object(sf_update, "safe_urlopen", self.transport))

    def transport(self, request, timeout=None):
        self.requests.append(request)
        if request.get_method() == "HEAD":
            return Response(headers={"Content-Length": str(len(BODY))})
        start, end = map(int, request.get_header("Range").removeprefix("bytes=").split("-"))
        return Response(request.full_url, BODY[start:end + 1],
                        {"Content-Range": "bytes %d-%d/%d" % (start, end, len(BODY))}, 206)

    def test_legacy_published_at_never_blocks_or_records_new_protocol_timestamp(self):
        legacy_invalid = info(published_at="not-a-time")
        self.assertEqual(Path(sf_update.download_update(legacy_invalid)).read_bytes(), BODY)
        cache = sf_update_cache.UpdateCache(self.root)
        self.assertIsNone(cache.diagnostics()["latest_published_at"])

        legacy_historical = info(published_at="2999-01-01T00:00:00Z", version="1.4.120")
        self.assertEqual(Path(sf_update.download_update(legacy_historical)).read_bytes(), BODY)
        self.assertIsNone(cache.diagnostics()["latest_published_at"])

        current = info(sequence=11, published_at="2026-11-19T00:00:00Z")
        self.assertEqual(Path(sf_update.download_update(current)).read_bytes(), BODY)
        self.assertEqual(cache.diagnostics()["latest_published_at"],
                         "2026-11-19T00:00:00Z")

    def test_verified_archive_uses_one_resolution_and_direct_ranges(self):
        archive = sf_update.download_update(info())
        self.assertEqual(Path(archive).read_bytes(), BODY)
        heads = [r for r in self.requests if r.get_method() == "HEAD"]
        gets = [r for r in self.requests if r.get_method() == "GET"]
        self.assertEqual(len(heads), 1)
        self.assertEqual(len(gets), 16)
        self.assertEqual({r.full_url for r in gets}, {ASSET})

    def _mixed_pool(self, partial=False):
        submitted = []
        rounds = []

        class Pool:
            def __init__(pool, max_workers, **kwargs):
                rounds.append(max_workers)

            def __enter__(pool):
                return pool

            def __exit__(pool, *args):
                return False

            def submit(pool, fn, url, start, end, path, cancel, timeout, progress, **kwargs):
                submitted.append((len(rounds), start))
                future = concurrent.futures.Future()
                if len(rounds) == 1 and start == 0:
                    if partial:
                        with open(path, "r+b") as out:
                            out.write(BODY[:5])
                        progress(5)
                    future.set_exception(sf_update.UpdateError("one failed part"))
                else:
                    try:
                        fn(url, start, end, path, cancel, timeout, progress, **kwargs)
                        future.set_result(None)
                    except Exception as exc:
                        future.set_exception(exc)
                return future

        self.track_patch(mock.patch.object(sf_update.concurrent.futures, "ThreadPoolExecutor", Pool))
        self.track_patch(mock.patch.object(sf_update.concurrent.futures, "as_completed", lambda futures: iter(futures)))
        return submitted, rounds

    def test_cache_constructor_failure_is_a_controlled_fatal_error(self):
        with mock.patch.object(sf_update, "UpdateCache", side_effect=sf_update_cache.UpdateCacheError("unsafe")):
            with self.assertRaises(sf_update.UpdateFatal):
                sf_update.download_update(info())

    def test_resume_wrapper_saves_and_reads_matching_cache_identity(self):
        update = info()
        cache = sf_update_cache.UpdateCache(self.root)
        path = cache.resume_path(update)
        sf_update._save_resume_state(path, update, {0, 2})  # discipline-ok(R5): legacy wrapper persistence is the behavior under test
        self.assertEqual(sf_update._load_resume_state(path, update), {0, 2})  # discipline-ok(R5): verify wrapper through persisted readback

    def test_resume_wrappers_reject_paths_not_owned_by_cache_identity(self):
        wrong = self.root / "wrong.resume.json"
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            sf_update._load_resume_state(wrong, info())  # discipline-ok(R5): exercise legacy wrapper path safety
        with self.assertRaises(sf_update_cache.UpdateCacheError):
            sf_update._save_resume_state(wrong, info(), {0})  # discipline-ok(R5): exercise legacy wrapper path safety

    def test_cancelled_download_cleans_partial_state_when_cancelled_before_network(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(sf_update, "safe_urlopen") as opened:
            with self.assertRaises(sf_update.UpdateCancelled):
                sf_update.download_update(info(), cancel_event=cancel)
        opened.assert_not_called()
        self.assertEqual(list(self.root.glob("*.part")), [])
        self.assertEqual(list(self.root.glob("*.resume.json")), [])

    def test_adaptive_download_stops_when_cancelled_before_worker_ladder(self):
        cancel = threading.Event()
        cancel.set()
        cache = sf_update_cache.UpdateCache(self.root)
        with self.assertRaises(sf_update.UpdateCancelled):
            sf_update._download_adaptive(info(), cache.part_path(info()),  # discipline-ok(R5): exercise adaptive cancellation boundary
                                         cache.resume_path(info()), cancel, 5)

    def test_download_cancels_after_adaptive_return_before_delivery(self):
        cancel = threading.Event()
        def adaptive(*args, **kwargs):
            cancel.set()
        with mock.patch.object(sf_update, "_download_adaptive", side_effect=adaptive), \
                self.assertRaises(sf_update.UpdateCancelled):
            sf_update.download_update(info(), cancel_event=cancel)

    def test_download_cancels_after_archive_verification_before_replace(self):
        cancel = threading.Event()
        def matches(*args):
            cancel.set()
            return True
        with mock.patch.object(sf_update, "_download_adaptive"), \
                mock.patch.object(sf_update, "_file_matches", side_effect=matches), \
                self.assertRaises(sf_update.UpdateCancelled):
            sf_update.download_update(info(), cancel_event=cancel)

    def test_download_rejects_expired_manifest_before_network(self):
        cache = sf_update_cache.UpdateCache(self.root)
        cache.accept_manifest_timestamp("2026-11-20T00:00:00Z")
        with mock.patch.object(sf_update, "safe_urlopen") as opened:
            with self.assertRaisesRegex(sf_update.UpdateError, "过期"):
                sf_update.download_update(info(sequence=11, published_at="2026-11-19T00:00:00Z"))
        opened.assert_not_called()

    def test_download_rejects_equal_time_different_identity_before_network(self):
        from dataclasses import replace
        accepted = info(sequence=11, published_at="2026-11-20T00:00:00Z")
        conflicting = replace(accepted, archive_sha256="f" * 64)
        cache = sf_update_cache.UpdateCache(self.root)
        with cache.write_lock():
            self.assertTrue(cache.accept_manifest_identity(accepted))
        with mock.patch.object(sf_update, "_download_adaptive") as download:
            with self.assertRaises(sf_update.UpdateError):
                sf_update.download_update(conflicting)
        download.assert_not_called()

    def test_cached_archive_reports_complete_progress_without_network(self):
        archive = sf_update_cache.UpdateCache(self.root).archive_path(info(sequence=11, published_at="2026-11-20T00:00:00Z"))
        archive.write_bytes(BODY)
        seen = []
        result = sf_update.download_update(info(sequence=11, published_at="2026-11-20T00:00:00Z"),
                                           progress_callback=lambda done, total: seen.append((done, total)))
        self.assertEqual(Path(result).read_bytes(), BODY)
        self.assertEqual(seen[-1], (len(BODY), len(BODY)))

    def test_cache_busy_space_and_layout_errors_map_to_update_errors(self):
        with mock.patch.object(sf_update_cache.UpdateCache, "write_lock",
                               side_effect=sf_update_cache.UpdateCacheBusyError("busy")):
            with self.assertRaises(sf_update.UpdateError):
                sf_update.download_update(info())
        with mock.patch.object(sf_update_cache.UpdateCache, "write_lock"), \
                mock.patch.object(sf_update_cache.UpdateCache, "require_space",
                                   side_effect=sf_update_cache.UpdateCacheSpaceError("space")):
            with self.assertRaises(sf_update.UpdateFatal):
                sf_update.download_update(info())
        with mock.patch.object(sf_update_cache.UpdateCache, "write_lock"), \
                mock.patch.object(sf_update_cache.UpdateCache, "invalidate_stale",
                                   side_effect=sf_update_cache.UpdateCacheError("unsafe")):
            with self.assertRaises(sf_update.UpdateFatal):
                sf_update.download_update(info())

    def test_failed_first_future_does_not_discard_other_successes(self):
        submitted, rounds = self._mixed_pool()
        archive = sf_update.download_update(info())
        self.assertEqual(Path(archive).read_bytes(), BODY)
        self.assertEqual(len(submitted), 17)
        self.assertEqual([start for phase, start in submitted if phase == 2], [0])
        self.assertEqual(rounds, [16, 1])
        self.assertEqual(sum(r.get_method() == "HEAD" for r in self.requests), 1)

    def test_retried_partial_bytes_do_not_inflate_progress(self):
        self._mixed_pool(partial=True)
        seen = []
        sf_update.download_update(info(), progress_callback=lambda done, total: seen.append(done))
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], len(BODY))
        self.assertEqual(seen[-2:], [155, 160])
        self.assertTrue(all(done < len(BODY) for done in seen[:-1]))

    def test_resume_with_all_parts_complete_needs_no_resolution(self):
        part = self.root / ("Skyrocket-%s.zip.part" % info().version)
        state = self.root / ("Skyrocket-%s.zip.resume.json" % info().version)
        part.write_bytes(BODY)
        state.write_text(json.dumps({"version": APP_VERSION, "size": len(BODY),
                                   "sha256": info().archive_sha256, "completed": list(range(16))}))
        self.assertEqual(Path(sf_update.download_update(info())).read_bytes(), BODY)
        self.assertEqual(self.requests, [])

    def test_hash_mismatch_is_rejected_after_direct_download(self):
        with self.assertRaises(sf_update.UpdateError):
            sf_update.download_update(info(digest="0" * 64))
        self.assertEqual(list(self.root.glob("*.zip")), [])
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_expired_asset_is_resolved_once_again_for_all_parts(self):
        heads = []

        def transport(request, timeout=None):
            if request.get_method() == "HEAD":
                heads.append(request)
                return Response(ASSET if len(heads) == 1 else FRESH)
            if request.full_url == ASSET:
                raise urllib.error.HTTPError(ASSET, 403, "expired", {}, None)
            return self.transport(request, timeout)

        self.track_patch(mock.patch.object(sf_update, "safe_urlopen", transport))
        archive = sf_update.download_update(info())
        self.assertEqual(Path(archive).read_bytes(), BODY)
        self.assertEqual(len(heads), 2)
        self.assertTrue(all(r.full_url == FRESH for r in self.requests))

    def test_rejected_asset_after_refresh_returns_to_signed_origin(self):
        heads = []

        def transport(request, timeout=None):
            if request.get_method() == "HEAD":
                heads.append(request)
                return Response(ASSET if len(heads) == 1 else FRESH)
            if request.full_url in (ASSET, FRESH):
                raise urllib.error.HTTPError(request.full_url, 403, "rejected", {}, None)
            return self.transport(request, timeout)

        self.track_patch(mock.patch.object(sf_update, "safe_urlopen", transport))
        archive = sf_update.download_update(info())
        self.assertEqual(Path(archive).read_bytes(), BODY)
        self.assertEqual(len(heads), 2, "workers must not keep resolving rejected URLs")
        self.assertEqual({r.full_url for r in self.requests}, {ORIGIN})

    def test_origin_fallback_does_not_weaken_hash_or_redirect_validation(self):
        for unsafe in (False, True):
            def transport(request, timeout=None):
                if request.get_method() == "HEAD":
                    return Response(ASSET)
                if request.full_url == ASSET:
                    raise urllib.error.HTTPError(ASSET, 403, "rejected", {}, None)
                response = self.transport(request, timeout)
                if unsafe:
                    response.url = "https://evil.example/a.zip"
                return response

            with self.subTest(unsafe=unsafe), \
                 mock.patch.object(sf_update, "safe_urlopen", transport), \
                 self.assertRaises(sf_update.UpdateError):
                sf_update.download_update(info(digest="0" * 64))
            self.assertEqual(list(self.root.glob("*.zip")), [])


class TestDownloadResolution(SFCase):
    def test_head_failure_falls_back_without_changing_signed_origin(self):
        for error in (urllib.error.URLError("offline"),
                      urllib.error.HTTPError(ORIGIN, 405, "no HEAD", {}, None)):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(sf_update, "safe_urlopen", side_effect=error):
                self.assertEqual(sf_update.resolve_download_url(info(), None, 5), ORIGIN)

    def test_direct_official_asset_does_not_need_head(self):
        with mock.patch.object(sf_update, "safe_urlopen") as opened:
            self.assertEqual(sf_update.resolve_download_url(info(url=ASSET), None, 5), ASSET)
        opened.assert_not_called()

    def test_unsafe_resolution_is_refused_and_response_closed(self):
        for url in ("https://evil.example/a.zip", "http://github.com/a.zip",
                    "https://user:password@github.com/a.zip", ASSET + "#fragment"):
            response = Response(url)
            with self.subTest(url=url), \
                 mock.patch.object(sf_update, "safe_urlopen", return_value=response), \
                 self.assertRaises(sf_update.UpdateError):
                sf_update.resolve_download_url(info(), None, 5)
            self.assertTrue(response.closed)

    def test_bad_head_lengths_are_rejected(self):
        for length in (str(len(BODY) + 1), "invalid"):
            response = Response(headers={"Content-Length": length})
            with self.subTest(length=length), \
                 mock.patch.object(sf_update, "safe_urlopen", return_value=response), \
                 self.assertRaises(sf_update.UpdateError):
                sf_update.resolve_download_url(info(), None, 5)

    def test_cancel_before_or_during_resolution_stops_download(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(sf_update, "safe_urlopen") as opened, \
             self.assertRaises(sf_update.UpdateError):
            sf_update.resolve_download_url(info(), cancel, 5)
        opened.assert_not_called()
        cancel.clear()

        def opened(request, timeout=None):
            cancel.set()
            return Response()

        with mock.patch.object(sf_update, "safe_urlopen", opened), \
             self.assertRaises(sf_update.UpdateError):
            sf_update.resolve_download_url(info(), cancel, 5)

    def test_shared_refresh_is_bounded_and_reuses_concurrent_refresh(self):
        with mock.patch.object(sf_update, "resolve_download_url", side_effect=[ASSET, FRESH]) as resolved:
            source = _Source(info(), None, 5)
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                refreshed = list(pool.map(source.refresh, [ASSET] * 8))
            self.assertTrue(all(refreshed))
            self.assertEqual(source.get_url(), FRESH)
            self.assertTrue(source.refresh(FRESH))
            self.assertEqual(source.get_url(), ORIGIN)
            self.assertFalse(source.refresh(ORIGIN))
            self.assertEqual(resolved.call_count, 2)

    def _rejected_lines(self, failed_url, reason=None, first_url=ASSET):
        """让共享地址走到「刷新后仍被拒、退回签名源」那一步，返回当时写出的日志行。"""
        lines = []
        self.track_patch(mock.patch.object(
            sf_update, "log_line", lambda msg, level="INFO": lines.append((level, msg))))
        with mock.patch.object(sf_update, "resolve_download_url",
                               side_effect=[first_url, failed_url]):
            source = _Source(info(), None, 5)
            self.assertTrue(source.refresh(first_url))      # 第一次：过期刷新
            self.assertTrue(source.refresh(failed_url, reason=reason))
            self.assertEqual(source.get_url(), ORIGIN)
        return [item for item in lines if "rejected after refresh" in item[1]]

    def test_rejection_log_names_host_and_failure_type_but_no_secret(self):
        secret = "https://release-assets.githubusercontent.com/a.zip?token=SECRET&sig=ABC123"
        error = urllib.error.HTTPError(secret, 403, "rejected", {}, None)
        self.addCleanup(error.close)
        rejected = self._rejected_lines(secret, reason=error)
        self.assertEqual(len(rejected), 1, "只在真正退回签名源的那一次写一行")
        level, message = rejected[0]
        self.assertEqual(level, "WARNING")
        self.assertIn("release-assets.githubusercontent.com", message)
        self.assertIn("HTTPError", message)
        for fragment in ("SECRET", "ABC123", "token=", "sig=", "?", "/a.zip"):
            self.assertNotIn(fragment, message, "日志里不许出现地址的路径、查询串或令牌")

    def test_rejection_log_without_a_reason_does_not_invent_one(self):
        rejected = self._rejected_lines(FRESH)
        self.assertEqual(len(rejected), 1)
        message = rejected[0][1]
        self.assertIn("release-assets.githubusercontent.com", message)
        self.assertNotIn("error=", message, "调用方没给异常对象就不写失败类型")
        self.assertNotIn("token=", message)

    def test_rejection_log_survives_an_unparseable_address(self):
        """主机名解析不了时写 unknown，既不抛错也不把整串地址回退进日志。"""
        for bad in ("https://[bad-host/a.zip?token=SECRET", "not a url?token=SECRET"):
            with self.subTest(url=bad):
                rejected = self._rejected_lines(bad, first_url=ASSET)
                self.assertEqual(len(rejected), 1)
                self.assertIn("host=unknown", rejected[0][1])
                self.assertNotIn("SECRET", rejected[0][1])

    def test_download_chain_passes_the_http_error_into_the_log(self):
        """走完整下载链路：调用方手里的 HTTPError 类型要出现在那一行日志里。"""
        lines = []
        self.track_patch(mock.patch.object(
            sf_update, "log_line", lambda msg, level="INFO": lines.append(msg)))
        heads = []
        origin_body = BODY

        def transport(request, timeout=None):
            if request.get_method() == "HEAD":
                heads.append(request)
                return Response(ASSET if len(heads) == 1 else FRESH)
            if request.full_url in (ASSET, FRESH):
                raise urllib.error.HTTPError(request.full_url, 403, "rejected", {}, None)
            start, end = map(int, request.get_header("Range").removeprefix("bytes=").split("-"))
            return Response(request.full_url, origin_body[start:end + 1],
                            {"Content-Range": "bytes %d-%d/%d" % (start, end, len(origin_body))},
                            206)

        with tempfile.TemporaryDirectory(prefix="sf-download-reject-") as root:
            self.track_patch(mock.patch.object(sf_update, "UPDATE_STAGING_DIR", root))
            self.track_patch(mock.patch.object(sf_update, "safe_urlopen", transport))
            self.assertEqual(Path(sf_update.download_update(info())).read_bytes(), BODY)
        rejected = [line for line in lines if "rejected after refresh" in line]
        self.assertEqual(len(rejected), 1, "多路并发下也只会有一个工作线程完成退回并写日志")
        self.assertIn("host=release-assets.githubusercontent.com", rejected[0])
        self.assertIn("error=HTTPError", rejected[0])
        self.assertNotIn("token=", rejected[0])

    def test_bad_range_redirect_cannot_write_bytes(self):
        response = Response("https://evil.example/a.zip", BODY,
                            {"Content-Range": "bytes 0-159/160"}, 206)
        with tempfile.TemporaryDirectory() as directory:
            part = Path(directory) / "a.part"
            part.write_bytes(b"\x00" * len(BODY))
            with mock.patch.object(sf_update, "safe_urlopen", return_value=response), \
                 self.assertRaises(sf_update.UpdateError):
                _range(ASSET, 0, len(BODY) - 1, part, None, 5)
            self.assertEqual(part.read_bytes(), b"\x00" * len(BODY))
        self.assertTrue(response.closed)
