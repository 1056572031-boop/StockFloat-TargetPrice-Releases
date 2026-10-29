"""Release wrap-up helper: archive receipt-matched evidence, retire a finished worktree safely."""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import io
import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from tests._base import SFCase
from tools import release_wrapup as w
from tools.gate_stamp import git_env

VER = "7.7.7"
STAMP = "2030-01-02T03:04:05+00:00"
SCRIPT = Path(w.__file__)
EXPECTED = 4 + len(w.COMPANIONS)  # candidate, its manifest, complete record, its manifest, companions


class WrapupCase(SFCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="sf-wrapup-")
        self.addCleanup(temp.cleanup)
        self.root = Path(os.path.realpath(temp.name))
        console = contextlib.ExitStack()
        console.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.err = console.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(console.close)
        self.env = git_env()
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_TERMINAL_PROMPT="0", LC_ALL="C")

    def git(self, cwd, *args):
        result = subprocess.run(["git", *args], cwd=str(cwd), env=self.env, capture_output=True,
                                text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def init_repo(self, path):
        path.mkdir(parents=True, exist_ok=True)
        self.git(path, "init", "-q", "--template=", "-b", "main")
        for key, value in (("user.name", "wrapup-test"), ("user.email", "wrapup@example.invalid"),
                           ("commit.gpgsign", "false")):
            self.git(path, "config", key, value)

    def put(self, name, data, root=None):
        path = (root or self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data, sort_keys=True), encoding="utf-8")
        return path


class ArchiveCase(WrapupCase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.tree = self.root / "tree"
        self.tree.mkdir()
        self.init_repo(self.repo)
        self.index = {"boundary": "kept", "files": [{"archived_at": "t0", "from": "old.json",
                                                     "sha256": "0" * 64, "to": "old/old.json"}],
                      "notes": ["first note"], "recorded_at": "r0", "reference_base": "repository_root"}
        self.index_path = self.put(w.ARCHIVE_DIR + "/" + w.INDEX_NAME, w.dump_index(self.index), self.repo)
        self.index_bytes = self.index_path.read_bytes()
        self.folder = self.repo / w.ARCHIVE_DIR / ("v" + VER)
        self.fingerprint = {"source": "abc"}
        self.stage(self.tree)

    def stage(self, root, label=None, fingerprint=None):
        """Evidence as a release tree leaves it: root-level quality records plus the receipt folder.

        label / fingerprint stage a same-version re-release: its receipt sits in vX.Y.Z-label and its
        quality records carry different bytes from the earlier release's."""
        fp = fingerprint or self.fingerprint
        folder = w.ARCHIVE_DIR + "/" + ("v" + VER if label is None else "v%s-%s" % (VER, label))
        cand = self.put("quality-v%s.json" % VER, {"artifact_fingerprint": fp, "stage": "candidate",
                                                   "release_qualification": {"stage": "candidate"}}, root)
        man = self.put("quality-v%s.manifest.json" % VER, {"manifest_of": "candidate"}, root)
        self.put("quality-v%s-complete.json" % VER, {"artifact_fingerprint": fp,
                                                      "release_qualification": {"stage": "complete"}}, root)
        self.put("quality-v%s-complete.manifest.json" % VER, {"manifest_of": "complete"}, root)
        self.put("quality-v%s-early.json" % VER, {"artifact_fingerprint": {"source": "older attempt"},
                                                  "release_qualification": {"stage": "candidate"}}, root)
        self.put(folder + "/publish.log", "published\n", root)
        self.put(folder + "/online-manifest.json", {"version": VER}, root)
        receipt = {"version": VER, "status": "incomplete", "candidate_sha256": w.sha256_file(cand),
                   "candidate_manifest_sha256": w.sha256_file(man)}
        if label is not None:
            receipt["archive_label"] = label
        self.put(folder + "/post_release.json", receipt, root)

    def receipt_path(self):
        return self.tree / w.ARCHIVE_DIR / ("v" + VER) / "post_release.json"

    def edit_receipt(self, **changes):
        data = json.loads(self.receipt_path().read_text(encoding="utf-8"))
        data.update(changes)
        self.put(w.ARCHIVE_DIR + "/v" + VER + "/post_release.json", data, self.tree)

    def run_archive(self, apply=True, tree=None):
        return w.archive(self.repo, tree or self.tree, VER, apply)

    def assert_refused(self, pattern):
        with self.assertRaisesRegex(w.WrapupError, pattern):
            self.run_archive()
        self.assertFalse(self.folder.exists(), "a refusal must not leave a half-made archive folder")
        self.assertEqual(self.index_path.read_bytes(), self.index_bytes)


class TestArchive(ArchiveCase):
    def test_dry_run_plans_everything_and_changes_nothing(self):
        summary = self.run_archive(apply=False)
        self.assertEqual(summary, {"copy": EXPECTED, "index": EXPECTED, "keep": 0})
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.index_path.read_bytes(), self.index_bytes)

    def test_apply_copies_only_receipt_matched_files_and_extends_the_index(self):
        self.track_patch(mock.patch.object(w, "utc_now", return_value=STAMP))
        self.assertEqual(self.run_archive(), {"copy": EXPECTED, "index": EXPECTED, "keep": 0})
        names = sorted(p.name for p in self.folder.iterdir())
        self.assertEqual(len(names), EXPECTED)
        self.assertNotIn("quality-v%s-early.json" % VER, names)
        index = json.loads(self.index_path.read_text(encoding="utf-8"))
        self.assertEqual(index["files"][0], self.index["files"][0])
        added = index["files"][1:]
        self.assertEqual(len(added), EXPECTED)
        for entry in added:
            source, dest = self.tree / entry["from"], self.repo / entry["to"]
            self.assertEqual(entry["archived_at"], STAMP)
            self.assertEqual(w.sha256_file(source), entry["sha256"])
            self.assertEqual(w.sha256_file(dest), entry["sha256"])
        self.assertEqual(index["notes"][0], "first note")
        self.assertIn(self.tree.name, index["notes"][1])
        self.assertEqual(self.index_path.read_text(encoding="utf-8"), w.dump_index(index))

    def test_second_run_is_idempotent(self):
        self.run_archive()
        after_first = self.index_path.read_bytes()
        self.assertEqual(self.run_archive(), {"copy": 0, "index": 0, "keep": EXPECTED})
        self.assertEqual(self.index_path.read_bytes(), after_first)

    def test_existing_destination_with_other_bytes_is_never_overwritten(self):
        other = self.put(w.ARCHIVE_DIR + "/v" + VER + "/publish.log", "someone else's bytes", self.repo)
        with self.assertRaisesRegex(w.WrapupError, "never overwritten"):
            self.run_archive()
        self.assertEqual(other.read_text(encoding="utf-8"), "someone else's bytes")
        self.assertEqual([p.name for p in self.folder.iterdir()], ["publish.log"])
        self.assertEqual(self.index_path.read_bytes(), self.index_bytes)

    def test_index_entry_with_a_different_hash_is_never_rewritten(self):
        self.index["files"].append({"archived_at": "t1", "from": "x", "sha256": "f" * 64,
                                    "to": w.ARCHIVE_DIR + "/v" + VER + "/publish.log"})
        self.index_path.write_text(w.dump_index(self.index), encoding="utf-8")
        self.index_bytes = self.index_path.read_bytes()
        self.assert_refused("never rewritten")

    def test_indexed_but_missing_file_is_copied_without_a_second_index_entry(self):
        digest = w.sha256_file(self.tree / w.ARCHIVE_DIR / ("v" + VER) / "publish.log")
        self.index["files"].append({"archived_at": "t1", "from": "x", "sha256": digest,
                                    "to": w.ARCHIVE_DIR + "/v" + VER + "/publish.log"})
        self.index_path.write_text(w.dump_index(self.index), encoding="utf-8")
        self.assertEqual(self.run_archive(), {"copy": EXPECTED, "index": EXPECTED - 1, "keep": 0})
        files = json.loads(self.index_path.read_text(encoding="utf-8"))["files"]
        self.assertEqual(len(files), 2 + EXPECTED - 1)

    def test_a_copy_that_changes_the_bytes_stops_before_the_index_is_written(self):
        def corrupt(source, dest):
            Path(dest).write_bytes(b"corrupt")
        self.track_patch(mock.patch.object(w.shutil, "copyfile", side_effect=corrupt))
        with self.assertRaisesRegex(w.WrapupError, "changed the bytes"):
            self.run_archive()
        self.assertEqual(self.index_path.read_bytes(), self.index_bytes)

    def test_tree_that_is_the_repository_copies_roots_and_only_indexes_in_place_files(self):
        self.stage(self.repo)
        summary = self.run_archive(tree=self.repo)
        self.assertEqual(summary, {"copy": 4, "index": EXPECTED, "keep": 0})
        index = json.loads(self.index_path.read_text(encoding="utf-8"))
        self.assertEqual(index["notes"], ["first note"])
        self.assertEqual(len(index["files"]), 1 + EXPECTED)


