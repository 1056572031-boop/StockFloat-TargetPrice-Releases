"""同一份字节在本进程里只做一次完整内容校验：令牌只省重复解析，不省任何保护。"""
import io
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

from sf_constants import APP_VERSION, BUNDLE_ID
from sf_diagnostics import DiagnosticArchive
from sf_feedback import FeedbackProtocolError, PrivacyRejected, build_archive, make_envelope
from sf_feedback_bundle import FULL_CONSENT_VERSION
from sf_feedback_bundle import read_diagnostics as real_read_diagnostics
from sf_feedback_capture import package_files, read_upload_archive, verify_upload_content
from sf_feedback_outbox import FeedbackOutbox
from sf_feedback_transport import submit_https, submit_pending
from sf_feedback_ui import FeedbackDialog
from tests._base import SFCase, wait_for
from tests.test_feedback_dialog import _FeedbackCase
from tests.test_feedback_full_bundle import diagnostic_bytes
from tests.test_feedback_pipeline import snapshot

ENDPOINT = "https://receiver.example/v1/feedback"


def full_package(root, name, description="SYNTHETIC TEST ONLY. No real user data."):
    state = snapshot()
    state["app_version"] = APP_VERSION
    state["omitted"] = ["camera", "screenshots"]
    files = package_files(state, "placeholder")
    files["problem.txt"] = description      # 直接写进包里，绕开描述清洗，才能造出“带凭据的包”
    files["diagnostics.zip"] = diagnostic_bytes()
    path = Path(root) / name
    build_archive(path, files, app_version=APP_VERSION, bundle_id=BUNDLE_ID, run_id=state["run_id"],
                  omitted=state["omitted"], truncated=state["truncated"],
                  capture_started_at=state["capture_started_at"], capture_completed_at=state["capture_completed_at"])
    envelope = make_envelope(archive_path=path, app_version=APP_VERSION, outbox_id="a" * 32,
                             consent_policy_version=FULL_CONSENT_VERSION)
    return path, envelope


def counting():
    """诊断包解析照常进行并计数：解析次数就是完整内容校验的次数。"""
    return patch("sf_feedback_capture.read_diagnostics", wraps=real_read_diagnostics)


