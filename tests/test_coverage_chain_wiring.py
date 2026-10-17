"""Exercise real shell wiring without running tests, installers or publishers."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from tests._base import SFCase

ROOT = Path(__file__).resolve().parents[1]
BASE = "0123456789abcdef0123456789abcdef01234567"
WRAPPER = r'''
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with open(os.environ["CHAIN_CALLS"], "a") as stream:
    stream.write(json.dumps(args) + "\n")
failures = json.loads(os.environ["CHAIN_FAILURES"])
if args == ["--version"]:
    print("isolated wrapper")
    raise SystemExit(0)
if args[:1] == ["-c"]:
    if os.environ.get("SF_PUBLIC_REGRESSION") == "1":
        Path("build/change-coverage.json").write_text(json.dumps({
            "schema": 1, "status": "unverified-public-history", "ok": False,
        }))
    print(os.environ["SF_CHANGE_BASE"])
    raise SystemExit(failures.get("base", 0))
if args[:1] == ["tools/coverage_provenance.py"]:
    action = args[1]
    code = failures.get("provenance:" + action, 0)
    if action == "finish" and args[2:] != ["--exit-code", "0"]:
        code = code or 1
    raise SystemExit(code)
if args[:3] == ["-m", "coverage", "run"]:
    raise SystemExit(failures.get("runner", 0))
if args == ["-m", "coverage", "erase"]:
    raise SystemExit(0)
if args[:1] == ["tools/change_coverage_gate.py"]:
    raise SystemExit(failures.get("changegate", 0))
if args[:1] == ["tools/coverage_gate.py"]:
    raise SystemExit(failures.get("global", 0))
if args[:1] == ["tools/gate_stamp.py"]:
    if args[1:] == ["write"]:
        Path("build/.gate_stamp").write_text("fixture stamp")
    raise SystemExit(0)
if args == ["tools/test_runner.py"]:
    raise SystemExit(failures.get("runner", 0))
if args[:1] == ["tools/verify_bottom_gap_px.py"]:
    print("== 结论：隔离像素工具替身通过 ==")
    raise SystemExit(0)
if args[:1] in (["tools/public_docs_check.py"], ["tools/static_gate.py"],
               ["tools/pyobjc_sig_check.py"]):
    print("isolated prerequisite passed")
    raise SystemExit(0)
raise SystemExit("unexpected command in isolated fixture: " + repr(args))
'''


class TestCoverageChainWiring(SFCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="sf-chain-wiring-")
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        for name in (".venv/bin", "bin", "build", "home", "tmp"):
            (self.repo / name).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "run_tests.sh", self.repo / "run_tests.sh")
        self.python = self.repo / ".venv/bin/python3"
        self.python.write_text("#!" + sys.executable + "\n" + textwrap.dedent(WRAPPER))
        self.python.chmod(0o700)
        pgrep = self.repo / "bin/pgrep"
        pgrep.write_text("#!/bin/sh\nexit 1\n")
        pgrep.chmod(0o700)
        self.calls = self.repo / "calls.jsonl"
        # Never inherit session paths, coverage startup hooks, skip switches or
        # git-hook repository selectors from the outer regression process.
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("SF_", "GIT_", "COVERAGE", "PYTHON"))
        }
        self.env.update(
            HOME=str(self.repo / "home"), TMPDIR=str(self.repo / "tmp"),
            PATH=str(self.repo / "bin") + ":/usr/bin:/bin",
            CHAIN_CALLS=str(self.calls), CHAIN_FAILURES="{}", SF_CHANGE_BASE=BASE,
        )

    def execute(self, command, failures=None, switches=None):
        env = dict(self.env)
        env["CHAIN_FAILURES"] = json.dumps(failures or {})
        env.update(switches or {})
        process = subprocess.Popen(command, cwd=self.repo, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        finally:
            # The real shell uses this fixed /tmp log, not TMPDIR. Remove only
            # the log belonging to the child we launched, never a wildcard.
            Path(f"/tmp/sf_bottom_gap_px_{process.pid}.log").unlink(missing_ok=True)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return process.returncode, stdout + stderr, calls

    def run_gate(self, **kwargs):
        return self.execute(["/bin/bash", str(self.repo / "run_tests.sh")], **kwargs)

    def assert_no_stamp(self, calls):
        self.assertNotIn(["tools/gate_stamp.py", "write"], calls)
        self.assertFalse((self.repo / "build/.gate_stamp").exists())

    def assert_old_stamp_invalidated(self, calls):
        self.assert_no_stamp(calls)
        archived = list((self.repo / "build/public-regression-invalidated").glob("*/.gate_stamp"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_text(), "oldstamp")

    def test_public_regression_runs_provenance_and_global_but_marks_change_unverified(self):
        (self.repo / "build/.gate_stamp").write_text("oldstamp")
        code, output, calls = self.run_gate(switches={"SF_PUBLIC_REGRESSION": "1"})
        self.assertEqual(code, 0, output)
        self.assertIn(["tools/coverage_provenance.py", "begin"], calls)
        self.assertIn(["tools/coverage_provenance.py", "finish", "--exit-code", "0"], calls)
        self.assertIn(["tools/coverage_provenance.py", "check"], calls)
        self.assertIn(["tools/coverage_gate.py"], calls)
        self.assert_old_stamp_invalidated(calls)
        report = json.loads((self.repo / "build/change-coverage.json").read_text())
        self.assertEqual(report["status"], "unverified-public-history")
        self.assertFalse(report["ok"])
        self.assertIn("公开回归不是发布资格", output)

    def test_public_regression_failure_also_invalidates_old_stamp(self):
        for failure in ("provenance:begin", "runner", "provenance:check", "base", "global"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                # Each run gets a real old stamp and real filesystem mv, not a mocked command.
                archived_root = self.repo / "build/public-regression-invalidated"
                if archived_root.exists():
                    shutil.move(str(archived_root), str(Path(temporary) / "previous"))
                self.calls.unlink(missing_ok=True)
                (self.repo / "build/.gate_stamp").write_text("oldstamp")
                code, output, calls = self.run_gate(
                    failures={failure: 23}, switches={"SF_PUBLIC_REGRESSION": "1"},
                )
                self.assertNotEqual(code, 0, output)
                self.assert_old_stamp_invalidated(calls)

    def test_begin_failure_never_runs_coverage_or_writes_stamp(self):
        code, output, calls = self.run_gate(failures={"provenance:begin": 19})
        self.assertNotEqual(code, 0, output)
        self.assertIn(["tools/coverage_provenance.py", "begin"], calls)
        self.assertFalse(any(call[:3] == ["-m", "coverage", "run"] for call in calls))
        self.assert_no_stamp(calls)

    def test_runner_failure_reaches_finish_with_original_exit_code(self):
        code, output, calls = self.run_gate(failures={"runner": 23})
        self.assertNotEqual(code, 0, output)
        self.assertIn(["tools/coverage_provenance.py", "finish", "--exit-code", "23"], calls)
        self.assertNotIn(["tools/coverage_provenance.py", "check"], calls)
        self.assert_no_stamp(calls)

    def test_source_check_failure_blocks_changed_and_global_gates_and_stamp(self):
        code, output, calls = self.run_gate(failures={"provenance:check": 17})
        self.assertNotEqual(code, 0, output)
        self.assertIn(["tools/coverage_provenance.py", "check"], calls)
        self.assertFalse(any(call[0] == "tools/change_coverage_gate.py" for call in calls))
        self.assertNotIn(["tools/coverage_gate.py"], calls)
        self.assert_no_stamp(calls)

    def test_changed_lines_failure_blocks_global_gate_and_stamp(self):
        code, output, calls = self.run_gate(failures={"changegate": 13})
        self.assertNotEqual(code, 0, output)
        self.assertTrue(any(call[0] == "tools/change_coverage_gate.py" for call in calls))
        self.assertNotIn(["tools/coverage_gate.py"], calls)
        self.assert_no_stamp(calls)

    def test_success_records_exact_order_and_explicit_base(self):
        code, output, calls = self.run_gate()
        self.assertEqual(code, 0, output)
        start = calls.index(["-m", "coverage", "erase"])
        self.assertEqual([call for call in calls[start:] if call[0] != "-c"], [
            ["-m", "coverage", "erase"],
            ["tools/coverage_provenance.py", "begin"],
            ["-m", "coverage", "run", "tools/test_runner.py"],
            ["tools/coverage_provenance.py", "finish", "--exit-code", "0"],
            ["tools/coverage_provenance.py", "check"],
            ["tools/change_coverage_gate.py", "--base", BASE, "--data-file", ".coverage",
             "--report-json", "build/change-coverage.json"],
            ["tools/coverage_gate.py"],
            ["tools/gate_stamp.py", "write"],
        ])
        self.assertTrue((self.repo / "build/.gate_stamp").is_file())

    def test_base_selection_failure_cannot_write_stamp(self):
        code, output, calls = self.run_gate(failures={"base": 1})
        self.assertNotEqual(code, 0, output)
        self.assertFalse(any(call[0] == "tools/change_coverage_gate.py" for call in calls))
        self.assert_no_stamp(calls)

    def test_global_gate_failure_cannot_write_stamp(self):
        code, output, calls = self.run_gate(failures={"global": 11})
        self.assertNotEqual(code, 0, output)
        self.assertIn(["tools/coverage_gate.py"], calls)
        self.assert_no_stamp(calls)

    def test_skip_and_random_modes_neither_use_provenance_nor_write_stamp(self):
        for switch in ("SF_SKIP_COVERAGE", "SF_RANDOM_ORDER"):
            with self.subTest(switch=switch):
                self.calls.unlink(missing_ok=True)
                code, output, calls = self.run_gate(switches={switch: "1"})
                self.assertEqual(code, 0, output)
                self.assertIn(["tools/test_runner.py"], calls)
                self.assertFalse(any(call[0] in (
                    "tools/coverage_provenance.py", "tools/change_coverage_gate.py",
                    "tools/coverage_gate.py", "-m",
                ) for call in calls))
                self.assert_no_stamp(calls)

    def run_release_gate(self, failures, runner_code, public=False):
        # Extract only as an executable fixture: running the full release script
        # would install/publish real artifacts. Assertions below observe calls and
        # exit status, not source strings or implementation spelling.
        source = (ROOT / "build_release.sh").read_text()
        start = source.index("GATE_STAMP_VALID=0\n")
        end = source.index("# ---- 1) 版本号", start)
        runner = self.repo / "run_tests.sh"
        runner.write_text(
            '#!/bin/sh\nprintf \'["isolated-regression"]\\n\' >> "$CHAIN_CALLS"\n'
            f"exit {runner_code}\n"
        )
        runner.chmod(0o700)
        block = 'set -euo pipefail\nSKIP_TESTS=0\nVENV_PY="$PWD/.venv/bin/python3"\n'
        if public:
            guard_start = source.index('PUBLIC_REGRESSION="${SF_PUBLIC_REGRESSION:-0}"')
            guard_end = source.index('VENV_PY="$PROJ/.venv/bin/python3"', guard_start)
            block += source[guard_start:guard_end]
        block += source[start:end]
        block += '\nprintf "release-gate-valid=%s\\n" "$GATE_STAMP_VALID"\n'
        return self.execute(["/bin/bash", "-c", block], failures=failures,
                            switches={"SF_PUBLIC_REGRESSION": "1"} if public else None)

    def test_release_rejects_public_regression_mode(self):
        code, output, calls = self.run_release_gate({}, 0, public=True)
        self.assertNotEqual(code, 0, output)
        self.assertIn("不具备发布资格", output)
        self.assertEqual(calls, [])

    def test_old_stamp_success_but_provenance_failure_requires_regression(self):
        code, output, calls = self.run_release_gate({"provenance:check": 9}, 0)
        self.assertEqual(code, 0, output)
        self.assertEqual(calls, [
            ["tools/gate_stamp.py", "check"],
            ["tools/coverage_provenance.py", "check"],
            ["isolated-regression"],
        ])
        self.assertIn("release-gate-valid=1", output)

    def test_required_regression_failure_aborts_release_gate(self):
        code, output, calls = self.run_release_gate({"provenance:check": 9}, 27)
        self.assertNotEqual(code, 0, output)
        self.assertIn(["isolated-regression"], calls)
        self.assertNotIn("release-gate-valid=1", output)

    def test_valid_stamp_and_provenance_do_not_repeat_regression(self):
        code, output, calls = self.run_release_gate({}, 27)
        self.assertEqual(code, 0, output)
        self.assertEqual(calls, [
            ["tools/gate_stamp.py", "check"],
            ["tools/coverage_provenance.py", "check"],
        ])
        self.assertIn("release-gate-valid=1", output)
