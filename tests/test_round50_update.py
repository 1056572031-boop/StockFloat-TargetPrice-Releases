# -*- coding: utf-8 -*-
"""2026-11-02 第 50 轮走查（H 路：更新/安装）的 Python 侧修复判据。

只在内存与临时目录里跑：暂存目录一律改到临时目录（`UPDATE_STAGING_DIR` 默认指向用户真实目录），
联网出口 `sf_update.safe_urlopen` 换成进程内假 GitHub，验签公钥换成一次性密钥。
shell 侧（updater/update_helper.sh、installer/一键安装.command）的判据在
tests/test_update_helper.py 与 tests/test_installer_script_branches.py。
"""

import base64
import concurrent.futures
import errno
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nacl.signing import SigningKey

import sf_update
from sf_update import UpdateError, UpdateInfo
from sf_update_flow import UpdateFlowController, update_flow_diagnostics
from tests._base import SFCase

URL = "https://github.com/o/r/releases/download/t/Skyrocket-9.9.9-macos.zip"
# 小到足以让 16 路并发各领一个分片，又大到每片不止一个读块。
SIZE = 16 * 70000 + 5
DATA = bytes(range(256)) * (SIZE // 256 + 1)
DATA = DATA[:SIZE]
SHA = hashlib.sha256(DATA).hexdigest()


class _Resp:
    """进程内假响应：read1 供 read_chunk 使用；status/headers/geturl 与真响应同形。"""

    def __init__(self, status, headers, body, on_read=None):
        self.status = status
        self.headers = headers
        self._buf = io.BytesIO(body)
        self._on_read = on_read

    def geturl(self):
        return URL

    def read1(self, size):
        chunk = self._buf.read(size)
        if self._on_read is not None:
            self._on_read()
        return chunk

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeGithub:
    """按 Range 回 206；ignore_range=True 时一律回 200（模拟不支持分段的服务端）。"""

    def __init__(self, ignore_range=False, on_read=None):
        self.ignore_range = ignore_range
        self.on_read = on_read
        self.requests = []
        self._lock = threading.Lock()

    def __call__(self, request, timeout=None):
        if request.get_method() == "HEAD":
            return _Resp(200, {"Content-Length": str(SIZE)}, b"")
        rng = request.get_header("Range")
        with self._lock:
            self.requests.append(rng)
        if rng is None or self.ignore_range:
            return _Resp(200, {"Content-Length": str(SIZE)}, DATA)
        start, end = [int(x) for x in rng.split("=")[1].split("-")]
        body = DATA[start:end + 1]
        return _Resp(206, {"Content-Range": "bytes %d-%d/%d" % (start, end, SIZE),
                           "Content-Length": str(len(body))}, body, on_read=self.on_read)


class _FullDiskFile(io.RawIOBase):
    """包住真文件：seek 照常，write 一律报磁盘已满。"""

    def __init__(self, real, on_write=None):
        self._real = real
        self._on_write = on_write

    def seek(self, *args):
        return self._real.seek(*args)

    def write(self, data):
        if self._on_write is not None:
            self._on_write()
        raise OSError(errno.ENOSPC, "No space left on device")

    def close(self):
        self._real.close()
        super().close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


class _StagedCase(SFCase):
    """暂存目录改到临时目录 + 假 GitHub；每个用例自己决定假服务端的行为。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sf_round50_update_")
        self.addCleanup(self._tmp.cleanup)
        self.staging = Path(self._tmp.name)
        self.track_patch(mock.patch.object(sf_update, "UPDATE_STAGING_DIR", str(self.staging)))
        self.info = UpdateInfo("9.9.9", "1. x", URL, SIZE, SHA)

    def serve(self, **kwargs):
        server = _FakeGithub(**kwargs)
        self.track_patch(mock.patch.object(sf_update, "safe_urlopen", server))
        return server


# ---------------------------------------------------------------------------
# F-H-06：取消下载不是失败
# ---------------------------------------------------------------------------

class _Host:
    """记录型假宿主：未显式给出的 `_xxx` 方法一律记一笔调用，不做事。"""

    def __init__(self):
        self.calls = []
        self._update_pending = None
        self._update_downloading = False
        self._update_checking = False
        self._update_transition_pending = False
        self._update_manual_wait = False
        self._update_check_aborted = False
        self._update_last_check_at = 0.0
        self._update_stale_resets = 0
        self._update_failure_count = 0
        self._update_retry_timer = None
        self._update_ready_archive = None
        self._update_ready_version = None
        self._update_prompt_when_ready = False
        self._update_cancel = None
        self._update_last_result = None
        self._update_last_download = None
        self._quitting = False
        self._threads_stopped = False
        self._update_check_dialog = None

    def __getattr__(self, name):
        if name.startswith("_") and name not in self.__dict__:
            def record(*args, **kwargs):
                self.calls.append((name, args, kwargs))
            return record
        raise AttributeError(name)

    def called(self, name):
        return [c for c in self.calls if c[0] == name]


class _Backend:
    def __init__(self):
        self.checks = []

    def __call__(self, host):
        return self

    def check(self, manual):
        self.checks.append(manual)

    def download(self, update, cancel):
        pass


def _flow():
    logs = []
    flow = UpdateFlowController(
        _Backend(), lambda message, level="INFO": logs.append((level, message)),
        lambda where, exc: logs.append(("SWALLOW", where)))
    return flow, logs


class CancelIsNotAFailureFlow(SFCase):
    def test_missing_update_module_is_reported_as_non_cancel(self):
        recorded = []
        flow = UpdateFlowController(_Backend(), lambda *a, **k: None,
                                    lambda *a: recorded.append(a))
        with mock.patch.dict(sys.modules, {"sf_update": None}):
            self.assertFalse(flow._is_cancel(RuntimeError("failed")))  # discipline-ok(R5): 验证依赖不可用的取消分类。
        self.assertEqual(recorded[0][0], "sf_update_flow.is_cancel")

    UPDATE = UpdateInfo("9.9.9", "1. x", URL, 10, "a" * 64)

    def test_a_cancelled_manual_download_shows_no_failure_popup(self):
        flow, logs = _flow()
        host = _Host()
        host._update_downloading = True  # discipline-ok(R5): 模拟下载中的宿主状态。
        flow.downloaded(host, self.UPDATE, None, sf_update.UpdateCancelled("下载已取消"))
        self.assertEqual(host.called("_show_update_message"), [],
                         "用户自己点了取消，不该再被告知『下载或校验失败』")
        self.assertFalse(host._update_downloading, "取消后下载状态位必须放下")  # discipline-ok(R5): 验证下载状态。
        self.assertTrue(any("cancel" in message.lower() for _level, message in logs),
                        "取消要留一行日志，别静默：%r" % (logs,))

    def test_an_ordinary_download_error_still_shows_the_failure_popup(self):
        flow, _logs = _flow()
        host = _Host()
        flow.downloaded(host, self.UPDATE, None, UpdateError("下载文件校验失败"))
        popups = host.called("_show_update_message")
        self.assertEqual(len(popups), 1)
        self.assertEqual(popups[0][1][0], "更新未完成")


class _RecordRefusingHost(_Host):
    """写『最近一次下载』这个状态位会抛错的宿主：记账失败不许拖垮下载收尾。"""

    @property
    def _update_last_download(self):
        return None

    @_update_last_download.setter
    def _update_last_download(self, value):
        if value is not None:
            raise RuntimeError("state slot refuses writes")


class LastDownloadIsRemembered(SFCase):
    """下载结束的收场方式要留在包里：用户说『更新总不成功』时，开发者得能分清
    是用户自己取消、磁盘写不进去、服务端不支持分段，还是普通网络失败。
    此前这几种只写 debug.log（会被轮转挤掉），包里只有一个检查失败次数。"""

    UPDATE = UpdateInfo("9.9.9", "1. x", URL, 10, "a" * 64)

    def _exported(self, host):
        """走诊断块的公开函数读，不碰宿主的私有状态位。"""
        block = update_flow_diagnostics(host, lambda _key, fn, _default: fn(), lambda: None)
        return block["last_download"]

    def test_nothing_is_reported_before_any_download_ended(self):
        self.assertIsNone(self._exported(_Host()))

    def test_a_deterministic_failure_is_marked_fatal_with_its_class_and_reason(self):
        flow, _logs = _flow()
        host = _Host()
        flow.downloaded(host, self.UPDATE, None, sf_update.UpdateFatal("本地磁盘写入失败: no space"))
        got = self._exported(host)
        self.assertEqual(got["outcome"], "failed")
        self.assertTrue(got["fatal"], "磁盘写满这类确定性失败要和可重试的网络失败分开")
        self.assertEqual(got["error_type"], "UpdateFatal")
        self.assertEqual(got["version"], self.UPDATE.version)
        self.assertIn("磁盘", got["error"])

    def test_an_ordinary_failure_is_failed_but_not_fatal(self):
        flow, _logs = _flow()
        host = _Host()
        flow.downloaded(host, self.UPDATE, None, UpdateError("下载文件校验失败"))
        got = self._exported(host)
        self.assertEqual(got["outcome"], "failed")
        self.assertFalse(got["fatal"])
        self.assertEqual(got["error_type"], "UpdateError")

    def test_a_user_cancel_is_recorded_as_cancelled_not_failed(self):
        flow, _logs = _flow()
        host = _Host()
        flow.downloaded(host, self.UPDATE, None, sf_update.UpdateCancelled("下载已取消"))
        got = self._exported(host)
        self.assertEqual(got["outcome"], "cancelled")
        self.assertFalse(got["fatal"])

    def test_a_finished_download_is_recorded_as_ok_without_an_error_text(self):
        flow, _logs = _flow()
        host = _Host()
        flow.downloaded(host, self.UPDATE, "/tmp/ready.zip", None)
        got = self._exported(host)
        self.assertEqual(got["outcome"], "ok")
        self.assertEqual(got["error"], "")
        self.assertEqual(got["error_type"], "")

    def test_a_background_download_is_told_apart_from_a_clicked_one(self):
        flow, _logs = _flow()
        host = _Host()
        host._update_prompt_when_ready = True  # discipline-ok(R5): 宿主状态位，下载入口没有公开读口。
        flow.downloaded(host, self.UPDATE, None, UpdateError("x"))
        self.assertTrue(self._exported(host)["silent"],
                        "自动检查的后台下载与用户点『更新并重启』后的重下都不弹窗，包里是它们唯一的痕迹")

    def test_a_long_error_text_is_cut_to_a_bounded_length(self):
        flow, _logs = _flow()
        host = _Host()
        flow.downloaded(host, self.UPDATE, None, UpdateError("x" * 5000))
        self.assertLessEqual(len(self._exported(host)["error"]), 200)

    def test_it_is_recorded_even_when_the_app_is_already_quitting(self):
        flow, _logs = _flow()
        host = _Host()
        host._quitting = True  # discipline-ok(R5): 模拟退出流程中的宿主。
        flow.downloaded(host, self.UPDATE, None, UpdateError("late"))
        self.assertEqual(self._exported(host)["outcome"], "failed")

    def test_a_missing_update_module_falls_back_to_plain_failure_and_says_so(self):
        flow, logs = _flow()
        host = _Host()
        with mock.patch.dict(sys.modules, {"sf_update": None}):
            flow.downloaded(host, self.UPDATE, None, sf_update.UpdateFatal("本地磁盘写入失败"))
        got = self._exported(host)
        self.assertEqual((got["outcome"], got["fatal"]), ("failed", False),
                         "取不到 sf_update 时分不出类别，只能退回普通失败，但记录本身必须照写")
        self.assertTrue(any(level == "SWALLOW" and where == "sf_update_flow.last_download.classify"
                            for level, where in logs),
                        "分类退回要留痕，不许静默：%r" % (logs,))

    def test_a_failing_record_does_not_break_the_download_wrap_up(self):
        flow, logs = _flow()
        host = _RecordRefusingHost()
        host._update_downloading = True  # discipline-ok(R5): 模拟下载中的宿主状态。
        flow.downloaded(host, self.UPDATE, None, UpdateError("x"))
        self.assertFalse(host._update_downloading, "记账失败也必须放下下载状态位")  # discipline-ok(R5): 验证下载状态。
        self.assertEqual(len(host.called("_show_update_message")), 1,
                         "记账失败不能吞掉给用户的失败提示")
        self.assertTrue(any(level == "SWALLOW" and "last_download" in where for level, where in logs),
                        "记账失败要留痕，不许静默：%r" % (logs,))


class CancelIsNotAFailureTransport(_StagedCase):
    def test_cancel_during_a_real_download_raises_UpdateCancelled(self):
        cancel = threading.Event()
        self.serve(on_read=cancel.set)
        with self.assertRaises(sf_update.UpdateCancelled):
            sf_update.download_update(self.info, cancel_event=cancel)

    def test_UpdateCancelled_is_still_an_UpdateError(self):
        self.assertTrue(issubclass(sf_update.UpdateCancelled, UpdateError),
                        "调用方的 except UpdateError 不能因为新子类漏接")

    def test_the_budget_check_raises_UpdateCancelled(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(sf_update.UpdateCancelled):
            sf_update._update_remaining(10 ** 12, cancel)  # discipline-ok(R5): 取消点之一，没有公开入口单独触达。

    def test_cancel_during_last_connection_attempt_is_cancelled(self):
        cancel = threading.Event()
        part = self.staging / "part.zip.part"
        part.write_bytes(b"x" * 100)

        def cancel_connection(*args, **kwargs):
            cancel.set()
            raise sf_update.ResponseCancelledError("cancelled")

        with mock.patch.object(sf_update, "safe_urlopen", side_effect=cancel_connection):
            with self.assertRaises(sf_update.UpdateCancelled):
                sf_update._download_range(URL, 0, 99, part, cancel, 5, attempts=1)  # discipline-ok(R5): 验证分片建连取消的错误类型。

    def test_cancel_while_reading_a_part_raises_UpdateCancelled(self):
        """分片内部读到一半被取消：整包下载层会把分片的取消吞成 last_error 再补一次取消，
        所以这一处只有直接驱动分片函数才测得到。"""
        cancel = threading.Event()
        server = self.serve(on_read=cancel.set)
        part = self.staging / "part.zip.part"
        with open(part, "wb") as out:
            out.truncate(SIZE)
        with self.assertRaises(sf_update.UpdateCancelled):
            sf_update._download_range(URL, 0, 99, part, cancel, 5)  # discipline-ok(R5): 分片内的取消点没有公开入口单独触达。
        self.assertEqual(len(server.requests), 1)

    def test_cancel_noticed_after_the_head_probe_raises_UpdateCancelled(self):
        cancel = threading.Event()

        class Headers:
            def get(self, key, default=None):
                if key == "Content-Length":
                    cancel.set()          # HEAD 回包被读到的那一刻用户点了取消
                    return str(SIZE)
                return default

        head = _Resp(200, Headers(), b"")
        self.track_patch(mock.patch.object(sf_update, "safe_urlopen", return_value=head))
        with self.assertRaises(sf_update.UpdateCancelled):
            sf_update.resolve_download_url(self.info, cancel, 5)

    def test_cancel_noticed_between_the_budget_check_and_the_ladder_raises_UpdateCancelled(self):
        cancel = threading.Event()
        cancel.set()
        # 预算检查本身放行（模拟取消发生在它与梯度循环自己的那一行判断之间）。
        self.track_patch(mock.patch.object(sf_update, "_update_remaining", return_value=1.0))
        self.serve()
        with self.assertRaises(sf_update.UpdateCancelled):
            sf_update.download_update(self.info, cancel_event=cancel)

    def test_cancel_noticed_after_verification_raises_UpdateCancelled(self):
        cancel = threading.Event()
        cancel.set()
        self.track_patch(mock.patch.object(sf_update, "_update_remaining", return_value=1.0))
        self.track_patch(mock.patch.object(sf_update, "_download_adaptive"))
        self.track_patch(mock.patch.object(sf_update, "_file_matches", side_effect=[False, True]))
        with self.assertRaises(sf_update.UpdateCancelled):
            sf_update.download_update(self.info, cancel_event=cancel)
        self.assertFalse((self.staging / "Skyrocket-9.9.9.zip").exists(),
                         "取消后不能把包落成最终文件")


# ---------------------------------------------------------------------------
# F-H-07：清单被撤回后不再残留旧的待更新版本
# ---------------------------------------------------------------------------

class WithdrawnManifestClearsPending(SFCase):
    UPDATE = UpdateInfo("9.9.9", "1. x", URL, 10, "a" * 64)

    def test_a_later_latest_answer_clears_the_pending_update_and_the_ready_package(self):
        flow, _logs = _flow()
        host = _Host()
        flow.checked(host, False, self.UPDATE, None)
        self.assertIsNotNone(host._update_pending)  # discipline-ok(R5): 验证待更新状态。
        host._update_ready_archive = "/tmp/never-used.zip"  # discipline-ok(R5): 模拟已就绪包。
        host._update_ready_version = "9.9.9"  # discipline-ok(R5): 模拟已就绪版本。
        host.calls.clear()
        flow.checked(host, False, None, None)               # 线上清单被撤回，检查说「已是最新」
        self.assertIsNone(host._update_pending)  # discipline-ok(R5): 验证待更新状态清除。
        self.assertIsNone(host._update_ready_archive)  # discipline-ok(R5): 验证就绪包状态清除。
        self.assertIsNone(host._update_ready_version)  # discipline-ok(R5): 验证就绪版本清除。
        # 用户随后手动点「检查更新」：必须重新联网查，而不是弹出已被撤回的版本。
        flow.check(host, manual=True)
        self.assertEqual(host.called("_prompt_update"), [], "不能再弹已撤回的 9.9.9")
        self.assertEqual(flow._backend(host).checks, [True], "手动检查要真的发起新检查")  # discipline-ok(R5): 检查后端请求。

    def test_a_failed_check_keeps_the_pending_update(self):
        """检查失败（断网等）不说明清单被撤回，已知的新版不能因此被忘掉。"""
        flow, _logs = _flow()
        host = _Host()
        flow.checked(host, False, self.UPDATE, None)
        flow.checked(host, False, None, UpdateError("无法连接更新服务"))
        self.assertIsNotNone(host._update_pending)  # discipline-ok(R5): 验证待更新状态保留。


# ---------------------------------------------------------------------------
# F-H-08：本地写盘失败与确定性错误不重试
# ---------------------------------------------------------------------------

class LocalWriteFailureIsNotRetried(_StagedCase):
    MAX_REQUESTS = 16     # 一档并发各发一次；阶梯重试会放大到 256。

    def _fill_the_disk(self, on_write=None):
        real_open = open

        def fake_open(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if str(path).endswith(".zip.part") and "+" in mode:
                return _FullDiskFile(handle, on_write)
            return handle

        self.track_patch(mock.patch.object(sf_update, "open", fake_open, create=True))

    def test_a_full_disk_stops_the_ladder_at_once_and_says_so(self):
        server = self.serve()
        self._fill_the_disk()
        with self.assertRaises(UpdateError) as caught:
            sf_update.download_update(self.info)
        self.assertLessEqual(len(server.requests), self.MAX_REQUESTS,
                             "写盘失败不是网络错误，不该放大成 %d 次请求" % len(server.requests))
        self.assertIn("磁盘", str(caught.exception))
        self.assertIsInstance(caught.exception, sf_update.UpdateFatal)
        self.assertNotIsInstance(caught.exception, sf_update.UpdateCancelled)

    def test_open_and_seek_failures_stop_other_workers(self):
        for failure in ("open", "seek"):
            with self.subTest(failure=failure):
                self.serve()
                part = self.staging / "disk.part"
                part.write_bytes(b"x" * 100)
                source = mock.Mock()
                source.deadline = __import__("time").monotonic() + 5
                source.stop = threading.Event()
                source.get_url.return_value = URL
                handle = mock.MagicMock()
                handle.seek.side_effect = OSError("seek failed")
                effect = OSError("open failed") if failure == "open" else None
                with mock.patch.object(sf_update, "open", create=True,
                                       side_effect=effect, return_value=handle):
                    with self.assertRaises(sf_update.UpdateFatal):
                        sf_update._download_range(URL, 0, 99, part, threading.Event(), 5, source=source)  # discipline-ok(R5): 直接验证分片磁盘错误与并发停止。
                self.assertTrue(source.stop.is_set())

    def test_resume_state_with_invalid_json_is_empty(self):
        state = self.staging / "bad.resume.json"
        state.write_text("{", encoding="utf-8")
        self.assertEqual(sf_update._load_resume_state(state, self.info), set())  # discipline-ok(R5): 续传状态解析的失败边界。

    def test_a_server_that_ignores_range_is_not_retried_through_the_ladder(self):
        server = self.serve(ignore_range=True)
        with self.assertRaises(UpdateError) as caught:
            sf_update.download_update(self.info)
        self.assertLessEqual(len(server.requests), self.MAX_REQUESTS,
                             "不支持分段是确定性错误，重试 %d 次没有意义" % len(server.requests))
        self.assertIn("分段", str(caught.exception))
        self.assertIsInstance(caught.exception, sf_update.UpdateFatal)

    def test_fatal_part_stops_other_parts_before_retry(self):
        started = threading.Event()
        fatal_returned = threading.Event()
        requests = []
        lock = threading.Lock()

        def fetch(request, timeout=None):
            if request.get_method() == "HEAD":
                return _Resp(200, {"Content-Length": str(SIZE)}, b"")
            with lock:
                requests.append(request.get_header("Range"))
                count = len(requests)
            if count == 1:
                started.wait(2)
                return _Resp(200, {}, b"")
            started.set()
            fatal_returned.wait(2)
            raise OSError("connection interrupted")

        download_range = sf_update._download_range  # discipline-ok(R5): 控制致命异常到达后的并发顺序。
        def observed_range(*args, **kwargs):
            try:
                return download_range(*args, **kwargs)
            except sf_update.UpdateFatal:
                fatal_returned.set()
                raise

        self.track_patch(mock.patch.object(sf_update, "_download_range", side_effect=observed_range))
        self.track_patch(mock.patch.object(sf_update, "safe_urlopen", fetch))
        with self.assertRaises(sf_update.UpdateFatal):
            sf_update.download_update(self.info)
        self.assertLessEqual(len(requests), self.MAX_REQUESTS)

    def test_cancel_while_fatal_waits_for_other_worker_wins(self):
        fatal_seen = threading.Event()
        release = threading.Event()
        cancel = threading.Event()
        calls = []
        lock = threading.Lock()
        result = []

        def work(*args, **kwargs):
            with lock:
                calls.append(1)
                first = len(calls) == 1
            if first:
                self.assertTrue(fatal_seen.wait(2))
                raise sf_update.UpdateFatal("fatal")
            fatal_seen.set()
            release.wait(3)
            raise sf_update.UpdateError("other worker")

        def run():
            try:
                sf_update.download_update(self.info, cancel_event=cancel)
            except Exception as exc:
                result.append(exc)

        self.serve()
        self.track_patch(mock.patch.object(sf_update, "_download_range", side_effect=work))
        worker = threading.Thread(target=run)
        original_cancel = concurrent.futures.Future.cancel
        caught = threading.Event()
        def observe_cancel(future):
            caught.set()
            return original_cancel(future)
        with mock.patch("concurrent.futures.Future.cancel", new=observe_cancel):
            worker.start()
            try:
                self.assertTrue(fatal_seen.wait(3))
                self.assertTrue(caught.wait(3))
                cancel.set()
                release.set()
                worker.join(5)
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], sf_update.UpdateCancelled)

    def test_a_dropped_connection_is_still_retried_and_resumed(self):
        """反向对照：真的网络读中断仍要续传，不能被『不可重试』误伤。"""
        state = {"dropped": set()}
        base = _FakeGithub()

        def flaky(request, timeout=None):
            response = base(request, timeout)
            rng = request.get_header("Range")
            if rng and rng not in state["dropped"]:
                state["dropped"].add(rng)
                original = response.read1
                sent = [0]

                def read1(size):
                    if sent[0] >= 1000:
                        raise ConnectionResetError("peer reset")
                    chunk = original(min(size, 1000 - sent[0]))
                    sent[0] += len(chunk)
                    return chunk
                response.read1 = read1
            return response

        self.track_patch(mock.patch.object(sf_update, "safe_urlopen", flaky))
        path = sf_update.download_update(self.info)
        self.assertEqual(Path(path).read_bytes(), DATA)

    def test_cancel_wins_over_a_fatal_error(self):
        """取消仍按取消处理：用户点取消的同时磁盘写满，也不弹失败框。"""
        cancel = threading.Event()
        self.serve()
        self._fill_the_disk(on_write=cancel.set)   # 写盘失败的同一刻，用户点了取消
        with self.assertRaises(sf_update.UpdateCancelled):
            sf_update.download_update(self.info, cancel_event=cancel)


# ---------------------------------------------------------------------------
# F-H-09：类型卫生
# ---------------------------------------------------------------------------

class TypeHygiene(SFCase):
    def setUp(self):
        super().setUp()
        self.key = SigningKey.generate()
        self.track_patch(mock.patch.object(
            sf_update, "UPDATE_PUBLIC_KEY_B64",
            base64.b64encode(bytes(self.key.verify_key)).decode()))

    def _signed(self, **changes):
        manifest = {"schema": 1, "channel": "stable", "stage": "stable", "version": "9.9.9",
                    "bundle_id": sf_update.BUNDLE_ID, "app_name": sf_update._APP_NAME,  # discipline-ok(R5): 签名清单需应用名称。
                    "notes": "1. x",
                    "archive": {"url": URL, "size": 100, "sha256": "a" * 64}}
        for name, value in changes.items():
            if name.startswith("archive_"):
                manifest["archive"][name[len("archive_"):]] = value
            else:
                manifest[name] = value
        payload = {k: v for k, v in manifest.items() if k != "signature"}
        manifest["signature"] = {
            "algorithm": "ed25519",
            "value": base64.b64encode(
                self.key.sign(sf_update._canonical_payload(payload)).signature).decode()}  # discipline-ok(R5): 验签用的规范化字节只有它能生成。
        return json.dumps(manifest, ensure_ascii=False).encode("utf-8")

    def test_the_good_manifest_still_verifies(self):
        info = sf_update.verify_manifest(self._signed())
        self.assertEqual((info.version, info.archive_size), ("9.9.9", 100))  # discipline-ok(R1): 固定签名夹具版本。

    def test_non_ascii_digits_are_not_a_version(self):
        for bad in ("1٤.0.0", "1.4.٩", "１.４.９", "१.०.०"):
            with self.subTest(version=bad):
                with self.assertRaises(UpdateError):
                    sf_update.parse_version(bad)

    def test_bool_is_not_an_int_for_schema_and_size(self):
        for label, changes in (("schema=True", {"schema": True}),
                               ("size=True", {"archive_size": True})):
            with self.subTest(case=label):
                with self.assertRaises(UpdateError):
                    sf_update.verify_manifest(self._signed(**changes))

    def test_a_non_string_url_is_an_UpdateError_not_an_AttributeError(self):
        for bad in (12345, None, ["https://github.com/a"], {"u": 1}):
            with self.subTest(url=bad):
                with self.assertRaises(UpdateError):
                    sf_update.verify_manifest(self._signed(archive_url=bad))

    def test_a_resume_file_that_is_not_an_object_reads_as_no_progress(self):
        info = UpdateInfo("9.9.9", "1. x", URL, SIZE, SHA)
        with tempfile.TemporaryDirectory(prefix="sf_round50_resume_") as tmp:
            state = Path(tmp) / "x.zip.resume.json"
            for text in ("[]", "null", "5", '"text"', "[1, 2]"):
                with self.subTest(content=text):
                    state.write_text(text, encoding="utf-8")
                    self.assertEqual(sf_update._load_resume_state(state, info), set())  # discipline-ok(R5): 续传状态读取只有内部入口。


class CorruptResumeFileHeals(_StagedCase):
    def test_a_non_object_resume_file_does_not_block_the_download(self):
        server = self.serve()
        final = self.staging / ("Skyrocket-%s.zip" % self.info.version)
        part = final.with_suffix(".zip.part")
        state = final.with_suffix(".zip.resume.json")
        with open(part, "wb") as out:
            out.truncate(SIZE)                      # 长度对得上：走『沿用半包 + 读续传状态』那条路
        state.write_text("[]", encoding="utf-8")
        path = sf_update.download_update(self.info)
        self.assertEqual(Path(path).read_bytes(), DATA)
        self.assertGreaterEqual(len(server.requests), 1)
        self.assertFalse(state.exists(), "下载完成后续传状态应被清掉")


if __name__ == "__main__":
    unittest.main()
