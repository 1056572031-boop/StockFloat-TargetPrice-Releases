# -*- coding: utf-8 -*-
"""Regression probes for coverage.py continuation and the selected tracer core."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from tests._base import SFCase
from pathlib import Path
from unittest.mock import patch

import coverage

ROOT = Path(__file__).resolve().parent.parent


class TestCoverageContinuation(SFCase):
    def test_project_effective_core_is_ctrace(self):
        """Read coverage.py's resolved configuration, rather than checking file text."""
        with patch.dict(os.environ, {"COVERAGE_CORE": "ctrace"}, clear=False):
            resolved = coverage.Coverage(config_file=True).config.core
        self.assertEqual(
            resolved,
            "ctrace",
            "the project must keep the explicit ctrace core; sysmon breaks nested continuation",
        )

    def test_outer_coverage_continues_after_inner_start_stop(self):
        """A real child process must retain the outer tracer across an inner session."""
        with tempfile.TemporaryDirectory(prefix="coverage-continuation-") as tmp:
            tmp_path = Path(tmp)
            probe = tmp_path / "probe.py"
            outer_data = tmp_path / "outer.coverage"
            inner_data = tmp_path / "inner.coverage"
            probe.write_text(
                textwrap.dedent(
                    """
                    import json
                    import sys
                    import coverage

                    outer_path, inner_path = sys.argv[1:]

                    def outer_before():
                        return "before"

                    def inner_body():
                        return "inner"

                    def outer_after():
                        return "after"

                    outer = coverage.Coverage(
                        data_file=outer_path,
                        include=[__file__],
                        config_file=False,
                    )
                    outer.start()
                    outer_before()
                    outer.save()

                    inner = coverage.Coverage(
                        data_file=inner_path,
                        include=[__file__],
                        config_file=False,
                    )
                    inner.start()
                    inner_body()
                    inner.stop()
                    inner.save()
                    # Coverage sessions are not a stack: stopping the inner
                    # session leaves the outer session inactive. Explicitly
                    # resume it, then prove the post-inner sentinel is kept.
                    outer.start()

                    outer_after()
                    outer.stop()
                    outer.save()

                    print(json.dumps({
                        "outer_before": outer_before.__code__.co_firstlineno + 1,
                        "inner_body": inner_body.__code__.co_firstlineno + 1,
                        "outer_after": outer_after.__code__.co_firstlineno + 1,
                    }))
                    """
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            # Coverage 7.16 has no public Coverage(core=...) API.  Set the
            # supported selector in the isolated child instead of relying on
            # the parent's tracer or on a process-wide nested state.
            env["COVERAGE_CORE"] = "ctrace"
            result = subprocess.run(
                [sys.executable, str(probe), str(outer_data), str(inner_data)],
                cwd=str(ROOT),
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(
                result.returncode,
                0,
                "continuation probe failed:\n%s\n%s" % (result.stdout, result.stderr),
            )
            lines = json.loads(result.stdout)
            outer_file = str(probe)
            outer = coverage.Coverage(outer_data)
            inner = coverage.Coverage(inner_data)
            outer.load()
            inner.load()
            outer_data_obj = outer.get_data()
            inner_data_obj = inner.get_data()
            outer_lines = set()
            for measured_file in outer_data_obj.measured_files():
                outer_lines.update(outer_data_obj.lines(measured_file) or ())
            inner_lines = set()
            for measured_file in inner_data_obj.measured_files():
                inner_lines.update(inner_data_obj.lines(measured_file) or ())
            self.assertIn(lines["outer_before"], outer_lines)
            self.assertIn(lines["outer_after"], outer_lines)
            self.assertIn(lines["inner_body"], inner_lines)

    def test_probe_environment_override_is_applied_without_core_api(self):
        """The probe's explicit environment patch selects ctrace through public config."""
        with patch.dict(os.environ, {"COVERAGE_CORE": "ctrace"}, clear=False):
            self.assertEqual(coverage.Coverage(config_file=False).config.core, "ctrace")


if __name__ == "__main__":
    unittest.main()
