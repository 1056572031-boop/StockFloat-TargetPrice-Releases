"""The bundled hardware check cannot label missing permission or stopped frames as success."""
import sf_constants as C
from sf_camera_check import is_installed_app_identity, judge_camera_result
from tests._base import SFCase


class CameraCheckQualityTests(SFCase):
    def test_permission_frames_processing_and_stop_are_all_required(self):
        self.assertEqual(judge_camera_result("authorized", 10, 0, True), "passed")
        self.assertEqual(judge_camera_result("undetermined", 10, 0, True), "blocked")
        self.assertEqual(judge_camera_result("denied", 10, 0, True), "blocked")
        self.assertEqual(judge_camera_result("authorized", 0, 0, True), "failed")
        self.assertEqual(judge_camera_result("authorized", 10, 1, True), "failed")
        self.assertEqual(judge_camera_result("authorized", 10, 0, False), "failed")


class CameraCheckIdentityTests(SFCase):
    """只有装机版的身份才允许跑摄像头核验；名单必须跟着 sf_constants 走，不再各存一份。"""

    def test_current_and_legacy_bundle_ids_are_accepted(self):
        for bundle_id in (C.BUNDLE_ID, *C.LEGACY_BUNDLE_IDS):
            self.assertTrue(is_installed_app_identity(bundle_id), bundle_id)

    def test_other_or_missing_identities_are_refused(self):
        for bundle_id in (None, "", "org.python.python", "com.apple.Terminal"):
            self.assertFalse(is_installed_app_identity(bundle_id), repr(bundle_id))

    def test_main_refuses_to_run_outside_the_installed_app(self):
        """从裸解释器跑（包标识不是本程序）：不开摄像头，写出 blocked 结果并以 2 退出。"""
        import json
        import os
        import tempfile
        from unittest import mock

        import Foundation
        import sf_camera_check
        import sf_presence

        class _FakeBundle:
            @staticmethod
            def mainBundle():
                return _FakeBundle()

            @staticmethod
            def bundleIdentifier():
                return "org.python.python"

        with tempfile.TemporaryDirectory(prefix="sf_camera_check_") as directory:
            out = os.path.join(directory, "result.json")
            with mock.patch.object(Foundation, "NSBundle", _FakeBundle), \
                    mock.patch.object(sf_presence, "camera_access_status",
                                      return_value="undetermined"), \
                    mock.patch.object(sf_presence, "FacePresenceMonitor") as monitor:
                rc = sf_camera_check.main(["--seconds", "1", "--output", out])
            with open(out, encoding="utf-8") as fh:
                result = json.load(fh)
        self.assertEqual(rc, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "requires-installed-app-identity")
        self.assertEqual(result["bundle_id"], "org.python.python")
        monitor.assert_not_called()
