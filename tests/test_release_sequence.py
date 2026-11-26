import importlib.util
import json
import plistlib
import tempfile
import unittest
from unittest import mock
import zipfile
from pathlib import Path

from tests._base import SFCase


ROOT = Path(__file__).resolve().parents[1]


def load_sequence_tool():
    path = ROOT / "tools" / "release_sequence.py"
    spec = importlib.util.spec_from_file_location("release_sequence", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseSequenceLedgerTests(SFCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = self.root / "release_sequence_ledger.json"
        self.constants = self.root / "sf_constants.py"
        self.constants.write_text(
            "APP_VERSION = '1.4.118'\nRELEASE_SEQUENCE = 0\n",
            encoding="utf-8",
        )
        self.tool = load_sequence_tool()

    def test_parallel_allocations_are_serialized_without_losing_records(self):
        import concurrent.futures
        import threading
        started = threading.Event()
        release = threading.Event()
        original = self.tool.load_ledger
        calls = []
        def delayed_load(path):
            ledger = original(path)
            calls.append(1)
            if len(calls) == 1:
                started.set()
                self.assertTrue(release.wait(2))
            return ledger
        with mock.patch.object(self.tool, "load_ledger", side_effect=delayed_load):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.tool.allocate, self.ledger, self.constants, "1.4.118")
                self.assertTrue(started.wait(1))
                second = pool.submit(self.tool.allocate, self.ledger, self.constants, "1.4.119")
                release.set()
                results = [first.result(timeout=3), second.result(timeout=3)]
        self.assertEqual(sorted(record["sequence"] for record in results), [1, 2])
        self.assertEqual(len(json.loads(self.ledger.read_text(encoding="utf-8"))["releases"]), 2)

    def test_separate_processes_allocate_and_mark_without_lost_updates(self):
        import os
        import subprocess
        import sys
        self.tool.allocate(self.ledger, self.constants, "1.4.118")
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); import release_sequence as s; "
            "s.mark_published(sys.argv[2], '1.4.118', 1) if sys.argv[4] == 'mark' "
            "else s.allocate(sys.argv[2], sys.argv[3], sys.argv[4])"
        )
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        processes = [subprocess.Popen(
            [sys.executable, "-c", script, str(ROOT / "tools"), str(self.ledger),
             str(self.constants), action], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            for action in ("mark", "1.4.119", "1.4.120")]
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, (stdout + stderr).decode(errors="replace"))
        records = self.tool.load_ledger(self.ledger)["releases"]
        self.assertEqual({item["sequence"] for item in records}, {1, 2, 3})
        published = next(item for item in records if item["version"] == "1.4.118")
        self.assertEqual(published["status"], "published")
        self.assertEqual(len(records), 3)

    def test_command_line_self_test_returns_success(self):
        import runpy
        import sys
        with mock.patch.object(sys, "argv", [str(ROOT / "tools" / "release_sequence.py"), "--self-test"]):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(ROOT / "tools" / "release_sequence.py"), run_name="__main__")
        self.assertEqual(result.exception.code, 0)

    def test_source_write_failure_preserves_consumed_sequence_for_retry(self):
        with mock.patch.object(self.tool, "update_source_sequence", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.tool.allocate(self.ledger, self.constants, "1.4.118")
        stored = self.tool.load_ledger(self.ledger)
        self.assertEqual(stored["releases"][0]["sequence"], 1)
        retry = self.tool.allocate(self.ledger, self.constants, "1.4.118")
        self.assertEqual(retry["sequence"], 1)
        self.assertEqual(len(self.tool.load_ledger(self.ledger)["releases"]), 1)

    def test_ledger_rejects_same_version_with_different_sequences(self):
        records = [{"sequence": 1, "version": "1.4.118"},
                   {"sequence": 2, "version": "1.4.118"}]
        self.ledger.write_text(json.dumps({"schema": 1, "releases": records}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "version.*重复"):
            self.tool.load_ledger(self.ledger)

    def test_mark_published_is_locked_idempotent_and_requires_allocated_identity(self):
        self.tool.allocate(self.ledger, self.constants, "1.4.118")
        published = self.tool.mark_published(self.ledger, "1.4.118", 1)
        self.assertEqual(published["status"], "published")
        repeated = self.tool.mark_published(self.ledger, "1.4.118", 1)
        self.assertEqual(repeated["status"], "published")
        with self.assertRaisesRegex(ValueError, "唯一"):
            self.tool.mark_published(self.ledger, "1.4.119", 2)

    def test_rollback_requires_published_identity_and_matching_zip(self):
        version = "1.4.118"
        self.tool.allocate(self.ledger, self.constants, version)
        manifest = self.root / "rollback.json"
        manifest.write_text(json.dumps({"version": version, "release_sequence": 1}), encoding="utf-8")
        archive = self.root / "rollback.zip"
        def write_archive(sequence):
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("Skyrocket.app/Contents/Info.plist", plistlib.dumps({
                    "CFBundleShortVersionString": version, "SkyrocketReleaseSequence": sequence}))
        write_archive(1)
        with self.assertRaisesRegex(ValueError, "已发布"):
            self.tool.verify_rollback(self.ledger, manifest, archive)
        self.tool.mark_published(self.ledger, version, 1)
        verified = self.tool.verify_rollback(self.ledger, manifest, archive)
        self.assertEqual(verified["release_sequence"], 1)
        write_archive(2)
        with self.assertRaisesRegex(ValueError, "身份不一致"):
            self.tool.verify_rollback(self.ledger, manifest, archive)

    def test_published_mark_rejects_withdrawn_record_without_rewriting_ledger(self):
        version = "fixture-version"
        self.ledger.write_text(json.dumps({"schema": 1, "releases": [
            {"version": version, "sequence": 1, "status": "withdrawn"}]}), encoding="utf-8")
        original = self.ledger.read_bytes()
        with self.assertRaisesRegex(ValueError, "allocated"):
            self.tool.mark_published(self.ledger, version, 1)
        self.assertEqual(self.ledger.read_bytes(), original)

    def test_invalid_readers_and_failed_atomic_replace_leave_original_untouched(self):
        manifest = self.root / "manifest.json"
        with self.assertRaisesRegex(ValueError, "无法读取签名清单"):
            self.tool.manifest_identity(manifest)
        manifest.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "签名清单无效"):
            self.tool.manifest_identity(manifest)
        manifest.write_text(json.dumps({"release_sequence": 1}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "version"):
            self.tool.manifest_identity(manifest)
        with mock.patch.object(self.tool.zipfile, "ZipFile", side_effect=OSError("unreadable")):
            with self.assertRaisesRegex(ValueError, "无法读取 ZIP"):
                self.tool.zip_identity(self.root / "missing.zip")
        original = self.constants.read_bytes()
        with mock.patch.object(self.tool.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.tool.update_source_sequence(self.constants, 2)
        self.assertEqual(self.constants.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".sf_constants.py.*")), [])

    def test_sequence_selftest_reports_broken_positive_and_negative_controls(self):
        original = self.tool.allocate
        with mock.patch.object(self.tool, "allocate", return_value={"sequence": 2}):
            self.assertEqual(self.tool.self_test(), 1)
        def broken_skip(*args, **kwargs):
            if kwargs.get("skip") is not None:
                return {"sequence": kwargs["skip"]}
            return original(*args, **kwargs)
        with mock.patch.object(self.tool, "allocate", side_effect=broken_skip):
            self.assertEqual(self.tool.self_test(), 1)
        self.assertEqual(self.tool.main([]), 2)

    def test_descending_ledger_and_invalid_zip_cannot_be_verified(self):
        self.ledger.write_text(json.dumps({"schema": 1, "releases": [
            {"version": "first", "sequence": 2, "skip_reason": "fixture"},
            {"version": "second", "sequence": 1}]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "严格递增"):
            self.tool.load_ledger(self.ledger)
        with mock.patch.object(self.tool.plistlib, "loads", return_value=[]):
            app = self.root / "invalid.app" / "Contents"
            app.mkdir(parents=True)
            (app / "Info.plist").write_bytes(b"invalid")
            with self.assertRaises(ValueError):
                self.tool.app_identity(app.parent)

    def test_app_identity_rejects_plist_without_version(self):
        app = self.root / "missing-version.app"
        info = app / "Contents" / "Info.plist"
        info.parent.mkdir(parents=True)
        info.write_bytes(plistlib.dumps({"SkyrocketReleaseSequence": 1}))
        with self.assertRaisesRegex(ValueError, "CFBundleShortVersionString"):
            self.tool.app_identity(app)

    def test_first_allocation_assigns_one_and_updates_the_unique_constant(self):
        record = self.tool.allocate(self.ledger, self.constants, "1.4.118")
        self.assertEqual(record["sequence"], 1)
        self.assertEqual(record["status"], "allocated")
        self.assertIn("RELEASE_SEQUENCE = 1", self.constants.read_text(encoding="utf-8"))
        stored = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(stored["releases"], [record])

    def test_prearchive_rejects_unallocated_development_placeholder(self):
        self.constants.write_text("APP_VERSION = '1.4.118'\nRELEASE_SEQUENCE = 1\n", encoding="utf-8")
        self.ledger.write_text(json.dumps({"schema": 1, "releases": []}), encoding="utf-8")
        info = self.root / "Skyrocket.app" / "Contents" / "Info.plist"
        info.parent.mkdir(parents=True)
        info.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "1.4.118",
                                         "SkyrocketReleaseSequence": 1}))
        with self.assertRaisesRegex(ValueError, "账本没有"):
            self.tool.verify_prearchive(self.ledger, self.constants, info.parent.parent)

    def test_existing_version_reuses_its_sequence(self):
        initial = self.tool.allocate(self.ledger, self.constants, "1.4.118")
        reused = self.tool.allocate(self.ledger, self.constants, "1.4.118")
        self.assertEqual(reused["sequence"], initial["sequence"])
        self.assertEqual(len(json.loads(self.ledger.read_text(encoding="utf-8"))["releases"]), 1)

    def test_skip_requires_a_reason_and_never_reclaims_the_sequence(self):
        self.tool.allocate(self.ledger, self.constants, "1.4.118")
        with self.assertRaisesRegex(ValueError, "reason"):
            self.tool.allocate(self.ledger, self.constants, "1.4.119", skip=3)
        skipped = self.tool.allocate(
            self.ledger, self.constants, "1.4.119", skip=3, reason="withdrawn candidate",
        )
        self.assertEqual(skipped["sequence"], 3)
        self.assertEqual(skipped["skip_reason"], "withdrawn candidate")
        later = self.tool.allocate(self.ledger, self.constants, "1.4.120")
        self.assertEqual(later["sequence"], 4)

    def test_verify_rejects_each_disagreement_across_source_app_manifest_and_zip(self):
        self.tool.allocate(self.ledger, self.constants, "1.4.118")
        app = self.root / "Skyrocket.app"
        info = app / "Contents" / "Info.plist"
        info.parent.mkdir(parents=True)
        plist = {
            "CFBundleShortVersionString": "1.4.118",
            "SkyrocketReleaseSequence": 1,
        }
        with info.open("wb") as stream:
            plistlib.dump(plist, stream)
        archive = self.root / "Skyrocket-1.4.118-macos.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("Skyrocket.app/Contents/Info.plist", plistlib.dumps(plist))
        manifest = self.root / "Skyrocket-update.json"
        manifest.write_text(
            json.dumps({"version": "1.4.118", "release_sequence": 1}), encoding="utf-8"
        )
        self.tool.verify(self.ledger, self.constants, app, manifest, archive)

        manifest.write_text(
            json.dumps({"version": "1.4.118", "release_sequence": 2}), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "签名清单"):
            self.tool.verify(self.ledger, self.constants, app, manifest, archive)

    def test_ledger_rejects_duplicate_sequence_and_unexplained_middle_gap(self):
        for records in (
            [
                {"sequence": 1, "version": "1.4.118"},
                {"sequence": 1, "version": "1.4.119"},
            ],
            [
                {"sequence": 1, "version": "1.4.118"},
                {"sequence": 3, "version": "1.4.119"},
            ],
        ):
            with self.subTest(records=records):
                self.ledger.write_text(json.dumps({"schema": 1, "releases": records}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "重复|跳号"):
                    self.tool.load_ledger(self.ledger)

    def test_existing_ledger_rejects_unexplained_initial_jump(self):
        self.ledger.write_text(json.dumps({
            "schema": 1,
            "releases": [{"sequence": 10, "version": "1.4.118",
                          "stage": "beta", "status": "allocated",
                          "allocated_at": "2026-11-19T00:00:00Z",
                          "notes": "", "skip_reason": None}],
        }, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "跳号|skip"):
            self.tool.load_ledger(self.ledger)

    def test_verify_rejects_each_of_four_identity_sources_independently(self):
        self.tool.allocate(self.ledger, self.constants, "1.4.118")
        app = self.root / "Skyrocket.app"
        info = app / "Contents" / "Info.plist"
        info.parent.mkdir(parents=True, exist_ok=True)
        plist = {"CFBundleShortVersionString": "1.4.118",
                 "SkyrocketReleaseSequence": 1}
        with info.open("wb") as stream:
            plistlib.dump(plist, stream)
        archive = self.root / "Skyrocket-1.4.118-macos.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("Skyrocket.app/Contents/Info.plist", plistlib.dumps(plist))
        manifest = self.root / "Skyrocket-update.json"
        manifest.write_text(json.dumps({"version": "1.4.118",
                                        "release_sequence": 1}), encoding="utf-8")
        for source in ("constants", "app", "zip", "manifest"):
            with self.subTest(source=source):
                original_constants = self.constants.read_text(encoding="utf-8")
                original_app = info.read_bytes()
                original_zip = archive.read_bytes()
                original_manifest = manifest.read_text(encoding="utf-8")
                try:
                    if source == "constants":
                        self.constants.write_text("APP_VERSION = '1.4.118'\nRELEASE_SEQUENCE = 2\n",
                                                  encoding="utf-8")
                    elif source == "app":
                        with info.open("wb") as stream:
                            plistlib.dump({**plist, "SkyrocketReleaseSequence": 2}, stream)
                    elif source == "zip":
                        with zipfile.ZipFile(archive, "w") as bundle:
                            bundle.writestr("Skyrocket.app/Contents/Info.plist",
                                            plistlib.dumps({**plist, "SkyrocketReleaseSequence": 2}))
                    else:
                        manifest.write_text(json.dumps({"version": "1.4.118",
                                                        "release_sequence": 2}), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.tool.verify(self.ledger, self.constants, app, manifest, archive)
                finally:
                    self.constants.write_text(original_constants, encoding="utf-8")
                    info.write_bytes(original_app)
                    archive.write_bytes(original_zip)
                    manifest.write_text(original_manifest, encoding="utf-8")

    def test_verify_rejects_bool_sequence_in_signed_manifest(self):
        self.tool.allocate(self.ledger, self.constants, "1.4.118")
        app = self.root / "Skyrocket.app"
        info = app / "Contents" / "Info.plist"
        info.parent.mkdir(parents=True, exist_ok=True)
        plist = {"CFBundleShortVersionString": "1.4.118", "SkyrocketReleaseSequence": 1}
        with info.open("wb") as stream:
            plistlib.dump(plist, stream)
        archive = self.root / "Skyrocket-1.4.118-macos.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("Skyrocket.app/Contents/Info.plist", plistlib.dumps(plist))
        manifest = self.root / "Skyrocket-update.json"
        manifest.write_text(json.dumps({"version": "1.4.118", "release_sequence": True}),
                            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不能用 bool"):
            self.tool.verify(self.ledger, self.constants, app, manifest, archive)


if __name__ == "__main__":
    unittest.main()
