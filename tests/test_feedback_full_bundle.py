"""Full diagnostic parity, bounded parsing and old/new feedback compatibility."""

import io
import hashlib
import json
import struct
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from sf_constants import APP_VERSION, BUNDLE_ID
from sf_diagnostics import DiagnosticArchive
from sf_feedback import MAX_ARCHIVE_BYTES, FeedbackProtocolError, PrivacyRejected, build_archive, make_envelope, validate_archive
from sf_feedback_analyzer import analyze_archive
from sf_feedback_bundle import FULL_CONSENT_VERSION, FULL_V3_CONSENT_VERSION, REQUIRED_DIAGNOSTICS, read_diagnostics
from sf_feedback_versions import PAYLOAD_V3
from sf_feedback_capture import CONSENT_VERSION, capture_snapshot, package_files, read_upload_archive, validate_upload_receipt
from sf_feedback_outbox import FeedbackOutbox
from tests._base import SFCase, wait_for
from tests.test_feedback_pipeline import snapshot
from tests.test_feedback_pipeline import MemoryGitHub
from tests.test_feedback_dialog import _FeedbackCase
from sf_feedback_github import GitHubInbox
from sf_feedback_ui import FeedbackDialog


def diagnostic_bytes(extra=None, issues=None, change_export=None, omit=()):
    files = {"diagnostics.txt": b"SYNTHETIC FULL DIAGNOSTIC TEST ONLY",
             "runtime.json": b'{"net_ok":true,"window":{"width":320},"age":1.5}',
             "debug.log": b"[test] WARNING: synthetic historical failure\n"}
    files.update(extra or {})
    for name in REQUIRED_DIAGNOSTICS - {"export.json"}:
        files.setdefault(name, b"{}")
    for name in omit:
        files.pop(name, None)
    exported = {"version": APP_VERSION, "issues": issues or [],
                "files": [{"name": name, "size_bytes": len(body)} for name, body in files.items()]}
    if change_export:
        change_export(exported)
    files["export.json"] = json.dumps(exported).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return output.getvalue()


def full_packet(root, content=None):
    state = snapshot()
    state["app_version"] = APP_VERSION
    state["omitted"] = ["camera", "screenshots"]
    files = package_files(state, "SYNTHETIC TEST ONLY. No real user data.")
    files["diagnostics.zip"] = diagnostic_bytes() if content is None else content
    path = root / "full-feedback.zip"
    build_archive(path, files, app_version=APP_VERSION, bundle_id=BUNDLE_ID, run_id=state["run_id"],
                  omitted=state["omitted"], truncated=state["truncated"],
                  capture_started_at=state["capture_started_at"], capture_completed_at=state["capture_completed_at"])
    envelope = make_envelope(archive_path=path, app_version=APP_VERSION, outbox_id="a" * 32,
                             consent_policy_version=FULL_CONSENT_VERSION)
    return path, envelope, files["diagnostics.zip"]


def v3_diagnostic_bytes():
    snapshots = {
        "environment.json": {"schema": "environment.v1", "available": True, "data": {}},
        "permissions.json": {"schema": "permissions.v1", "available": False, "reason": "no_bundle"},
        "feedback_preferences.json": {"schema": "feedback_preferences.v1", "available": True, "data": {}},
        "feedback_context.json": {"schema": "feedback_context.v1", "available": True, "data": {}},
    }
    return diagnostic_bytes(
        {name: json.dumps(value, ensure_ascii=False).encode() for name, value in snapshots.items()},
        change_export=lambda exported: exported.update(diagnostic_schema_version=3),
    )


def v3_packet(root, content=None):
    state = snapshot()
    state["app_version"] = APP_VERSION
    state["omitted"] = ["camera", "screenshots"]
    files = package_files(state, "SYNTHETIC V3 TEST ONLY. No real user data.")
    files["diagnostics.zip"] = v3_diagnostic_bytes() if content is None else content
    path = root / "v3-feedback.zip"
    build_archive(path, files, app_version=APP_VERSION, bundle_id=BUNDLE_ID, run_id=state["run_id"],
                  omitted=state["omitted"], truncated=state["truncated"],
                  capture_started_at=state["capture_started_at"], capture_completed_at=state["capture_completed_at"],
                  diagnostic_payload_version=PAYLOAD_V3)
    envelope = make_envelope(archive_path=path, app_version=APP_VERSION, outbox_id="b" * 32,
                             consent_policy_version=FULL_V3_CONSENT_VERSION)
    return path, envelope


