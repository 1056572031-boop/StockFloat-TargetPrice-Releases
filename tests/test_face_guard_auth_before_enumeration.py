# -*- coding: utf-8 -*-
"""人脸守卫启动：先查摄像头授权，再枚举摄像头设备。

背景（2026-10-21）：真实用户机器上守卫开着但摄像头未授权，程序每约 2 秒重试一次启动，
旧顺序是每次都先枚举设备再查授权。启动后约 2.5 秒的第一次枚举，会让 AVFoundation 往主队列
投一个等设备列表的块，实测主线程停 0.2~0.55 秒（不采样的看门狗，基线 3/3 卡）。
没有授权就起不了会话，枚举只是白付代价。

守三件事（按「枚举函数被调了几次」与最终状态这两个可观测行为断言）：
  1. 未授权 / 被拒 / 受限：一次枚举都不许发生，状态与旧行为一致；
  2. 已授权：枚举恰好一次，没摄像头仍报 NO_CAMERA，有摄像头照常建会话；
  3. 读不出授权状态（unsupported）：照旧落到枚举 —— 这是旧行为，不能因调序变成「一律不枚举」。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_presence
from sf_presence import (STATE_DENIED, STATE_NEEDS_AUTH, STATE_NO_CAMERA, STATE_RESTRICTED,
                         STATE_RUNNING, STATE_UNSUPPORTED, FacePresenceMonitor)
from tests._base import SFCase


class AuthBeforeEnumeration(SFCase):

    def setup_monitor(self, status, device=object()):
        """返回 (监控器, 枚举替身, 建会话替身)。授权状态与摄像头设备都由用例决定。"""
        self.track_patch(patch.object(sf_presence, "_load_frameworks",
                                      return_value=({"AVCaptureDevice": object()}, "")))
        self.track_patch(patch.object(sf_presence, "camera_access_status", return_value=status))
        enum = self.started_mock(patch.object(sf_presence, "_camera_devices", return_value=device))
        build = self.started_mock(patch.object(FacePresenceMonitor, "_build_session"))
        return FacePresenceMonitor(), enum, build

    def started_mock(self, patcher):
        """起补丁并返回替身本身。SFCase.track_patch 返回的是补丁对象，不能拿来断言调用次数。"""
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def assert_blocked_without_enumeration(self, status, state):
        mon, enum, build = self.setup_monitor(status)
        self.assertEqual(mon.start(), state, "状态必须原样给界面显示")
        enum.assert_not_called()
        build.assert_not_called()

    def test_not_yet_asked_never_enumerates_cameras(self):
        self.assert_blocked_without_enumeration("undetermined", STATE_NEEDS_AUTH)

    def test_denied_never_enumerates_cameras(self):
        self.assert_blocked_without_enumeration(STATE_DENIED, STATE_DENIED)

    def test_restricted_never_enumerates_cameras(self):
        self.assert_blocked_without_enumeration(STATE_RESTRICTED, STATE_RESTRICTED)

    def test_retries_while_unauthorized_never_enumerate(self):
        """未授权时主窗每约 2 秒重试一次启动：每一次都不许枚举，否则就是一直在付那笔代价。"""
        mon, enum, _build = self.setup_monitor("undetermined")
        for _ in range(5):
            self.assertEqual(mon.start(), STATE_NEEDS_AUTH)
        self.assertEqual(enum.call_count, 0, "重试里仍在枚举摄像头")

    def test_authorized_without_camera_reports_no_camera_after_one_enumeration(self):
        mon, enum, build = self.setup_monitor("authorized", device=None)
        self.assertEqual(mon.start(), STATE_NO_CAMERA)
        self.assertEqual(enum.call_count, 1)
        build.assert_not_called()

    def test_authorized_with_camera_enumerates_once_then_builds_the_session(self):
        camera = object()
        mon, enum, build = self.setup_monitor("authorized", device=camera)
        self.assertEqual(mon.start(), STATE_RUNNING)
        self.assertEqual(enum.call_count, 1)
        self.assertEqual(build.call_count, 1)
        self.assertIs(build.call_args.args[1], camera, "建会话用的不是枚举到的那台摄像头")

    def test_unreadable_status_still_falls_through_to_enumeration(self):
        """读不出授权状态是旧行为的放行方向：不能因为调序变成一律不枚举、守卫永远起不来。"""
        mon, enum, _build = self.setup_monitor(STATE_UNSUPPORTED, device=None)
        self.assertEqual(mon.start(), STATE_NO_CAMERA)
        self.assertEqual(enum.call_count, 1)


if __name__ == "__main__":
    unittest.main()
