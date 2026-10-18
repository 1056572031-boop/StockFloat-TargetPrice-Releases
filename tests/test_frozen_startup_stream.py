"""冻结包冷启动取证工具的判据用例（纯函数层；真屏幕专项见 outputs/，不进用例）。"""
from __future__ import annotations

import contextlib
import io
import plistlib
from unittest import mock

from tests._base import SFCase
from tools import frozen_startup_stream as F


class VariantPlanTests(SFCase):
    def test_both_variants_place_window_inside_recording_rect(self):
        for variant in ("cached", "no-cache"):
            plan = F.variant_plan(variant)
            self.assertTrue(plan["window_inside_rect"], variant)
            self.assertEqual(len(plan["recording_rect"]), 4, variant)

    def test_cached_seeds_cache_and_no_cache_does_not(self):
        # cached 语义=缓存冷启动：必须有种子行情；no-cache 的 A21 现场恰恰是"什么都没有"。
        self.assertTrue(F.VARIANTS["cached"]["seed_last_quote"])
        self.assertIsNotNone(F.VARIANTS["cached"]["config"])
        self.assertFalse(F.VARIANTS["no-cache"]["seed_last_quote"])
        self.assertIsNone(F.VARIANTS["no-cache"]["config"])

    def test_unknown_variant_is_rejected(self):
        with self.assertRaises(KeyError):
            F.variant_plan("fullscreen")


class GeometryJumpTests(SFCase):
    @staticmethod
    def frames_with(y_values, start=180):
        # Swift 解码回写的坐标是 int；夹具必须同形，否则判据的 isinstance 门会静默跳过。
        return [{"time": 0.1, "visible": False}] + [
            {"time": 0.2 + i * 0.05, "visible": True, "window_x": 400, "window_y": y}
            for i, y in enumerate(y_values)]

    def test_stable_layout_and_subthreshold_jitter_pass(self):
        stable = self.frames_with([180.0] * 10)
        self.assertEqual(F.geometry_jumps(stable), [])
        jitter = self.frames_with([180.0 + (1 if i % 2 else 0) for i in range(10)])
        self.assertEqual(F.geometry_jumps(jitter), [])

    def test_height_jump_after_first_frame_is_caught(self):
        # A21 的“先高后扁”会移动价格带 y；阈值放宽时这条必须红。
        moved = self.frames_with([180, 150, 150])
        jumps = F.geometry_jumps(moved)
        self.assertEqual(len(jumps), 1)
        self.assertEqual(jumps[0]["from"], [400, 180])
        self.assertEqual(jumps[0]["to"], [400, 150])

    def test_invisible_or_positionless_frames_neither_crash_nor_jump(self):
        self.assertEqual(F.geometry_jumps([{"time": 0.1, "visible": False}]), [])
        self.assertEqual(F.geometry_jumps([{"time": 0.1, "visible": True}]), [])


