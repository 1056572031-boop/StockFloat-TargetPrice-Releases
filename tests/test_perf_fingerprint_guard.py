# -*- coding: utf-8 -*-
"""tools/performance_audit.fingerprint_sha256 的单测（改动覆盖率门禁要求）。

为什么要单独测这个小函数：真实渲染基准只能在有窗口服务的机器上跑，测试里无法执行；
但 PyQt6 6.12 的存根把 `QImage.constBits()` 标成 `voidptr | None` 之后，
"图像没有像素数据"这条错误分支必须有人兜住，否则失败现场会伪装成探针自身崩溃。
这里用假 image 对象覆盖两条路径，不需要 Qt 窗口。
"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import performance_audit as PA  # noqa: E402
from tests._base import SFCase  # noqa: E402


class _FakeBits:
    def __init__(self, payload: bytes):
        self.payload = payload

    def asstring(self, size: int) -> bytes:
        return self.payload


class _FakeImage:
    def __init__(self, bits):
        self._bits = bits

    def constBits(self):
        return self._bits

    def sizeInBytes(self) -> int:
        return len(getattr(self._bits, "payload", b""))


class FingerprintGuardTest(SFCase):
    def test_returns_sha256_of_pixel_buffer(self):
        got = PA.fingerprint_sha256(_FakeImage(_FakeBits(b"abc")), "legacy-184x155-1x")
        self.assertEqual(got, hashlib.sha256(b"abc").hexdigest())

    def test_none_bits_raises_oserror_naming_the_key(self):
        with self.assertRaises(OSError) as ctx:
            PA.fingerprint_sha256(_FakeImage(None), "translucent-380x420-2x")
        self.assertIn("translucent-380x420-2x", str(ctx.exception))
        self.assertIn("constBits", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
