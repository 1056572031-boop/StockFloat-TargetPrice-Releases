# -*- coding: utf-8 -*-
"""验证 run_tests.sh 的互斥锁：同一时间只许一轮完整回归。

起因（2026-10-21）：两个会话在同一台机器上先后起了四轮整档，五个落盘路径与覆盖率回执
被互相覆盖，三轮的覆盖率结论被判"来源不可信"，每轮白烧约 9 分钟。

为什么真起子进程而不是读源码字符串（AGENTS §6 第 2 条例外）：价值就在"脚本跑起来会不会
拒绝"，引号、`set -e`、退出码和 trap 写没写对，读源码断言不了。配合 `SF_LOCK_PROBE=1`
取到锁就走，判错立刻返回，不会嵌套跑一整轮回归；`SF_LOCK_PROBE_DIR` 只在探针模式下才改道，
所以这里的假锁不会碰到外层正在跑的真锁。
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._base import SFCase

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_TESTS = PROJECT_ROOT / "run_tests.sh"
TMP_LOCK = "sf_run_tests.lock"
TREE_LOCK = ".run_tests.tree.lock"
REFUSED = 75


def _start_time_of(pid):
    """与脚本里 sf_lstart_of 同一口径：C locale 的 ps lstart，空白折叠成单个空格。"""
    out = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], text=True, capture_output=True,
                         env=dict(os.environ, LC_ALL="C"), check=False).stdout
    return " ".join(out.split())


class TestRunTestsMutualExclusion(SFCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="sf-lock-probe-")
        self.addCleanup(tmp.cleanup)
        self.lock_dir = Path(tmp.name) / "locks"
        self.stub_dir = Path(tmp.name) / "bin"
        self.stub_dir.mkdir()
        pgrep = self.stub_dir / "pgrep"
        pgrep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")   # 没有正式版在运行
        pgrep.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("SF_", "GIT_", "COVERAGE"))}
        self.env["PATH"] = "%s:%s" % (self.stub_dir, self.env["PATH"])
        self.env["SF_LOCK_PROBE"] = "1"
        self.env["SF_LOCK_PROBE_DIR"] = str(self.lock_dir)
        self.lock_dir.mkdir()

    def _holder(self):
        """起一个真实存活的进程，用它的 pid 与启动时刻冒充'另一轮回归'。"""
        proc = subprocess.Popen(["sleep", "120"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc.pid, _start_time_of(proc.pid)

    def _dead_pid(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def _hold(self, name, record):
        os.symlink(record, self.lock_dir / name)

    def _run(self):
        return subprocess.run(["bash", str(RUN_TESTS)], cwd=PROJECT_ROOT, env=self.env,
                              text=True, capture_output=True, check=False, timeout=60)

    def _target(self, name):
        return os.readlink(self.lock_dir / name)

    def test_live_holder_of_the_temp_lock_refuses_with_75_and_keeps_its_lock(self):
        pid, started = self._holder()
        record = "%d|%s|/other/tree" % (pid, started)
        self._hold(TMP_LOCK, record)
        result = self._run()
        self.assertEqual(result.returncode, REFUSED, result.stdout + result.stderr)
        self.assertIn(str(pid), result.stderr)
        self.assertEqual(self._target(TMP_LOCK), record, "被拒的一轮不许动别人的锁")
        self.assertFalse((self.lock_dir / TREE_LOCK).exists(), "没拿到第一把就不该留第二把")

    def test_live_holder_of_the_tree_lock_refuses_and_releases_the_temp_lock_it_took(self):
        pid, started = self._holder()
        record = "%d|%s|/other/tree" % (pid, started)
        self._hold(TREE_LOCK, record)
        result = self._run()
        self.assertEqual(result.returncode, REFUSED, result.stdout + result.stderr)
        self.assertEqual(self._target(TREE_LOCK), record)
        self.assertFalse((self.lock_dir / TMP_LOCK).is_symlink(),
                         "第二把没拿到时，已拿到的第一把必须放回去，否则会把自己锁死")

    def test_refusal_code_is_distinct_from_the_running_app_guard(self):
        pid, started = self._holder()
        self._hold(TMP_LOCK, "%d|%s|/x" % (pid, started))
        self.assertNotEqual(self._run().returncode, 2,
                            "2 是'正式版在运行'，两件事必须能从退出码上分开")

    def test_dead_owner_is_reclaimed_and_the_run_goes_ahead(self):
        self._hold(TMP_LOCK, "%d|Mon Jan  1 00:00:00 2001|/gone" % self._dead_pid())
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("已取得两把锁", result.stdout)

    def test_reused_pid_with_a_different_start_time_counts_as_stale(self):
        pid, _ = self._holder()
        self._hold(TMP_LOCK, "%d|Mon Jan  1 00:00:00 2001|/gone" % pid)
        result = self._run()
        self.assertEqual(result.returncode, 0,
                         "pid 还活着但启动时刻对不上，是被别的进程复用了，不是持有者：" + result.stderr)

    def test_live_holder_with_an_unreadable_start_time_still_refuses(self):
        """记录里启动时刻为空（写锁时 ps 取不到）：证明不了 pid 被复用，活着的持有者不得被回收。

        这是 2026-10-22 复核实测出的宽松侧缺口：旧写法把空时刻判成陈旧，第二轮照样拿到锁，
        两轮并跑，正是这把锁当初要治的事故。
        """
        pid, _ = self._holder()
        record = "%d||/other/tree" % pid
        self._hold(TMP_LOCK, record)
        result = self._run()
        self.assertEqual(result.returncode, REFUSED, result.stdout + result.stderr)
        self.assertEqual(self._target(TMP_LOCK), record, "活着的持有者的锁不许被回收")

    def test_dead_holder_with_an_unreadable_start_time_is_reclaimed(self):
        """读不到启动时刻也不能让死锁永久卡住：进程已不在就照常回收。"""
        self._hold(TMP_LOCK, "%d||/gone" % self._dead_pid())
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("已取得两把锁", result.stdout)

    def test_live_holder_is_not_reclaimed_when_ps_cannot_be_read_now(self):
        """现在这一刻 ps 读不到启动时刻（环境受限）：记录再完整，也不能当成陈旧。"""
        pid, started = self._holder()
        record = "%d|%s|/other/tree" % (pid, started)
        self._hold(TMP_LOCK, record)
        broken = Path(self.stub_dir) / "ps"
        broken.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        broken.chmod(0o755)
        result = self._run()
        self.assertEqual(result.returncode, REFUSED, result.stdout + result.stderr)
        self.assertEqual(self._target(TMP_LOCK), record)

    def test_garbage_record_is_treated_as_stale_not_as_a_live_owner(self):
        self._hold(TMP_LOCK, "not-a-pid|whatever|/x")
        self.assertEqual(self._run().returncode, 0)

    def test_a_finished_run_leaves_no_lock_behind(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sorted(p.name for p in self.lock_dir.iterdir()), [],
                         "跑完必须把两把锁都放掉，否则下一轮会被自己人拦住")

    def test_a_reclaimed_stale_lock_is_also_released_afterwards(self):
        self._hold(TREE_LOCK, "%d|Mon Jan  1 00:00:00 2001|/gone" % self._dead_pid())
        self.assertEqual(self._run().returncode, 0)
        self.assertEqual(sorted(p.name for p in self.lock_dir.iterdir()), [])

    def test_probe_dir_cannot_redirect_the_lock_without_probe_mode(self):
        """没开探针时，SF_LOCK_PROBE_DIR 不起作用：不能靠一个环境变量把锁挪走来关掉互斥。"""
        pid, started = self._holder()
        real_tmp = Path(self.lock_dir.parent) / "real-tmp"      # 脚本真正会用的 TMPDIR
        real_tmp.mkdir()
        record = "%d|%s|/x" % (pid, started)
        os.symlink(record, real_tmp / TMP_LOCK)
        env = dict(self.env)
        env.pop("SF_LOCK_PROBE")
        env["TMPDIR"] = str(real_tmp)
        # 改道目录里什么锁都没有：若改道被误认，这一轮会在那里顺利取锁并往下跑整档。
        result = subprocess.run(["bash", str(RUN_TESTS)], cwd=PROJECT_ROOT, env=env, text=True,
                                capture_output=True, check=False, timeout=60)
        self.assertEqual(result.returncode, REFUSED, result.stdout + result.stderr)
        self.assertEqual(os.readlink(real_tmp / TMP_LOCK), record, "别人的锁原样还在")
        self.assertEqual(list(self.lock_dir.iterdir()), [], "没开探针就不该往改道目录里放任何东西")


if __name__ == "__main__":
    unittest.main()
