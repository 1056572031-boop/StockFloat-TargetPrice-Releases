"""Exercise merge workflow boundaries in disposable repositories, never the real tree."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from tests._base import SFCase
from tools.gate_stamp import git_env

ROOT = Path(__file__).resolve().parents[1]


class TestBranchMergeWorkflow(SFCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(prefix="sf-merge-workflow-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.repo = self.root / "repository with spaces"
        self.repo.mkdir()
        self.env = git_env()
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        self.git("init", "-q", "--template=", "-b", "mainline")
        self.git("config", "user.name", "workflow-test")
        self.git("config", "user.email", "workflow@example.invalid")
        self.write("base.txt", "base\n")
        self.commit("base", "base.txt")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()

    def run_command(self, args, *, cwd=None, expected=0):
        result = subprocess.run(args, cwd=cwd or self.repo, env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def git(self, *args, cwd=None, expected=0):
        return self.run_command(["git", *args], cwd=cwd, expected=expected)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def commit(self, message, *paths):
        self.git("add", "--", *paths)
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def source_branch(self, name="feature"):
        self.git("switch", "-qc", name)
        self.write("feature.txt", "feature\n")
        source = self.commit("feature", "feature.txt")
        self.git("switch", "-q", "mainline")
        return source

    def installer_fixture(self):
        for name in ("install-hooks.sh", "pre-commit"):
            path = self.repo / "git-hooks" / name
            path.parent.mkdir(exist_ok=True)
            if name == "pre-commit":
                path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            else:
                shutil.copyfile(ROOT / "git-hooks" / name, path)
        self.commit("installer", "git-hooks/install-hooks.sh", "git-hooks/pre-commit")

    def install(self, *, cwd=None, expected=0, args=()):
        repo = cwd or self.repo
        return self.run_command(["/bin/bash", str(repo / "git-hooks/install-hooks.sh"),
                                 *args], cwd=repo, expected=expected)

    def hook_path(self, cwd=None):
        return Path(self.git("rev-parse", "--path-format=absolute", "--git-path",
                             "hooks/pre-commit", cwd=cwd).stdout.strip())

    def hook_compile_fixture(self, *, bad=False):
        """Real hook in a disposable repo; the interpreter and run_tests.sh are logging stubs."""
        hook = self.repo / "git-hooks/pre-commit"
        hook.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / "git-hooks/pre-commit", hook)
        hook.chmod(0o755)
        calls = self.repo / "compile-calls.log"
        stub = self.write(".venv/bin/python3", (
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "Python stub"; exit 0; fi\n'
            # 钩子里取并行车道默认值也会调一次解释器（-c），那不是编译；这条用例守的是
            # “全部项目文件共用一次 py_compile 批处理”，所以只记 -m py_compile 的调用。
            'if [ "$1" != "-m" ]; then exit 0; fi\n'
            f'printf "%s\\n" "--CALL--" >> "{calls}"\n'
            'shift 2\n'
            'for f in "$@"; do\n'
            f'  printf "%s\\n" "$f" >> "{calls}"\n'
            f'  case "$f" in *bad.py) exit 1;; esac\n'
            "done\n"))
        stub.chmod(0o755)
        runner = self.write("run_tests.sh", '#!/bin/sh\nprintf ran > tests-ran\n')
        runner.chmod(0o755)
        included = ["a.py", "dir with space/b.py", "sub/build/nested.py", "sub/backups/nested.py"]
        excluded = ["backups/x.py", ".venv/lib/x.py", ".pyenvs/x.py", "build/x.py",
                    "dist/x.py", "pkg/__pycache__/x.py"]
        for name in included + excluded + (["bad.py"] if bad else []):
            self.write(name, "VALUE = 1\n")
        return calls, included, excluded

    def read_compile_calls(self, calls):
        batches, current = [], None
        for line in calls.read_text(encoding="utf-8").splitlines():
            if line == "--CALL--":
                current = []
                batches.append(current)
            else:
                current.append(line)
        return batches

    def test_pre_commit_compiles_the_project_python_set_in_one_batch_with_exclusions(self):
        calls, included, excluded = self.hook_compile_fixture()
        result = self.run_command(["/bin/bash", str(self.repo / "git-hooks/pre-commit")])
        batches = self.read_compile_calls(calls)
        self.assertEqual(len(batches), 1, "every project file must share one interpreter start")
        seen = sorted(path.removeprefix("./") for path in batches[0])
        self.assertEqual(seen, sorted(included))
        for name in excluded:
            self.assertNotIn(name, seen, result.stdout)
        self.assertEqual((self.repo / "tests-ran").read_text(), "ran")

    def test_pre_commit_stops_before_the_test_gate_when_one_batched_file_fails(self):
        calls, _included, _excluded = self.hook_compile_fixture(bad=True)
        result = self.run_command(["/bin/bash", str(self.repo / "git-hooks/pre-commit")],
                                  expected=1)
        self.assertIn("py_compile", result.stdout + result.stderr)
        self.assertEqual(len(self.read_compile_calls(calls)), 1)
        self.assertFalse((self.repo / "tests-ran").exists())

    def test_no_ff_no_commit_stops_before_moving_mainline_and_commit_runs_hook(self):
        source = self.source_branch()
        hook = self.hook_path()
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\nprintf ran > hook-ran\n", encoding="ascii")
        hook.chmod(0o755)
        self.git("merge", "--no-ff", "--no-commit", "--no-autostash", source)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.base)
        self.assertEqual(self.git("rev-parse", "MERGE_HEAD").stdout.strip(), source)
        tree = self.git("write-tree").stdout.strip()
        self.git("diff", "--quiet", "--exit-code")
        self.git("commit", "-qm", "merge reviewed source")
        self.assertEqual((self.repo / "hook-ran").read_text(), "ran")
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}").stdout.strip(), tree)
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", "HEAD")
                         .stdout.split()[1:], [self.base, source])

    def test_unstaged_change_is_rejected_by_index_worktree_comparison(self):
        self.write("base.txt", "staged\n")
        self.git("add", "--", "base.txt")
        staged_tree = self.git("write-tree").stdout.strip()
        self.write("base.txt", "tested but not staged\n")
        self.git("diff", "--quiet", "--exit-code", expected=1)
        self.assertEqual(self.git("write-tree").stdout.strip(), staged_tree)
        self.assertEqual(self.git("show", ":base.txt").stdout, "staged\n")

    def test_hook_index_mutation_changes_commit_tree_and_invalidates_saved_tree(self):
        source = self.source_branch()
        self.git("merge", "--no-ff", "--no-commit", "--no-autostash", source)
        reviewed_tree = self.git("write-tree").stdout.strip()
        hook = self.hook_path()
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\nprintf changed > base.txt\n"
                        "git add -- base.txt\n", encoding="ascii")
        hook.chmod(0o755)
        self.git("commit", "-qm", "hook changed index")
        self.assertNotEqual(self.git("rev-parse", "HEAD^{tree}").stdout.strip(),
                            reviewed_tree)

    def test_failed_commit_hook_preserves_uncommitted_merge_for_inspection(self):
        source = self.source_branch()
        self.git("merge", "--no-ff", "--no-commit", "--no-autostash", source)
        hook = self.hook_path()
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="ascii")
        hook.chmod(0o755)
        self.git("commit", "-qm", "must fail", expected=1)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.base)
        self.assertEqual(self.git("rev-parse", "MERGE_HEAD").stdout.strip(), source)

    def test_clean_preview_does_not_change_head_or_index(self):
        source = self.source_branch()
        index = self.git("write-tree").stdout.strip()
        preview = self.git("merge-tree", "--write-tree", self.base, source)
        self.git("cat-file", "-t", preview.stdout.splitlines()[0])
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.base)
        self.assertEqual(self.git("write-tree").stdout.strip(), index)

    def test_conflict_has_nonzero_preview_and_unresolved_index_then_abort_restores(self):
        self.git("switch", "-qc", "feature")
        self.write("base.txt", "feature\n")
        source = self.commit("feature", "base.txt")
        self.git("switch", "-q", "mainline")
        self.write("base.txt", "mainline\n")
        mainline = self.commit("mainline", "base.txt")
        self.git("merge-tree", "--write-tree", mainline, source, expected=1)
        self.git("merge", "--no-ff", "--no-commit", "--no-autostash", source, expected=1)
        self.assertIn("base.txt", self.git("diff", "--name-only", "--diff-filter=U").stdout)
        result = self.git("write-tree", expected=128)
        self.assertIn("unmerged", result.stderr)
        self.git("merge", "--abort")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), mainline)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_already_integrated_source_has_no_merge_head_or_new_commit(self):
        result = self.git("merge", "--no-ff", "--no-commit", "--no-autostash", self.base)
        self.assertIn("Already up to date", result.stdout)
        self.git("rev-parse", "--verify", "MERGE_HEAD", expected=128)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.base)

    def test_ignored_runtime_is_invisible_to_status_but_source_tree_reveals_symlink(self):
        self.write(".gitignore", ".venv\n.venv/\n")
        self.commit("ignore runtime", ".gitignore")
        self.git("switch", "-qc", "feature")
        (self.repo / ".venv").symlink_to("/nonexistent/runtime")
        self.git("add", "-f", "--", ".venv")
        self.git("commit", "-qm", "unsafe source")
        source = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("switch", "-q", "mainline")
        self.write(".venv", "local runtime\n")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        entries = self.git("ls-tree", "-r", source, "--", ".venv").stdout
        self.assertTrue(entries.startswith("120000 "), entries)
        self.assertIn(".venv", self.git("ls-files", "--others", "--ignored",
                                       "--exclude-standard").stdout)

    def test_installer_supports_linked_worktree_and_is_idempotent(self):
        self.installer_fixture()
        linked = self.root / "linked worktree"
        self.git("worktree", "add", "-q", "-b", "linked", str(linked))
        self.assertTrue((linked / ".git").is_file())
        self.install(cwd=linked)
        self.install(cwd=linked)
        hook = self.hook_path(linked)
        self.assertEqual(hook.read_bytes(), (linked / "git-hooks/pre-commit").read_bytes())
        self.assertTrue(os.access(hook, os.X_OK))

    def test_installer_honors_relative_absolute_and_in_tree_hooks_paths(self):
        self.installer_fixture()
        for hooks in ("custom hooks", str(self.root / "absolute hooks"), "git-hooks"):
            with self.subTest(hooks=hooks):
                self.git("config", "core.hooksPath", hooks)
                self.install()
                self.assertEqual(self.hook_path().read_bytes(),
                                 (self.repo / "git-hooks/pre-commit").read_bytes())

    def test_installer_preserves_different_existing_hook_and_rejects_symlink(self):
        self.installer_fixture()
        hook = self.hook_path()
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("do not overwrite\n", encoding="ascii")
        self.install(expected=1)
        self.assertEqual(hook.read_text(), "do not overwrite\n")
        hook.unlink()
        target = self.root / "outside hook"
        target.write_text("preserve\n", encoding="ascii")
        hook.symlink_to(target)
        self.install(expected=1)
        self.assertTrue(hook.is_symlink())
        self.assertEqual(target.read_text(), "preserve\n")

    def test_installer_rejects_unknown_arguments_without_writing_hook(self):
        self.installer_fixture()
        self.install(expected=2, args=("--force",))
        self.assertFalse(self.hook_path().exists())

    def test_installer_rejects_foreign_git_environment_without_touching_other_repo(self):
        self.installer_fixture()
        foreign = self.root / "foreign"
        foreign.mkdir()
        self.git("init", "-q", "--template=", str(foreign))
        for variables in ({"GIT_DIR": str(foreign / ".git")},
                          {"GIT_WORK_TREE": str(foreign)},
                          {"GIT_INDEX_FILE": str(foreign / "index")},
                          {"GIT_COMMON_DIR": str(foreign / ".git")}):
            with self.subTest(variables=variables):
                self.env.update(variables)
                try:
                    self.install(expected=1)
                finally:
                    for key in variables:
                        self.env.pop(key)
                self.assertFalse((foreign / ".git/hooks/pre-commit").exists())
                self.assertFalse(self.hook_path().exists())

    def test_installer_rejects_non_repository_missing_source_and_directory_target(self):
        self.installer_fixture()
        outside = self.root / "not a repository"
        (outside / "git-hooks").mkdir(parents=True)
        shutil.copyfile(ROOT / "git-hooks/install-hooks.sh",
                        outside / "git-hooks/install-hooks.sh")
        self.install(cwd=outside, expected=1)
        self.assertFalse((outside / ".git").exists())
        source = self.repo / "git-hooks/pre-commit"
        source.unlink()
        self.install(expected=1)
        self.assertFalse(self.hook_path().exists())
        source.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        self.hook_path().mkdir(parents=True)
        self.install(expected=1)
        self.assertTrue(self.hook_path().is_dir())

    def test_push_target_readback_uses_push_url_not_fetch_url(self):
        fetch = self.root / "fetch.git"
        push = self.root / "push.git"
        for remote in (fetch, push):
            self.git("init", "-q", "--bare", "--template=", str(remote))
        self.git("remote", "add", "origin", str(fetch))
        self.git("remote", "set-url", "--push", "origin", str(push))
        self.assertEqual(self.git("remote", "get-url", "--push", "--all", "origin")
                         .stdout.strip(), str(push))
        self.git("config", "push.followTags", "true")
        self.git("config", "remote.origin.mirror", "true")
        self.git("tag", "-a", "local-only", "-m", "must not push", self.base)
        self.git("-c", "push.followTags=false", "-c", "remote.origin.mirror=false",
                 "push", "--no-follow-tags", "--recurse-submodules=no", "origin",
                 self.base + ":refs/heads/mainline")
        self.assertEqual(self.git("ls-remote", "--exit-code", str(push),
                                  "refs/heads/mainline").stdout.split()[0], self.base)
        self.git("ls-remote", "--exit-code", str(push), "refs/tags/local-only", expected=2)
        self.git("ls-remote", "--exit-code", str(fetch), "refs/heads/mainline", expected=2)
