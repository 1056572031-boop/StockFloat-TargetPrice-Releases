# -*- coding: utf-8 -*-
"""实时股价 v1.2.42 崩溃报告关键段抽取纯逻辑单测（sf_crashreport 侧）。

覆盖 sf_crashreport（无 Qt 依赖纯函数）：
1. is_crash_report_name：StockFloatTargetPrice 前缀 + .ips/.crash 后缀识别（含非法名/大小写）
2. select_crash_reports：空目录兜底、过滤非崩溃/非法 mtime、7 天内优先、
   7 天无则回退取最新、max_reports 上限、按 mtime 新→旧排序
3. extract_crash_key_sections：
   - 空文本/垃圾文本安全兜底（不抛异常）
   - .crash 纯文本 → 异常类型/终止原因/崩溃线程堆栈 + 二进制镜像关键行
   - .ips JSON → 异常/终止、崩溃线程与主线程栈、关键镜像
   - limit_bytes 体积上限：超限截断 + [truncated] 标记
   - bytes 输入解码
4. 模块导入不依赖 Qt（无 PyQt6 / Foundation import）

运行方式（在 StockFloatTargetPrice 目录）：
    python3 -m unittest tests.test_crashreport -v
"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sf_crashreport import (MAX_SEGMENT_BYTES, extract_crash_key_sections,
                            is_crash_report_name, select_crash_reports)

# 固定"now"便于测试 7 天窗口
_NOW = 1785000000.0          # 2026-08-26 附近
_DAY = 86400.0
_OLD_TS = _NOW - 30 * _DAY   # > 7 天前


class TestIsCrashReportName(unittest.TestCase):
    def test_valid_names(self):
        for fn in ("StockFloatTargetPrice.crash", "StockFloatTargetPrice.ips",
                   "StockFloatTargetPrice-2026-08-25-100000.ips",
                   "StockFloatTargetPrice 2026-08-25 10:00:00.crash"):
            self.assertTrue(is_crash_report_name(fn), fn)

    def test_invalid_names(self):
        for fn in ("Other.ips", "Other.crash", "StockFloatTargetPrice.diag",
                   "StockFloatTargetPrice.txt", "StockFloatTargetPrice", "StockFloatTargetPrice.IPS",
                   "StockFloatTargetPrice.CRASH", "stockfloat.ips", "StockFloatTargetPrice.ips.bak",
                   "", None, 123):
            self.assertFalse(is_crash_report_name(fn), fn)


class TestSelectCrashReports(unittest.TestCase):
    def test_empty_entries_returns_empty(self):
        self.assertEqual(select_crash_reports([], now=_NOW), [])

    def test_filters_non_crash_and_bad_mtime(self):
        entries = [
            ("NotOurs.ips", _NOW - _DAY),
            ("StockFloatTargetPrice.crash", "bad-mtime"),
            ("StockFloatTargetPrice.diag", _NOW),
            ("StockFloatTargetPrice-1.ips", _NOW - 2 * _DAY),
            ("StockFloatTargetPrice-2.ips", _NOW - 5 * _DAY),
        ]
        got = select_crash_reports(entries, now=_NOW, max_reports=5)
        self.assertEqual([f for f, _ in got],
                         ["StockFloatTargetPrice-1.ips", "StockFloatTargetPrice-2.ips"])

    def test_window_days_priority_and_cap(self):
        entries = [
            ("StockFloatTargetPrice-old.ips", _OLD_TS),
            ("StockFloatTargetPrice-new2.ips", _NOW - 2 * _DAY),
            ("StockFloatTargetPrice-new1.ips", _NOW - _DAY),
        ]
        got = select_crash_reports(entries, now=_NOW, max_reports=2)
        # 7 天内优先且最多 2 个，按新→旧
        self.assertEqual([f for f, _ in got],
                         ["StockFloatTargetPrice-new1.ips", "StockFloatTargetPrice-new2.ips"])

    def test_fallback_when_none_in_window(self):
        entries = [
            ("StockFloatTargetPrice-a.ips", _OLD_TS),
            ("StockFloatTargetPrice-b.crash", _OLD_TS - _DAY),
        ]
        got = select_crash_reports(entries, now=_NOW)
        # 7 天内没有任何报告 → 回退取最新 2 个
        self.assertEqual([f for f, _ in got],
                         ["StockFloatTargetPrice-a.ips", "StockFloatTargetPrice-b.crash"])

    def test_max_reports_cap_applies(self):
        entries = [("StockFloatTargetPrice-%d.ips" % i, _NOW - i * _DAY) for i in range(5)]
        got = select_crash_reports(entries, now=_NOW, max_reports=2)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0][0], "StockFloatTargetPrice-0.ips")  # 最新在前

    def test_sorting_newest_first(self):
        entries = [("StockFloatTargetPrice-a.ips", _NOW - 3 * _DAY),
                   ("StockFloatTargetPrice-b.ips", _NOW - _DAY),
                   ("StockFloatTargetPrice-c.ips", _NOW - 2 * _DAY)]
        got = select_crash_reports(entries, now=_NOW, max_reports=3)
        self.assertEqual([f for f, _ in got],
                         ["StockFloatTargetPrice-b.ips", "StockFloatTargetPrice-c.ips", "StockFloatTargetPrice-a.ips"])


_CRASH_PLAIN = """Date/Time:       2026-08-25 10:00:00.000 +0800
OS Version:      macOS 26.6.2
Exception Type:  EXC_BAD_ACCESS (SIGSEGV)
Exception Codes: KERN_INVALID_ADDRESS at 0x0000000000000000
Termination Reason: Namespace SIGNAL, Code 11
Termination Signal: 11