class RefusalTests(SFCase):
    def test_installed_app_running_blocks_evidence(self):
        # 只认 /Applications 两代路径；探针自己的 dist 副本同名不算。
        with mock.patch.object(F.subprocess, "run", return_value=mock.Mock(returncode=0)):
            self.assertTrue(F.installed_app_running())
        with mock.patch.object(F.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertFalse(F.installed_app_running())

    def test_version_reader_reads_plist_not_constant(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            app = Path(td)
            info = app / "Contents"
            info.mkdir()
            expected = "fixture-version"
            (info / "Info.plist").write_bytes(
                plistlib.dumps({"CFBundleShortVersionString": expected}))
            self.assertEqual(F.app_version(app), expected)

    def test_bounds_tolerance_rejects_wrong_size_and_missing(self):
        self.assertTrue(F.bounds_close({"X": 402, "Y": 181, "Width": 184, "Height": 224}, (400, 180)))
        self.assertFalse(F.bounds_close({"X": 400, "Y": 180, "Width": 184, "Height": 300}, (400, 180)))
        self.assertFalse(F.bounds_close(None, (400, 180)))


class LogAnchorTests(SFCase):
    def test_anchors_and_uncaught_are_named(self):
        text = "noise\n%sTEST\nfirst-frame painted\nUNCAUGHT boom\n" % F.MARK_STARTUP
        anchors = F.log_anchors(text)
        self.assertTrue(anchors["startup"])
        self.assertTrue(anchors["first_frame"])
        self.assertEqual(len(anchors["uncaught"]), 1)
        self.assertEqual(anchors["lines"], 4)

    def test_empty_log_has_no_anchors(self):
        anchors = F.log_anchors("")
        self.assertFalse(anchors["startup"])
        self.assertFalse(anchors["first_frame"])


class PhaseBudgetOverrideTests(SFCase):
    """first_visible_budget 是 shared 尺子的显式覆盖口；默认两档行为不许变。"""

    @staticmethod
    def dense(first_delay, end=0.8):
        frames = [{"time": 0.0, "visible": False}]
        t = 0.0
        while t < end - 1e-9:
            t += 0.05
            frames.append({"time": round(t, 3), "visible": t >= first_delay,
                           "window_x": 1, "window_y": 1})
        return frames

    def test_override_allows_slow_first_price_and_still_rejects_later(self):
        # 夹具必须满足尺子前提（15fps 下限、首尾覆盖），否则红的原因是夹具不是判据。
        loose = F.phase_report(self.dense(0.45), 0.0, 0.8, startup=True, first_visible_budget=3.0)
        self.assertTrue(loose["passed"], loose["errors"])
        strict = F.phase_report(self.dense(0.45), 0.0, 0.8, startup=True, first_visible_budget=0.2)
        self.assertFalse(strict["passed"])

    def test_default_budgets_unchanged_without_override(self):
        from tools.continuous_frames import MAX_STARTUP_DELAY
        report = F.phase_report(self.dense(MAX_STARTUP_DELAY + 0.05, end=MAX_STARTUP_DELAY + 0.6),
                                0.0, MAX_STARTUP_DELAY + 0.6, startup=True)
        self.assertFalse(report["passed"])


class TemplateForegroundTests(SFCase):
    """模板自检与 Swift PriceMask 同规则；cICP 色彩块事故的回归锚在 _write_plain_png 的注释里。"""

    @staticmethod
    def synthetic(with_digits=True):
        from PyQt6.QtGui import QColor, QImage
        img = QImage(184, 224, QImage.Format.Format_ARGB32)
        img.fill(QColor(25, 28, 30, 255))
        if with_digits:
            from PyQt6.QtGui import QPainter
            painter = QPainter(img)
            for x0 in (20, 35, 50, 65):
                painter.fillRect(x0, 78, 7, 16, QColor(235, 235, 235, 255))
            painter.end()
        return img

    def test_digit_template_passes_and_blank_fails(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            good = Path(td) / "good.png"
            F._write_plain_png(self.synthetic(True), good)  # discipline-ok(R5): PNG writer is the behavior under test
            self.assertTrue(F.template_foreground_ok(good))
            blank = Path(td) / "blank.png"
            F._write_plain_png(self.synthetic(False), blank)  # discipline-ok(R5): same writer, blank-image negative control
            self.assertFalse(F.template_foreground_ok(blank))

    def test_wrong_template_dimensions_are_rejected(self):
        import tempfile
        from pathlib import Path
        from PyQt6.QtGui import QImage
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "small.png"
            QImage(20, 20, QImage.Format.Format_ARGB32).save(str(path))
            self.assertFalse(F.template_foreground_ok(path))

    def test_plain_png_has_no_color_management_chunks(self):
        # iCCP/cICP 是本轮真跑事故元凶：cICP 在场时 ImageIO 做 P3→sRGB 转换，
        # 饱和绿红通道被钳到 0，模板与视频红差 82>75 全帧误判不可见。
        import struct
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "t.png"
            F._write_plain_png(self.synthetic(True), path)  # discipline-ok(R5): inspect exact bytes emitted by the writer
            data = path.read_bytes()
            pos, types = 8, []
            while pos < len(data):
                length = struct.unpack(">I", data[pos:pos+4])[0]
                types.append(data[pos+4:pos+8].decode())
                pos += 12 + length
            self.assertEqual(types, ["IHDR", "IDAT", "IEND"])


class CliTests(SFCase):
    def test_unknown_arguments_exit_2_without_writing_anything(self):
        # 判据起作用而不是碰巧 2：不带程序名直接喂未知参数（argv 约定不含 argv[0]）。
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(F.main(["--definitely-not-a-flag"]), 2)

    def test_self_test_passes(self):
        self.assertEqual(F.main(["--self-test"]), 0)

    def test_missing_variant_or_out_prints_usage(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(F.main([]), 2)

    def test_successful_result_is_rendered_and_returns_zero(self):
        result = {"passed": True, "problems": [],
                  "startup_report": {"first_visible_delay": 0.12,
                                     "blank_upper_seconds": 0.04,
                                     "max_source_gap": 0.06},
                  "geometry_jumps": []}
        with mock.patch.object(F, "run", return_value=result), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(F.main(["--variant", "cached", "--out", "out"]), 0)
        self.assertIn("通过", output.getvalue())

    def test_rejected_result_is_rendered_and_returns_one(self):
        result = {"passed": False, "problems": ["geometry jump"],
                  "startup_report": {}, "geometry_jumps": [{"time": 1}]}
        with mock.patch.object(F, "run", return_value=result), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(F.main(["--variant", "cached", "--out", "out"]), 1)
        self.assertIn("拒绝", output.getvalue())