class TokenTests(SFCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path, self.envelope = full_package(self.root, "valid.zip")

    def test_verifying_parses_the_diagnostics_once_and_a_token_read_parses_nothing(self):
        with counting() as parsed:
            token = verify_upload_content(self.path)
            self.assertEqual(parsed.call_count, 1)
            with_token = read_upload_archive(self.path, self.envelope, verified=token)
            self.assertEqual(parsed.call_count, 1, "带令牌的读取不该再解析一遍诊断包")
        with counting() as parsed:
            plain = read_upload_archive(self.path, self.envelope)
            self.assertEqual(parsed.call_count, 1, "对照：不带令牌必须解析，否则上面的计数什么也没证明")
        self.assertEqual(with_token, plain, "令牌路径与完整校验给出同样的结果")

    def test_a_token_never_vouches_for_different_bytes(self):
        token = verify_upload_content(self.path)
        other, _ = full_package(self.root, "other.zip", description="password=hunter2")
        with self.assertRaises(PrivacyRejected):
            read_upload_archive(other, verified=token)

    def test_a_token_is_not_honoured_when_the_envelope_names_different_bytes(self):
        token = verify_upload_content(self.path)
        other, other_envelope = full_package(self.root, "other-valid.zip", description="a different, harmless note")
        with counting() as parsed:
            with self.assertRaises(PrivacyRejected):
                read_upload_archive(self.path, other_envelope, verified=token)
            self.assertEqual(parsed.call_count, 1, "信封说的不是令牌那份字节：令牌不作数，必须走完整校验")

    def test_content_that_fails_validation_never_gets_a_token(self):
        bad, _ = full_package(self.root, "bad.zip", description="password=hunter2")
        with self.assertRaises(PrivacyRejected):
            verify_upload_content(bad)

    def test_the_envelope_binding_is_recomputed_even_when_a_token_is_present(self):
        token = verify_upload_content(self.path)
        for change in ({"archive_sha256": "0" * 64}, {"size_bytes": self.envelope["size_bytes"] + 1},
                       {"consent_policy_version": "github-private-feedback-v1"}, {"app_version": "0.0.0"}):
            with self.subTest(change=change):
                with self.assertRaises(PrivacyRejected):
                    read_upload_archive(self.path, {**self.envelope, **change}, verified=token)

    def test_bytes_changed_while_verifying_get_no_token(self):
        real = read_upload_archive

        def changes_the_file_after_reading(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            with open(path, "ab") as stream:
                stream.write(b"\0")
            return result

        with patch("sf_feedback_capture.read_upload_archive", side_effect=changes_the_file_after_reading):
            with self.assertRaises(PrivacyRejected):
                verify_upload_content(self.path)

    def test_a_token_hands_out_independent_copies(self):
        token = verify_upload_content(self.path)
        first = read_upload_archive(self.path, self.envelope, verified=token)
        original = (first["state"]["app_version"], first["files"]["problem.txt"])
        first["state"]["app_version"] = "tampered"
        first["files"]["problem.txt"] = "tampered"
        second = read_upload_archive(self.path, self.envelope, verified=token)
        self.assertEqual((second["state"]["app_version"], second["files"]["problem.txt"]), original)

    def test_damaged_bytes_are_refused_whatever_the_token_says(self):
        token = verify_upload_content(self.path)
        damaged = bytearray(self.path.read_bytes())
        damaged[len(damaged) // 2] ^= 0xFF
        self.path.write_bytes(bytes(damaged))
        with self.assertRaises(FeedbackProtocolError):
            read_upload_archive(self.path, self.envelope, verified=token)


class TransportTests(SFCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path, self.envelope = full_package(self.root, "valid.zip")
        self.outbox = FeedbackOutbox(self.root / "outbox.sqlite3", self.root / "objects")
        self.row = self.outbox.enqueue(self.path, app_version=APP_VERSION,
                                       consent_policy_version=FULL_CONSENT_VERSION)

    def test_submit_pending_with_a_token_reparses_nothing_but_still_sends(self):
        token = verify_upload_content(self.path)
        sender = Mock(side_effect=TimeoutError("synthetic"))
        with counting() as parsed, self.assertRaises(TimeoutError):
            submit_pending(self.outbox, self.row["outbox_id"], ENDPOINT, sender=sender, verified=token)
        self.assertEqual(parsed.call_count, 0)
        sender.assert_called_once()

    def test_submit_pending_without_a_token_still_parses_in_full(self):
        sender = Mock(side_effect=TimeoutError("synthetic"))
        with counting() as parsed, self.assertRaises(TimeoutError):
            submit_pending(self.outbox, self.row["outbox_id"], ENDPOINT, sender=sender)
        self.assertEqual(parsed.call_count, 1)

    def test_a_swapped_queue_copy_is_refused_before_the_sender_whatever_the_token_says(self):
        token = verify_upload_content(self.path)
        other, _ = full_package(self.root, "other.zip", description="password=hunter2")
        Path(self.row["archive_path"]).write_bytes(other.read_bytes())
        sender = Mock()
        with self.assertRaises(FeedbackProtocolError):
            submit_pending(self.outbox, self.row["outbox_id"], ENDPOINT, sender=sender, verified=token)
        sender.assert_not_called()

    def test_submit_https_with_a_token_reparses_nothing_and_a_bad_envelope_never_reaches_the_network(self):
        token = verify_upload_content(self.path)
        with patch("sf_feedback_transport.build_opener") as opened, counting() as parsed:
            opened.return_value.open.side_effect = URLError("synthetic offline")
            with self.assertRaises(URLError):
                submit_https(ENDPOINT, self.envelope, self.path, verified=token)
            self.assertEqual(parsed.call_count, 0)
            opened.return_value.open.assert_called_once()
            opened.reset_mock()
            with self.assertRaises(FeedbackProtocolError):
                submit_https(ENDPOINT, {**self.envelope, "archive_sha256": "0" * 64}, self.path, verified=token)
            opened.return_value.open.assert_not_called()


class DialogTests(_FeedbackCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()

    def capture(self, path):
        job = DiagnosticArchive(path)
        with zipfile.ZipFile(io.BytesIO(diagnostic_bytes())) as package:
            for name in package.namelist():
                job.writestr(name, package.read(name))
        return job

    def make(self, directory, sender=None):
        state = snapshot()
        state["app_version"] = APP_VERSION
        return self.track_widget(FeedbackDialog(snapshot=state, storage_dir=directory, endpoint=ENDPOINT,
                                                diagnostic_capture=self.capture, sender=sender))

    def wait_idle(self, dialog):
        def finished():
            self.app.processEvents()
            return dialog.worker is None
        self.assertTrue(wait_for(finished, timeout=5))

    def test_one_click_parses_the_diagnostics_once_and_a_retry_parses_them_in_full_again(self):
        with tempfile.TemporaryDirectory() as directory, patch("sf_system_alert.show_alert", return_value=None):
            dialog = self.make(directory, sender=Mock(side_effect=TimeoutError("synthetic")))
            dialog.description.setPlainText("SYNTHETIC TEST ONLY")
            # 只数 sf_feedback_capture 里的完整校验。界面采集那一段自己会解析一次诊断包（统计截断和问题数），
            # 那是另一回事，不在发送前复核的账上。
            with counting() as full_checks:
                dialog.upload_button.click()
                self.wait_idle(dialog)
                first = full_checks.call_count
                self.assertEqual(dialog.stage, "pending")
                dialog.upload_button.click()
                self.wait_idle(dialog)
                second = full_checks.call_count
            self.assertEqual(first, 1, "一次点击：打包后的完整校验是唯一一次，发送前复核不再解析")
            self.assertEqual(second - first, 1, "重试没有令牌，发送前复核走一次完整校验")

    def test_the_default_sender_gets_the_token_too(self):
        with tempfile.TemporaryDirectory() as directory, patch("sf_system_alert.show_alert", return_value=None):
            dialog = self.make(directory)
            dialog.description.setPlainText("SYNTHETIC TEST ONLY")
            with patch("sf_feedback_transport.build_opener") as opened, counting() as full_checks:
                opened.return_value.open.side_effect = URLError("synthetic offline")
                dialog.upload_button.click()
                self.wait_idle(dialog)
            opened.return_value.open.assert_called_once()
            self.assertEqual(full_checks.call_count, 1, "默认发送也不该再解析第二、第三遍")