class TestArchiveRefusals(ArchiveCase):
    def quality(self, suffix):
        return self.tree / ("quality-v%s%s.json" % (VER, suffix))

    def test_receipt_for_another_version_is_refused(self):
        self.edit_receipt(version="0.0.1")
        self.assert_refused("is for version")

    def test_receipt_without_a_candidate_hash_is_refused(self):
        self.edit_receipt(candidate_sha256="")
        self.assert_refused("carries no candidate_sha256")

    def test_hash_that_matches_no_record_is_refused(self):
        self.edit_receipt(candidate_sha256="1" * 64)
        self.assert_refused("found 0")

    def test_two_records_with_the_receipt_hash_are_refused(self):
        self.put("quality-v%s-copy.json" % VER, self.quality("").read_text(encoding="utf-8"), self.tree)
        self.assert_refused("found 2")

    def test_receipt_manifest_that_is_not_next_to_the_candidate_is_refused(self):
        self.edit_receipt(candidate_manifest_sha256=w.sha256_file(self.quality("-complete")))
        self.assert_refused("not the one next to")

    def test_no_complete_stage_record_is_refused(self):
        self.put("quality-v%s-complete.json" % VER, {"artifact_fingerprint": self.fingerprint,
                                                      "release_qualification": {"stage": "candidate"}}, self.tree)
        self.assert_refused("complete-stage record")

    def test_two_complete_stage_records_are_refused(self):
        self.put("quality-v%s-complete-again.json" % VER, self.quality("-complete").read_text(encoding="utf-8"),
                 self.tree)
        self.assert_refused("complete-stage record bound to the candidate, found 2")

    def test_complete_record_bound_to_another_candidate_is_refused(self):
        self.put("quality-v%s-complete.json" % VER, {"artifact_fingerprint": {"source": "elsewhere"},
                                                      "release_qualification": {"stage": "complete"}}, self.tree)
        self.assert_refused("found 0")

    def test_missing_manifest_next_to_a_record_is_refused(self):
        self.quality("-complete.manifest").unlink()
        self.assert_refused("missing manifest next to")

    def test_missing_companion_file_is_refused(self):
        (self.tree / w.ARCHIVE_DIR / ("v" + VER) / "publish.log").unlink()
        self.assert_refused("missing evidence files: publish.log")

    def test_unreadable_receipts_are_refused(self):
        for text in ("{not json", "[1, 2]"):
            with self.subTest(text=text):
                self.receipt_path().write_text(text, encoding="utf-8")
                self.assert_refused("cannot read|not a JSON object")
        self.receipt_path().unlink()
        self.assert_refused("cannot read")


class TestHelpers(WrapupCase):
    def test_utc_now_is_an_iso_stamp_in_utc(self):
        parsed = datetime.datetime.fromisoformat(w.utc_now())
        self.assertEqual(parsed.utcoffset(), datetime.timedelta(0))

    def test_version_arg_accepts_only_three_numeric_parts(self):
        self.assertEqual(w.version_arg(VER), VER)
        for bad in ("1.4", "v1.4.101", "1.4.101-beta", "1.4.*"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                w.version_arg(bad)

    def test_repo_root_outside_any_repository_is_a_refusal(self):
        with self.assertRaisesRegex(w.WrapupError, "rev-parse"):
            w.repo_root(self.root)

    def test_blob_sha256_of_a_missing_spec_is_none(self):
        self.init_repo(self.root / "r")
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertIsNone(w.blob_sha256(self.root / "r", "main:absent.txt"))


class RetireCase(WrapupCase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.init_repo(self.repo)
        self.commit(self.repo, "base.txt", "base\n")
        self.tree = self.root / "release tree"
        self.git(self.repo, "worktree", "add", "-q", "-b", "release/x", str(self.tree))
        self.tree = Path(os.path.realpath(self.tree))
        self.trash = self.root / "trash"

    def commit(self, cwd, name, text):
        self.put(name, text, cwd)
        self.git(cwd, "add", "--", name)
        self.git(cwd, "commit", "-qm", "add " + name)

    def retire(self, **overrides):
        args = dict(repo=self.repo, tree=self.tree, base="main", allow=[], trash=self.trash,
                    remote=False, apply=True)
        args.update(overrides)
        return w.retire(**args)

    def assert_blocked(self, pattern, **overrides):
        with self.assertRaisesRegex(w.WrapupError, pattern):
            self.retire(**overrides)
        self.assertTrue(self.tree.exists(), "a refusal must leave the tree where it was")
        self.assertFalse(self.trash.exists(), "a refusal must not touch the Trash")

    def branches(self):
        return self.git(self.repo, "branch", "--list", "release/x").stdout.strip()

    def add_origin(self, *refs):
        origin = self.root / "origin.git"
        origin.mkdir()
        self.git(origin, "init", "-q", "--bare", "--template=")
        self.git(self.repo, "remote", "add", "origin", str(origin))
        if refs:
            self.git(self.repo, "push", "-q", "origin", *refs)
        return origin

    def remote_heads(self):
        return self.git(self.repo, "ls-remote", "--heads", "origin").stdout

    def spawn(self, *extra, cwd=None):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *extra],
                                cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)


class TestRetire(RetireCase):
    def test_dry_run_reports_the_plan_and_changes_nothing(self):
        result = self.retire(apply=False)
        self.assertEqual(result, {"tree": "planned", "branch": "release/x", "remote": "not requested"})
        self.assertTrue(self.tree.is_dir())
        self.assertFalse(self.trash.exists())
        self.assertTrue(self.branches())

    def test_apply_moves_the_tree_to_the_trash_and_drops_the_merged_branch(self):
        result = self.retire()
        self.assertEqual((result["tree"], result["branch"]), ("moved", "deleted"))
        self.assertFalse(self.tree.exists())
        moved = [p.name for p in self.trash.iterdir()]
        self.assertEqual(len(moved), 1)
        self.assertTrue(moved[0].startswith(self.tree.name))
        self.assertEqual([r["worktree"] for r in w.worktree_records(self.repo)], [str(self.repo)])
        self.assertEqual(self.branches(), "")

    def test_trash_names_never_collide(self):
        self.track_patch(mock.patch.object(w, "utc_now", return_value=STAMP))
        first = w.trash_destination(self.trash, self.tree)
        self.assertEqual(first.name, self.tree.name + "-20300102T030405")
        first.mkdir(parents=True)
        second = w.trash_destination(self.trash, self.tree)
        self.assertEqual(second.name, first.name + "-2")
        second.mkdir()
        self.assertEqual(w.trash_destination(self.trash, self.tree).name, first.name + "-3")

    def test_the_main_worktree_a_plain_folder_and_a_vanished_tree_are_refused(self):
        self.assert_blocked("main worktree", tree=self.repo)
        plain = self.root / "plain"
        plain.mkdir()
        with self.assertRaisesRegex(w.WrapupError, "not a registered"):
            self.retire(tree=plain)
        os.rename(self.tree, self.root / "moved away")
        with self.assertRaisesRegex(w.WrapupError, "not a registered, existing worktree"):
            self.retire()

    def test_locked_worktree_is_refused_with_or_without_a_reason(self):
        self.git(self.repo, "worktree", "lock", "--reason", "busy", str(self.tree))
        self.assert_blocked("locked: busy")
        self.git(self.repo, "worktree", "unlock", str(self.tree))
        self.git(self.repo, "worktree", "lock", str(self.tree))
        self.assert_blocked("no reason given")

    def test_unknown_base_and_tree_on_the_base_branch_are_refused(self):
        self.assert_blocked("does not exist", base="no-such-branch")
        self.assert_blocked("on the base branch", base="release/x")

    def test_commit_missing_from_the_base_would_be_lost(self):
        self.commit(self.tree, "only-here.txt", "x\n")
        self.assert_blocked("not contained in main")

    def test_uncommitted_and_untracked_work_is_listed_and_refused(self):
        self.put("base.txt", "edited\n", self.tree)
        self.put("scratch.txt", "x\n", self.tree)
        self.put("说明.txt", "x\n", self.tree)
        with self.assertRaises(w.WrapupError) as raised:
            self.retire()
        text = str(raised.exception)
        for expected in ("uncommitted change: base.txt", "untracked and not preserved: scratch.txt",
                         "untracked and not preserved: 说明.txt"):
            self.assertIn(expected, text)
        self.assertTrue(self.tree.exists())

    def test_explicitly_allowed_untracked_file_does_not_block(self):
        self.put("scratch.txt", "x\n", self.tree)
        self.assertEqual(self.retire(apply=False, allow=["scratch.txt"])["tree"], "planned")
        self.assert_blocked("scratch.txt", allow=["other.txt"])

    def test_untracked_file_already_committed_at_the_same_path_is_preserved(self):
        self.commit(self.repo, "shared.txt", "same\n")
        self.put("shared.txt", "same\n", self.tree)
        self.assertEqual(self.retire(apply=False)["tree"], "planned")
        self.put("shared.txt", "different\n", self.tree)
        self.assert_blocked("untracked and not preserved: shared.txt")

    def test_untracked_file_is_preserved_when_the_committed_index_archives_the_same_bytes(self):
        data = "archived evidence\n"
        digest = hashlib.sha256(data.encode()).hexdigest()
        entry = {"sha256": digest, "to": "outputs/kept/evidence.json"}
        self.put(w.ARCHIVE_DIR + "/" + w.INDEX_NAME, {"files": [entry]}, self.repo)
        self.put(entry["to"], data, self.repo)
        self.git(self.repo, "add", "--", w.ARCHIVE_DIR, "outputs/kept")
        self.git(self.repo, "commit", "-qm", "archive")
        self.put("quality-copy.json", data, self.tree)
        self.assertEqual(self.retire(apply=False)["tree"], "planned")

    def test_archive_entry_whose_file_is_missing_or_changed_does_not_preserve(self):
        data = "archived evidence\n"
        entry = {"sha256": hashlib.sha256(data.encode()).hexdigest(), "to": "outputs/kept/evidence.json"}
        self.put(w.ARCHIVE_DIR + "/" + w.INDEX_NAME, {"files": [entry]}, self.repo)
        self.git(self.repo, "add", "--", w.ARCHIVE_DIR)
        self.git(self.repo, "commit", "-qm", "index only")
        self.put("quality-copy.json", data, self.tree)
        self.assert_blocked("quality-copy.json")
        self.commit(self.repo, entry["to"], "changed since it was indexed\n")
        self.assert_blocked("quality-copy.json")

    def advance_base_file(self, text="advanced\n"):
        """Main moves base.txt on; the release tree still sits on the old commit."""
        self.commit(self.repo, "base.txt", text)

    def test_tracked_change_whose_bytes_equal_the_base_version_does_not_block(self):
        self.advance_base_file()
        self.put("base.txt", "advanced\n", self.tree)
        self.assertIn(" M base.txt", self.git(self.tree, "status", "--porcelain").stdout)
        self.assertEqual(self.retire(apply=False)["tree"], "planned")
        self.git(self.tree, "add", "--", "base.txt")  # staged edits are judged by the same working copy
        self.assertEqual(self.retire(apply=False)["tree"], "planned")

    def test_tracked_change_that_differs_from_the_base_version_still_blocks(self):
        self.advance_base_file()
        self.put("base.txt", "advanced, then edited here\n", self.tree)
        self.assert_blocked("uncommitted change: base.txt")

    def test_mode_only_change_and_deletion_of_a_tracked_file_still_block(self):
        os.chmod(self.tree / "base.txt", 0o755)
        self.assertIn(" M base.txt", self.git(self.tree, "status", "--porcelain").stdout)
        self.assert_blocked("uncommitted change: base.txt")
        os.chmod(self.tree / "base.txt", 0o644)
        os.remove(self.tree / "base.txt")
        self.assert_blocked("uncommitted change: base.txt")

    def test_rename_and_new_staged_file_still_block_even_though_their_names_match_nothing_in_base(self):
        self.git(self.tree, "mv", "base.txt", "moved.txt")
        self.assert_blocked("uncommitted change: moved.txt")
        self.git(self.tree, "mv", "moved.txt", "base.txt")
        self.put("fresh.txt", "x\n", self.tree)
        self.git(self.tree, "add", "--", "fresh.txt")
        self.assert_blocked("uncommitted change: fresh.txt")


