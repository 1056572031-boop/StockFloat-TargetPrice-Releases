"""The bundled hardware check cannot label missing permission or stopped frames as success."""
from sf_camera_check import judge_camera_result
from tests._base import SFCase


class CameraCheckQualityTests(SFCase):
    def test_permission_frames_processing_and_stop_are_all_required(self):
        self.assertEqual(judge_camera_result("authorized", 10, 0, True), "passed")
        self.assertEqual(judge_camera_result("undetermined", 10, 0, True), "blocked")
        self.assertEqual(judge_camera_result("denied", 10, 0, True), "blocked")
        self.assertEqual(judge_camera_result("authorized", 0, 0, True), "failed")
        self.assertEqual(judge_camera_result("authorized", 10, 1, True), "failed")
        self.assertEqual(judge_camera_result("authorized", 10, 0, False), "failed")
