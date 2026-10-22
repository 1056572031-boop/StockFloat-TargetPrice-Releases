# -*- coding: utf-8 -*-
"""摄像头帧率上限：先问设备当前档位支不支持，不支持就不去撞系统的拒绝。

真实现场：用户机器上同一型号相机在采集档位下只报 30~30fps，15fps 上限每次启动都被
系统当场拒绝并记一条 WARNING（日志里累计 43 次同一句），而开发机报 15~30fps。
区间随机器、随档位变，所以判据同时覆盖“支持 / 不支持 / 读不到”三种答案。
"""
import pathlib
import sys
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sf_presence as presence  # noqa: E402
from sf_constants import FACE_GUARD_CAMERA_FPS  # noqa: E402

try:
    from _base import SFCase                  # noqa: E402
except ImportError:                           # 以包形式被 discover 导入时
    from tests._base import SFCase            # noqa: E402

apply_profile = presence.FacePresenceMonitor._apply_power_profile  # discipline-ok(R5): 设备配置契约本身就是这个私有方法


def _range(lo, hi):
    return types.SimpleNamespace(minFrameRate=lambda: lo, maxFrameRate=lambda: hi)


def _device(ranges, calls):
    """真机形状：lockForConfiguration_ 返回 (BOOL, error) 元组；activeFormat 是方法。"""
    fmt = types.SimpleNamespace(videoSupportedFrameRateRanges=lambda: ranges)
    return types.SimpleNamespace(
        activeFormat=lambda: fmt,
        lockForConfiguration_=lambda err: calls.append("lock") or (True, None),
        setActiveVideoMinFrameDuration_=lambda dur: calls.append("min"),
        setActiveVideoMaxFrameDuration_=lambda dur: calls.append("max"),
        unlockForConfiguration=lambda: calls.append("unlock"))


class FpsProbe(SFCase):
    def setUp(self):
        super().setUp()
        self.log = mock.Mock()
        self.track_patch(mock.patch.object(presence, "log_line", self.log))
        self.session = types.SimpleNamespace(canSetSessionPreset_=lambda preset: True,
                                             setSessionPreset_=lambda preset: None)
        self.calls = []
        self.monitor = presence.FacePresenceMonitor()

    def _apply(self, device):
        apply_profile(self.monitor, self.session, device)
        return self.monitor.applied_profile

    def _warnings(self):
        return [c for c in self.log.call_args_list if len(c.args) > 1 and c.args[1] == "WARNING"]

    def test_unsupported_rate_is_skipped_without_locking_or_warning(self):
        profile = self._apply(_device([_range(30.0, 30.0)], self.calls))
        self.assertEqual(self.calls, [], "不支持就不该去锁设备、更不该去设帧率")
        self.assertFalse(profile["fps_applied"])
        self.assertIs(profile["fps_supported"], False)
        self.assertEqual(profile["fps_ranges"], [[30.0, 30.0]])
        self.assertEqual(self._warnings(), [], "这是已知的设备能力，不是故障，不该记 WARNING")
        self.assertTrue(any("只支持" in c.args[0] for c in self.log.call_args_list),
                        "跳过时仍要留一行说明区间，远程才分得清“设备不支持”和“没去设”")

    def test_supported_rate_is_applied_as_before(self):
        profile = self._apply(_device([_range(15.0, 30.0)], self.calls))
        self.assertEqual(self.calls, ["lock", "min", "max", "unlock"])
        self.assertTrue(profile["fps_applied"])
        self.assertIs(profile["fps_supported"], True)

    def test_the_boundary_of_a_range_counts_as_supported(self):
        boundary = float(FACE_GUARD_CAMERA_FPS)
        self._apply(_device([_range(boundary, 30.0)], self.calls))
        self.assertIn("min", self.calls)

    def test_any_matching_range_is_enough(self):
        self._apply(_device([_range(60.0, 120.0), _range(10.0, 20.0)], self.calls))
        self.assertIn("min", self.calls)

    def test_unreadable_ranges_keep_the_old_set_then_report_path(self):
        profile = self._apply(types.SimpleNamespace(
            lockForConfiguration_=lambda err: self.calls.append("lock") or (True, None),
            setActiveVideoMinFrameDuration_=lambda dur: self.calls.append("min"),
            setActiveVideoMaxFrameDuration_=lambda dur: self.calls.append("max"),
            unlockForConfiguration=lambda: self.calls.append("unlock")))
        self.assertEqual(self.calls, ["lock", "min", "max", "unlock"], "读不到区间时必须退回原路径")
        self.assertIsNone(profile["fps_supported"])
        self.assertEqual(profile["fps_ranges"], [])

    def test_an_empty_range_list_is_treated_as_unreadable_not_unsupported(self):
        profile = self._apply(_device([], self.calls))
        self.assertIsNone(profile["fps_supported"])
        self.assertIn("min", self.calls)

    def test_a_rejected_set_still_leaves_the_warning_when_ranges_claimed_support(self):
        device = _device([_range(15.0, 30.0)], self.calls)
        device.setActiveVideoMinFrameDuration_ = mock.Mock(side_effect=ValueError("rejected"))
        profile = self._apply(device)
        self.assertFalse(profile["fps_applied"])
        self.assertEqual(len(self._warnings()), 1, "区间说支持却被拒，是异常，必须仍然留痕")


@unittest.skipUnless(sys.platform == "darwin", "只在 macOS 上有 AVFoundation")
class RealDeviceShape(SFCase):
    """假设备会把桥接形状藏起来：用真设备（有才测，只读、不要授权）核对读法。"""

    def test_real_active_format_ranges_are_readable_floats(self):
        try:
            import AVFoundation as AV
            device = AV.AVCaptureDevice.defaultDeviceWithMediaType_(AV.AVMediaTypeVideo)
        except Exception as exc:                      # 没装桥接包
            self.skipTest("AVFoundation 不可用：%r" % (exc,))
        if device is None:
            self.skipTest("这台机器没有摄像头；CI 与虚拟机属于这种情况，不算通过也不算失败")
        ranges = presence._supported_fps_ranges(device)  # discipline-ok(R5): 要核对的就是这个私有读法与真桥接的形状
        self.assertIsNotNone(ranges, "真设备读不出帧率区间：桥接形状变了，_apply_power_profile 会退回旧路径")
        for lo, hi in ranges:
            self.assertIsInstance(lo, float)
            self.assertLessEqual(lo, hi)


if __name__ == "__main__":
    unittest.main()
