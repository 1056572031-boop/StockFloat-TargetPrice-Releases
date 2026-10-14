# -*- coding: utf-8 -*-
"""v1.2.95 第3轮走查：日志节流与单实例常驻线程。

守护三条修复：
  1. log_line_throttled 首条必记、其后按间隔记（附累计次数），绝不抛异常。
  2. 单实例探活线程改为阻塞 accept —— 原实现 settimeout(0.5)+sleep(0.2)
     约 1.4 次/秒唤醒，贯穿进程全生命周期，与 App Nap 省电目标相悖。
  3. 单实例本身的功能（多开保护）不受影响。
"""
import errno
import inspect
import os
import tempfile
import unittest
from unittest import mock

import sf_logging


class TestThrottledLog(unittest.TestCase):

    def setUp(self):
        self.logged = []
        self._orig = sf_logging.log_line
        sf_logging.log_line = lambda m, *a, **k: self.logged.append(m)
        sf_logging.reset_log_throttle()

    def tearDown(self):
        sf_logging.log_line = self._orig
        sf_logging.reset_log_throttle()

    def test_first_is_always_logged(self):
        """首条必须立刻可见 —— 故障的第一时间可见性不能被节流牺牲。"""
        sf_logging.log_line_throttled("k", "boom", every=30)
        self.assertEqual(len(self.logged), 1)
        self.assertIn("boom", self.logged[0])

    def test_burst_is_throttled(self):
        """100 条同类只应记首条加每 30 条一次，而不是 100 条。"""
        for _ in range(100):
            sf_logging.log_line_throttled("k", "boom", every=30)
        self.assertLessEqual(len(self.logged), 5,
                             "100 条同类告警实记 %d 条，节流未生效" % len(self.logged))
        self.assertGreaterEqual(len(self.logged), 1)

    def test_cumulative_count_is_surfaced(self):
        """被压掉的条目不能凭空消失，累计次数要写进日志。"""
        for _ in range(60):
            sf_logging.log_line_throttled("k", "boom", every=30)
        joined = "\n".join(self.logged)
        self.assertIn("60", joined, "必须体现出同类已累计的次数")

    def test_keys_are_independent(self):
        sf_logging.log_line_throttled("a", "aaa", every=30)
        sf_logging.log_line_throttled("b", "bbb", every=30)
        self.assertEqual(len(self.logged), 2, "不同 key 互不干扰")

    def test_reset(self):
        sf_logging.log_line_throttled("k", "boom", every=30)
        sf_logging.reset_log_throttle("k")
        sf_logging.log_line_throttled("k", "boom", every=30)
        self.assertEqual(len(self.logged), 2, "复位后应重新记为首条")

    def test_never_raises(self):
        """日志工具绝不能成为新的故障源。"""
        sf_logging.log_line = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
        try:
            for _ in range(5):
                sf_logging.log_line_throttled("k", "boom")
        except Exception as exc:  # pragma: no cover
            self.fail("log_line_throttled 抛出了异常: %r" % (exc,))


class TestSingleInstanceNoBusyWait(unittest.TestCase):

    def test_no_polling_sleep_loop(self):
        """探活线程不得再带 settimeout(0.5) + sleep(0.2) 的常驻轮询。"""
        src = inspect.getsource(sf_logging._become_single_instance)
        # 注意：探测端（连别人的那一侧）保留 settimeout(0.5) 是必要的连接超时，
        # 这里只约束"自己监听的那一侧"不得再设超时轮询。
        self.assertNotIn("server.settimeout", src,
                         "监听端 socket 不应再设超时，否则 accept 变成轮询")
        self.assertNotIn("time.sleep(0.2)", src,
                         "单实例探活线程不应再有 0.2s 常驻轮询")

    def test_stale_cleanup_refuses_non_socket_path(self):
        """权限/路径异常不得被误判为陈旧 socket 而删除普通文件。"""
        with tempfile.TemporaryDirectory(prefix="sf_single_") as tmpdir:
            path = os.path.join(tmpdir, "not-a-socket")
            with open(path, "w", encoding="utf-8") as f:
                f.write("keep")
            self.assertFalse(sf_logging._unlink_stale_single_socket(path))
            self.assertTrue(os.path.exists(path))

    def test_stale_cleanup_removes_confirmed_socket_only(self):
        """仅无监听者的 Unix socket 可被清理，以便下一实例安全绑定。"""
        with tempfile.TemporaryDirectory(prefix="sf_single_") as tmpdir:
            path = os.path.join(tmpdir, "stale.sock")
            server = sf_logging.socket.socket(sf_logging.socket.AF_UNIX, sf_logging.socket.SOCK_STREAM)
            try:
                server.bind(path)
            finally:
                server.close()
            self.assertTrue(sf_logging._unlink_stale_single_socket(path))
            self.assertFalse(os.path.exists(path))

    def test_single_instance_still_guards(self):
        """改成阻塞 accept 后，多开保护本身必须照常工作。"""
        tmpdir = tempfile.mkdtemp(prefix="sf_single_")
        sock = os.path.join(tmpdir, "single.sock")
        orig = sf_logging.SINGLE_SOCK
        sf_logging.SINGLE_SOCK = sock
        try:
            self.assertTrue(sf_logging._become_single_instance(),
                            "首个实例应成为唯一实例")
            self.assertFalse(sf_logging._become_single_instance(),
                             "已有实例在运行时，第二个实例应被挡下")
        finally:
            sf_logging.SINGLE_SOCK = orig
            try:
                os.unlink(sock)
            except OSError:
                pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
