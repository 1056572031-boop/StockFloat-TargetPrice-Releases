"""Behavioral release-document and public-history guard tests; never use real network."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

from tests._base import SFCase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import public_docs_check as docs  # noqa: E402
import sf_constants as C  # noqa: E402
import release_identity  # noqa: E402
import public_release as public  # noqa: E402


class PublicReleaseCase(SFCase):
    def test_current_document_resource_checks_match_the_documents_before_packaging(self):
        import release_feature_check as features

        parsed = features.parse_checks(ROOT / "release_checks.txt")
        self.assertEqual(parsed["errors"], [])
        paths = {"Contents/Resources/docs/README.md": "README.md",
                 "Contents/Resources/docs/INSTALL.md": "docs/INSTALL.md"}
        checks = [spec for spec in parsed["checkfile"] if spec.split("::", 1)[0] in paths]
        self.assertGreaterEqual(len(checks), len(paths))
        app = self.root / "candidate.app"
        for destination, source in paths.items():
            target = app / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / source).read_bytes())
        document_checks = {"checkfile": checks, "forbidfile": []}
        self.assertEqual(features.resource_problems(str(app), document_checks), [])
        (app / "Contents/Resources/docs/README.md").write_text("outdated documents")
        self.assertTrue(features.resource_problems(str(app), document_checks))

    def test_feedback_namespaces_are_exact_and_never_mask_other_secrets(self):
        repo = b"1056572031-boop/Skyrocket-Private-Feedback"
        origin = b"https://skyrocket-feedback-1056572031.pages.dev"
        for value in (repo, origin + b"/v1/feedback", origin + b"/health"):
            with self.subTest(value=value):
                self.assertEqual(public.privacy_problems(value, "unused"), [])
        for value in (repo + b"-evil", b"prefix-" + repo, origin + b".evil/x",
                      origin + b"@evil.invalid", b"http:" + origin[6:],
                      origin + b"/phone/" + repo.split(b"-", 1)[0],
                      repo + b" ghp_" + b"a" * 30,
                      origin + b" " + b"-----BEGIN " + b"PRIVATE KEY-----",
                      origin + b" phone=138" + b"00138000"):
            with self.subTest(value=value):
                self.assertTrue(public.privacy_problems(value, "unused"))
        self.assertTrue(public.privacy_problems(repo, "unused", (repo,)))

    def test_actual_feedback_source_passes_the_public_privacy_guard(self):
        names = ["sf_constants.py", "docs/GITHUB_FEEDBACK.md", "tests/test_public_release.py"]
        names += [str(p.relative_to(ROOT)) for p in ROOT.glob("sf_feedback*.py")]
        names += [str(p.relative_to(ROOT)) for p in (ROOT / "tools").glob("*feedback*.py")]
        names += [str(p.relative_to(ROOT)) for p in (ROOT / "tests").glob("test_feedback*.py")]
        names += [str(p.relative_to(ROOT)) for p in (ROOT / "services/feedback-worker").rglob("*.mjs")
                  if not any(part in ("node_modules", "dist", ".wrangler") for part in p.parts)]
        for name in names:
            with self.subTest(name=name):
                data = public.sanitize((ROOT / name).read_text(), str(Path.home())).encode()
                self.assertEqual(public.privacy_problems(data, str(Path.home())), [])

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

    def test_numbering_length_and_install_artifact_names_fail_before_packaging(self):
        notes = self.root / "release_notes.txt"
        original = notes.read_text()
        # 越界样本按唯一来源 NOTES_MAX_CHARS 推算（上限现为 200），不写死字数：上限再调时本用例跟着走。
        for body in ("2. missing first item", "1. " + "x" * (docs.NOTES_MAX_CHARS + 1)):
            notes.write_text("V%s release\n%s\n" % (docs.APP_VERSION, body))
            self.assertTrue(any("release_notes.txt:" in item for item in self.document_problems()))
        notes.write_text(original)
        guide = self.root / "docs/INSTALL.md"
        guide.write_text(guide.read_text().replace("Skyrocket-%s-SHA256SUMS.txt" % docs.APP_VERSION, "old-checksums.txt"))
        self.assertTrue(any("missing current artifact" in item for item in self.document_problems()))

    def test_over_limit_report_names_the_real_count_and_how_far_over(self):
        # 期望字数在这里按构造独立算出（标题 + 编号 + 正文），不调用被测的计数函数：
        # 否则计数口径错了，报错里的数字和断言会一起错、判据照样绿。
        notes = self.root / "release_notes.txt"
        title = "V%s release" % docs.APP_VERSION
        extra = 7
        title_chars = len(title.replace(" ", ""))
        filler = "x" * (docs.NOTES_MAX_CHARS - title_chars - len("1.") + extra)
        notes.write_text("%s\n1. %s\n" % (title, filler))
        expected = title_chars + len("1.") + len(filler)
        self.assertEqual(expected, docs.NOTES_MAX_CHARS + extra)
        report = [item for item in self.document_problems() if "release_notes.txt:" in item and "limit" in item]
        self.assertEqual(len(report), 1, report)
        self.assertIn("%d characters" % expected, report[0])
        self.assertIn("limit %d" % docs.NOTES_MAX_CHARS, report[0])
        self.assertIn("over by %d" % extra, report[0])
        self.assertIn("title line", report[0])
        # 刚好在限的说明不报：证明上面那条红的是字数，而不是别的缺陷。
        notes.write_text("%s\n1. %s\n" % (title, filler[:-extra]))
        self.assertEqual([item for item in self.document_problems() if "limit" in item], [])

    def test_notes_count_ignores_every_kind_of_whitespace_and_counts_the_rest(self):
        self.assertEqual(release_identity.count_notes_chars("甲 乙\t丙\n丁\u3000戊\u00a0己"), 6)
        self.assertEqual(release_identity.count_notes_chars(" \n\t\u3000 "), 0)
        # V 1 . 4 . 9（6）+ 发布说明（4）+ 冒号（1）+ 编号“1.”（2）+ 甲（1）= 14
        self.assertEqual(release_identity.count_notes_chars("V1.4.9 发布说明:\n1. 甲"), 14)

    def test_asset_gate_message_carries_the_same_count_and_the_overshoot(self):
        # 打包闸门（make_update_assets）与公开资料检查必须是同一个数：同一份说明不许一处过、一处红。
        import make_update_assets as assets
        extra = 3
        title = "V1.4.46 发布说明:"
        filler = "甲" * (release_identity.NOTES_MAX_CHARS - len(title.replace(" ", "")) - len("1.") + extra)
        raw = "%s\n1. %s\n" % (title, filler)
        with self.assertRaises(SystemExit) as caught:
            assets.require_own_section(raw, "1.4.46")
        message = str(caught.exception)
        self.assertIn("%d 字，上限 %d 字，超出 %d 字" % (release_identity.NOTES_MAX_CHARS + extra,
                                                  release_identity.NOTES_MAX_CHARS, extra), message)
        self.assertIn("至少删掉 %d 字" % extra, message)

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

    def test_public_git_identity_is_field_scoped_and_preserves_other_findings(self):
        owner = public.REPO.split("/", 1)[0].encode()
        identity = owner + b" <" + public.SUPPORT_EMAIL.encode() + b"> 1792169467 +0800"
        commit = b"tree " + b"a" * 40 + b"\nauthor " + identity + b"\ncommitter " + identity + b"\n\nsafe\n"
        self.assertTrue(public.privacy_problems(commit, "/private-home"))
        self.assertEqual(public.object_privacy_problems(commit, "/private-home", kind="commit"), [])
        rejected = [
            (commit, "blob", ()),
            (commit + b"old owner " + owner, "commit", ()),
            (commit.replace(b"author " + owner, b"author prefix" + owner), "commit", ()),
            (commit.replace(public.SUPPORT_EMAIL.encode(), b"someone@" + b"gmail.com"), "commit", ()),
            (commit + b"ghp_" + b"a" * 30, "commit", ()),
            (commit + b"phone=" + b"138" + b"00138000", "commit", ()),
            (commit, "commit", (owner,)),
            (commit.replace(b"+0800", b"+08x0"), "commit", ()),
            (commit.replace(b"\n\n", b"\n"), "commit", ()),
            (commit + b"/private-home/data", "commit", ()),
        ]
        for data, kind, secrets in rejected:
            with self.subTest(kind=kind, data=data[-60:]):
                self.assertTrue(public.object_privacy_problems(data, "/private-home", secrets, kind))
        tag = b"object " + b"a" * 40 + b"\ntagger " + identity + b"\n\nsafe tag\n"
        self.assertEqual(public.object_privacy_problems(tag, "/private-home", kind="tag"), [])
        self.assertEqual(public.object_privacy_problems(b"tree x\nauthor safe <public@example.invalid> 1 +0000\n\nsafe\n",
                                                      "/private-home", kind="commit"), [])

    def test_real_history_keeps_approved_git_identity_without_rewriting_objects(self):
        self.init_repo()
        tree = public.run(["git", "rev-parse", "HEAD^{tree}"], self.root).strip()
        parent = public.run(["git", "rev-parse", "HEAD"], self.root).strip()
        owner = public.REPO.split("/", 1)[0]
        env = public.git_env()
        env.update(GIT_AUTHOR_NAME=owner, GIT_COMMITTER_NAME=owner,
                   GIT_AUTHOR_EMAIL=public.SUPPORT_EMAIL, GIT_COMMITTER_EMAIL=public.SUPPORT_EMAIL)
        commit = subprocess.run(["git", "commit-tree", tree, "-p", parent, "-m", "public identity"],
                                cwd=self.root, env=env, text=True, capture_output=True, check=True).stdout.strip()
        public.run(["git", "update-ref", "HEAD", commit], self.root)
        before = public.run(["git", "cat-file", "-p", commit], self.root, binary=True)
        public.run(["git", "tag", "-a", "public-fixture", "-m", "safe tag"], self.root)
        public.audit_history(self.root, "/private-home")
        self.assertEqual(before, public.run(["git", "cat-file", "-p", commit], self.root, binary=True))
        public.run(["git", "commit", "--allow-empty", "-qm", "old owner " + owner], self.root)
        with self.assertRaisesRegex(ValueError, "outside GitHub namespace"):
            public.audit_history(self.root, "/private-home")

    def test_history_preflight_uses_local_remote_and_does_not_push_or_move_private_head(self):
        self.init_repo()
        remote = self.root.parent / "public.git"
        public.run(["git", "init", "--bare", "-q", str(remote)], self.root)
        public.run(["git", "push", "-q", str(remote), "HEAD:refs/heads/public-source"], self.root)
        before = public.run(["git", "rev-parse", "HEAD"], self.root)
        refs = public.run(["git", "ls-remote", "--heads", str(remote)], self.root)
        (self.root / "untracked-private.py").write_text("private local fixture\n")
        self.assertTrue(public.audit_public_history(self.root, str(remote)))
        self.assertEqual(before, public.run(["git", "rev-parse", "HEAD"], self.root))
        self.assertEqual(refs, public.run(["git", "ls-remote", "--heads", str(remote)], self.root))
        self.assertTrue((self.root / "untracked-private.py").is_file())
        empty = self.root.parent / "empty-public.git"
        public.run(["git", "init", "--bare", "-q", str(empty)], self.root)
        self.assertFalse(public.audit_public_history(self.root, str(empty)))

    def test_history_preflight_cli_is_separate_from_publish_and_export(self):
        with patch.object(public, "audit_public_history", return_value=True) as audit, \
                patch.object(public, "export_source") as export, patch.object(public, "push_source") as push:
            self.assertEqual(public.main(["--audit-public-history"]), 0)
        audit.assert_called_once_with()
        export.assert_not_called()
        push.assert_not_called()
        for flag in ("--publish", "--source-only", "--self-test"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit), \
                    patch.object(public, "audit_public_history") as audit:
                public.main(["--audit-public-history", flag])
            audit.assert_not_called()

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
        self.assertEqual(public.privacy_problems(C.SUPPORT_EMAIL.encode(), "unused"), [])
        other_mail = b"someone" + b"@" + b"qq.com"
        self.assertIn("personal contact", public.privacy_problems(other_mail, "unused"))
        number = public.REPO.split("/", 1)[0].split("-", 1)[0].encode()
        self.assertTrue(public.privacy_problems(number, "unused"))

    def test_release_tools_share_public_identity_without_loading_qt(self):
        import subprocess
        code = """