class TestRetireSafetyEdges(RetireCase):
    def test_a_real_process_working_in_the_tree_blocks_retirement(self):
        self.spawn(str(self.tree))
        self.assert_blocked("still in use")

    def test_a_process_in_a_similarly_named_sibling_does_not_block(self):
        sibling = Path(str(self.tree) + "-old")
        sibling.mkdir()
        self.spawn(str(sibling), cwd=sibling)
        self.assertEqual(self.retire(apply=False)["tree"], "planned")

    def test_an_idle_process_standing_in_the_tree_or_below_it_blocks_retirement(self):
        below = self.tree / "sub dir"
        below.mkdir()
        self.spawn(cwd=below)  # no mention of the tree on its command line: only lsof can see it
        self.assert_blocked("has its working directory at .*sub dir")

    def test_working_directory_filter_keeps_only_the_tree_and_what_is_below_it(self):
        listing = "\n".join(["p11", "fcwd", "n%s" % self.tree, "p22", "fcwd", "n%s/deep/er" % self.tree,
                             "p33", "fcwd", "n%s-old" % self.tree, "p44", "fcwd", "n/"]) + "\n"
        fake = subprocess.CompletedProcess([], 1, stdout=listing, stderr="")
        with mock.patch.object(w.subprocess, "run", return_value=fake):
            holders = w.cwd_holders(self.tree)
        self.assertEqual([h.split()[1] for h in holders], ["11", "22"])

    def test_empty_working_directory_listing_means_the_tree_cannot_be_proven_idle(self):
        fake = subprocess.CompletedProcess([], 1, stdout="", stderr="lsof: not permitted")
        with mock.patch.object(w.subprocess, "run", return_value=fake), \
                self.assertRaisesRegex(w.WrapupError, "cannot list working directories"):
            w.cwd_holders(self.tree)

    def test_process_filter_skips_ourselves_and_this_tool_but_keeps_real_users(self):
        lines = ["%d 1 python edit %s/a.txt" % (os.getpid(), self.tree),
                 "4242 1 python tools/release_wrapup.py retire --tree %s" % self.tree,
                 "4343 1 vim %s/real.txt" % self.tree,
                 "4444 1 vim %s-old/other.txt" % self.tree,
                 "4545 1 vim %s_scratch/other.txt" % self.tree]
        fake = subprocess.CompletedProcess([], 0, stdout="\n".join(lines) + "\n", stderr="")
        with mock.patch.object(w.subprocess, "run", return_value=fake):
            self.assertEqual(w.processes_using(self.tree), ["4343 vim %s/real.txt" % self.tree])

    def test_processes_that_launched_this_check_do_not_count_but_unrelated_ones_do(self):
        me = os.getpid()
        lines = ["%d 7002 python wrapper.py" % me,  # this process, no mention
                 "7002 7001 python tools/land_branch.py --tree %s --apply --retire" % self.tree,  # caller
                 "7001 7000 /bin/zsh -c cd %s && run" % self.tree,  # the shell wrapper around the caller
                 "7000 1 launcher --cwd %s" % self.tree,  # the top of the chain
                 "8001 7000 vim %s/notes.txt" % self.tree,  # shares the grandparent, not an ancestor
                 "8002 1 tail -f %s/log" % self.tree]
        fake = subprocess.CompletedProcess([], 0, stdout="\n".join(lines) + "\n", stderr="")
        with mock.patch.object(w.subprocess, "run", return_value=fake):
            users = w.processes_using(self.tree)
        self.assertEqual([u.split()[0] for u in users], ["8001", "8002"])

    def test_calling_chain_follows_parents_stops_at_gaps_and_survives_cycles(self):
        parents = {"10": "9", "9": "8", "8": "1", "1": "0", "50": "51", "51": "50"}
        self.assertEqual(w.calling_chain(parents, "10"), {"10", "9", "8", "1"})
        self.assertEqual(w.calling_chain(parents, "50"), {"50", "51"})
        self.assertEqual(w.calling_chain(parents, "999"), set())

    def test_a_real_caller_naming_the_tree_on_its_own_command_line_does_not_block(self):
        # The parent's own command line carries the tree path but not the tool's file name (that comes from
        # the environment), so the old "skip lines that name release_wrapup" rule cannot hide it: only
        # recognising it as the caller's ancestor lets the retire check pass.
        code = ("import os, subprocess, sys; sys.exit(subprocess.run([sys.executable, os.environ['SF_TEST_TOOL'],"
                " 'retire', '--tree', sys.argv[1], '--repo', sys.argv[2], '--base', 'main']).returncode)")
        env = dict(self.env, PYTHONPATH=str(SCRIPT.parent.parent), SF_TEST_TOOL=str(SCRIPT))
        caller = subprocess.run([sys.executable, "-c", code, str(self.tree), str(self.repo)], cwd=str(self.repo),
                                env=env, capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(caller.returncode, 0, caller.stdout + caller.stderr)
        self.assertIn("dry-run", caller.stdout)
        self.assertTrue(self.tree.exists())

    def test_unreadable_process_list_means_the_tree_cannot_be_proven_idle(self):
        fake = subprocess.CompletedProcess([], 1, stdout="", stderr="denied")
        with mock.patch.object(w.subprocess, "run", return_value=fake), \
                self.assertRaisesRegex(w.WrapupError, "cannot list running processes"):
            w.processes_using(self.tree)

    def test_a_tree_whose_git_state_cannot_be_read_is_refused(self):
        (self.tree / ".git").write_text("not a gitfile\n", encoding="utf-8")
        self.assert_blocked("git status failed")

    def test_detached_tree_is_moved_and_no_branch_is_touched(self):
        detached = Path(os.path.realpath(self.root / "detached"))
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(detached))
        result = self.retire(tree=detached, remote=True)
        self.assertEqual((result["tree"], result["branch"], result["remote"]),
                         ("moved", "detached", "not requested"))
        self.assertTrue(self.branches())

    def test_branch_merged_elsewhere_but_not_into_head_is_kept_not_forced(self):
        self.commit(self.tree, "feature.txt", "x\n")
        self.git(self.repo, "branch", "stable", "release/x")
        result = self.retire(base="stable")
        self.assertEqual(result["tree"], "moved")
        self.assertTrue(result["branch"].startswith("kept: "), result["branch"])
        self.assertTrue(self.branches())


