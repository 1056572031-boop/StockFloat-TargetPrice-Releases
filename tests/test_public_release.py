"""Behavioral release-document and public-history guard tests; never use real network."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from unittest.mock import patch

from tests._base import SFCase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import public_docs_check as docs  # noqa: E402
import public_release as public  # noqa: E402


class PublicReleaseCase(SFCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="sfpub_test_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repo"
        self.root.mkdir()
        for name in public.DOCS + ("release_notes.txt", "fonts/OFL-D-DIN.txt"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)

    def init_repo(self):
        public.run(["git", "init", "-q"], self.root)
        public.run(["git", "add", "."], self.root)
        public.run(["git", "commit", "-qm", "fixture"], self.root)

    def document_problems(self):
        return docs.problems(self.root, docs.APP_VERSION)

    def test_current_documents_and_cli_pass(self):
        self.assertEqual(self.document_problems(), [])
        self.assertEqual(docs.main(["--root", str(self.root)]), 0)

    def test_selftest_rejects_real_missing_and_stale_documents(self):
        self.assertEqual(docs.self_test(), 0)
        with patch.object(docs, "problems", return_value=[]):
            self.assertEqual(docs.self_test(), 1)

    def test_old_readme_and_stale_change_text_are_rejected(self):
        readme = self.root / "README.md"
        body = readme.read_text()
        readme.write_text(body.replace("v" + docs.APP_VERSION, "v0.0.0", 1))
        self.assertTrue(self.document_problems())
        notes = (self.root / "release_notes.txt").read_text()
        section = re.search(r"^V%s\s+[^\n]+\n(.*?)(?=^V\d+\.\d+\.\d+\s|\Z)"
                            % re.escape(docs.APP_VERSION), notes, re.M | re.S)
        self.assertIsNotNone(section)
        change_text = section[1].strip()
        self.assertIn(change_text, body)
        stale_body = body.replace(change_text, "old changes", 1)
        self.assertNotEqual(stale_body, body, "The stale-change injection must modify the public document")
        readme.write_text(stale_body)
        self.assertTrue(self.document_problems())

    def test_missing_and_empty_release_notes_fail(self):
        notes = self.root / "release_notes.txt"
        notes.unlink()
        self.assertTrue(self.document_problems())
        notes.write_text("V%s 发布说明\n" % docs.APP_VERSION)
        self.assertTrue(self.document_problems())

    def test_cli_fails_on_missing_license_and_unknown_arguments(self):
        (self.root / "LICENSE").unlink()
        self.assertEqual(docs.main(["--root", str(self.root)]), 1)
        with self.assertRaises(SystemExit) as raised:
            docs.main(["--unknown"])
        self.assertEqual(raised.exception.code, 2)

    def test_git_subprocesses_clear_hook_environment(self):
        with patch.dict(os.environ, {"GIT_DIR": "/bad", "GIT_INDEX_FILE": "/bad-index"}):
            self.assertNotIn("GIT_DIR", public.git_env())
            self.assertNotIn("GIT_INDEX_FILE", public.git_env())
            self.init_repo()
        self.assertTrue((self.root / ".git").is_dir())

    def test_export_excludes_internal_docs_and_cleans_paths(self):
        home = str(Path.home())
        (self.root / "example.py").write_text("# %s/project .private_tools\n" % home)
        (self.root / "AGENTS.md").write_text("private internal instructions")
        reports = self.root / "outputs"
        reports.mkdir()
        (reports / "report.md").write_text("internal report")
        self.init_repo()
        out = self.root.parent / "snapshot"
        public.export_source(out, self.root)
        self.assertFalse((out / "AGENTS.md").exists())
        self.assertFalse((out / "outputs").exists())
        self.assertEqual((out / "example.py").read_text(), "# /Users/example/project .private_tools\n")
        self.assertEqual(public.run(["git", "rev-list", "--count", "HEAD"], out).strip(), "1")
        exported_problems = docs.problems(out, docs.APP_VERSION)
        self.assertEqual(exported_problems, [])

    def test_export_rejects_uncommitted_code_but_carries_generated_guide(self):
        self.init_repo()
        (self.root / "new.py").write_text("print('uncommitted')")
        with self.assertRaises(ValueError):
            public.export_source(self.root.parent / "rejected", self.root)
        (self.root / "new.py").unlink()
        guide = self.root / "docs/INSTALL.md"
        with guide.open("a") as stream:
            stream.write("\nNew generated checksum explanation.\n")
        out = self.root.parent / "accepted"
        public.export_source(out, self.root)
        self.assertEqual((out / "docs/INSTALL.md").read_bytes(), guide.read_bytes())

    def test_export_rejects_missing_security_and_personal_contact(self):
        self.init_repo()
        (self.root / "SECURITY.md").write_text("incomplete")
        with self.assertRaises(ValueError):
            public.export_source(self.root.parent / "missing", self.root)

    def test_history_audit_rejects_old_secrets_even_after_they_are_removed(self):
        self.assertEqual(public.self_test(), 0)

    def test_empty_history_is_not_treated_as_clean(self):
        public.run(["git", "init", "-q"], self.root)
        with self.assertRaises(ValueError):
            public.audit_history(self.root, "/Users/" + "fixture-owner")

    def test_private_history_entries_are_rejected(self):
        (self.root / "config.json").write_text("{}")
        self.init_repo()
        with self.assertRaises(ValueError):
            public.audit_history(self.root, "/Users/" + "fixture-owner")

    def test_exact_signing_material_is_detected_even_in_binary_blob(self):
        secret = bytes(range(32))
        self.assertIn("release signing secret", public.privacy_problems(b"\0" + secret, "unused", [secret]))
        key = self.root / ".private_tools/update_signing_key.txt"
        key.parent.mkdir()
        key.write_text(secret.hex())
        self.assertIn(secret, public.signing_secrets(self.root))
        import base64
        key.write_bytes(base64.b64encode(secret))
        self.assertIn(secret, public.signing_secrets(self.root))

    def test_namespace_exemption_does_not_allow_bare_numeric_identifier(self):
        owner = public.REPO.split("/", 1)[0].encode()
        number = owner.split(b"-", 1)[0]
        self.assertEqual(public.privacy_problems(public.REPO.encode(), "unused"), [])
        self.assertTrue(public.privacy_problems(number, "unused"))

    def test_only_approved_support_mailbox_is_public(self):
        self.assertEqual(public.privacy_problems(b"1056572031@qq.com", "unused"), [])
        other_mail = b"someone" + b"@" + b"qq.com"
        self.assertIn("personal contact", public.privacy_problems(other_mail, "unused"))
        number = public.REPO.split("/", 1)[0].split("-", 1)[0].encode()
        self.assertTrue(public.privacy_problems(number, "unused"))

    def test_public_push_uses_only_the_existing_public_parent(self):
        calls = []

        def command(args, *extra, **kwargs):
            calls.append(args)
            if "ls-remote" in args:
                return "abc refs/heads/public-source\n"
            if "commit-tree" in args or "rev-parse" in args:
                return "public-commit\n"
            return ""

        with patch.object(public, "run", side_effect=command), \
                patch.object(public, "audit_history") as audit:
            self.assertEqual(public.push_source(self.root), "public-commit")
        self.assertTrue(any("FETCH_HEAD" in call and "commit-tree" in call for call in calls))
        self.assertFalse(any("--force" in call for call in calls))
        audit.assert_called_once()

    def test_public_first_push_has_no_private_parent(self):
        calls = []

        def command(args, *extra, **kwargs):
            calls.append(args)
            return "commit\n" if "rev-parse" in args else ""

        with patch.object(public, "run", side_effect=command), patch.object(public, "audit_history"):
            public.push_source(self.root)
        self.assertFalse(any("commit-tree" in call or "fetch" in call for call in calls))

    def test_upload_does_not_overwrite_same_name_asset(self):
        asset = self.root / "asset.zip"
        asset.write_bytes(b"example")
        digest = "sha256:" + hashlib.sha256(asset.read_bytes()).hexdigest()
        response = {"assets": [{"name": asset.name, "digest": digest}]}
        with patch.object(public, "run", return_value=json.dumps(response)) as command:
            public.upload_asset(asset)
        command.assert_called_once()
        response["assets"][0]["digest"] = "sha256:different"
        with patch.object(public, "run", return_value=json.dumps(response)), self.assertRaises(ValueError):
            public.upload_asset(asset)

    def test_upload_new_asset_and_cli_source_only(self):
        asset = self.root / "asset.zip"
        asset.write_bytes(b"example")
        with patch.object(public, "run", side_effect=[json.dumps({"assets": []}), ""]) as command:
            public.upload_asset(asset)
        self.assertIn("upload", command.call_args.args[0])
        with patch.object(public, "ROOT", self.root), patch.object(public, "export_source") as export, \
                patch.object(public, "push_source", return_value="commit"), \
                patch.object(public, "upload_asset") as upload:
            self.assertEqual(public.main(["--publish", "--source-only"]), 0)
        export.assert_called_once()
        upload.assert_not_called()

    def test_cli_selftest_and_audit_modes(self):
        with patch.object(public, "self_test", return_value=0) as check:
            self.assertEqual(public.main(["--self-test"]), 0)
        check.assert_called_once()
        self.init_repo()
        with patch.object(public, "ROOT", self.root):
            self.assertEqual(public.main(["--audit", str(self.root)]), 0)

    def test_main_publishes_source_and_dmg_only_after_audited_export(self):
        application = self.root / "build/update_assets" / ("V" + public.APP_VERSION) / (
            "Skyrocket-%s-macos.zip" % public.APP_VERSION)
        application.parent.mkdir(parents=True)
        application.write_bytes(b"application")
        (self.root / "build" / ("Skyrocket-V%s.dmg" % public.APP_VERSION)).write_bytes(b"dmg")
        def archive_command(args, *extra, **kwargs):
            Path(args[args.index("-o") + 1]).write_bytes(b"source")

        with patch.object(public, "ROOT", self.root), patch.object(public, "export_source") as export, \
                patch.object(public, "push_source", return_value="commit"), \
                patch.object(public, "run", side_effect=archive_command) as command, \
                patch.object(public, "upload_asset") as upload, \
                patch.object(public, "publish_documents") as published_docs:
            self.assertEqual(public.main(["--publish"]), 0)
        export.assert_called_once()
        self.assertEqual(upload.call_count, 3)
        self.assertIn("archive", command.call_args.args[0])
        published_docs.assert_called_once()

    def test_main_stops_after_export_push_or_first_upload_failure(self):
        failures = ("export", "push", "upload")
        for failure in failures:
            with self.subTest(failure=failure):
                calls = []
                application = self.root / "build/update_assets" / ("V" + public.APP_VERSION) / (
                    "Skyrocket-%s-macos.zip" % public.APP_VERSION)
                application.parent.mkdir(parents=True, exist_ok=True)
                application.write_bytes(b"application")
                (self.root / "build" / ("Skyrocket-V%s.dmg" % public.APP_VERSION)).write_bytes(b"dmg")

                def archive_command(args, *extra, **kwargs):
                    calls.append("archive")
                    Path(args[args.index("-o") + 1]).write_bytes(b"source")

                def fail_export(*args, **kwargs):
                    calls.append("export")
                    raise ValueError("export failed")

                def fail_push(*args, **kwargs):
                    calls.append("push")
                    raise ValueError("push failed")

                def fail_upload(*args, **kwargs):
                    calls.append("upload")
                    raise ValueError("upload failed")

                export = fail_export if failure == "export" else lambda *a, **k: calls.append("export")
                push = fail_push if failure == "push" else lambda *a, **k: (calls.append("push") or "commit")
                upload = fail_upload if failure == "upload" else lambda *a, **k: calls.append("upload")
                with patch.object(public, "ROOT", self.root), \
                        patch.object(public, "export_source", side_effect=export), \
                        patch.object(public, "push_source", side_effect=push), \
                        patch.object(public, "run", side_effect=archive_command), \
                        patch.object(public, "upload_asset", side_effect=upload), \
                        patch.object(public, "publish_documents") as published_docs:
                    with self.assertRaises(ValueError):
                        public.main(["--publish"])
                self.assertIn(failure, calls)
                failure_index = calls.index(failure)
                if failure == "export":
                    self.assertEqual(failure_index, 0)
                    self.assertNotIn("push", calls)
                    self.assertNotIn("upload", calls)
                    self.assertNotIn("archive", calls)
                elif failure == "push":
                    self.assertEqual(calls[:2], ["export", "push"])
                    self.assertNotIn("upload", calls)
                    self.assertNotIn("archive", calls)
                else:
                    self.assertEqual(calls[:2], ["export", "push"])
                    self.assertEqual(calls.count("upload"), 1)
                    self.assertNotIn("publish", calls)
                    published_docs.assert_not_called()

    def test_default_branch_docs_update_never_stages_the_manifest(self):
        calls = []

        def command(args, *extra, **kwargs):
            calls.append(args)
            if "clone" in args:
                checkout = Path(args[-1])
                checkout.mkdir()
                (checkout / "Skyrocket-update.json").write_text("unchanged manifest")
            if "diff" in args:
                return "README.md\n"
            return ""

        with patch.object(public, "run", side_effect=command):
            public.publish_documents(self.root)
        stage = next(call for call in calls if "add" in call)
        self.assertNotIn("Skyrocket-update.json", stage)
        self.assertFalse(any("--force" in call for call in calls))
        self.assertTrue(any("push" in call for call in calls))
