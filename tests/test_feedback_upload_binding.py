"""Nothing unpreviewed may leave the client, and receipts must bind GitHub identity."""
import json
import tempfile
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from tests._base import SFCase
from tests.test_feedback_pipeline import MemoryGitHub, snapshot
from sf_feedback import FeedbackProtocolError, build_archive, make_envelope
from sf_feedback_capture import CONSENT_VERSION, package_files, read_upload_archive, validate_upload_receipt
from sf_feedback_github import GitHubInbox
from sf_feedback_outbox import FeedbackOutbox
from sf_feedback_transport import submit_https, submit_pending


class UploadBindingTests(SFCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "valid.zip"
        build_archive(self.path, package_files(snapshot(), "synthetic only"),
                      app_version="1.4.96", bundle_id="com.skyrocket.app", run_id="abcdef")
        self.envelope = make_envelope(archive_path=self.path, app_version="1.4.96",
                                      outbox_id="a" * 32, consent_policy_version=CONSENT_VERSION)

    def test_unapproved_attachment_never_reaches_the_network(self):
        path = self.root / "extra.zip"
        build_archive(path, {**package_files(snapshot(), "visible"), "extra.txt": "not previewed"},
                      app_version="1.4.96", bundle_id="com.skyrocket.app", run_id="abcdef")
        envelope = make_envelope(archive_path=path, app_version="1.4.96",
                                 outbox_id="b" * 32, consent_policy_version=CONSENT_VERSION)
        with patch("sf_feedback_transport.build_opener") as opened:
            with self.assertRaises(FeedbackProtocolError):
                submit_https("https://synthetic.invalid/v1/feedback", envelope, path)
            opened.assert_not_called()

    def test_forged_repository_or_case_never_becomes_a_successful_local_receipt(self):
        for change in ({"repository": "unapproved/repository"}, {"case_id": "feedback-" + "0" * 32}):
            with self.subTest(change=change):
                index = len(list(self.root.glob("queue-*.sqlite3")))
                outbox = FeedbackOutbox(self.root / ("queue-%d.sqlite3" % index), self.root / ("objects-%d" % index))
                row = outbox.enqueue(self.path, app_version="1.4.96", consent_policy_version=CONSENT_VERSION)
                receipt = GitHubInbox(MemoryGitHub()).accept(row["envelope"], self.path.read_bytes())
                with self.assertRaises(FeedbackProtocolError):
                    submit_pending(outbox, row["outbox_id"], "https://synthetic.invalid/v1/feedback",
                                   sender=lambda *args: {**receipt, **change})
                self.assertNotEqual(outbox.get(row["outbox_id"])["status"], "receipt_persisted")

    def test_https_rejects_receipt_with_correct_hash_but_foreign_repository(self):
        receipt = GitHubInbox(MemoryGitHub()).accept(self.envelope, self.path.read_bytes())
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({**receipt, "repository": "unapproved/repository"}).encode()
        opener = Mock()
        opener.open.return_value = response
        with patch("sf_feedback_transport.build_opener", return_value=opener):
            with self.assertRaises(FeedbackProtocolError):
                submit_https("https://synthetic.invalid/v1/feedback", self.envelope, self.path)

    def rewrite_manifest(self, changes):
        with zipfile.ZipFile(self.path) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(files["manifest.json"])
        manifest.update(changes)
        files["manifest.json"] = json.dumps(manifest).encode()
        path = self.root / "mutated.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name, body in files.items():
                archive.writestr(name, body)
        return path

    def test_unknown_manifest_metadata_and_mismatched_identity_are_refused(self):
        for changes in ({"unexpected": "unpreviewed metadata"}, {"run_id": "111111"},
                        {"schema_version": True}, {"diagnostic_payload_version": True},
                        {"capture_started_at": "invalid timestamp"}, {"omitted": ["unexpected"]}):
            with self.subTest(changes=changes), self.assertRaises(FeedbackProtocolError):
                read_upload_archive(self.rewrite_manifest(changes))

    def test_envelope_whitelist_identity_policy_size_and_hash_are_checked(self):
        for changes in ({"unexpected": "unpreviewed"}, {"schema_version": True},
                        {"diagnostic_payload_version": True}, {"idempotency_key": "invalid"},
                        {"idempotency_key": int("1" * 32)}, {"outbox_id": int("1" * 32)},
                        {"outbox_id": "invalid"}, {"consent_policy_version": "old-policy"},
                        {"app_version": "1.4.95"}, {"size_bytes": True}, {"size_bytes": 1},
                        {"archive_sha256": "0" * 64}):
            with self.subTest(changes=changes), self.assertRaises(FeedbackProtocolError):
                read_upload_archive(self.path, {**self.envelope, **changes})

    def test_boolean_file_length_is_not_an_integer_metadata_value(self):
        self.path = self.root / "one-character.zip"
        build_archive(self.path, package_files(snapshot(), "z"), app_version="1.4.96",
                      bundle_id="com.skyrocket.app", run_id="abcdef")
        with zipfile.ZipFile(self.path) as archive:
            entries = json.loads(archive.read("manifest.json"))["files"]
        for entry in entries:
            if entry["name"] == "problem.txt":
                entry["size_bytes"] = True
        with self.assertRaises(FeedbackProtocolError):
            read_upload_archive(self.rewrite_manifest({"files": entries}))

    def test_member_sizes_and_description_codepoints_are_bounded(self):
        base = package_files(snapshot(), "valid")
        for files in ({**base, "state.json": base["state.json"] + " " * (48 * 1024)},
                      {**base, "problem.txt": "x" * 4001}):
            path = self.root / "over-limit.zip"
            build_archive(path, files, app_version="1.4.96", bundle_id="com.skyrocket.app", run_id="abcdef")
            with self.assertRaises(FeedbackProtocolError):
                read_upload_archive(path)

    def test_container_limit_and_post_validation_read_change_never_send(self):
        path = self.root / "oversized.zip"
        path.write_bytes(b"x" * (256 * 1024 + 1) + self.path.read_bytes())
        envelope = make_envelope(archive_path=path, app_version="1.4.96", outbox_id="b" * 32,
                                 consent_policy_version=CONSENT_VERSION)
        with patch("sf_feedback_transport.build_opener") as opened:
            with self.assertRaises(FeedbackProtocolError):
                submit_https("https://synthetic.invalid", envelope, path)
            opened.assert_not_called()
        with patch("sf_feedback_transport.json.dumps", return_value="x" * (64 * 1024 + 1)), \
                patch("sf_feedback_transport.build_opener") as opened:
            with self.assertRaises(FeedbackProtocolError):
                submit_https("https://synthetic.invalid", self.envelope, self.path)
            opened.assert_not_called()
        body = self.path.read_bytes()
        with patch.object(Path, "read_bytes", side_effect=[body, body + b"changed"]), \
                patch("sf_feedback_transport.build_opener") as opened:
            with self.assertRaises(FeedbackProtocolError):
                submit_https("https://synthetic.invalid", self.envelope, self.path)
            opened.assert_not_called()

    def test_cached_receipts_are_not_exempt_from_identity_and_archive_checks(self):
        outbox = FeedbackOutbox(self.root / "cached.sqlite3", self.root / "cached-objects")
        row = outbox.enqueue(self.path, app_version="1.4.96", consent_policy_version=CONSENT_VERSION)
        receipt = GitHubInbox(MemoryGitHub()).accept(row["envelope"], self.path.read_bytes())
        outbox.transition(row["outbox_id"], "submitting")
        outbox.persist_receipt(row["outbox_id"], {**receipt, "repository": "unapproved/repository"})
        sender = Mock()
        with self.assertRaises(FeedbackProtocolError):
            submit_pending(outbox, row["outbox_id"], "https://synthetic.invalid", sender=sender)
        sender.assert_not_called()

    def test_github_adapter_binds_positive_asset_id_to_the_actual_stored_zip(self):
        api = MemoryGitHub()
        inbox = GitHubInbox(api)
        receipt = inbox.accept(self.envelope, self.path.read_bytes())
        asset = next(item for item in api.rows.values() if item["name"].endswith(".receipt.json"))
        api.bodies[asset["id"]] = json.dumps({**receipt, "asset_id": 999999}).encode()
        with self.assertRaises(FeedbackProtocolError):
            inbox.accept(self.envelope, self.path.read_bytes())

    def test_standalone_row_hash_and_receipt_metadata_cannot_fake_success(self):
        outbox = FeedbackOutbox(self.root / "hash.sqlite3", self.root / "hash-objects")
        row = outbox.enqueue(self.path, app_version="1.4.96", consent_policy_version=CONSENT_VERSION)
        with sqlite3.connect(outbox.database_path) as db:
            db.execute("UPDATE outbox SET archive_sha256=?", ("0" * 64,))
        sender = Mock()
        with self.assertRaises(FeedbackProtocolError):
            submit_pending(outbox, row["outbox_id"], "https://synthetic.invalid", sender=sender)
        sender.assert_not_called()
        receipt = GitHubInbox(MemoryGitHub()).accept(self.envelope, self.path.read_bytes())
        for changes in ({"asset_id": True}, {"asset_id": 0}, {"asset_id": 2 ** 53},
                        {"consent_policy_version": "old-policy"}, {"retention_policy": "automatic"},
                        {"retention_until": "future"}, {"storage_status": "pending"},
                        {"validation_status": "pending"}, {"unexpected": "unpreviewed"},
                        {"received_at": {"invalid": 1}}, {"received_at": "invalid"},
                        {"received_at": "2026-10-19T00:00:00"}):
            with self.subTest(changes=changes), self.assertRaises(FeedbackProtocolError):
                validate_upload_receipt({**receipt, **changes}, self.envelope)