class TestRetireRemote(RetireCase):
    def test_dry_run_names_the_remote_commit_and_deletes_nothing(self):
        self.add_origin("main", "release/x")
        result = self.retire(remote=True, apply=False)
        self.assertRegex(result["remote"], "^[0-9a-f]{40}$")
        self.assertIn("refs/heads/release/x", self.remote_heads())

    def test_apply_deletes_the_merged_branch_on_origin(self):
        self.add_origin("main", "release/x")
        result = self.retire(remote=True)
        self.assertEqual((result["tree"], result["branch"], result["remote"]), ("moved", "deleted", "deleted"))
        self.assertNotIn("release/x", self.remote_heads())
        self.assertIn("refs/heads/main", self.remote_heads())

    def test_branch_that_origin_never_had_is_reported_absent(self):
        self.add_origin("main")
        self.assertEqual(self.retire(remote=True)["remote"], "absent")

    def test_remote_branch_with_commits_outside_the_base_is_refused(self):
        self.add_origin("main")
        tree_id = self.git(self.repo, "rev-parse", "main^{tree}").stdout.strip()
        side = self.git(self.repo, "commit-tree", "-p", "main", "-m", "side", tree_id).stdout.strip()
        self.git(self.repo, "push", "-q", "origin", side + ":refs/heads/release/x")
        self.assert_blocked("origin/release/x .* is not contained in main", remote=True)

    def test_missing_origin_is_a_refusal_before_anything_moves(self):
        self.assert_blocked("ls-remote", remote=True)

    def test_remote_delete_failure_says_the_tree_is_already_in_the_trash(self):
        origin = self.add_origin("main", "release/x")
        self.git(origin, "config", "receive.denyDeletes", "true")
        with self.assertRaisesRegex(w.WrapupError, "already in the Trash"):
            self.retire(remote=True)
        self.assertFalse(self.tree.exists())
        self.assertIn("release/x", self.remote_heads())


class TestArchiveLabel(ArchiveCase):
    """A same-version re-release: the earlier vX.Y.Z archive stays, the new evidence lands in vX.Y.Z-label."""

    def setUp(self):
        super().setUp()
        self.label = "stable"
        self.labeled_folder = self.repo / w.ARCHIVE_DIR / ("v%s-%s" % (VER, self.label))
        self.tree2 = self.root / "tree2"
        self.tree2.mkdir()
        self.stage(self.tree2, label=self.label, fingerprint={"source": "re-release"})

    def archive_earlier_release(self):
        self.run_archive()
        return {p.name: p.read_bytes() for p in self.folder.iterdir()}

    def labeled_receipt(self, **changes):
        path = self.tree2 / w.ARCHIVE_DIR / ("v%s-%s" % (VER, self.label)) / "post_release.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        for key in [k for k, v in changes.items() if v is None]:
            del data[key]
        self.put(w.ARCHIVE_DIR + "/v%s-%s/post_release.json" % (VER, self.label), data, self.tree2)

    def test_labeled_archive_lands_beside_the_earlier_folder_and_leaves_it_untouched(self):
        earlier = self.archive_earlier_release()
        before = json.loads(self.index_path.read_text(encoding="utf-8"))["files"]
        self.assertEqual(w.archive(self.repo, self.tree2, VER, True, self.label),
                         {"copy": EXPECTED, "index": EXPECTED, "keep": 0})
        self.assertEqual({p.name: p.read_bytes() for p in self.folder.iterdir()}, earlier)
        self.assertEqual(len(list(self.labeled_folder.iterdir())), EXPECTED)
        index = json.loads(self.index_path.read_text(encoding="utf-8"))
        self.assertEqual(index["files"][:len(before)], before)
        added = index["files"][len(before):]
        prefix = "%s/v%s-%s/" % (w.ARCHIVE_DIR, VER, self.label)
        self.assertEqual(len(added), EXPECTED)
        self.assertTrue(all(entry["to"].startswith(prefix) for entry in added))
        self.assertIn("v%s-%s entries" % (VER, self.label), index["notes"][-1])
        self.assertEqual(w.archive(self.repo, self.tree2, VER, True, self.label),
                         {"copy": 0, "index": 0, "keep": EXPECTED})

    def test_the_same_evidence_without_a_label_collides_with_the_earlier_archive(self):
        self.archive_earlier_release()
        tree3 = self.root / "tree3"
        tree3.mkdir()
        self.stage(tree3, fingerprint={"source": "re-release"})
        with self.assertRaisesRegex(w.WrapupError, "already exists with different bytes"):
            w.archive(self.repo, tree3, VER, True)

    def test_receipt_label_and_requested_label_must_agree(self):
        before = self.index_path.read_bytes()
        self.labeled_receipt(archive_label="other")
        with self.assertRaisesRegex(w.WrapupError, "archive label 'other' but 'stable' was asked for"):
            w.archive(self.repo, self.tree2, VER, True, self.label)
        self.labeled_receipt(archive_label=None)
        with self.assertRaisesRegex(w.WrapupError, "archive label None but 'stable' was asked for"):
            w.archive(self.repo, self.tree2, VER, True, self.label)
        self.edit_receipt(archive_label=self.label)
        with self.assertRaisesRegex(w.WrapupError, "archive label 'stable' but None was asked for"):
            w.archive(self.repo, self.tree, VER, True)
        for folder in (self.folder, self.labeled_folder):
            self.assertFalse(folder.exists(), "a refusal must not leave a half-made archive folder")
        self.assertEqual(self.index_path.read_bytes(), before)

    def test_unsafe_labels_are_refused_before_any_file_is_read(self):
        for label in ("../x", "Stable", "a/b", "", "x" * 40):
            with self.subTest(label=label), self.assertRaises(w.WrapupError):
                w.archive(self.repo, self.tree2, VER, True, label)
        self.assertFalse(self.labeled_folder.exists())
        self.assertEqual(self.index_path.read_bytes(), self.index_bytes)

    def test_command_line_label_selects_the_labeled_folder_and_rejects_bad_ones(self):
        argv = ["archive", VER, "--tree", str(self.tree2), "--repo", str(self.repo)]
        self.assertEqual(w.main(argv + ["--label", self.label]), 0)
        self.assertFalse(self.labeled_folder.exists(), "without --apply nothing is written")
        self.assertEqual(w.main(argv + ["--label", self.label, "--apply"]), 0)
        self.assertEqual(len(list(self.labeled_folder.iterdir())), EXPECTED)
        for bad in ("Bad/Name", "..", ""):
            with self.subTest(label=bad), self.assertRaises(SystemExit) as raised:
                w.main(argv + ["--label", bad])
            self.assertEqual(raised.exception.code, 2)