import sys
sys.path.insert(0, sys.argv[1] + '/tools')
import release_identity, make_update_assets, publish_update_release
assert 'sf_constants' not in sys.modules
assert not any(name.startswith('PyQt6') for name in sys.modules)
assert make_update_assets.REPO == publish_update_release.REPO == release_identity.REPO
assert make_update_assets.TAG == publish_update_release.TAG == release_identity.TAG
"""
        result = subprocess.run([sys.executable, "-c", code, str(ROOT)],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(release_identity.REPO, C.RELEASE_REPO)
        self.assertEqual(release_identity.TAG, C.RELEASE_TAG)
        self.assertEqual(release_identity.SUPPORT_EMAIL, C.SUPPORT_EMAIL)
        self.assertEqual(release_identity.MANIFEST_PATH, C.UPDATE_MANIFEST_NAME)
        from tools import publish_update_release as publisher
        import render_install_guide as guide
        self.assertEqual(publisher.RAW_MANIFEST_URL, C.UPDATE_MANIFEST_URL)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(guide.contact(), C.SUPPORT_EMAIL)
        # Public documents intentionally contain rendered URLs, not Python expressions.
        for name in ("README.md", "docs/INSTALL.md"):
            body = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("https://github.com/" + C.RELEASE_REPO, body)
        self.assertIn(C.SUPPORT_EMAIL, (ROOT / "docs/INSTALL.md").read_text())

    def test_identity_reader_rejects_missing_duplicate_and_executable_values(self):
        config = self.root / "identity.py"
        values = release_identity.load_identity()
        safe = "\n".join("%s = %r" % item for item in values.items())
        config.write_text("raise RuntimeError('must not execute')\n" + safe)
        self.assertEqual(release_identity.load_identity(config), values)
        for invalid in ("", safe + "\nRELEASE_REPO = 'duplicate'",
                        safe.replace(repr(values["SUPPORT_EMAIL"]), "''"),
                        safe.replace(repr(values["SUPPORT_EMAIL"]), "123"),
                        safe.replace(repr(values["SUPPORT_EMAIL"]), "str('not literal')")):
            with self.subTest(config=invalid):
                config.write_text(invalid)
                with self.assertRaises(ValueError):
                    release_identity.load_identity(config)

    def test_old_numeric_contacts_fail_but_normal_numbers_and_urls_pass(self):
        # Compose synthetic old contacts so the public test source itself is clean.
        old_phone = b"138" + b"00138000"
        old_id = b"927" + b"45163"
        owner = C.RELEASE_REPO.split("/", 1)[0].encode()
        rejected = (old_phone, b"phone=" + old_phone, b"QQ: " + old_id,
                    b"old owner " + owner,
                    b"https://example.com/" + C.RELEASE_REPO.encode(),
                    b"prefix" + C.SUPPORT_EMAIL.encode())
        for value in rejected:
            with self.subTest(value=value):
                self.assertTrue(public.privacy_problems(value, "unused"))
        accepted = (C.SUPPORT_EMAIL.encode(), b"https://github.com/" + owner + b"/repo",
                    b"https://raw.githubusercontent.com/" + C.RELEASE_REPO.encode() + b"/main/file",
                    b"https://api.github.com/repos/" + C.RELEASE_REPO.encode(),
                    b"run 387" + b"04366170", b"value 11.000000000000002",
                    b"sha256 abc" + old_phone + b"def", b"20261016001")
        for value in accepted:
            with self.subTest(value=value):
                self.assertEqual(public.privacy_problems(value, "unused"), [])

    def test_phone_shaped_github_owner_is_exempt_only_in_namespace(self):
        owner = b"138" + b"00138000" + b"-maintainer"
        repo = owner.decode() + "/release"
        with patch.object(public, "REPO", repo):
            for prefix in (b"https://github.com/", b"https://raw.githubusercontent.com/",
                           b"https://api.github.com/repos/"):
                self.assertEqual(public.privacy_problems(prefix + repo.encode(), "unused"), [])
            self.assertTrue(public.privacy_problems(owner, "unused"))
            self.assertTrue(public.privacy_problems(b"https://github.com.evil/" + repo.encode(),
                                                   "unused"))

    def test_one_old_number_in_reachable_history_is_rejected(self):
        self.init_repo()
        path = self.root / "contact.txt"
        path.write_bytes(b"138" + b"00138000")
        public.run(["git", "add", "contact.txt"], self.root)
        public.run(["git", "commit", "-qm", "old contact fixture"], self.root)
        path.write_bytes(b"contact removed")
        public.run(["git", "add", "contact.txt"], self.root)
        public.run(["git", "commit", "-qm", "clean current tree"], self.root)
        with self.assertRaisesRegex(ValueError, "personal phone number"):
            public.audit_history(self.root, "/Users/fixture-owner")

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
        self.init_repo()
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
        receipt = json.loads((self.root / "build" / ("publication-v%s.json" % public.APP_VERSION)).read_text())
        self.assertEqual(receipt["public_commit"], "commit")
        self.assertEqual(receipt["source_zip"]["kind"], "file")
        self.assertTrue(receipt["source_zip"]["content_sha256"])

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