Thread 7 Crashed:
0   libcrypto.3.dylib          0x0000000103a00000 EVP_CIPHER_CTX_free + 32
1   StockFloatTargetPrice                 0x0000000102c00000 0x102c00000 + 123456
2   libsystem_pthread.dylib    0x00007ff800000000 _pthread_start + 0

Thread 0:
0   libsystem_kernel.dylib     0x00007ff700000000 mach_msg_trap + 8
1   QtCore                     0x0000000104000000 QEventLoop::exec + 100

Binary Images:
0x0000000102c00000 StockFloatTargetPrice
0x0000000103a00000 libcrypto.3.dylib
0x0000000104000000 QtCore
0x00007ff700000000 libsystem_kernel.dylib
"""

_CRASH_JSON = """{
  "app_name": "StockFloatTargetPrice",
  "timestamp": "2026-08-26 12:00:00.00 +0800",
  "exception": {"type": "EXC_BAD_ACCESS", "signal": "SIGSEGV",
                "codes": "KERN_INVALID_ADDRESS at 0x0"},
  "termination": {"indicator": "SIGNAL", "codes": "11"},
  "triggeredByCThread": 3,
  "threads": [
    {"frames": [{"symbol": "QEventLoop::exec", "imageIndex": 1}]},
    {"frames": [{"symbol": "mach_msg_trap", "imageIndex": 2}]},
    {},
    {"frames": [{"symbol": "EVP_CIPHER_CTX_free", "imageIndex": 0},
                {"symbol": "ssl_read", "imageIndex": 0}]}
  ],
  "usedImages": [
    {"name": "StockFloatTargetPrice", "base": 8992768000, "arch": "arm64e"},
    {"name": "QtCore", "base": 8990000000, "arch": "arm64"},
    {"name": "libcrypto.3.dylib", "base": 8988000000, "arch": "arm64"},
    {"name": "libsystem_kernel.dylib", "base": 8991000000, "arch": "x86_64"}
  ]
}
"""


class TestExtractPlainCrash(unittest.TestCase):
    def test_empty_and_garbage_safe(self):
        self.assertEqual(extract_crash_key_sections(""), "")
        # 二进制垃圾 + 非 JSON + 无标记 → 安全返回空串，不抛异常
        self.assertEqual(extract_crash_key_sections("\x00\xff\x01 not a crash at all"), "")
        self.assertEqual(extract_crash_key_sections("[1,2,3]"), "")
        self.assertEqual(extract_crash_key_sections(None), "")
        self.assertEqual(extract_crash_key_sections("   "), "")

    def test_exception_and_termination_lines(self):
        out = extract_crash_key_sections(_CRASH_PLAIN)
        self.assertIn("Exception Type", out)
        self.assertIn("SIGSEGV", out)
        self.assertIn("Termination Reason", out)
        self.assertIn("Namespace SIGNAL", out)

    def test_crashed_thread_stack(self):
        out = extract_crash_key_sections(_CRASH_PLAIN)
        self.assertIn("Thread N Crashed", out)
        self.assertIn("EVP_CIPHER_CTX_free", out)   # OpenSSL 线程崩溃帧
        self.assertIn("StockFloatTargetPrice", out)

    def test_main_thread_stack(self):
        out = extract_crash_key_sections(_CRASH_PLAIN)
        self.assertIn("Main Thread", out)
        self.assertIn("QEventLoop::exec", out)

    def test_key_images_section(self):
        out = extract_crash_key_sections(_CRASH_PLAIN)
        self.assertIn("Key Images", out)
        self.assertIn("libcrypto.3.dylib", out)


class TestExtractJsonIps(unittest.TestCase):
    def test_exception_and_termination(self):
        out = extract_crash_key_sections(_CRASH_JSON)
        self.assertIn("Exception: type=EXC_BAD_ACCESS", out)
        self.assertIn("signal=SIGSEGV", out)
        self.assertIn("Termination:", out)

    def test_crashed_thread_by_triggeredByCThread(self):
        out = extract_crash_key_sections(_CRASH_JSON)
        self.assertIn("Crashed Thread", out)
        self.assertIn("EVP_CIPHER_CTX_free", out)
        self.assertIn("Main Thread", out)
        self.assertIn("QEventLoop::exec", out)

    def test_key_images_from_usedImages(self):
        out = extract_crash_key_sections(_CRASH_JSON)
        self.assertIn("Key Images", out)
        self.assertIn("libcrypto.3.dylib", out)
        self.assertIn("arm64", out)

    def test_invalid_json_safe_fallback(self):
        # 纯 JSON 开头但非法 → 不抛异常，走纯文本兜底或空
        out = extract_crash_key_sections('{"exception": {broken')
        self.assertIsInstance(out, str)

    # ---- v1.4.59（S5）：真实 .ips 是"两份 JSON 文档" --------------------------------
    # 上面那些用例用的 `_CRASH_JSON` 是**单份** JSON —— 而 macOS 落盘的 .ips 是
    # "首行元数据 + 次行正文"两份拼在一起。旧解析器 `json.loads(text[index("{"):])`
    # 一次喂两份必然 `Extra data` 抛错，于是退回纯文本兜底：崩溃线程堆栈、终止原因、
    # 镜像清单一条都进不了诊断包。本机 25 个真样本实测 **0/14 能解析**，
    # 也就是说这条取证链自 v1.4.18 建起来就没通过 —— 而用例一直全绿，
    # 因为**夹具的形状不是现实的形状**。下面两条用真形状守它。
    _IPS_HEADER = {"app_name": "Skyrocket", "app_version": "1.4.57",
                   "bug_type": "309", "os_version": "macOS 27.0.1 (26A434)",
                   "timestamp": "2026-10-09 16:48:09.00 +0800"}

    def _real_ips_text(self):
        body = json.loads(_CRASH_JSON)
        return (json.dumps(self._IPS_HEADER, ensure_ascii=False) + "\n"
                + json.dumps(body, ensure_ascii=False))

    def test_real_ips_shape_renders_crashed_stack_and_identity(self):
        text = self._real_ips_text()
        out = extract_crash_key_sections(text)
        self.assertIn("IPS header:", out, "元数据那一半（进程名/版本/系统/时刻）也要渲染")
        self.assertIn("Skyrocket", out)
        self.assertIn("Exception: type=EXC_BAD_ACCESS", out)
        self.assertIn("EVP_CIPHER_CTX_free", out, "崩溃线程栈帧必须进诊断包")
        self.assertIn("libcrypto.3.dylib", out, "镜像清单必须进诊断包")

    def test_real_ips_shape_is_not_silently_downgraded_to_plain_text(self):
        """判据可失败的那一半：解析成功时**不许**走纯文本兜底。

        旧实现这里返回的是 186 字符左右的噪声（没有 Thread/Exception 行），
        所以这条断言就是把"静默降级"这件事钉死：一旦退回兜底，长度与内容都会露馅。
        """
        from sf_crashreport import _render_json_body
        text = self._real_ips_text()
        rendered = _render_json_body(text)
        self.assertTrue(rendered, "两份 JSON 的 .ips 解析成空 → 会静默退回纯文本噪声")
        self.assertIn("Crashed Thread", rendered)


class TestExtractLimit(unittest.TestCase):
    def _big_json(self, frame_count):
        """构造 .ips JSON：崩溃线程含大量帧，渲染后远超 limit。"""
        frames = [{"symbol": "frame_%d 0x123456789 libcrypto symbol_%d" % (i, i),
                   "imageIndex": 0} for i in range(frame_count)]
        return ('{"exception": {"type": "EXC_BAD_ACCESS", "signal": "SIGSEGV"},'
                '"termination": {"indicator": "SIGNAL"}, "triggeredByCThread": 1,'
                '"threads": [{"frames": [{"symbol": "main_frame", "imageIndex": 1}]},'
                '{"frames": %s}],'
                '"usedImages": [{"name": "libcrypto.3.dylib", "base": 0, "arch": "arm64"}]}'
                % json.dumps(frames))

    def test_size_cap_with_truncation(self):
        big = self._big_json(5000)
        out = extract_crash_key_sections(big, limit_bytes=512)
        # 输出必须 ≤ limit + 截断标记长度，且不能完整包含原文
        n = len(out.encode("utf-8"))
        self.assertLessEqual(n, 512 + 40, "out bytes=%d" % n)
        self.assertIn("truncated", out)
        self.assertNotIn("symbol_4999", out)

    def test_within_limit_not_truncated(self):
        out = extract_crash_key_sections(_CRASH_PLAIN, limit_bytes=MAX_SEGMENT_BYTES)
        self.assertNotIn("truncated", out)
        self.assertIn("Exception Type", out)

    def test_bytes_input_decoded(self):
        out = extract_crash_key_sections(
            ("Exception Type: EXC_CRASH (SIGABRT)\n"
             "Termination Reason: Namespace SIGNAL, Code 6\n").encode("utf-8"))
        self.assertIn("Exception Type", out)
        self.assertIn("SIGABRT", out)


class TestModulePurity(unittest.TestCase):
    def test_no_qt_or_foundation_import(self):
        """模块不得 import PyQt6 / Foundation / AppKit（纯逻辑，可脱离 Qt 单测）。
        注意：源码中含 "PyQt6" 字样是合法的——它只是二进制镜像关键行的过滤关键词，
        因此这里只校验 import 语句，而非整个源码文本。"""
        with open(os.path.join(os.path.dirname(os.path.dirname(
                  os.path.abspath(__file__))), "sf_crashreport.py"),
                  encoding="utf-8") as f:
            src = f.read()
        bad = re.findall(r"^\s*(?:from\s+\S+|import\s+\S+)",
                         src, flags=re.MULTILINE)
        for line in bad:
            self.assertNotIn("PyQt6", line)
            self.assertNotIn("Foundation", line)
            self.assertNotIn("AppKit", line)


if __name__ == "__main__":
    unittest.main()
