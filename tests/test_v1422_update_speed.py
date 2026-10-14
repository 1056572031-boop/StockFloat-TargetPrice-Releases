# -*- coding: utf-8 -*-
"""v1.4.22 回归：更新下载提速（更多分片 + 单片断点重试 + 读取中断纳入降级）。

实测依据（2026-09-23，真实 26.2 MB 更新包，3 轮交错 A/B，参照 CDN 归一化）：
  旧实现（8 分片、无重试）28.5s / 29.0s；新实现 14.9s / 17.4s —— 约 1.7~2 倍。
根因：CDN 会掐断个别连接，一处失败让整轮并行报废、一路路降到单路（单路实测
只有 1.5 MB/s）。所以分片更多、且**每个分片从断点续传重试**，把一次坏连接的
代价从"整轮重来"降到"补一小段"。

另修一个连带缺陷：读取中途连接断开抛的是裸 OSError，此前会直接逃出
`_download_adaptive` 的降级阶梯，把整次更新打成"更新未完成"。
"""

import hashlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_update


class TestPartLayout(unittest.TestCase):
    """分片数量：包越大片越多，小包不被切出非法区间。"""

    def test_real_sized_archive_uses_sixteen_parts(self):
        size = 27_475_937                       # 与线上 1.4.21 包同量级
        ranges = sf_update._download_ranges(size)
        self.assertEqual(len(ranges), 16,
                         "16 路并发实测吞吐约为 8 路的两倍，别退回 8 片")
        self.assertEqual(sum(e - s + 1 for s, e in ranges), size)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], size - 1)

    def test_worker_ladder_scales_with_part_count(self):
        self.assertEqual(sf_update._worker_ladder(16), [16, 8, 4, 1])
        self.assertEqual(sf_update._worker_ladder(8), [8, 4, 1],
                         "分片只有 8 个时不得出现 8、8、4、1 重复档")
        self.assertEqual(sf_update._worker_ladder(3), [3, 1])
        self.assertEqual(sf_update._worker_ladder(1), [1])


class _Resp:
    """可编程的 206 响应：写到某个偏移就掐断连接。"""

    def __init__(self, begin, payload, drop_at=None):
        self.status = 206
        end = len(payload) - 1
        self.headers = {"Content-Range": "bytes %d-%d/%d" % (begin, end, end + 1)}
        self._payload = payload
        self._pos = begin
        self._end = end
        self._drop_at = drop_at
        self.reads = 0

    def read(self, count):
        self.reads += 1
        if self._drop_at is not None and self._pos >= self._drop_at:
            raise ConnectionResetError("connection reset by peer")
        chunk = self._payload[self._pos:min(self._pos + count, self._end + 1)]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def geturl(self):
        return "https://release-assets.githubusercontent.com/a.zip"

    def __exit__(self, *args):
        return False


