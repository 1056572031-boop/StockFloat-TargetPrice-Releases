"""发布读回的离线正反例，不访问 GitHub、不挂卷、不启动用户应用。"""
import contextlib
import hashlib
import io
import json
import os
import runpy
import stat
import subprocess
import tempfile
import urllib.parse
import zipfile
from pathlib import Path
from unittest import mock

from tests._base import SFCase
from tools import release_readback as readback


def blob(content):
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def archive_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content, mode in entries:
            info = zipfile.ZipInfo("Skyrocket-source/" + name)
            info.external_attr = mode << 16
            archive.writestr(info, content)
    return output.getvalue()


class ReleaseReadbackCase(SFCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="sf_readback_test_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_asset_url_encodes_non_ascii_tag_and_filename(self):
        url = readback.asset_url("fixture 文件.zip")
        self.assertTrue(url.isascii())
        self.assertIn("%20", url)
        self.assertTrue(urllib.parse.unquote(url).endswith(readback.TAG + "/fixture 文件.zip"))

    def test_source_archive_checks_content_executable_and_symlink(self):
        entries = [("main.py", b"print('fixture')", stat.S_IFREG | 0o755),
                   ("link", b"main.py", stat.S_IFLNK | 0o777)]
        tree = {"truncated": False, "tree": [{"path": name, "type": "blob", "sha": blob(content),
                 "mode": "120000" if stat.S_ISLNK(mode) else "100755"} for name, content, mode in entries]}
        self.assertEqual(set(readback.source_archive(archive_bytes(entries), tree)), {"main.py", "link"})
        for broken in ([entries[0]], [("../escape", b"bad", 0o644)],
                       [("main.py", b"changed", stat.S_IFREG | 0o755), entries[1]],
                       [("main.py", entries[0][1], stat.S_IFREG | 0o644), entries[1]]):
            with self.assertRaises(ValueError):
                readback.source_archive(archive_bytes(broken), tree)
        with self.assertRaisesRegex(ValueError, "不完整"):
            readback.source_archive(archive_bytes(entries), dict(tree, truncated=True))

    def test_source_archive_accepts_missing_unix_metadata_not_wrong_blob(self):
        raw = archive_bytes([("plain", b"data", 0)])
        tree = {"tree": [{"path": "plain", "type": "blob", "sha": blob(b"data"), "mode": "100644"}]}
        self.assertEqual(readback.source_archive(raw, tree), {"plain": b"data"})
        tree["tree"][0]["sha"] = blob(b"other")
        with self.assertRaisesRegex(ValueError, "blob"):
            readback.source_archive(raw, tree)

    def test_ci_binds_exact_commit_and_both_jobs(self):
        run = {"id": 1, "run_number": 2, "head_sha": "fixture", "head_branch": "public-source",
               "status": "completed", "conclusion": "success"}
        jobs = [{"name": name, "conclusion": "success"} for name in ("test-offscreen", "test-cocoa-native")]
        with mock.patch.object(readback, "api", side_effect=[{"workflow_runs": [run]}, {"jobs": jobs}]):
            self.assertEqual(readback.check_ci("fixture")["run_id"], 1)
        for runs, observed in (([], jobs), ([dict(run, head_sha="wrong")], jobs),
                               ([dict(run, conclusion="failure")], jobs), ([run], jobs[:1])):
            with mock.patch.object(readback, "api", side_effect=[{"workflow_runs": runs}, {"jobs": observed}]), \
                    self.assertRaises(ValueError):
                readback.check_ci("fixture")

    def test_download_preserves_evidence_and_refuses_overwrite(self):
        target = self.root / "download.zip"
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = [b"downloaded", b""]
        with mock.patch.object(readback.urllib.request, "urlopen", return_value=response):
            self.assertEqual(readback.download("https://example.test/file", target), b"downloaded")
        with self.assertRaisesRegex(ValueError, "不覆盖"):
            readback.download("https://example.test/file", target)

    def test_api_clears_git_routing_and_download_size_is_bounded(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": "bad-route"}), \
                mock.patch.object(subprocess, "run", return_value=mock.Mock(stdout='{"fixture": true}')) as runner:
            self.assertEqual(readback.api("fixture"), {"fixture": True})
        self.assertNotIn("GIT_DIR", runner.call_args.kwargs["env"])

        class Oversized(bytes):
            def __len__(self):
                return 512 * 1024 * 1024 + 1

        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = Oversized(b"x")
        with mock.patch.object(readback.urllib.request, "urlopen", return_value=response), \
                self.assertRaisesRegex(ValueError, "上限"):
            readback.download("https://example.test/file", self.root / "oversize.zip")

    def test_directory_zip_entries_are_not_treated_as_source_files(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("Skyrocket-source/", b"")
            archive.writestr("Skyrocket-source/main.py", b"fixture")
        tree = {"tree": [{"path": "main.py", "type": "blob", "sha": blob(b"fixture"), "mode": "100644"}]}
        self.assertEqual(readback.source_archive(data.getvalue(), tree), {"main.py": b"fixture"})

    def test_readback_refuses_existing_output_and_mismatched_publication(self):
        out = self.root / "existing.json"
        out.write_text("preserve existing evidence")
        with self.assertRaisesRegex(ValueError, "不覆盖"):
            readback.verify(self.root, self.root / "state", self.root / "publication", out, self.root / "app")
        publication = self.root / "publication.json"
        publication.write_text("{}")
        with mock.patch.object(readback.release_state, "validate", return_value={"source": {}, "version": "fixture"}), \
                self.assertRaisesRegex(ValueError, "不同源"):
            readback.verify(self.root, self.root / "state", publication, self.root / "new.json", self.root / "app")

    def test_dmg_unmounts_when_app_mismatches(self):
        with mock.patch.object(readback.subprocess, "run") as runner, \
                mock.patch.object(readback.evidence, "artifact_token", return_value={"sha256": "wrong"}), \
                self.assertRaisesRegex(ValueError, "不一致"):
            readback.check_dmg(self.root / "download.dmg", {"path": "candidate", "sha256": "expected"})
        self.assertEqual(runner.call_args_list[-1].args[0][1], "detach")

    def exercise(self, failure=None):
        root = self.root / (failure or "good")
        root.mkdir()
        app = root / "Skyrocket.app"
        app.mkdir()
        (app / "payload").write_bytes(b"fixture")
        files = {}
        for role, name, content in (("dmg", "fixture.dmg", b"dmg"), ("zip", "fixture.zip", b"zip"),
                                     ("ota_manifest", "fixture.json", b"{}")):
            path = root / name
            path.write_bytes(content)
            files[role] = readback.release_state.reference(path)
        files["app"] = readback.release_state.reference(app)
        state = {"stage": "released_pending_acceptance", "version": "fixture", "source": {"head": "fixture"},
                 "artifacts": files}
        source = root / "source.zip"
        source.write_bytes(b"source")
        sums = root / "SHA256SUMS.txt"
        sums.write_text("".join(value["content_sha256"] + "  " + Path(value["path"]).name + "\n"
                               for value in (files["dmg"], files["zip"], readback.release_state.reference(source))))
        if failure == "checksum_shape":
            sums.write_text("malformed\n")
        elif failure == "checksum_value":
            sums.write_text("".join("0" * 64 + "  " + name + "\n" for name in ("fixture.dmg", "fixture.zip", "source.zip")))
        publication = root / "publication.json"
        publication.write_text(json.dumps({"source": state["source"], "version": state["version"],
                              "public_commit": "public-fixture", "source_zip": readback.release_state.reference(source),
                              "checksums": readback.release_state.reference(sums)}))

        def download(url, target):
            self.assertTrue(url.isascii(), "发行标签必须先编码再请求")
            original = {"online-manifest.json": Path(files["ota_manifest"]["path"])}.get(target.name, root / target.name)
            content = original.read_bytes()
            if failure == "zip_hash" and target.name == "fixture.zip":
                content = b"changed download"
            target.write_bytes(content)
            return content

        def api(path):
            if path.startswith("git/ref/"):
                return {"object": {"sha": "other-commit" if failure == "public_ref" else "public-fixture"}}
            if path.startswith("git/trees/"):
                return {}
            return {"content": "d3Jvbmc=" if failure == "public_docs" else "Zml4dHVyZQ=="}

        docs = {name: b"fixture" for name in ("README.md", "docs/INSTALL.md", "SECURITY.md", "NOTICE.md", "LICENSE")}
        import sf_update
        import smoke_frozen_app
        installed = app
        if failure == "installed":
            installed = root / "other.app"
            installed.mkdir()
            (installed / "payload").write_bytes(b"other installed app")
        with mock.patch.object(readback.release_state, "validate", return_value=state), \
                mock.patch.object(readback, "download", side_effect=download), \
                mock.patch.object(readback, "api", side_effect=api), \
                mock.patch.object(readback, "source_archive", return_value=docs), \
                mock.patch.object(readback, "check_ci", side_effect=ValueError("CI pending") if failure == "ci" else None,
                                  return_value={"jobs": "fixture"}), \
                mock.patch.object(readback, "check_dmg"), \
                mock.patch.object(sf_update, "verify_manifest", return_value=mock.Mock(version="other" if failure == "ota" else "fixture")), \
                mock.patch.object(readback.subprocess, "run"), \
                mock.patch.object(smoke_frozen_app, "run_smoke", return_value=(1 if failure == "startup" else 0, {"fixture": True})):
            out = root / "readback.json"
            result = readback.verify(root, root / "state.json", publication, out, installed)
        return result, json.loads(out.read_text())

    def test_orchestration_keeps_failures_and_does_not_promote(self):
        result, report = self.exercise()
        self.assertEqual(result, 0, report)
        self.assertEqual(len(report["checks"]), 7)
        for failure, key in (("ci", "public_ci"), ("startup", "final_startup"), ("installed", "final_startup"),
                             ("ota", "ota_manifest"), ("zip_hash", "zip"), ("public_ref", "public_source"),
                             ("public_docs", "public_source"), ("checksum_shape", "checksums"), ("checksum_value", "checksums")):
            result, report = self.exercise(failure)
            self.assertEqual(result, 1)
            self.assertFalse(report["verified"])
            self.assertEqual(report["checks"][key]["status"], "failed")
            self.assertEqual(len(report["checks"]), 7, "independent checks still collected")

    def test_cli_incomplete_and_file_errors(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                readback.main([])
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(readback.main(["--state", str(self.root / "missing"), "--publication", "missing",
                                             "--out", str(self.root / "out.json")]), 1)
            with mock.patch.object(readback.sys, "argv", [readback.__file__]), self.assertRaises(SystemExit) as raised:
                runpy.run_path(readback.__file__, run_name="__main__")
            self.assertEqual(raised.exception.code, 2)