class TestCommandLine(ArchiveCase):
    def argv(self, *extra):
        return ["archive", VER, "--tree", str(self.tree), "--repo", str(self.repo), *extra]

    def test_archive_is_a_dry_run_until_apply_is_given(self):
        self.assertEqual(w.main(self.argv()), 0)
        self.assertFalse(self.folder.exists())
        self.assertEqual(w.main(self.argv("--apply")), 0)
        self.assertEqual(len(list(self.folder.iterdir())), EXPECTED)

    def test_refusals_exit_1_and_name_the_reason_on_stderr(self):
        self.edit_receipt(candidate_sha256="1" * 64)
        self.assertEqual(w.main(self.argv("--apply")), 1)
        self.assertIn("refused:", self.err.getvalue())

    def test_bad_arguments_exit_2(self):
        for argv in ([], ["archive", "1.4", "--tree", str(self.tree)], ["archive", VER], ["unknown"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as raised:
                w.main(argv)
            self.assertEqual(raised.exception.code, 2)


class TestRetireCommandLine(RetireCase):
    def argv(self, *extra):
        return ["retire", "--tree", str(self.tree), "--repo", str(self.repo), "--base", "main",
                "--trash", str(self.trash), *extra]

    def test_retire_is_a_dry_run_until_apply_is_given(self):
        self.assertEqual(w.main(self.argv()), 0)
        self.assertTrue(self.tree.exists())
        self.put("scratch.txt", "x\n", self.tree)
        self.assertEqual(w.main(self.argv("--apply")), 1)
        self.assertTrue(self.tree.exists())
        self.assertEqual(w.main(self.argv("--apply", "--allow", "scratch.txt")), 0)
        self.assertFalse(self.tree.exists())

    def test_remote_flag_without_an_origin_is_a_refusal(self):
        self.assertEqual(w.main(self.argv("--remote", "--apply")), 1)
        self.assertIn("ls-remote", self.err.getvalue())
        self.assertTrue(self.tree.exists())

    def test_running_the_file_directly_exits_with_the_command_status(self):
        previous = list(sys.path)
        self.addCleanup(lambda: setattr(sys, "path", previous))
        argv = [str(SCRIPT), "retire", "--tree", str(self.root / "nowhere"), "--repo", str(self.repo)]
        with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as raised:
            runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("not a registered", self.err.getvalue())


class PushCase(WrapupCase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.init_repo(self.repo)
        self.commit("base.txt", "base\n")
        self.origin = self.root / "origin.git"
        self.origin.mkdir()
        self.git(self.origin, "init", "-q", "--bare", "--template=", "-b", "main")
        self.git(self.repo, "remote", "add", "origin", str(self.origin))
        self.git(self.repo, "push", "-q", "origin", "main")

    def commit(self, name, text, cwd=None):
        cwd = cwd or self.repo
        self.put(name, text, cwd)
        self.git(cwd, "add", "--", name)
        self.git(cwd, "commit", "-qm", "add " + name)

    def head(self):
        return self.git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def tip(self, ref="refs/heads/main"):
        out = self.git(self.repo, "ls-remote", str(self.origin), ref).stdout.split()
        return out[0] if out else ""

    def push(self, **overrides):
        args = dict(repo=self.repo, branch="main", remote="origin", apply=True)
        args.update(overrides)
        return w.push(**args)


class TestPush(PushCase):
    def test_dry_run_lists_the_pending_commits_and_pushes_nothing(self):
        before = self.tip()
        self.commit("one.txt", "1\n")
        self.commit("two.txt", "2\n")
        result = self.push(apply=False)
        self.assertEqual((result["commits"], result["before"], result["head"]), (2, before, self.head()))
        self.assertNotIn("after", result)
        self.assertEqual(self.tip(), before)

    def test_apply_fast_forwards_and_reads_the_tip_back(self):
        self.commit("one.txt", "1\n")
        result = self.push()
        self.assertEqual(result["after"], self.head())
        self.assertEqual(self.tip(), self.head())
        self.assertEqual(result["remote"], str(self.origin))

    def test_nothing_to_push_is_reported_and_not_pushed(self):
        result = self.push()
        self.assertEqual(result["commits"], 0)
        self.assertNotIn("after", result)

    def test_only_the_checked_out_branch_can_be_pushed(self):
        self.git(self.repo, "checkout", "-q", "-b", "side")
        with self.assertRaisesRegex(w.WrapupError, "checked-out branch is side, not main"):
            self.push()
        self.git(self.repo, "checkout", "-q", "--detach")
        with self.assertRaisesRegex(w.WrapupError, "a detached HEAD, not main"):
            self.push()

    def test_unknown_remote_and_remote_without_the_branch_are_refused(self):
        with self.assertRaisesRegex(w.WrapupError, "remote get-url --push nope failed"):
            self.push(remote="nope")
        empty = self.root / "empty.git"
        empty.mkdir()
        self.git(empty, "init", "-q", "--bare", "--template=")
        self.git(self.repo, "remote", "add", "empty", str(empty))
        with self.assertRaisesRegex(w.WrapupError, "empty has no main"):
            self.push(remote="empty")

    def test_remote_that_moved_on_is_refused_never_forced(self):
        other = self.root / "other"
        self.git(self.root, "clone", "-q", str(self.origin), str(other))
        for key, value in (("user.name", "other"), ("user.email", "other@example.invalid"),
                           ("commit.gpgsign", "false")):
            self.git(other, "config", key, value)
        self.commit("theirs.txt", "t\n", other)
        self.git(other, "push", "-q", "origin", "main")
        theirs = self.tip()
        self.commit("mine.txt", "m\n")
        with self.assertRaisesRegex(w.WrapupError, "never forces"):
            self.push()
        self.assertEqual(self.tip(), theirs)

    def test_a_rejected_push_is_reported_and_leaves_the_remote_alone(self):
        hook = self.origin / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="ascii")
        hook.chmod(0o755)
        before = self.tip()
        self.commit("one.txt", "1\n")
        with self.assertRaisesRegex(w.WrapupError, "git push failed"):
            self.push()
        self.assertEqual(self.tip(), before)

    def test_a_push_that_claims_success_but_did_not_land_is_not_believed(self):
        real, calls = w.git, []

        def fake(cwd, *args):
            if args[0] == "push":
                calls.append(args)
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")
            return real(cwd, *args)
        self.track_patch(mock.patch.object(w, "git", side_effect=fake))
        self.commit("one.txt", "1\n")
        with self.assertRaisesRegex(w.WrapupError, "reads back"):
            self.push()
        self.assertEqual(calls, [("push", "origin", "refs/heads/main:refs/heads/main")])

    def test_readback_follows_the_push_url_when_it_differs_from_the_fetch_url(self):
        mirror = self.root / "mirror.git"
        mirror.mkdir()
        self.git(mirror, "init", "-q", "--bare", "--template=", "-b", "main")
        self.git(self.repo, "push", "-q", str(mirror), "main")
        self.git(self.repo, "config", "remote.origin.pushurl", str(mirror))
        self.commit("one.txt", "1\n")
        result = self.push()
        self.assertEqual(result["remote"], str(mirror))
        self.assertEqual(self.git(self.repo, "ls-remote", str(mirror), "refs/heads/main").stdout.split()[0],
                         self.head())
        self.assertNotEqual(self.tip(), self.head(), "the fetch URL side must be untouched")

    def test_a_lookalike_ref_is_not_mistaken_for_the_branch(self):
        real = self.tip()
        self.commit("one.txt", "1\n")
        # ls-remote matches patterns by tail, and 'refs/a/...' sorts before 'refs/heads/...'
        self.git(self.repo, "push", "-q", "origin", "HEAD:refs/a/refs/heads/main")
        self.assertEqual(w.remote_tip(self.repo, str(self.origin), "refs/heads/main"), real)
        self.assertEqual(w.remote_tip(self.repo, str(self.origin), "refs/heads/absent"), "")

    def test_credentials_in_urls_never_reach_the_output(self):
        self.assertEqual(w.redact("https://user:tok@github.com/a/b.git"), "https://***@github.com/a/b.git")
        self.assertEqual(w.redact("/plain/path.git"), "/plain/path.git")
        leaky = subprocess.CompletedProcess([], 128, stdout="", stderr="fatal: https://u:secret@h/x.git denied")
        self.track_patch(mock.patch.object(w, "git", return_value=leaky))
        with self.assertRaises(w.WrapupError) as raised:
            w.git_out(self.repo, "fetch", "https://u:secret@h/x.git")
        self.assertNotIn("secret", str(raised.exception))


class TestPushCommandLine(PushCase):
    def argv(self, *extra):
        return ["push", "--repo", str(self.repo), "--branch", "main", "--to", "origin", *extra]

    def test_push_is_a_dry_run_until_apply_is_given(self):
        before = self.tip()
        self.commit("one.txt", "1\n")
        self.assertEqual(w.main(self.argv()), 0)
        self.assertEqual(self.tip(), before)
        self.assertEqual(w.main(self.argv("--apply")), 0)
        self.assertEqual(self.tip(), self.head())

    def test_refusals_exit_1_and_nothing_is_pushed(self):
        before = self.tip()
        self.commit("one.txt", "1\n")
        self.assertEqual(w.main(["push", "--repo", str(self.repo), "--branch", "other", "--apply"]), 1)
        self.assertIn("refused:", self.err.getvalue())
        self.assertEqual(self.tip(), before)


class CommitCase(WrapupCase):
    HEAD_AUTHOR = ("Head Author", "head@example.invalid")

    def setUp(self):
        super().setUp()
        self.track_patch(mock.patch.dict(os.environ))  # restored on stop, so the pops below stay local
        for name in ("SF_SKIP_COVERAGE", "SF_RANDOM_ORDER", "SF_PUBLIC_REGRESSION", "SF_GUARD_PROBE"):
            os.environ.pop(name, None)
        # The product's commit environment deliberately drops every GIT_* variable, so the isolation that
        # WrapupCase gives its own git calls never reached the preflight and `git commit` it launches. Those
        # then read the host's global and system git config: a CI runner with git-lfs installed carries
        # filter.lfs.*, which the preflight rightly refuses (special_checkout_configuration), and five cases
        # failed there while passing on a machine without it. Re-apply the isolation on top of the real
        # function; the product behaviour under test is unchanged.
        self.real_commit_env = w.commit_env

        def isolated_commit_env(identity=None):
            env = self.real_commit_env(identity)
            env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
            return env

        self.track_patch(mock.patch.object(w, "commit_env", isolated_commit_env))
        self.repo = self.root / "repo"
        self.init_repo(self.repo)
        self.git(self.repo, "config", "user.name", "config-user")
        self.git(self.repo, "config", "user.email", "config@example.invalid")
        for name in ("base.txt", "other.txt"):
            self.put(name, "base\n", self.repo)
        self.git(self.repo, "add", "--", "base.txt", "other.txt")
        self.git_as(self.repo, self.HEAD_AUTHOR, "commit", "-qm", "base")
        self.install_hook("#!/bin/sh\necho gate-says-hello\necho ran >> .git/hook-ran\nexit 0\n")
        self.msg = self.put("msg.txt", "feat: probe\n\nbody line\n")
        self.log = self.root / "hook.log"

    def git_as(self, cwd, who, *args):
        env = dict(self.env, GIT_AUTHOR_NAME=who[0], GIT_AUTHOR_EMAIL=who[1],
                   GIT_COMMITTER_NAME=who[0], GIT_COMMITTER_EMAIL=who[1])
        result = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True,
                                timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def install_hook(self, body):
        source = self.put("git-hooks/pre-commit", body, self.repo)
        source.chmod(0o755)
        self.git(self.repo, "add", "--", "git-hooks/pre-commit")
        self.git_as(self.repo, self.HEAD_AUTHOR, "commit", "-qm", "hook")
        installed = self.repo / ".git" / "hooks" / "pre-commit"
        installed.parent.mkdir(exist_ok=True)
        installed.write_text(body, encoding="utf-8")
        installed.chmod(0o755)

    def head(self):
        return self.git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def staged(self):
        return self.git(self.repo, "diff", "--cached", "--name-only").stdout.split()

    def hook_ran(self):
        return (self.repo / ".git" / "hook-ran").exists()

    def commit(self, paths=("base.txt",), **overrides):
        args = dict(repo=self.repo, paths=list(paths), message_file=self.msg, author=None,
                    expected_head=None, apply=True, timeout=60, log=self.log)
        args.update(overrides)
        return w.commit(**args)

    def change(self, name="base.txt", text="changed\n"):
        self.put(name, text, self.repo)

    def ident(self):
        return self.git(self.repo, "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce").stdout.strip().split("\0")


class TestCommit(CommitCase):
    def test_dry_run_plans_the_commit_and_touches_nothing(self):
        self.change()
        before = self.head()
        result = self.commit(apply=False)
        self.assertEqual((result["branch"], result["head"], result["paths"]), ("main", before, ["base.txt"]))
        self.assertEqual(result["author"], "%s <%s>" % self.HEAD_AUTHOR)
        self.assertEqual((self.staged(), self.head(), self.hook_ran()), ([], before, False))

    def test_apply_commits_exactly_the_named_files_through_the_real_hook(self):
        self.change()
        self.change("new.txt", "new\n")
        self.change("scratch.txt", "not mine\n")
        before = self.head()
        result = self.commit(paths=["base.txt", "new.txt"])
        self.assertEqual(result["commit"], self.head())
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD^").stdout.strip(), before)
        files = self.git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.split()
        self.assertEqual(sorted(files), ["base.txt", "new.txt"])
        self.assertEqual(self.ident(), list(self.HEAD_AUTHOR) * 2, "the HEAD author, not the repo's config user")
        body = self.git(self.repo, "log", "-1", "--format=%B").stdout.strip()
        self.assertEqual(body, "feat: probe\n\nbody line")
        self.assertTrue(self.hook_ran(), "the real pre-commit hook must run")
        self.assertIn("gate-says-hello", self.log.read_text(encoding="utf-8"))
        self.assertEqual(self.git(self.repo, "status", "--porcelain").stdout.strip(), "?? scratch.txt")

    def test_host_git_filter_config_does_not_decide_the_outcome(self):
        # The CI-only failure: the host's own git config (here a global one under HOME, which the product's
        # environment cleaning does not touch) carries a filter, and the preflight refused the commit.
        home = self.root / "host-home"
        home.mkdir()
        (home / ".gitconfig").write_text('[filter "lfs"]\n\tclean = cat\n\tsmudge = cat\n', encoding="utf-8")
        os.environ["HOME"] = str(home)  # restored with the rest of os.environ in tearDown
        self.change()
        self.assertEqual(self.commit()["commit"], self.head())

    def test_explicit_author_applies_to_this_commit_only(self):
        self.change()
        self.commit(author="Ex Plicit <ex@example.invalid>")
        self.assertEqual(self.ident(), ["Ex Plicit", "ex@example.invalid"] * 2)
        self.assertEqual(self.git(self.repo, "config", "user.name").stdout.strip(), "config-user")

    def test_without_a_log_path_the_hook_output_goes_to_a_temp_file(self):
        self.change()
        result = self.commit(log=None)
        log = Path(result["log"])
        self.addCleanup(log.unlink, missing_ok=True)
        self.assertIn("gate-says-hello", log.read_text(encoding="utf-8"))

    def test_expected_head_that_still_matches_is_accepted(self):
        self.change()
        self.assertEqual(self.commit(expected_head=self.head())["commit"], self.head())


class TestCommitRefusals(CommitCase):
    def assert_refused(self, pattern, **overrides):
        before = self.head()
        with self.assertRaisesRegex(w.WrapupError, pattern):
            self.commit(**overrides)
        self.assertEqual((self.staged(), self.head(), self.hook_ran()), ([], before, False))

    def test_head_that_moved_since_the_scope_was_approved(self):
        self.change()
        self.assert_refused("not the expected", expected_head="1" * 40)

    def test_a_path_without_a_pending_change_or_a_directory_is_refused(self):
        self.change()
        self.assert_refused("no pending change in: other.txt", paths=["base.txt", "other.txt"])
        self.change("sub/a.txt", "a\n")
        self.assert_refused("no pending change in: sub", paths=["sub"])

    def test_bad_message_files_are_refused(self):
        self.change()
        self.assert_refused("cannot read the message file", message_file=self.root / "missing.txt")
        binary = self.root / "binary.txt"
        binary.write_bytes(b"\xff\xfe\x00")
        self.assert_refused("cannot read the message file", message_file=binary)
        self.assert_refused("is empty", message_file=self.put("blank.txt", " \n\n"))

    def test_detached_head_and_a_dirty_index_are_refused(self):
        self.change()
        self.git(self.repo, "add", "--", "base.txt")
        before = self.head()
        with self.assertRaisesRegex(w.WrapupError, "already holds staged changes"):
            self.commit()
        self.assertEqual((self.staged(), self.head()), (["base.txt"], before))
        self.git(self.repo, "reset", "-q")
        self.git(self.repo, "checkout", "-q", "--detach")
        # the preflight would refuse a detached HEAD too, with the word 'detached' in its own message;
        # requiring this tool's wording proves the early guard fires before anything is staged
        self.assert_refused("HEAD is detached; commit on a branch")

    def test_bad_author_and_bad_paths_are_refused(self):
        self.change()
        self.assert_refused("--author must look like", author="nobody")
        for paths in ([], ["../x"], ["base.txt", "base.txt"], ["/abs"], ["./base.txt"], ["a//b"]):
            with self.subTest(paths=paths):
                self.assert_refused("exact, repo-relative", paths=paths)

    def test_preflight_refusal_unstages_again_and_leaves_the_edits_alone(self):
        self.change()
        self.change("other.txt", "edited but not in scope\n")
        self.assert_refused("commit_preflight refused, nothing committed: .*unstaged_tracked_changes")
        self.assertEqual((self.repo / "other.txt").read_text(encoding="utf-8"), "edited but not in scope\n")
        self.assertEqual((self.repo / "base.txt").read_text(encoding="utf-8"), "changed\n")

    def test_quality_short_circuit_environment_is_refused_by_the_preflight(self):
        self.change()
        os.environ["SF_SKIP_COVERAGE"] = "1"
        self.assert_refused("quality_environment_override")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestCommitHookOutcomes(CommitCase):
    def test_a_failing_hook_means_no_commit_and_its_message_is_shown(self):
        self.install_hook("#!/bin/sh\necho gate-rejected-this >&2\nexit 1\n")
        self.change()
        before = self.head()
        with self.assertRaisesRegex(w.WrapupError, "exited 1, nothing committed") as raised:
            self.commit()
        self.assertIn("gate-rejected-this", str(raised.exception))
        self.assertEqual((self.staged(), self.head()), ([], before))
        self.assertEqual((self.repo / "base.txt").read_text(encoding="utf-8"), "changed\n")

    def test_a_hook_that_changes_the_index_is_reported_after_the_fact(self):
        self.install_hook("#!/bin/sh\necho more >> base.txt\ngit add base.txt\nexit 0\n")
        self.change()
        before = self.head()
        with self.assertRaisesRegex(w.WrapupError, "different tree from the one reviewed"):
            self.commit()
        self.assertNotEqual(self.head(), before, "the commit exists, and the error says which one")

    def test_a_hook_that_hangs_is_stopped_with_everything_it_started(self):
        self.install_hook("#!/bin/sh\ntrap '' TERM\necho $$ > .git/hook-pid\n"
                          "sleep 300 &\necho $! > .git/sleep-pid\nwait\n")
        self.change()
        before = self.head()
        with self.assertRaisesRegex(w.WrapupError, "did not finish within"):
            self.commit(timeout=3)
        pids = [int((self.repo / ".git" / name).read_text(encoding="utf-8")) for name in ("hook-pid", "sleep-pid")]
        for pid in pids:
            self.addCleanup(lambda p=pid: alive(p) and os.kill(p, signal.SIGKILL))
            self.assertTrue(self.wait_until_false(lambda p=pid: alive(p), timeout=10),
                            "process %d outlived the call" % pid)
        self.assertEqual((self.staged(), self.head()), ([], before))


class TestCommitPieces(CommitCase):
    def test_path_scope_rules(self):
        w.check_paths(["a.py", "dir/b.py"])
        for bad in ([], [""], ["."], ["a", "a"], ["/a"], ["../a"], ["a/../b"], ["a//b"], ["./a"]):
            with self.subTest(bad=bad), self.assertRaises(w.WrapupError):
                w.check_paths(bad)

    def test_author_parsing(self):
        self.assertEqual(w.parse_author(" Some One <so@example.invalid> "), ("Some One", "so@example.invalid"))
        for bad in ("", "Name", "<a@b.c>", "Name <a>", "Name <a@b> extra", "A <b@c> <d@e>"):
            with self.subTest(bad=bad), self.assertRaises(w.WrapupError):
                w.parse_author(bad)

    def test_commit_environment_drops_routing_and_pins_the_identity(self):
        os.environ["GIT_DIR"] = "/elsewhere"
        os.environ["SF_RERUN_FAILED"] = "1"
        plain = self.real_commit_env()  # the product function itself, not the isolated test wrapper
        self.assertNotIn("GIT_DIR", plain)
        self.assertNotIn("GIT_AUTHOR_NAME", plain)
        self.assertEqual(plain["SF_RERUN_FAILED"], "0")
        env = self.real_commit_env(("N", "n@example.invalid"))
        for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
            self.assertEqual(env[key], "N")
        for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
            self.assertEqual(env[key], "n@example.invalid")

    def test_changed_paths_counts_each_file_once_including_renames(self):
        self.git(self.repo, "mv", "base.txt", "renamed.txt")
        self.git(self.repo, "rm", "-q", "other.txt")
        self.change("sub/new.txt", "n\n")
        self.assertEqual(w.changed_paths(self.repo), {"renamed.txt", "other.txt", "sub/new.txt"})
        with self.assertRaisesRegex(w.WrapupError, "git status failed"):
            w.changed_paths(self.root)

    def test_head_identity_reads_the_last_authors_name_and_email(self):
        self.assertEqual(w.head_identity(self.repo), self.HEAD_AUTHOR)

    def test_preflight_wrapper_reports_garbage_and_unexplained_failures(self):
        head = self.head()  # read before patching: self.git goes through the same subprocess.run
        garbage = subprocess.CompletedProcess([], 1, stdout="not json", stderr="boom")
        with mock.patch.object(w.subprocess, "run", return_value=garbage), \
                self.assertRaisesRegex(w.WrapupError, "gave no report: boom"):
            w.preflight_problems(self.repo, ["base.txt"], head)
        silent = subprocess.CompletedProcess([], 1, stdout='{"checks_passed": false}', stderr="")
        with mock.patch.object(w.subprocess, "run", return_value=silent):
            self.assertIn("without naming a reason", w.preflight_problems(self.repo, ["base.txt"], head))

    def test_verify_commit_rejects_each_way_the_result_can_differ_from_the_plan(self):
        self.change()
        parent = self.head()
        tree_after = self.commit()["tree"]
        new = self.head()
        ident = self.HEAD_AUTHOR
        good = dict(repo=self.repo, parent=parent, tree=tree_after, paths=["base.txt"], identity=ident)
        self.assertEqual(w.verify_commit(**good), new)
        for override, pattern in ((dict(parent=new), "HEAD did not move"),
                                  (dict(parent="0" * 40), "does not sit directly"),
                                  (dict(tree="0" * 40), "different tree"),
                                  (dict(paths=["other.txt"]), "other than the requested"),
                                  (dict(identity=("Someone", "else@example.invalid")), "carries the identity")):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(w.WrapupError, pattern):
                w.verify_commit(**dict(good, **override))


class TestCommitCommandLine(CommitCase):
    def argv(self, *extra):
        return ["commit", "--repo", str(self.repo), "--path", "base.txt", "-m", str(self.msg),
                "--log", str(self.log), *extra]

    def test_commit_is_a_dry_run_until_apply_is_given(self):
        self.change()
        before = self.head()
        self.assertEqual(w.main(self.argv()), 0)
        self.assertEqual((self.staged(), self.head()), ([], before))
        self.assertEqual(w.main(self.argv("--apply", "--author", "Cli User <cli@example.invalid>",
                                          "--expected-head", before, "--timeout", "60")), 0)
        self.assertEqual(self.ident(), ["Cli User", "cli@example.invalid"] * 2)

    def test_refusals_exit_1_and_nothing_is_staged(self):
        self.assertEqual(w.main(self.argv("--apply")), 1)
        self.assertIn("refused: no pending change", self.err.getvalue())
        self.assertEqual(self.staged(), [])

    def test_path_and_message_are_required(self):
        for argv in (["commit", "-m", str(self.msg)], ["commit", "--path", "base.txt"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as raised:
                w.main(argv)
            self.assertEqual(raised.exception.code, 2)


class TestCheckReady(CommitCase):
    """Every refusal `commit` can make before it touches the index, asked on its own and without side effects."""

    def ready(self, **overrides):
        args = dict(repo=self.repo, paths=["base.txt"], message_file=self.msg, author=None, expected_head=None)
        args.update(overrides)
        return w.check_ready(**args)

    def test_it_reports_the_plan_without_printing_or_changing_anything(self):
        self.change()
        before = self.head()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ready = self.ready()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual((ready["branch"], ready["head"], ready["paths"]), ("main", before, ["base.txt"]))
        self.assertEqual((ready["identity"], ready["author"]), (self.HEAD_AUTHOR, "%s <%s>" % self.HEAD_AUTHOR))
        self.assertIn("feat: probe", ready["message"])
        self.assertEqual((self.staged(), self.head(), self.hook_ran()), ([], before, False))

    def test_the_author_option_replaces_the_head_identity(self):
        self.change()
        self.assertEqual(self.ready(author="Cli User <cli@example.invalid>")["identity"],
                         ("Cli User", "cli@example.invalid"))

    def test_every_refusal_is_raised_before_anything_is_staged(self):
        self.change()
        cases = ((dict(paths=["/abs.txt"]), "distinct, exact"),
                 (dict(message_file=self.root / "missing.txt"), "cannot read the message file"),
                 (dict(message_file=self.put("blank.txt", " \n")), "is empty"),
                 (dict(expected_head="0" * 40), "not the expected"),
                 (dict(author="nonsense"), "--author must look like"),
                 (dict(paths=["other.txt"]), "no pending change in: other.txt"))
        for override, pattern in cases:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(w.WrapupError, pattern):
                self.ready(**override)
        self.assertEqual(self.staged(), [])

    def test_an_index_that_already_holds_staged_changes_is_refused(self):
        self.change("other.txt", "x\n")
        self.git(self.repo, "add", "--", "other.txt")
        self.change()
        with self.assertRaisesRegex(w.WrapupError, "already holds staged changes"):
            self.ready()
        self.assertEqual(self.staged(), ["other.txt"])

    def test_a_detached_head_is_refused(self):
        self.change()
        self.git(self.repo, "checkout", "-q", "--detach")
        with self.assertRaisesRegex(w.WrapupError, "HEAD is detached"):
            self.ready()

class MergeCase(CommitCase):
    """A repository with `main` and a diverged `side` branch, so a merge between them is a real two-parent one."""

    def setUp(self):
        super().setUp()
        self.git(self.repo, "checkout", "-q", "-b", "side")
        self.put("side.txt", "side\n", self.repo)
        self.git(self.repo, "add", "--", "side.txt")
        self.git_as(self.repo, self.HEAD_AUTHOR, "commit", "-qm", "side work")
        self.side = self.head()
        self.git(self.repo, "checkout", "-q", "main")
        self.put("main.txt", "main\n", self.repo)
        self.git(self.repo, "add", "--", "main.txt")
        self.git_as(self.repo, self.HEAD_AUTHOR, "commit", "-qm", "main work")
        (self.repo / ".git" / "hook-ran").unlink(missing_ok=True)  # the setup commits above ran the hook too
        self.main = self.head()

    def start_merge(self):
        """Leave an unfinished, conflict-free merge of `side` into `main` and return its index tree."""
        self.git(self.repo, "merge", "-q", "--no-ff", "--no-commit", "side")
        return self.git(self.repo, "write-tree").stdout.strip()

    def start_conflict(self):
        """Leave an unfinished merge that has an unresolved conflict in base.txt."""
        self.git(self.repo, "checkout", "-q", "-b", "clash", self.main)
        self.change("base.txt", "clash side\n")
        self.git(self.repo, "add", "--", "base.txt")
        self.git_as(self.repo, self.HEAD_AUTHOR, "commit", "-qm", "clash")
        self.git(self.repo, "checkout", "-q", "main")
        self.change("base.txt", "main side\n")
        self.git(self.repo, "add", "--", "base.txt")
        self.git_as(self.repo, self.HEAD_AUTHOR, "commit", "-qm", "main edit")
        merged = subprocess.run(["git", "merge", "-q", "--no-ff", "--no-commit", "clash"], cwd=str(self.repo),
                                env=self.env, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(merged.returncode, 1, merged.stdout + merged.stderr)

    def write_hook(self, body):
        """Replace the installed hook without committing (a commit would end the merge being tested)."""
        installed = self.repo / ".git" / "hooks" / "pre-commit"
        installed.write_text(body, encoding="utf-8")
        installed.chmod(0o755)


class TestMergeState(MergeCase):
    def test_no_merge_means_no_heads(self):
        self.assertEqual(w.merge_heads(self.repo), [])
        self.assertEqual(w.merge_heads(self.root), [])  # not a repository at all

    def test_an_unfinished_merge_names_the_commit_it_brings_in(self):
        tree = self.start_merge()
        self.assertEqual(w.merge_heads(self.repo), [self.side])
        state = w.merge_state(self.repo)
        self.assertEqual(state, {"head": self.main, "merge_heads": [self.side], "tree": tree, "unmerged": [],
                                 "worktree_clean": True})

    def test_an_edited_working_file_is_reported_as_not_clean(self):
        self.start_merge()
        self.change("main.txt", "edited after review\n")
        self.assertFalse(w.merge_state(self.repo)["worktree_clean"])

    def test_a_conflict_is_listed_and_leaves_no_tree(self):
        self.start_conflict()
        state = w.merge_state(self.repo)
        self.assertEqual((state["unmerged"], state["tree"]), (["base.txt"], ""))
        self.assertEqual(len(state["merge_heads"]), 1)

    def test_outside_a_repository_every_field_fails_the_comparison(self):
        state = w.merge_state(self.root)
        self.assertEqual((state["head"], state["merge_heads"], state["tree"], state["unmerged"]), ("", [], "", []))

class TestCheckMergeReady(MergeCase):
    def ready(self, **overrides):
        args = dict(repo=self.repo, message_file=self.msg, author=None, expected_head=self.main,
                    source=self.side, expected_tree=self.tree)
        args.update(overrides)
        return w.check_merge_ready(**args)

    def setUp(self):
        super().setUp()
        self.tree = self.start_merge()

    def test_the_reviewed_merge_is_ready_and_nothing_is_printed_or_changed(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ready = self.ready()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual((ready["branch"], ready["head"], ready["source"], ready["tree"]),
                         ("main", self.main, self.side, self.tree))
        self.assertEqual((ready["identity"], ready["author"]), (self.HEAD_AUTHOR, "%s <%s>" % self.HEAD_AUTHOR))
        self.assertEqual((self.head(), w.merge_heads(self.repo), self.hook_ran()), (self.main, [self.side], False))

    def test_the_reviewed_values_are_optional_for_a_caller_that_has_none(self):
        self.assertEqual(self.ready(expected_head=None, source=None, expected_tree=None)["source"], self.side)

    def test_the_author_option_replaces_the_head_identity(self):
        self.assertEqual(self.ready(author="Cli User <cli@example.invalid>")["identity"],
                         ("Cli User", "cli@example.invalid"))

    def test_every_refusal_names_what_is_wrong(self):
        cases = ((dict(source="0" * 40), "not the expected source"),
                 (dict(expected_tree="0" * 40), "not the reviewed"),
                 (dict(expected_head="0" * 40), "not the expected"),
                 (dict(message_file=self.root / "missing.txt"), "cannot read the message file"),
                 (dict(message_file=self.put("blank.txt", "\n")), "is empty"),
                 (dict(author="nonsense"), "--author must look like"))
        for override, pattern in cases:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(w.WrapupError, pattern):
                self.ready(**override)

    def test_a_working_file_that_differs_from_the_index_is_refused(self):
        self.change("main.txt", "edited after review\n")
        with self.assertRaisesRegex(w.WrapupError, "working files differ from the index"):
            self.ready()

    def test_a_merge_with_two_sources_is_refused(self):
        (self.repo / ".git" / "MERGE_HEAD").write_text("%s\n%s\n" % (self.side, self.main), encoding="utf-8")
        with self.assertRaisesRegex(w.WrapupError, "lists 2 commits"):
            self.ready()

    def test_a_tree_that_cannot_be_computed_is_refused(self):
        broken = dict(w.merge_state(self.repo), tree="")
        with mock.patch.object(w, "merge_state", return_value=broken), \
                self.assertRaisesRegex(w.WrapupError, "write-tree failed"):
            self.ready()

    def test_no_merge_at_all_is_refused(self):
        self.git(self.repo, "merge", "--abort")
        with self.assertRaisesRegex(w.WrapupError, "no merge is in progress"):
            self.ready()

    def test_an_unresolved_conflict_is_listed_by_name(self):
        self.git(self.repo, "merge", "--abort")
        self.start_conflict()
        with self.assertRaisesRegex(w.WrapupError, "unresolved conflicts: base.txt"):
            self.ready(expected_head=None, source=None, expected_tree=None)


class TestVerifyMerge(MergeCase):
    def test_only_the_reviewed_merge_commit_is_accepted(self):
        tree = self.start_merge()
        self.git_as(self.repo, self.HEAD_AUTHOR, "commit", "-qm", "merge side")
        good = dict(repo=self.repo, head=self.main, source=self.side, tree=tree, identity=self.HEAD_AUTHOR)
        self.assertEqual(w.verify_merge(**good), self.head())
        new = self.head()
        for override, pattern in ((dict(head=new), "HEAD did not move"),
                                  (dict(head=self.side, source=self.main), "has parents"),
                                  (dict(source="0" * 40), "has parents"),
                                  (dict(tree="0" * 40), "different tree"),
                                  (dict(identity=("Someone", "else@example.invalid")), "carries the identity")):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(w.WrapupError, pattern):
                w.verify_merge(**dict(good, **override))
        (self.repo / ".git" / "MERGE_HEAD").write_text(self.side + "\n", encoding="utf-8")
        with self.assertRaisesRegex(w.WrapupError, "MERGE_HEAD is still present"):
            w.verify_merge(**good)

class TestMergeCommit(MergeCase):
    def setUp(self):
        super().setUp()
        self.tree = self.start_merge()

    def merge(self, **overrides):
        args = dict(repo=self.repo, message_file=self.msg, author=None, expected_head=self.main, source=self.side,
                    expected_tree=self.tree, apply=True, timeout=60, log=self.log)
        args.update(overrides)
        return w.merge_commit(**args)

    def parents(self):
        return self.git(self.repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()[1:]

    def test_a_dry_run_plans_the_merge_and_runs_nothing(self):
        result = self.merge(apply=False)
        self.assertEqual((result["head"], result["source"], result["tree"]), (self.main, self.side, self.tree))
        self.assertEqual((self.head(), w.merge_heads(self.repo), self.hook_ran(), self.log.exists()),
                         (self.main, [self.side], False, False))

    def test_apply_commits_the_reviewed_merge_through_the_real_hook(self):
        result = self.merge()
        self.assertEqual(result["commit"], self.head())
        self.assertEqual(self.parents(), [self.main, self.side])
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD^{tree}").stdout.strip(), self.tree)
        self.assertEqual(self.ident(), list(self.HEAD_AUTHOR) * 2)
        self.assertTrue(self.hook_ran(), "the real pre-commit hook must run")
        self.assertIn("gate-says-hello", self.log.read_text(encoding="utf-8"))
        self.assertEqual(w.merge_heads(self.repo), [])
        self.assertEqual(self.git(self.repo, "log", "-1", "--format=%s").stdout.strip(), "feat: probe")

    def test_the_author_option_is_applied_to_this_commit_only(self):
        self.merge(author="Cli User <cli@example.invalid>")
        self.assertEqual(self.ident(), ["Cli User", "cli@example.invalid"] * 2)
        self.assertEqual(self.git(self.repo, "config", "user.name").stdout.strip(), "config-user")

    def test_without_a_log_path_the_hook_output_goes_to_a_temp_file(self):
        result = self.merge(log=None)
        log = Path(result["log"])
        self.addCleanup(log.unlink, missing_ok=True)
        self.assertIn("gate-says-hello", log.read_text(encoding="utf-8"))

    def test_a_refused_merge_never_reaches_the_hook(self):
        with self.assertRaisesRegex(w.WrapupError, "not the reviewed"):
            self.merge(expected_tree="0" * 40)
        self.assertEqual((self.hook_ran(), self.head(), w.merge_heads(self.repo)), (False, self.main, [self.side]))

    def test_a_failing_hook_leaves_the_merge_exactly_as_it_was_and_shows_its_message(self):
        self.write_hook("#!/bin/sh\necho gate-rejected-this >&2\nexit 1\n")
        with self.assertRaisesRegex(w.WrapupError, "still uncommitted") as raised:
            self.merge()
        self.assertIn("gate-rejected-this", str(raised.exception))
        self.assertEqual((self.head(), w.merge_heads(self.repo)), (self.main, [self.side]))
        self.assertEqual(w.merge_state(self.repo)["tree"], self.tree)
        self.write_hook("#!/bin/sh\nexit 0\n")  # the same merge can simply be committed again
        self.assertEqual(self.merge()["commit"], self.head())

    def test_the_lost_lock_marker_survives_into_the_error_text(self):
        self.write_hook("#!/bin/sh\necho 已有另一轮完整回归 >&2\nexit 1\n")
        with self.assertRaises(w.WrapupError) as raised:
            self.merge()
        self.assertIn("已有另一轮完整回归", str(raised.exception))

    def test_a_hook_that_changes_the_index_is_reported_after_the_fact(self):
        self.write_hook("#!/bin/sh\necho more >> side.txt\ngit add side.txt\nexit 0\n")
        with self.assertRaisesRegex(w.WrapupError, "different tree from the one reviewed"):
            self.merge()
        self.assertNotEqual(self.head(), self.main, "the commit exists, and the error says which one")