class FullDiagnosticProtocolTests(SFCase):
    def test_v3_full_bundle_is_accepted_with_explicit_unavailable_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            path, envelope = v3_packet(Path(directory))
            packet = read_upload_archive(path, envelope)
            self.assertEqual(packet["manifest"]["diagnostic_payload_version"], PAYLOAD_V3)
            diagnostics = read_diagnostics(packet["files"]["diagnostics.zip"])
            self.assertEqual(diagnostics["export.json"]["diagnostic_schema_version"], 3)
            self.assertEqual(diagnostics["permissions.json"], {
                "schema": "permissions.v1", "available": False, "reason": "no_bundle"})

    def test_v3_policy_and_inner_schema_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, envelope = v3_packet(root)
            with self.assertRaises(PrivacyRejected):
                read_upload_archive(path, {**envelope, "consent_policy_version": FULL_CONSENT_VERSION})
            v2_path, v2_envelope, _ = full_packet(root)
            with self.assertRaises(PrivacyRejected):
                read_upload_archive(v2_path, {**v2_envelope, "diagnostic_payload_version": PAYLOAD_V3,
                                               "consent_policy_version": FULL_V3_CONSENT_VERSION})

    def test_legacy_declaration_disagreement_remains_readable_but_never_reaches_http(self):
        from sf_feedback_transport import submit_https

        with tempfile.TemporaryDirectory() as directory:
            state = snapshot()
            path = Path(directory) / "historical.zip"
            build_archive(path, package_files(state, "historical synthetic archive"),
                          app_version=state["app_version"], bundle_id=BUNDLE_ID, run_id=state["run_id"],
                          capture_started_at=state["capture_started_at"], capture_completed_at=state["capture_completed_at"])
            envelope = make_envelope(archive_path=path, app_version=state["app_version"], outbox_id="a" * 32,
                                     consent_policy_version=CONSENT_VERSION)
            original = path.read_bytes()
            packet = read_upload_archive(path, envelope)
            self.assertEqual(packet["manifest"]["omitted"], [])
            self.assertEqual(packet["state"]["omitted"], ["raw_logs"])
            with patch("sf_feedback_transport.build_opener") as opened:
                with self.assertRaises(FeedbackProtocolError):
                    submit_https("https://synthetic.invalid/v1/feedback", envelope, path)
                opened.assert_not_called()
            self.assertEqual(path.read_bytes(), original)

    def test_raw_zero_byte_member_names_are_rejected_before_name_truncation(self):
        class RawNameInfo(zipfile.ZipInfo):
            def _encodeFilenameFlags(self):
                return self.orig_filename.encode(), self.flag_bits

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = diagnostic_bytes()
            with zipfile.ZipFile(io.BytesIO(original)) as package:
                files = {name: package.read(name) for name in package.namelist()}
            forged = io.BytesIO()
            with zipfile.ZipFile(forged, "w") as package:
                for name, body in files.items():
                    info = RawNameInfo(name + "\x00hidden") if name == "runtime.json" else name
                    package.writestr(info, body)
            with self.assertRaises(PrivacyRejected):
                read_diagnostics(forged.getvalue())
            path, _, _ = full_packet(root)
            with zipfile.ZipFile(path) as package:
                outer = {name: package.read(name) for name in package.namelist()}
            with zipfile.ZipFile(path, "w") as package:
                for name, body in outer.items():
                    package.writestr(RawNameInfo(name + "\x00hidden") if name == "problem.txt" else name, body)
            with self.assertRaises(FeedbackProtocolError):
                validate_archive(path)

    def test_inventory_names_and_byte_counts_have_strict_types(self):
        for value in (True, 1.0, [], None):
            with self.subTest(value=value):
                def malformed(exported):
                    item = next(item for item in exported["files"] if item["name"] == "runtime.json")
                    item["name" if isinstance(value, list) or value is None else "size_bytes"] = value
                with self.assertRaises(PrivacyRejected):
                    read_diagnostics(diagnostic_bytes({"runtime.json": b"0"}, change_export=malformed))

    def test_full_preflight_matches_receiver_storage_and_metadata_limits(self):
        from sf_feedback_transport import submit_https

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, _ = full_packet(root)
            with zipfile.ZipFile(path) as archive:
                files = {name: archive.read(name) for name in archive.namelist()}
            for changed in ("diagnostics.zip", "manifest.json", "state.json"):
                with self.subTest(changed=changed):
                    forged = root / (changed + ".zip")
                    modified = dict(files)
                    if changed == "state.json":
                        modified[changed] += b" " * (32 * 1024 + 1 - len(modified[changed]))
                        manifest = json.loads(modified["manifest.json"])
                        for item in manifest["files"]:
                            if item["name"] == changed:
                                item.update(size_bytes=len(modified[changed]), sha256=hashlib.sha256(modified[changed]).hexdigest())
                        modified["manifest.json"] = json.dumps(manifest).encode()
                    if changed == "manifest.json":
                        modified[changed] += b" " * (16 * 1024 + 1 - len(modified[changed]))
                    with zipfile.ZipFile(forged, "w", zipfile.ZIP_DEFLATED) as archive:
                        for name, body in modified.items():
                            compression = zipfile.ZIP_DEFLATED if changed == name == "diagnostics.zip" else (
                                zipfile.ZIP_STORED if name == "diagnostics.zip" else zipfile.ZIP_DEFLATED)
                            archive.writestr(name, body, compress_type=compression)
                    envelope = make_envelope(archive_path=forged, app_version=APP_VERSION, outbox_id="a" * 32,
                                             consent_policy_version=FULL_CONSENT_VERSION)
                    with patch("sf_feedback_transport.build_opener") as opened:
                        with self.assertRaises(PrivacyRejected):
                            submit_https("https://synthetic.invalid/v1/feedback", envelope, forged)
                        opened.assert_not_called()

    def test_incomplete_private_keys_and_quoted_authorization_are_fully_scrubbed(self):
        import sf_diag
        from sf_feedback_capture import clean_description
        from sf_privacy import scrub_secrets

        secret = "controlled-test-secret-value"
        pem = "-----BEGIN " + "PRIVATE KEY-----\n" + secret
        for text in (pem, 'Authorization="Bearer ' + secret, "Authorization='Basic " + secret):
            with self.subTest(text=text):
                self.assertNotIn(secret, clean_description(text))
                self.assertNotIn(secret, sf_diag.scrub_text(text, ()))
        self.assertNotIn(secret, scrub_secrets('Authorization="Bearer ' + secret))
        self.assertNotIn(secret, scrub_secrets("Authorization='Basic " + secret))

    def test_unredacted_sealed_descriptions_are_blocked_before_any_http_request(self):
        from sf_feedback_transport import submit_https

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for full in (False, True):
                state = snapshot()
                state["app_version"] = APP_VERSION
                for text in ("-----BEGIN " + "PRIVATE KEY-----\ncontrolled-test-secret",
                             "Authorization=Bearer controlled-test-secret"):
                    with self.subTest(full=full, text=text):
                        files = package_files(state, "safe description")
                        files["problem.txt"] = text
                        if full:
                            files["diagnostics.zip"] = diagnostic_bytes()
                        path = root / "description.zip"
                        build_archive(path, files, app_version=APP_VERSION, bundle_id=BUNDLE_ID, run_id=state["run_id"],
                                      omitted=state["omitted"], truncated=state["truncated"],
                                      capture_started_at=state["capture_started_at"], capture_completed_at=state["capture_completed_at"])
                        envelope = make_envelope(archive_path=path, app_version=APP_VERSION, outbox_id="a" * 32,
                                                 consent_policy_version=FULL_CONSENT_VERSION if full else CONSENT_VERSION)
                        with patch("sf_feedback_transport.build_opener") as opened:
                            with self.assertRaises(PrivacyRejected):
                                submit_https("https://synthetic.invalid/v1/feedback", envelope, path)
                            opened.assert_not_called()

    def test_conflicting_manifest_and_snapshot_context_is_rejected_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, _ = full_packet(root)
            with zipfile.ZipFile(path) as archive:
                files = {name: archive.read(name) for name in archive.namelist()}
            original = json.loads(files["manifest.json"])
            changes = {"capture_started_at": "2026-10-18T00:00:00Z",
                       "capture_completed_at": "2026-10-19T00:00:01Z",
                       "omitted": ["camera"], "truncated": ["counters"]}
            for field, value in changes.items():
                with self.subTest(field=field):
                    modified = {**original, field: value}
                    forged = root / (field + ".zip")
                    with zipfile.ZipFile(forged, "w", zipfile.ZIP_DEFLATED) as archive:
                        for name, body in files.items():
                            archive.writestr(name, json.dumps(modified).encode() if name == "manifest.json" else body,
                                             compress_type=zipfile.ZIP_STORED if name == "diagnostics.zip" else zipfile.ZIP_DEFLATED)
                    envelope = make_envelope(archive_path=forged, app_version=APP_VERSION, outbox_id="a" * 32,
                                             consent_policy_version=FULL_CONSENT_VERSION)
                    with self.assertRaises(PrivacyRejected):
                        read_upload_archive(forged, envelope)

    def test_full_index_does_not_run_the_legacy_collector_or_claim_unknown_counts(self):
        with patch("sf_feedback_capture.sf_diag.collect", side_effect=RuntimeError("synthetic unavailable")) as collect:
            state = capture_snapshot(None, app_version=APP_VERSION, bundle_id=BUNDLE_ID, run_id="abcdef", index_only=True)
        collect.assert_not_called()
        self.assertNotIn("loaded_modules", state["summary"])
        self.assertNotIn("capture_issue_count", state["summary"])
    def test_container_metadata_limits_are_checked_before_entry_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "legacy.zip"
            build_archive(path, {"state.json": "{}"}, app_version=APP_VERSION, bundle_id=BUNDLE_ID, run_id="abcdef")
            body = path.read_bytes()
            path.write_bytes(b"x" * MAX_ARCHIVE_BYTES + body)
            with self.assertRaisesRegex(FeedbackProtocolError, "legacy archive exceeds"):
                validate_archive(path)
            malformed = bytearray(body)
            trailer = malformed.rfind(b"PK\x05\x06")
            struct.pack_into("<I", malformed, trailer + 12, 256 * 1024 + 1)
            path.write_bytes(malformed)
            with self.assertRaisesRegex(FeedbackProtocolError, "central directory"):
                validate_archive(path)
            with self.assertRaisesRegex(FeedbackProtocolError, "full archive members"):
                build_archive(root / "incomplete.zip", {"diagnostics.zip": b"synthetic"},
                              app_version=APP_VERSION, bundle_id=BUNDLE_ID, run_id="abcdef")

    def test_unapproved_compression_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsupported.zip"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_BZIP2) as archive:
                archive.writestr("state.json", "{}")
            with self.assertRaisesRegex(FeedbackProtocolError, "compression"):
                validate_archive(path)

    def test_trailer_count_cannot_hide_extra_entries_or_a_bad_central_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "legacy.zip"
            build_archive(path, {"state.json": "{}"}, app_version=APP_VERSION, bundle_id=BUNDLE_ID, run_id="abcdef")
            original = path.read_bytes()
            wrong_count = bytearray(original)
            trailer = wrong_count.rfind(b"PK\x05\x06")
            struct.pack_into("<HH", wrong_count, trailer + 8, 1, 1)
            path.write_bytes(wrong_count)
            with patch("sf_feedback.MAX_MEMBER_COUNT", 1), self.assertRaisesRegex(FeedbackProtocolError, "too many members"):
                validate_archive(path)
            invalid = bytearray(original)
            central = invalid.rfind(b"PK\x01\x02")
            invalid[central:central + 4] = b"BAD!"
            path.write_bytes(invalid)
            with self.assertRaisesRegex(FeedbackProtocolError, "not a valid ZIP"):
                validate_archive(path)

    def test_full_zip_bytes_survive_sealing_persistence_and_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, envelope, content = full_packet(root)
            packet = read_upload_archive(path, envelope)
            self.assertEqual(packet["files"]["diagnostics.zip"], content)
            store = FeedbackOutbox(root / "outbox.db", root / "objects")
            row = store.enqueue(path, app_version=APP_VERSION, consent_policy_version=FULL_CONSENT_VERSION)
            persisted = read_upload_archive(row["archive_path"], row["envelope"])
            self.assertEqual(persisted["files"]["diagnostics.zip"], content)
            self.assertEqual(read_diagnostics(content)["runtime.json"]["age"], 1.5)

    def test_new_policy_is_not_silently_applied_to_old_or_incomplete_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, envelope, _content = full_packet(root)
            with self.assertRaises(PrivacyRejected):
                read_upload_archive(path, {**envelope, "consent_policy_version": CONSENT_VERSION})
            with self.assertRaises(PrivacyRejected):
                read_upload_archive(path, {**envelope, "diagnostic_payload_version": 1})
            path, envelope, _content = full_packet(root, b"not a ZIP")
            with self.assertRaises(PrivacyRejected):
                read_upload_archive(path, envelope)

    def test_legacy_packets_cannot_claim_full_attachment_truncation(self):
        for declare_manifest in (False, True):
            with self.subTest(declare_manifest=declare_manifest), tempfile.TemporaryDirectory() as directory:
                state = snapshot()
                state["truncated"] = ["diagnostic_attachments"]
                path = Path(directory) / "legacy.zip"
                build_archive(path, package_files(state, "synthetic"), app_version=state["app_version"],
                              bundle_id=BUNDLE_ID, run_id=state["run_id"],
                              truncated=state["truncated"] if declare_manifest else [])
                with self.assertRaises(PrivacyRejected):
                    read_upload_archive(path)

    def test_export_version_and_receipt_validation_scope_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = diagnostic_bytes(change_export=lambda value: value.update(version="different-source"))
            path, envelope, _content = full_packet(root, content)
            with self.assertRaises(PrivacyRejected):
                read_upload_archive(path, envelope)
            path, envelope, _content = full_packet(root)
            receipt = GitHubInbox(MemoryGitHub()).accept(envelope, path.read_bytes())
            receipt.pop("validation_scope")
            with self.assertRaises(PrivacyRejected):
                validate_upload_receipt(receipt, envelope)
            self.assertEqual(validate_upload_receipt(receipt, envelope, allow_legacy_v2=True)["case_id"],
                             receipt["case_id"])

    def test_saved_copy_hash_mismatch_cannot_be_reported_as_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _envelope, _content = full_packet(root)
            store = FeedbackOutbox(root / "outbox.db", root / "objects")
            wrong = store.objects_dir / "wrong.zip"
            build_archive(wrong, {"state.json": "{}"}, app_version=APP_VERSION, bundle_id=BUNDLE_ID, run_id="abcdef")
            with patch.object(store, "_copy_archive", return_value=wrong), self.assertRaises(FeedbackProtocolError):
                store.enqueue(path, app_version=APP_VERSION, consent_policy_version=FULL_CONSENT_VERSION)
            self.assertEqual(store.pending(), [])
            self.assertTrue(path.is_file())

    def test_unknown_paths_bad_json_and_nonfinite_numbers_are_rejected(self):
        for extra in ({"../escaped.txt": b"no"}, {"payload.sh": b"no"},
                      {"runtime.json": b'{"x":1,"x":2}'}, {"runtime.json": b'{"x":NaN}'},
                      {"runtime.json": b"\xff"}):
            with self.subTest(extra=list(extra)), self.assertRaises(PrivacyRejected):
                read_diagnostics(diagnostic_bytes(extra))

    def test_member_limits_apply_before_inflating_and_inventory_cannot_lie(self):
        with patch("sf_feedback_bundle.MAX_DIAGNOSTIC_EXPANDED", 5), self.assertRaises(PrivacyRejected):
            read_diagnostics(diagnostic_bytes())
        with self.assertRaises(PrivacyRejected):
            read_diagnostics(diagnostic_bytes({"export.json": b'{"issues":[],"files":[]}'}))
        with self.assertRaises(PrivacyRejected):
            read_diagnostics(b"")

    def test_report_reads_full_snapshots_logs_crashes_and_collection_failures(self):
        content = diagnostic_bytes({"crash_reports/Skyrocket-test.ips.txt": b"Exception: synthetic crash\n"},
                                   issues=["calendar.json: synthetic read failure"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _envelope, _content = full_packet(root, content)
            report = analyze_archive(path, root / "reports")
            evidence = report["full_diagnostics"]
            self.assertTrue(evidence["snapshots"]["runtime.json"]["net_ok"])
            self.assertEqual(evidence["text_evidence"]["debug.log"]["matching_line_count"], 1)
            self.assertIn("calendar.json: synthetic read failure", evidence["collection_issues"])
            self.assertIn("完整诊断资料", Path(report["markdown_path"]).read_text())
            self.assertEqual(report["conclusion"], "insufficient_evidence")

    def test_report_excerpts_are_bounded_without_discarding_source_package(self):
        content = diagnostic_bytes({"debug.log": (("ERROR " + "x" * 1100 + "\n") * 50).encode()})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _envelope, _content = full_packet(root, content)
            report = analyze_archive(path, root / "reports")
            excerpt = report["full_diagnostics"]["text_evidence"]["debug.log"]
            self.assertEqual(excerpt["matching_line_count"], 50)
            self.assertEqual(len(excerpt["tail"]), 40)
            self.assertTrue(excerpt["excerpt_truncated"])
            self.assertEqual(read_upload_archive(path)["files"]["diagnostics.zip"], content)

    def test_credentials_and_failed_identity_scrubbing_block_full_upload(self):
        for text in (b"password=synthetic-value", b'password="synthetic-value', ("ghp_" + "A" * 24).encode(),
                     ("-----BEGIN " + "PRIVATE KEY-----").encode()):
            with self.subTest(text_size=len(text)), self.assertRaises(PrivacyRejected):
                read_diagnostics(diagnostic_bytes({"debug.log": text}))
        with self.assertRaises(PrivacyRejected):
            read_diagnostics(diagnostic_bytes(issues=["getpass.getuser: unavailable"]))
        self.assertIn("debug.log", read_diagnostics(diagnostic_bytes({"debug.log": b"password=<REDACTED>"})))
        for status in ("unknown", "no_bundle", "authorized"):
            body = json.dumps({"authorization": status}).encode()
            self.assertIn("notify.json", read_diagnostics(diagnostic_bytes({"notify.json": body})))

    def test_shared_scrubbing_covers_incomplete_quotes_and_private_key_tails(self):
        import sf_diag

        secret = "synthetic-secret-fragment"
        for raw in ('password="' + secret, 'prefix\n' + secret + '\n-----END ' + 'PRIVATE KEY-----'):
            cleaned = sf_diag.scrub_text(raw, ())
            self.assertNotIn(secret, cleaned)
            self.assertIn("<REDACTED>", cleaned)

    def test_malformed_inventory_and_json_resource_limits_are_rejected(self):
        changes = (lambda value: value.update(issues=None), lambda value: value.update(files=[]),
                   lambda value: value["files"][0].update(size_bytes=-1))
        for change in changes:
            with self.subTest(change=change), self.assertRaises(PrivacyRejected):
                read_diagnostics(diagnostic_bytes(change_export=change))
        for body in (b'{"x":1e400}', b'[' * 34 + b'0' + b']' * 34,
                     ('{"x":' + '1' + '0' * 310 + '}').encode(),
                     json.dumps({"x" * 4097: 1}).encode(),
                     json.dumps({str(n): n for n in range(4097)}).encode()):
            with self.subTest(body_size=len(body)), self.assertRaises(PrivacyRejected):
                read_diagnostics(diagnostic_bytes({"runtime.json": body}))

    def test_missing_categories_require_reasons_and_stream_lengths_are_checked(self):
        with self.assertRaises(PrivacyRejected):
            read_diagnostics(diagnostic_bytes(omit=("calendar.json",)))
        files = read_diagnostics(diagnostic_bytes(omit=("calendar.json",), issues=["calendar.json: synthetic failure"]))
        self.assertNotIn("calendar.json", files)
        content = diagnostic_bytes()
        with patch("sf_feedback_bundle.zipfile.ZipFile.open", return_value=io.BytesIO(b"x")), self.assertRaises(PrivacyRejected):
            read_diagnostics(content)
        with patch("sf_feedback_bundle.MAX_DIAGNOSTIC_BYTES", 1), self.assertRaises(PrivacyRejected):
            read_diagnostics(diagnostic_bytes())

    def test_archive_entry_count_duplicates_links_and_missing_overview_are_rejected(self):
        for kind in ("empty", "many", "duplicate", "link", "missing"):
            with self.subTest(kind=kind):
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w") as archive:
                    if kind == "many":
                        for n in range(41):
                            archive.writestr(f"{n}.txt", b"test")
                    elif kind == "duplicate":
                        archive.writestr("runtime.json", b"{}")
                        archive.writestr("RUNTIME.JSON", b"{}")
                    elif kind == "link":
                        info = zipfile.ZipInfo("runtime.json")
                        info.create_system = 3
                        info.external_attr = 0o120777 << 16
                        archive.writestr(info, b"outside")
                    elif kind == "missing":
                        archive.writestr("runtime.json", b"{}")
                with self.assertRaises(PrivacyRejected):
                    read_diagnostics(output.getvalue())


class FullDiagnosticDialogTests(_FeedbackCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()

    def pending(self, root, received=False, payload=2):
        if payload == PAYLOAD_V3:
            path, _envelope = v3_packet(root)
            policy = FULL_V3_CONSENT_VERSION
        else:
            path, _envelope, _content = full_packet(root)
            policy = FULL_CONSENT_VERSION
        store = FeedbackOutbox(root / "outbox.sqlite3", root / "objects")
        row = store.enqueue(path, app_version=APP_VERSION, consent_policy_version=policy)
        if received:
            receipt = GitHubInbox(MemoryGitHub()).accept(row["envelope"], Path(row["archive_path"]).read_bytes())
            store.transition(row["outbox_id"], "submitting")
            store.persist_receipt(row["outbox_id"], receipt)
        return row

    def finish(self, dialog):
        def finished():
            self.app.processEvents()
            return dialog.worker is None
        self.assertTrue(wait_for(finished, timeout=5))

    def test_legacy_context_disagreement_preserves_packet_and_offers_a_new_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = snapshot()
            path = root / "historical.zip"
            build_archive(path, package_files(state, "historical synthetic archive"),
                          app_version=state["app_version"], bundle_id=BUNDLE_ID, run_id=state["run_id"],
                          capture_started_at=state["capture_started_at"], capture_completed_at=state["capture_completed_at"])
            store = FeedbackOutbox(root / "outbox.sqlite3", root / "objects")
            row = store.enqueue(path, app_version=state["app_version"], consent_policy_version=CONSENT_VERSION)
            sealed = Path(row["archive_path"])
            original = sealed.read_bytes()
            sent = []
            dialog = self.track_widget(FeedbackDialog(snapshot=state, storage_dir=root,
                endpoint="https://synthetic.invalid/v1/feedback", sender=lambda *args: sent.append(args)))
            self.assertEqual(dialog.last_error, "local_package_invalid")
            self.assertEqual(dialog.upload_button.text(), "新建反馈")
            dialog.upload_button.click()
            self.assertEqual(dialog.stage, "draft")
            self.assertEqual(sealed.read_bytes(), original)
            self.assertEqual(sent, [])

    def test_disabled_handler_cannot_create_a_worker_or_pending_packet(self):
        with tempfile.TemporaryDirectory() as directory, patch("sf_system_alert.show_alert", return_value=None):
            dialog = self.track_widget(FeedbackDialog(snapshot=snapshot(), storage_dir=directory,
                endpoint="http://not-allowed.example"))
            self.assertFalse(dialog.upload_button.isEnabled())
            dialog._upload()  # discipline-ok(R5): direct handler call verifies the disabled-entry guard
            self.assertIsNone(dialog.worker)
            self.assertEqual(dialog.outbox.pending(), [])

    def test_large_pending_packets_restore_in_background_without_recapturing(self):
        for payload in (2, PAYLOAD_V3):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory, \
                    patch("sf_system_alert.show_alert", return_value=None):
                root = Path(directory)
                row = self.pending(root, payload=payload)
                inbox = GitHubInbox(MemoryGitHub())
                dialog = self.track_widget(FeedbackDialog(snapshot=snapshot(), storage_dir=root,
                    endpoint="https://receiver.example", diagnostic_capture=lambda _: self.fail("pending data recaptured"),
                    sender=lambda _url, envelope, path: inbox.accept(envelope, Path(path).read_bytes())))
                self.finish(dialog)
                self.assertEqual(dialog.current_id, row["outbox_id"])
                self.assertEqual(dialog.stage, "pending")
                self.assertNotIn("diagnostics.zip", dialog.preview_files)
                dialog.upload_button.click()
                self.finish(dialog)
                self.assertEqual(dialog.stage, "received")

    def test_completed_full_receipts_restore_as_received(self):
        with tempfile.TemporaryDirectory() as directory, patch("sf_system_alert.show_alert", return_value=None):
            root = Path(directory)
            self.pending(root, received=True)
            dialog = self.track_widget(FeedbackDialog(snapshot=snapshot(), storage_dir=root, endpoint="https://receiver.example"))
            dialog.history.setCurrentIndex(1)
            self.finish(dialog)
            self.assertEqual(dialog.stage, "received")
            self.assertEqual(dialog.upload_button.text(), "已上传")

    def test_corrupt_full_packet_is_preserved_and_new_feedback_does_not_send(self):
        with tempfile.TemporaryDirectory() as directory, patch("sf_system_alert.show_alert", return_value=None):
            root = Path(directory)
            row = self.pending(root)
            path = Path(row["archive_path"])
            path.write_bytes(b"synthetic corrupt packet")
            sent = []
            dialog = self.track_widget(FeedbackDialog(snapshot=snapshot(), storage_dir=root,
                endpoint="https://receiver.example", sender=lambda *args: sent.append(args)))
            self.finish(dialog)
            self.assertEqual(dialog.last_error, "local_package_invalid")
            self.assertEqual(dialog.upload_button.text(), "新建反馈")
            dialog.upload_button.click()
            self.assertEqual(dialog.stage, "draft")
            self.assertEqual(sent, [])
            self.assertEqual(path.read_bytes(), b"synthetic corrupt packet")
            self.assertEqual(len(dialog.outbox.pending()), 1)

    def test_restore_worker_start_failure_is_visible_and_does_not_discard_packet(self):
        with tempfile.TemporaryDirectory() as directory, patch("sf_system_alert.show_alert", return_value=None):
            root = Path(directory)
            row = self.pending(root)
            with patch("sf_feedback_ui.threading.Thread.start", side_effect=RuntimeError("synthetic thread limit")):
                dialog = self.track_widget(FeedbackDialog(snapshot=snapshot(), storage_dir=root, endpoint="https://receiver.example"))
            self.assertIsNone(dialog.worker)
            self.assertEqual(dialog.last_error, "local_package_invalid")
            self.assertTrue(Path(row["archive_path"]).is_file())

    def test_full_capture_notes_are_reflected_in_the_index_without_losing_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory, patch("sf_system_alert.show_alert", return_value=None):
            automatic = json.dumps({"issues": [], "truncated": ["synthetic field cap"]}).encode()
            content = diagnostic_bytes({"state.json": automatic}, issues=["debug.log: source truncated, synthetic cap"])
            def capture(path):
                job = DiagnosticArchive(path)
                with zipfile.ZipFile(io.BytesIO(content)) as package:
                    for name in package.namelist():
                        job.writestr(name, package.read(name))
                return job
            packets = []
            inbox = GitHubInbox(MemoryGitHub())
            def sender(_url, envelope, path):
                packets.append(read_upload_archive(path, envelope))
                return inbox.accept(envelope, Path(path).read_bytes())
            state = snapshot()
            state["app_version"] = APP_VERSION
            dialog = self.track_widget(FeedbackDialog(snapshot=state, storage_dir=directory,
                endpoint="https://receiver.example", diagnostic_capture=capture, sender=sender))
            dialog.upload_button.click()
            self.finish(dialog)
            self.assertEqual(dialog.stage, "received")
            self.assertEqual(set(packets[0]["state"]["truncated"]), {"automatic_collection", "diagnostic_attachments"})
            self.assertEqual(read_diagnostics(packets[0]["files"]["diagnostics.zip"])["state.json"]["truncated"], ["synthetic field cap"])

    def test_full_capture_charges_main_thread_and_worker_time_to_one_capture_segment(self):
        # 采集的前半（拍快照、建任务）在用户点击后的主线程，后半（写出诊断压缩包）在上传线程。
        # 假时钟让两半各推进固定秒数：只量后台 finish() 会得到 11 秒，漏掉主线程那 5 秒。
        with tempfile.TemporaryDirectory() as directory, patch("sf_system_alert.show_alert", return_value=None):
            now = [1000.0]

            def capture(_context):
                now[0] += 5.0
                job = DiagnosticArchive(None)
                real_finish = job.finish

                def finish():
                    now[0] += 11.0
                    return real_finish()

                job.finish = finish
                with zipfile.ZipFile(io.BytesIO(diagnostic_bytes())) as package:
                    for name in package.namelist():
                        job.writestr(name, package.read(name))
                return job

            inbox = GitHubInbox(MemoryGitHub())
            state = snapshot()
            state["app_version"] = APP_VERSION
            dialog = self.track_widget(FeedbackDialog(snapshot=state, storage_dir=directory,
                endpoint="https://receiver.example", diagnostic_capture=capture,
                sender=lambda _url, envelope, path: inbox.accept(envelope, Path(path).read_bytes())))
            with patch("sf_feedback_ui._clock", lambda: now[0]), patch("sf_feedback_ui.log_line") as log:
                dialog.upload_button.click()
                self.finish(dialog)
            self.assertEqual(dialog.stage, "received")
            timing = dialog.last_upload_timing
            self.assertEqual(timing["capture_ms"], 16000)
            for name in ("pack_ms", "enqueue_ms", "network_ms", "other_ms"):
                self.assertEqual(timing[name], 0, name)
            self.assertEqual(timing["total_ms"], 16000)
            self.assertFalse(timing["resumed"])
            lines = [call.args[0] for call in log.call_args_list if "feedback upload timing:" in call.args[0]]
            self.assertEqual(len(lines), 1)
            self.assertIn("capture_ms=16000 pack_ms=0 enqueue_ms=0 network_ms=0 other_ms=0 total_ms=16000", lines[0])

    def test_upload_and_retry_keep_the_same_complete_capture_and_description(self):
        with tempfile.TemporaryDirectory() as directory, patch("sf_system_alert.show_alert", return_value=None):
            root = Path(directory)
            captures, submitted = [], []
            inbox = GitHubInbox(MemoryGitHub())

            def capture(path):
                captures.append(path)
                job = DiagnosticArchive(path)
                with zipfile.ZipFile(io.BytesIO(diagnostic_bytes())) as package:
                    for name in package.namelist():
                        job.writestr(name, package.read(name))
                return job

            def sender(_url, envelope, archive):
                submitted.append(Path(archive).read_bytes())
                if len(submitted) == 1:
                    raise TimeoutError("synthetic lost response")
                return inbox.accept(envelope, Path(archive).read_bytes())

            state = snapshot()
            state["app_version"] = APP_VERSION
            dialog = self.track_widget(FeedbackDialog(snapshot=state, storage_dir=root,
                endpoint="https://receiver.example", sender=sender, diagnostic_capture=capture))
            dialog.description.setPlainText("SYNTHETIC TEST ONLY. No real user data.")
            def finished():
                self.app.processEvents()
                return dialog.worker is None

            dialog.upload_button.click()
            self.assertTrue(wait_for(finished, timeout=5))
            self.assertEqual(dialog.stage, "pending")
            dialog.upload_button.click()
            self.assertTrue(wait_for(finished, timeout=5))
            self.assertEqual(dialog.stage, "received")
            self.assertEqual(len(captures), 1)
            self.assertEqual(submitted[0], submitted[1])
            self.assertEqual(dialog.outbox.pending()[0]["envelope"]["consent_policy_version"], FULL_CONSENT_VERSION)

    def test_feature_flag_enables_v3_capture_and_policy_together(self):
        with tempfile.TemporaryDirectory() as directory, patch("sf_system_alert.show_alert", return_value=None), \
                patch("sf_feedback_ui.feature_enabled", return_value=True):
            contexts, packets = [], []
            inbox = GitHubInbox(MemoryGitHub())
            def capture(context):
                contexts.append(context)
                job = DiagnosticArchive(str(Path(directory) / "v3-diagnostics.zip"))
                with zipfile.ZipFile(io.BytesIO(v3_diagnostic_bytes())) as package:
                    for name in package.namelist():
                        job.writestr(name, package.read(name))
                return job
            def sender(_url, envelope, path):
                packets.append(read_upload_archive(path, envelope))
                return inbox.accept(envelope, Path(path).read_bytes())
            state = snapshot()
            state["app_version"] = APP_VERSION
            dialog = self.track_widget(FeedbackDialog(snapshot=state, storage_dir=directory,
                endpoint="https://receiver.example", diagnostic_capture=capture, sender=sender))
            self.assertIn("运行环境、权限状态、反馈偏好和本次反馈上下文", dialog.upload_button.toolTip())
            self.assertIn("不采集摄像头画面或截图", dialog.upload_button.toolTip())
            dialog.upload_button.click()
            self.finish(dialog)
            self.assertEqual(contexts, [{"trigger": "manual"}])
            self.assertEqual(packets[0]["manifest"]["diagnostic_payload_version"], PAYLOAD_V3)
            self.assertEqual(dialog.outbox.pending()[0]["envelope"]["consent_policy_version"], FULL_V3_CONSENT_VERSION)

    def test_v3_pending_packet_restores_with_full_upload_disclosure(self):
        with tempfile.TemporaryDirectory() as directory, patch("sf_system_alert.show_alert", return_value=None):
            root = Path(directory)
            path, envelope = v3_packet(root)
            store = FeedbackOutbox(root / "outbox.sqlite3", root / "objects")
            row = store.enqueue(path, app_version=APP_VERSION,
                                consent_policy_version=envelope["consent_policy_version"])
            dialog = self.track_widget(FeedbackDialog(snapshot=snapshot(), storage_dir=root,
                endpoint="https://receiver.example", diagnostic_capture=lambda _: self.fail("pending v3 recaptured")))
            self.finish(dialog)
            self.assertEqual(dialog.current_id, row["outbox_id"])
            self.assertEqual(dialog.payload_version, PAYLOAD_V3)
            self.assertIn("运行环境、权限状态、反馈偏好和本次反馈上下文", dialog.upload_button.toolTip())
            self.assertNotIn("旧版反馈", dialog.upload_button.toolTip())

    def test_failed_full_capture_does_not_fall_back_to_summary_upload(self):
        with tempfile.TemporaryDirectory() as directory, patch("sf_system_alert.show_alert", return_value=None):
            def failed(_path):
                raise OSError("synthetic capture failed")

            sent = []
            dialog = self.track_widget(FeedbackDialog(snapshot=snapshot(), storage_dir=directory,
                endpoint="https://receiver.example", diagnostic_capture=failed,
                sender=lambda *args: sent.append(args)))
            dialog.upload_button.click()
            self.assertIsNone(dialog.worker)
            self.assertEqual(sent, [])
            self.assertEqual(dialog.outbox.pending(), [])

    def test_unsealed_capture_failure_can_be_collected_again_without_losing_description(self):
        with tempfile.TemporaryDirectory() as directory, patch("sf_system_alert.show_alert", return_value=None):
            attempts = []
            inbox = GitHubInbox(MemoryGitHub())

            def capture(path):
                attempts.append(path)
                job = DiagnosticArchive(path)
                with zipfile.ZipFile(io.BytesIO(diagnostic_bytes())) as package:
                    for name in package.namelist():
                        job.writestr(name, package.read(name))
                if len(attempts) == 1:
                    def failed():
                        raise OSError("synthetic read failure")
                    job.defer(failed)
                return job

            state = snapshot()
            state["app_version"] = APP_VERSION
            dialog = self.track_widget(FeedbackDialog(snapshot=state, storage_dir=directory,
                endpoint="https://receiver.example", diagnostic_capture=capture,
                sender=lambda _url, envelope, path: inbox.accept(envelope, Path(path).read_bytes())))
            dialog.description.setPlainText("synthetic retained description")
            def finished():
                self.app.processEvents()
                return dialog.worker is None
            dialog.upload_button.click()
            self.assertTrue(wait_for(finished, timeout=5))
            self.assertEqual(dialog.stage, "draft")
            self.assertEqual(dialog.description.toPlainText(), "synthetic retained description")
            self.assertIsNone(dialog.full_capture)
            dialog.upload_button.click()
            self.assertTrue(wait_for(finished, timeout=5))
            self.assertEqual(dialog.stage, "received")
            self.assertEqual(len(attempts), 2)