class TestRangeRetry(unittest.TestCase):
    """单片断点重试：坏连接只赔一小段，而不是整轮报废。"""

    def setUp(self):
        self.payload = bytes((index * 7) % 256 for index in range(4096))
        self.start, self.end = 0, len(self.payload) - 1

    def _part_file(self, folder):
        part = Path(folder) / "a.part"
        with open(part, "wb") as out:
            out.truncate(len(self.payload))
        return part

    def test_resumes_from_offset_after_connection_drop(self):
        # 载荷必须大于 1MB 的单次读取块，断连才会落在中途而不是"还没读就断"
        payload = bytes((index * 7) % 256 for index in range(2 * 1024 * 1024 + 777))
        start, end = 0, len(payload) - 1
        resume_at = 1024 * 1024                 # 第一条连接送满 1MB 后被掐断
        calls = []
        responses = [_Resp(start, payload, drop_at=1),
                     _Resp(resume_at, payload)]

        def fake_urlopen(request, timeout=None):
            calls.append(request.headers["Range"])
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as folder:
            part = Path(folder) / "a.part"
            with open(part, "wb") as out:
                out.truncate(len(payload))
            with patch.object(sf_update, "safe_urlopen", fake_urlopen):
                sf_update._download_range("https://github.com/a/b/releases/download/v/a.zip",
                                          start, end, part, threading.Event(), 60)
            self.assertEqual(part.read_bytes(), payload, "断点续传后内容必须完整")
        self.assertEqual(calls[0], "bytes=0-%d" % end)
        self.assertEqual(calls[1], "bytes=%d-%d" % (resume_at, end),
                         "重试必须从断点接着下，不能整片重来")

    def test_persistent_read_failure_raises_update_error_not_raw_oserror(self):
        """连接反复被掐：必须收敛成 UpdateError 走降级，裸 OSError 会废掉整轮下载。"""
        responses = [_Resp(self.start, self.payload, drop_at=0)
                     for _ in range(sf_update._PART_ATTEMPTS)]

        def fake_urlopen(request, timeout=None):
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as folder:
            part = self._part_file(folder)
            with patch.object(sf_update, "safe_urlopen", fake_urlopen), \
                 self.assertRaises(sf_update.UpdateError):
                sf_update._download_range("https://github.com/a/b/releases/download/v/a.zip",
                                          self.start, self.end, part,
                                          threading.Event(), 60)

    def test_cancel_is_never_retried(self):
        """用户点取消必须立即停下，不能被重试逻辑拖着继续下。"""
        started = threading.Event()
        gate = threading.Event()

        class _Gate:
            def __init__(self):
                self.count = 0

            def is_set(self):
                self.count += 1
                started.set()
                return gate.wait(0.2) or self.count > 1

        cancel = threading.Event()
        responses = [_Resp(self.start, self.payload, drop_at=0)]

        def fake_urlopen(request, timeout=None):
            cancel.set()                      # 第一条连接建立后立刻请求取消
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as folder:
            part = self._part_file(folder)
            with patch.object(sf_update, "safe_urlopen", fake_urlopen), \
                 self.assertRaises(sf_update.UpdateError) as caught:
                sf_update._download_range("https://github.com/a/b/releases/download/v/a.zip",
                                          self.start, self.end, part, cancel, 60)
        self.assertIn("取消", str(caught.exception))


class TestAdaptiveStillDegrades(unittest.TestCase):
    """降级阶梯契约不变：并行失败后仍按阶梯降到单路。"""

    def test_eight_part_body_still_walks_eight_four_one(self):
        body = b"abcdefgh"
        info = sf_update.UpdateInfo("1.3.71", "", "https://github.com/a/b/releases/download/v/a.zip",
                                    len(body), hashlib.sha256(body).hexdigest())
        worker_counts = []

        class _Pool:
            def __init__(self, max_workers, **kwargs):
                del kwargs
                worker_counts.append(max_workers)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                del args

            def submit(self, fn, *args, **kwargs):
                future = concurrent_futures.Future()
                if worker_counts[-1] in (8, 4):
                    future.set_exception(sf_update.UpdateError("down"))
                else:
                    try:
                        fn(*args, **kwargs)
                        future.set_result(None)
                    except Exception as exc:
                        future.set_exception(exc)
                return future

        def fake_range(url, start, end, part_path, cancel_event, timeout, progress, source=None):
            del url, cancel_event, timeout
            with open(part_path, "r+b") as out:
                out.seek(start)
                out.write(body[start:end + 1])
            progress(end - start + 1)

        import concurrent.futures as concurrent_futures

        with tempfile.TemporaryDirectory() as folder, \
             patch.object(sf_update.concurrent.futures, "ThreadPoolExecutor", _Pool), \
             patch.object(sf_update, "resolve_download_url", return_value=info.archive_url), \
             patch.object(sf_update, "_download_range", side_effect=fake_range):
            part = Path(folder) / "a.part"
            state = Path(folder) / "a.resume.json"
            sf_update._download_adaptive(info, part, state, threading.Event(), 60)
            self.assertEqual(part.read_bytes(), body)
        self.assertEqual(worker_counts, [8, 4, 1],
                         "8 字节小包只有 8 个分片，阶梯必须还是 8→4→1")


if __name__ == "__main__":
    unittest.main()
