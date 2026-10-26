"""发版链分步耗时：只观测，不参与判定，中途失败也要留下已完成各步的耗时。

计时函数从脚本里按标记抽出来，在系统自带的 /bin/bash（macOS 上是 3.2）里真跑；
时钟靠重写 step_tick 构造，不靠真睡眠。整份脚本只跑到“质量记录缺失”那一步为止，
绝不会走到打包、安装或发布。
"""
from __future__ import annotations

import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

from tests._base import SFCase

ROOT = pathlib.Path(__file__).resolve().parents[1]
BEGIN = "# BEGIN step_timing"
END = "# END step_timing"


def timing_block():
    source = (ROOT / "build_release.sh").read_text(encoding="utf-8")
    return source.split(BEGIN, 1)[1].split(END, 1)[0]


def run_bash(script):
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=20)


def fake_clock_harness(body):
    """严格模式下加载计时块，并把取时钟换成读变量 CLOCK。"""
    return ("set -euo pipefail\n" + timing_block()
            + "\nstep_tick() { STEP_NOW=$CLOCK; }\nCLOCK=0\n" + body)


class StepTimingFunctions(SFCase):
    def test_each_step_is_charged_to_the_step_that_was_open(self):
        result = run_bash(fake_clock_harness(
            'step_begin "alpha"\nCLOCK=7\nstep_begin "beta"\nCLOCK=12\nstep_report\n'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("共 12s", result.stdout)
        self.assertRegex(result.stdout, r"\s7s  alpha")
        self.assertRegex(result.stdout, r"\s5s  beta")

    def test_steps_are_listed_in_the_order_they_ran(self):
        result = run_bash(fake_clock_harness(
            'step_begin "first"\nCLOCK=1\nstep_begin "second"\nCLOCK=3\nstep_begin "third"\nCLOCK=6\nstep_report\n'))
        names = [line.split()[-1] for line in result.stdout.splitlines() if line.startswith("   ")]
        self.assertEqual(names, ["first", "second", "third"])

    def test_report_without_any_step_prints_nothing_and_succeeds(self):
        result = run_bash(fake_clock_harness("step_report\n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_report_is_idempotent_so_the_exit_trap_cannot_print_it_twice(self):
        result = run_bash(fake_clock_harness(
            'step_begin "only"\nCLOCK=4\nstep_report\nstep_report\n'))
        self.assertEqual(result.stdout.count("分步耗时"), 1)

    def test_the_report_closes_the_open_step_at_that_moment(self):
        result = run_bash(fake_clock_harness('step_begin "still running"\nCLOCK=9\nstep_report\n'))
        self.assertRegex(result.stdout, r"\s9s  still running")

    def test_functions_survive_strict_mode_with_unset_variables_on_bash_3(self):
        # 不覆盖时钟，走真实的 $SECONDS：只证明在 set -u 下能跑完，不比较具体秒数。
        script = "set -euo pipefail\n" + timing_block() + '\nstep_begin "real clock"\nstep_report\n'
        result = run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"共 \d+s")
        self.assertRegex(result.stdout, r"\d+s  real clock")


class WholeScriptFailurePath(SFCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="sf_step_timing_")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        (self.root / "build_release.sh").write_bytes((ROOT / "build_release.sh").read_bytes())
        stub = self.root / ".venv" / "bin" / "python3"
        stub.parent.mkdir(parents=True)
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.lock = self.root / ".build_release_targetprice.lock"

    def invoke(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "SF_QUALITY_RECORD"}
        return subprocess.run(["/bin/bash", str(self.root / "build_release.sh"), *args],
                              capture_output=True, text=True, timeout=20, env=env, cwd=str(self.root))

    def test_a_step_that_fails_midway_still_leaves_its_timing_and_releases_the_lock(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("缺少 SF_QUALITY_RECORD", result.stderr)
        self.assertIn("发版链分步耗时", result.stdout)
        self.assertRegex(result.stdout, r"\d+s  质量记录与发布基线校验")
        self.assertFalse(self.lock.exists())

    def test_an_argument_error_before_any_step_prints_no_timing_table(self):
        result = self.invoke("--invalid-timing-probe")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("分步耗时", result.stdout + result.stderr)
        self.assertFalse(self.lock.exists())


class StepNamesAreSafe(SFCase):
    def test_no_step_name_contains_the_column_separator(self):
        # 只能读脚本文本：整份发版脚本不能当单元跑（跑一次等于发一次版）。
        # 竖线是账本的列分隔符，名字里有它会把一行拆成三列，表就读歪了。
        source = (ROOT / "build_release.sh").read_text(encoding="utf-8")
        names = re.findall(r'^step_begin "([^"]*)"', source, re.M)
        self.assertGreaterEqual(len(names), 20, "计时点被大量删掉了，分步耗时表会残缺")
        self.assertEqual([n for n in names if "|" in n or not n.strip()], [])


if __name__ == "__main__":
    unittest.main()
