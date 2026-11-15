# -*- coding: utf-8 -*-
"""只检查相对基线新增代码的架构门禁。"""
import importlib.util
import io
import ast
import os
import pathlib
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests._base import SFCase

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _run_git(root, *args, input_text=None, env=None):
    command_env = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    if env:
        command_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=root,
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
        env=command_env,
    )


def _init_git_repo():
    directory = tempfile.TemporaryDirectory()
    root = pathlib.Path(directory.name)
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "test@example.invalid")
    _run_git(root, "config", "user.name", "Test User")
    return directory, root


def _commit_file(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _run_git(root, "add", "--", relative)
    _run_git(root, "commit", "-qm", "base")


def _stage_file(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _run_git(root, "add", "--", relative)


def _assert_has_rule(testcase, findings, rule, path):
    testcase.assertTrue(
        any(item.rule == rule and item.path == path for item in findings),
        findings,
    )


def _assert_no_path(testcase, findings, path):
    testcase.assertFalse(any(item.path == path for item in findings), findings)



def _load_gate():
    source_path = ROOT / "tools" / "architecture_increment_gate.py"
    spec = importlib.util.spec_from_file_location(
        "architecture_increment_gate", source_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # discipline-ok(R5): 测试加载工作树门禁实现
    return module


class IncrementGateTests(SFCase):
    def test_script_entrypoint_returns_success_for_clean_repository(self):
        directory, root = _init_git_repo()
        self.addCleanup(directory.cleanup)
        _commit_file(root, "module.py", "value = 1\n")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "architecture_increment_gate.py"),
                "--root",
                str(root),
                "--base",
                "HEAD",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(result.stdout, "")

    def test_script_entrypoint_runs_in_process(self):
        directory, root = _init_git_repo()
        self.addCleanup(directory.cleanup)
        _commit_file(root, "module.py", "value = 1\n")
        old_argv = sys.argv
        sys.argv = [
            str(ROOT / "tools" / "architecture_increment_gate.py"),
            "--root",
            str(root),
            "--base",
            "HEAD",
        ]
        try:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(
                    str(ROOT / "tools" / "architecture_increment_gate.py"),
                    run_name="__main__",
                )
        finally:
            sys.argv = old_argv
        self.assertEqual(raised.exception.code, 0)

    def test_new_window_network_call_is_rejected(self):
        gate = _load_gate()
        findings = gate.scan_source(
            "stock_float_window.py",
            """
import urllib.request

def added_feature(self):
    return urllib.request.urlopen('https://example.invalid')
""",
        )
        self.assertEqual([item.rule for item in findings], ["window.external_io"])

    def test_existing_window_network_call_is_not_scanned_as_new_code(self):
        gate = _load_gate()
        findings = gate.scan_source(
            "stock_float_window.py",
            """
import urllib.request

def legacy_feature(self):
    return urllib.request.urlopen('https://example.invalid')
""",
            added_lines=set(),
        )
        self.assertEqual(findings, [])

    def test_new_module_host_private_access_is_rejected(self):
        gate = _load_gate()
        findings = gate.scan_source(
            "new_feature.py",
            """
def run(host):
    return host._update_checking
""",
        )
        self.assertEqual([item.rule for item in findings], ["integration.host_private_state"])

    def test_new_diagnostic_side_effect_is_rejected(self):
        gate = _load_gate()
        findings = gate.scan_source(
            "new_feature.py",
            """
def diagnostics():
    with open('/tmp/diagnostics.json', 'w') as stream:
        stream.write('{}')
    return {}
""",
        )
        self.assertEqual([item.rule for item in findings], ["diagnostics.side_effect"])

    def test_clean_new_controller_is_accepted(self):
        gate = _load_gate()
        findings = gate.scan_source(
            "new_feature.py",
            """
class FeatureController:
    def __init__(self):
        self._state = {}

    def diagnostics(self):
        return dict(self._state)
""",
        )
        self.assertEqual(findings, [])

    def test_untracked_new_module_is_scanned(self):
        gate = _load_gate()
        directory, root = _init_git_repo()
        try:
            _commit_file(root, "base.py", "value = 1\n")
            (root / "new_feature.py").write_text(
                "def run(host):\n    return host._private\n", encoding="utf-8"
            )
            findings = gate._untracked_python_files(root)  # discipline-ok(R5): 真实临时 Git 仓库验证未跟踪路径读取
        finally:
            directory.cleanup()
        self.assertEqual(findings["new_feature.py"], {1, 2})

    def test_finding_value_contract_and_unknown_call_shape(self):
        gate = _load_gate()
        first = gate.Finding("rule", "path.py", 3, "message")
        same = gate.Finding("rule", "path.py", 3, "message")
        other = gate.Finding("other", "path.py", 3, "message")
        self.assertEqual(first, same)
        self.assertNotEqual(first, other)
        self.assertEqual(hash(first), hash(same))
        self.assertEqual(
            gate.scan_source("new.py", "def f():\n    return (lambda: 1)()\n"),
            [],
        )

    def test_syntax_error_is_reported(self):
        gate = _load_gate()
        findings = gate.scan_source("broken.py", "def broken(:\n")
        self.assertEqual(findings[0].rule, "syntax.error")

    def test_new_window_storage_call_is_rejected(self):
        gate = _load_gate()
        findings = gate.scan_source(
            "stock_float_window.py",
            "def save(self):\n    return save_config('code')\n",
        )
        self.assertEqual([item.rule for item in findings], ["window.external_io"])

    def test_changed_python_files_collects_new_lines_and_new_root_file(self):
        gate = _load_gate()
        directory, root = _init_git_repo()
        try:
            _commit_file(root, "base.py", "value = 1\n")
            _stage_file(root, "new.py", "value = 2\n")
            changes = gate.changed_python_files(root)  # discipline-ok(R5): 真实临时 Git 仓库验证索引视图
        finally:
            directory.cleanup()
        self.assertEqual(changes["new.py"], {1})

    def test_changed_python_files_collects_added_lines_from_git_diff(self):
        gate = _load_gate()
        directory, root = _init_git_repo()
        try:
            _commit_file(root, "changed.py", "value = 1\n")
            _stage_file(root, "changed.py", "value = 1\nvalue = 2\n")
            cached_diff = _run_git(
                root, "diff", "--cached", "--unified=0", "--", "changed.py"
            )
            self.assertIn("@@ -1,0 +2 @@", cached_diff.stdout)
            changes = gate.changed_python_files(root)
        finally:
            directory.cleanup()
        self.assertEqual(changes["changed.py"], {2})

    def test_changed_python_files_skips_path_removed_before_read(self):
        gate = _load_gate()
        directory, root = _init_git_repo()
        try:
            _commit_file(root, "gone.py", "value = 1\n")
            _run_git(root, "rm", "-q", "gone.py")
            self.assertEqual(gate.changed_python_files(root), {})  # discipline-ok(R5): 删除文件边界必须直接验证
        finally:
            directory.cleanup()

    def test_scan_repo_merges_untracked_and_skips_missing_path(self):
        gate = _load_gate()
        directory, root = _init_git_repo()
        try:
            _commit_file(root, "base.py", "value = 1\n")
            (root / "new.py").write_text("value = 1\n", encoding="utf-8")
            self.assertEqual(gate.scan_repo(root), [])
        finally:
            directory.cleanup()

    def test_main_reports_findings_and_returns_zero_when_clean(self):
        gate = _load_gate()
        finding = gate.Finding("rule", "new.py", 1, "bad")
        with mock.patch.object(gate, "scan_repo", return_value=[finding]), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(gate.main(["--root", ".", "--base", "HEAD"]), 1)
            self.assertIn("new.py:1: rule: bad", output.getvalue())
        with mock.patch.object(gate, "scan_repo", return_value=[]):
            self.assertEqual(gate.main(["--root", ".", "--base", "HEAD"]), 0)

    def test_main_module_exit_codes_are_one_for_findings_and_zero_when_clean(self):
        script = ROOT / "tools" / "architecture_increment_gate.py"
        for root_content, expected in (("def run(host):\n    return host._private\n", 1), ("value = 1\n", 0)):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                (root / "feature.py").write_text(root_content, encoding="utf-8")
                _run_git(root, "init", "-q")
                result = subprocess.run(
                    [sys.executable, str(script), "--root", str(root), "--base", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, expected)

    def test_module_cli_exit_path(self):
        self.test_main_module_exit_codes_are_one_for_findings_and_zero_when_clean()

    def test_staged_content_is_scanned_even_when_worktree_differs(self):
        gate = _load_gate()
        directory, root = _init_git_repo()
        try:
            _commit_file(root, "feature.py", "value = 1\n")
            _stage_file(root, "feature.py", "def run(host):\n    return host._private\n")
            (root / "feature.py").write_text("value = 2\n", encoding="utf-8")

            findings = gate.scan_repo(root)
        finally:
            directory.cleanup()
        _assert_has_rule(self, findings, "integration.host_private_state", "feature.py")

    def test_staged_new_file_is_scanned_after_worktree_deletion(self):
        gate = _load_gate()
        directory, root = _init_git_repo()
        try:
            _stage_file(root, "new_feature.py", "def run(host):\n    return host._private\n")
            (root / "new_feature.py").unlink()

            findings = gate.scan_repo(root)
        finally:
            directory.cleanup()
        _assert_has_rule(self, findings, "integration.host_private_state", "new_feature.py")

    def test_staged_rename_scans_index_destination_content(self):
        gate = _load_gate()
        directory, root = _init_git_repo()
        try:
            _commit_file(root, "old_feature.py", "value = 1\n")
            _run_git(root, "mv", "old_feature.py", "renamed_feature.py")
            (root / "renamed_feature.py").write_text(
                "def run(host):\n    return host._private\n", encoding="utf-8"
            )
            _run_git(root, "add", "--", "renamed_feature.py")

            findings = gate.scan_repo(root)
        finally:
            directory.cleanup()
        _assert_has_rule(self, findings, "integration.host_private_state", "renamed_feature.py")
        _assert_no_path(self, findings, "old_feature.py")

    def test_partially_staged_file_scans_only_index_content(self):
        gate = _load_gate()
        directory, root = _init_git_repo()
        try:
            _commit_file(root, "partial.py", "value = 1\n")
            _stage_file(root, "partial.py", "def run(host):\n    return host._private\n")
            (root / "partial.py").write_text(
                "def run(host):\n    return public_value\n", encoding="utf-8"
            )

            findings = gate.scan_repo(root)
        finally:
            directory.cleanup()
        _assert_has_rule(self, findings, "integration.host_private_state", "partial.py")

    def test_nested_untracked_python_file_is_scanned(self):
        gate = _load_gate()
        directory, root = _init_git_repo()
        try:
            _commit_file(root, "README.txt", "base\n")
            (root / "nested").mkdir()
            (root / "nested/中文.py").write_text(
                "def run(host):\n    return host._private\n", encoding="utf-8"
            )

            findings = gate.scan_repo(root)
        finally:
            directory.cleanup()
        _assert_has_rule(self, findings, "integration.host_private_state", "nested/中文.py")

    def test_deleted_tracked_file_is_not_read_from_worktree(self):
        gate = _load_gate()
        directory, root = _init_git_repo()
        try:
            _commit_file(root, "gone.py", "def run(host):\n    return host._private\n")
            _run_git(root, "rm", "-q", "gone.py")

            findings = gate.scan_repo(root)
        finally:
            directory.cleanup()
        _assert_no_path(self, findings, "gone.py")

    def test_special_paths_are_scanned_with_either_git_quote_path_setting(self):
        gate = _load_gate()
        for quote_path in ("true", "false"):
            with self.subTest(core_quote_path=quote_path):
                directory, root = _init_git_repo()
                try:
                    _run_git(root, "config", "core.quotePath", quote_path)
                    relative = "目录/引号 '功能'.py"
                    _stage_file(root, relative, "def run(host):\n    return host._private\n")
                    findings = gate.scan_repo(root)
                finally:
                    directory.cleanup()
                _assert_has_rule(self, findings, "integration.host_private_state", relative)

    def test_git_environment_removes_every_git_prefixed_variable(self):
        gate = _load_gate()
        with mock.patch.dict(
            os.environ,
            {
                "GIT_DIR": "/wrong/repository",
                "GIT_INDEX_FILE": "/wrong/index",
                "GIT_REVIEW_UNKNOWN": "must-not-leak",
            },
            clear=False,
        ):
            cleaned = gate._git_env()  # discipline-ok(R5): 直接验证所有 GIT_* 环境变量清洗
        self.assertFalse(any(name.startswith("GIT_") for name in cleaned))

    def test_git_error_from_oserror_is_reported(self):
        gate = _load_gate()
        with mock.patch.object(gate.subprocess, "run", side_effect=OSError("git missing")):
            with self.assertRaisesRegex(
                gate._GitViewError,  # discipline-ok(R5): 直接断言门禁内部错误类型
                r"git command failed: git missing",
            ):
                gate._git(  # discipline-ok(R5): 直接验证 Git OSError 错误流
                    pathlib.Path("."), ["status"]
                )

        gate = _load_gate()
        for stderr, expected in ((b"denied", "denied"), ("denied", "denied"), (123, "123"), (b"", "exit status 1")):
            with self.subTest(stderr_type=type(stderr).__name__):
                completed = subprocess.CompletedProcess([], 1, stdout=b"", stderr=stderr)
                with mock.patch.object(gate.subprocess, "run", return_value=completed):
                    with self.assertRaisesRegex(
                        gate._GitViewError,  # discipline-ok(R5): 直接断言门禁内部错误类型
                        rf"git show :missing.py failed: {expected}",
                    ):
                        gate._git(  # discipline-ok(R5): 直接验证 Git 错误流的 stderr 归一化
                            pathlib.Path("."), ["show", ":missing.py"], text=False
                        )

    def test_decode_git_paths_rejects_invalid_lists_and_accepts_empty(self):
        gate = _load_gate()
        self.assertEqual(gate._decode_git_paths(b""), [])  # discipline-ok(R5): 直接验证路径解码空输入
        self.assertEqual(gate._decode_git_paths("a.py\0"), ["a.py"])  # discipline-ok(R5): 直接验证路径解码文本输入
        with self.assertRaisesRegex(gate._GitViewError, "invalid path list"):  # discipline-ok(R5): 直接断言门禁内部错误类型
            gate._decode_git_paths(123)  # discipline-ok(R5): 直接验证非法 Git 路径输出
        with self.assertRaisesRegex(gate._GitViewError, "non-NUL-terminated"):  # discipline-ok(R5): 直接断言门禁内部错误类型
            gate._decode_git_paths(b"a.py")  # discipline-ok(R5): 直接验证非 NUL 终止路径输出

    def test_untracked_path_scanner_skips_non_python_paths(self):
        gate = _load_gate()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "notes.txt").write_text("plain\n", encoding="utf-8")
            completed = types.SimpleNamespace(stdout=b"notes.txt\0")
            with mock.patch.object(gate, "_git", return_value=completed):
                self.assertEqual(gate._untracked_python_files(root), {})  # discipline-ok(R5): 直接验证未跟踪路径筛选


    def test_untracked_path_scanner_skips_missing_python_paths(self):
        gate = _load_gate()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            completed = types.SimpleNamespace(stdout=b"missing.py\0")
            with mock.patch.object(gate, "_git", return_value=completed):
                self.assertEqual(gate._untracked_python_files(root), {})  # discipline-ok(R5): 直接验证未跟踪路径筛选


    def test_diff_base_resolution_reports_errors(self):
        gate = _load_gate()
        for stderr, expected in (("unknown base", "unknown base"), ("", "cannot resolve base 'missing'")):
            with self.subTest(stderr=stderr):
                result = types.SimpleNamespace(returncode=1, stderr=stderr)
                with mock.patch.object(gate.subprocess, "run", return_value=result):
                    with self.assertRaisesRegex(gate._GitViewError, expected):  # discipline-ok(R5): 断言门禁内部错误类型
                        gate._diff_args("missing", pathlib.Path("."))  # discipline-ok(R5): 验证 base 解析错误

    def test_diff_base_resolution_converts_git_oserror(self):
        gate = _load_gate()
        with mock.patch.object(gate.subprocess, "run", side_effect=OSError("rev-parse unavailable")):
            with self.assertRaisesRegex(
                gate._GitViewError,  # discipline-ok(R5): 断言门禁内部错误类型
                r"git command failed: rev-parse unavailable",
            ):
                gate._diff_args("HEAD", pathlib.Path("."))  # discipline-ok(R5): 验证 rev-parse 异常流

    def test_diff_hunk_parser_accepts_empty_and_rejects_malformed_hunks(self):
        gate = _load_gate()
        probe = types.SimpleNamespace(returncode=0, stderr="")
        with mock.patch.object(gate.subprocess, "run", return_value=probe):
            with mock.patch.object(gate, "_git", return_value=types.SimpleNamespace(stdout="")):
                self.assertEqual(
                    gate._added_lines_for_path(  # discipline-ok(R5): 直接验证空 diff hunk
                        pathlib.Path("."), "HEAD", "feature.py"
                    ),
                    set(),
                )
            with mock.patch.object(
                gate, "_git", return_value=types.SimpleNamespace(stdout="@@ malformed\n")
            ):
                with self.assertRaisesRegex(gate._GitViewError, "cannot parse git diff hunk"):  # discipline-ok(R5): 直接断言门禁内部错误类型
                    gate._added_lines_for_path(  # discipline-ok(R5): 直接验证坏 diff hunk 防御
                        pathlib.Path("."), "HEAD", "feature.py"
                    )

    def test_index_source_rejects_non_bytes_content(self):
        gate = _load_gate()
        with mock.patch.object(
            gate, "_git", return_value=types.SimpleNamespace(stdout=object())
        ):
            with self.assertRaisesRegex(gate._GitViewError, "invalid index content"):  # discipline-ok(R5): 直接断言门禁内部错误类型
                gate._index_source(  # discipline-ok(R5): 直接验证异常 Git index 内容
                    pathlib.Path("."), "feature.py"
                )

    def test_scan_repo_converts_git_failure_to_finding(self):
        gate = _load_gate()
        error = gate._GitViewError("cannot inspect index")  # discipline-ok(R5): 直接构造门禁内部错误以验证 scan_repo 转换
        with mock.patch.object(gate, "changed_python_files", side_effect=error):
            findings = gate.scan_repo(pathlib.Path("/tmp/unused"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule, "git.error")
        self.assertIn("cannot inspect index", findings[0].message)

    def test_scan_repo_reports_index_and_worktree_read_failures(self):
        gate = _load_gate()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with mock.patch.object(
                gate, "changed_python_files", return_value={"missing.py": {1}}
            ), mock.patch.object(gate, "_cached_python_paths", return_value=[]):
                findings = gate.scan_repo(root)
            self.assertEqual([item.rule for item in findings], ["git.error"])
            self.assertEqual(findings[0].path, "missing.py")

            with mock.patch.object(
                gate, "changed_python_files", return_value={"indexed.py": {1}}
            ), mock.patch.object(gate, "_cached_python_paths", return_value=["indexed.py"]), mock.patch.object(
                gate, "_index_source", side_effect=gate._GitViewError("bad blob")  # discipline-ok(R5): 直接构造 index 读取错误
            ):
                findings = gate.scan_repo(root)
            self.assertEqual([item.rule for item in findings], ["git.error"])
            self.assertIn("bad blob", findings[0].message)

    def test_untracked_python_read_failures_become_git_view_errors(self):
        gate = _load_gate()
        for failure in (OSError("read denied"), UnicodeError("invalid source")):
            with self.subTest(failure=type(failure).__name__):
                directory, root = _init_git_repo()
                try:
                    _commit_file(root, "base.py", "value = 1\n")
                    path = root / "untracked.py"
                    path.write_text("value = 1\n", encoding="utf-8")
                    with mock.patch.object(
                        pathlib.Path, "read_text", side_effect=failure
                    ):
                        with self.assertRaisesRegex(
                            gate._GitViewError,  # discipline-ok(R5): 断言未跟踪文件读取错误类型
                            "cannot read untracked Python file 'untracked.py'",
                        ):
                            gate._untracked_python_files(root)  # discipline-ok(R5): 真实 Git 路径验证未跟踪读取失败
                finally:
                    directory.cleanup()

    def test_scan_repo_reports_untracked_python_read_failures(self):
        gate = _load_gate()
        for failure in (OSError("read denied"), UnicodeError("invalid source")):
            with self.subTest(failure=type(failure).__name__):
                directory, root = _init_git_repo()
                try:
                    _commit_file(root, "base.py", "value = 1\n")
                    path = root / "untracked.py"
                    path.write_text("value = 1\n", encoding="utf-8")
                    with mock.patch.object(
                        pathlib.Path, "read_text", side_effect=failure
                    ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                        result = gate.main(["--root", str(root), "--base", "HEAD"])
                finally:
                    directory.cleanup()
                self.assertEqual(result, 1)
                self.assertIn("git.error", output.getvalue())
                self.assertIn("cannot read untracked Python file 'untracked.py'", output.getvalue())


    def test_diagnostic_side_effect_call_families_are_rejected(self):
        gate = _load_gate()
        source = """
import os
import pathlib
import requests
import httpx
import subprocess
import threading
import urllib.request


def diagnostics():
    urllib.request.urlopen('https://example.invalid')
    requests.get('https://example.invalid')
    httpx.get('https://example.invalid')
    subprocess.run(['tool'])
    subprocess.Popen(['tool'])
    threading.Thread(target=lambda: None)
    threading.Timer(1, lambda: None)
    pathlib.Path('diagnostics.json').write_text('{}')
    pathlib.Path('diagnostics.bin').write_bytes(b'')
    pathlib.Path('diagnostics.json').unlink()
    pathlib.Path('diagnostics.json').replace('other.json')
    pathlib.Path('diagnostics.json').rename('other.json')
    os.replace('a', 'b')
"""
        findings = gate.scan_source("new_feature.py", source)
        side_effects = [item for item in findings if item.rule == "diagnostics.side_effect"]
        self.assertEqual(len(side_effects), 13)

    def test_diagnostic_side_effect_call_helper_accepts_known_module_call(self):
        gate = _load_gate()
        call = ast.parse("subprocess.run([])").body[0].value
        self.assertTrue(gate._is_diagnostic_side_effect_call(call))  # discipline-ok(R5): 覆盖副作用 helper 分支

    def test_host_private_state_ignores_non_name_receiver(self):
        gate = _load_gate()
        findings = gate.scan_source(
            "new_feature.py",
            "def run():\n    return make_host()._private\n",
        )
        self.assertEqual(findings, [])

    def test_diagnostic_local_state_and_strings_are_not_side_effects(self):
        gate = _load_gate()
        findings = gate.scan_source(
            "new_feature.py",
            """
def diagnostics():
    value = 'write_text'
    local = object()
    local.write_text('not a file')
    return value, local.replace('a', 'b'), 'subprocess.run'
""",
        )
        self.assertEqual(findings, [])

    def test_diagnostic_side_effects_require_known_call_roots(self):
        gate = _load_gate()
        findings = gate.scan_source(
            "new_feature.py",
            """
def diagnostics():
    host.controller.write_text('not a file')
    state.rename('not a file')
    return state.replace('a', 'b')
""",
        )
        self.assertEqual(findings, [])

    def test_host_private_state_access_traverses_host_controller_chain(self):
        gate = _load_gate()
        findings = gate.scan_source(
            "new_feature.py",
            """
def run(self, host):
    return self.host._private

def check(host):
    return host.controller._private
""",
        )
        self.assertEqual(
            [item.rule for item in findings],
            ["integration.host_private_state", "integration.host_private_state"],
        )

    def test_mutation_probe_detects_each_rule(self):
        gate = _load_gate()
        cases = (
            ("stock_float_window.py", "import urllib.request\ndef f(self):\n    return urllib.request.urlopen('x')\n", "window.external_io"),
            ("new_feature.py", "def f(host):\n    return host._private\n", "integration.host_private_state"),
            ("new_feature.py", "def diagnostics():\n    open('x', 'w')\n", "diagnostics.side_effect"),
        )
        for filename, source, rule in cases:
            with self.subTest(rule=rule):
                self.assertEqual(gate.scan_source(filename, source)[0].rule, rule)
    def test_git_error_normalizes_bytes_and_text_stderr(self):
        gate = _load_gate()
        for stderr in (b"denied", "denied", 17):
            with self.subTest(stderr_type=type(stderr).__name__):
                completed = subprocess.CompletedProcess([], 1, stdout=b"", stderr=stderr)
                with mock.patch.object(gate.subprocess, "run", return_value=completed):
                    with self.assertRaisesRegex(
                        gate._GitViewError,  # discipline-ok(R5): 直接断言门禁内部错误类型
                        r"git show :missing.py failed:",
                    ):
                        gate._git(  # discipline-ok(R5): 直接验证 Git 错误流
                            pathlib.Path("/tmp"), ["show", ":missing.py"], text=False
                        )

    def test_git_returns_successful_result_unchanged(self):
        gate = _load_gate()
        completed = subprocess.CompletedProcess([], 0, stdout=b"ok", stderr=b"")
        with mock.patch.object(gate.subprocess, "run", return_value=completed):
            result = gate._git(  # discipline-ok(R5): 直接验证 Git 成功流
                pathlib.Path("/tmp"), ["status"], text=False
            )
        self.assertIs(result, completed)

    def test_all_source_lines_handles_empty_and_multiline_source(self):
        gate = _load_gate()
        self.assertEqual(
            gate._all_source_lines(  # discipline-ok(R5): 直接验证源码行号计算入口
                ""
            ),
            set(),
        )
        self.assertEqual(
            gate._all_source_lines(  # discipline-ok(R5): 直接验证多行源码行号计算入口
                "one\ntwo\n"
            ),
            {1, 2},
        )

    def test_git_oserror_is_reported_as_git_view_error(self):
        gate = _load_gate()
        with mock.patch.object(gate.subprocess, "run", side_effect=OSError("spawn denied")):
            with self.assertRaisesRegex(
                gate._GitViewError,  # discipline-ok(R5): 直接断言门禁内部错误类型
                r"git command failed: spawn denied",
            ):
                gate._git(  # discipline-ok(R5): 直接验证 Git 异常入口
                    pathlib.Path("/tmp"), ["status"]
                )

    def test_git_path_decoder_rejects_invalid_and_non_nul_lists(self):
        gate = _load_gate()
        with self.assertRaisesRegex(
            gate._GitViewError,  # discipline-ok(R5): 直接断言门禁内部错误类型
            r"git returned an invalid path list",
        ):
            gate._decode_git_paths(  # discipline-ok(R5): 直接验证非法路径列表
                object()
            )
        with self.assertRaisesRegex(
            gate._GitViewError,  # discipline-ok(R5): 直接断言门禁内部错误类型
            r"git returned a non-NUL-terminated path list",
        ):
            gate._decode_git_paths(  # discipline-ok(R5): 直接验证非 NUL 终止路径列表
                b"broken.py"
            )

    def test_git_path_decoder_accepts_text_path_lists(self):
        gate = _load_gate()
        self.assertEqual(
            gate._decode_git_paths(  # discipline-ok(R5): 直接验证文本路径列表
                "one.py\0two.py\0"
            ),
            ["one.py", "two.py"],
        )

    def test_diff_args_falls_back_to_root_for_unborn_head(self):
        gate = _load_gate()
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="unknown revision")
        with mock.patch.object(gate.subprocess, "run", return_value=result):
            self.assertEqual(
                gate._diff_args(  # discipline-ok(R5): 直接验证未出生 HEAD 的基线回退
                    "HEAD", pathlib.Path("/tmp")
                ),
                ["--root"],
            )

    def test_diff_args_returns_base_when_tree_resolves(self):
        gate = _load_gate()
        result = subprocess.CompletedProcess([], 0, stdout="tree", stderr="")
        with mock.patch.object(gate.subprocess, "run", return_value=result):
            self.assertEqual(
                gate._diff_args(  # discipline-ok(R5): 直接验证可解析基线的参数
                    "abc123", pathlib.Path("/tmp")
                ),
                ["abc123"],
            )

    def test_diff_args_rejects_unresolvable_non_head_base(self):
        gate = _load_gate()
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="unknown revision")
        with mock.patch.object(gate.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(
                gate._GitViewError,  # discipline-ok(R5): 直接断言门禁内部错误类型
                "unknown revision",
            ):
                gate._diff_args(  # discipline-ok(R5): 直接验证不可解析基线错误
                    "missing-base", pathlib.Path("/tmp")
                )

    def test_added_lines_accepts_empty_diff_and_rejects_bad_hunk(self):
        gate = _load_gate()
        with mock.patch.object(
            gate, "_git", return_value=mock.Mock(stdout="")
        ):
            self.assertEqual(
                gate._added_lines_for_path(  # discipline-ok(R5): 直接验证空 diff hunk
                    pathlib.Path("/tmp"), "HEAD", "empty.py"
                ),
                set(),
            )
        with mock.patch.object(
            gate, "_git", return_value=mock.Mock(stdout="@@ malformed\n")
        ):
            with self.assertRaisesRegex(
                gate._GitViewError,  # discipline-ok(R5): 直接断言门禁内部错误类型
                r"cannot parse git diff hunk for 'bad.py'",
            ):
                gate._added_lines_for_path(  # discipline-ok(R5): 直接验证坏 diff hunk 防御
                    pathlib.Path("/tmp"), "HEAD", "bad.py"
                )

    def test_index_source_rejects_non_bytes_git_content(self):
        gate = _load_gate()
        with mock.patch.object(
            gate, "_git", return_value=mock.Mock(stdout="not bytes")
        ):
            with self.assertRaisesRegex(
                gate._GitViewError,  # discipline-ok(R5): 直接断言门禁内部错误类型
                r"git returned invalid index content for 'broken.py'",
            ):
                gate._index_source(  # discipline-ok(R5): 直接验证异常 Git index 内容
                    pathlib.Path("/tmp"), "broken.py"
                )

    def test_scan_repo_reports_git_error_when_git_view_fails(self):
        gate = _load_gate()
        error = gate._GitViewError("bad base")  # discipline-ok(R5): 直接构造门禁内部错误
        with mock.patch.object(gate, "changed_python_files", side_effect=error):
            findings = gate.scan_repo(pathlib.Path("/tmp"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule, "git.error")
        self.assertIn("bad base", findings[0].message)

    def test_scan_repo_reports_read_failure_for_worktree_source(self):
        gate = _load_gate()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "untracked.py").write_text("value = 1\n", encoding="utf-8")
            with mock.patch.object(gate, "changed_python_files", return_value={"untracked.py": {1}}), \
                    mock.patch.object(gate, "_cached_python_paths", return_value=[]), \
                    mock.patch.object(pathlib.Path, "read_text", side_effect=OSError("read denied")):
                findings = gate.scan_repo(root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule, "git.error")
        self.assertEqual(findings[0].path, "untracked.py")
        self.assertIn("read denied", findings[0].message)

    def test_module_cli_exits_one_when_findings_exist(self):
        script = ROOT / "tools" / "architecture_increment_gate.py"
        directory, root = _init_git_repo()
        try:
            _commit_file(root, "base.py", "value = 1\n")
            _stage_file(root, "bad.py", "def run(host):\n    return host._private\n")
            old_argv = sys.argv
            try:
                sys.argv = [str(script), "--root", str(root), "--base", "HEAD"]
                with self.assertRaises(SystemExit) as raised:
                    runpy.run_path(str(script), run_name="__main__")
            finally:
                sys.argv = old_argv
        finally:
            directory.cleanup()
        self.assertEqual(raised.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
