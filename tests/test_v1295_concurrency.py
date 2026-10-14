# -*- coding: utf-8 -*-
"""v1.2.95 第6轮走查：后台循环退避必须可唤醒，退出时不能硬等。

原实现在"崩溃过于频繁"的退避分支用的是 time.sleep(30.0)：退出流程
_shutdown_threads 置 running=False 并 _wake.set() 后 join 超时只有 1.5s，
而 time.sleep 对 _wake 完全无感 —— 线程会带着未完成的请求被进程强杀，
退出期间还可能残留网络访问。

修复后统一改用可唤醒的 self._sleep()（Event.wait 实现）。
"""
import inspect
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sf_quote
import stock_float_window as m


def _fake_http_get(url, **kw):
    if "hq.sinajs.cn" in url:
        return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


class _WinCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", _fake_http_get),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m, "log_line", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        self.win._anim_timer.stop()

    def tearDown(self):
        try:
            self.win._anim_timer.stop()
            self.win.running = False
            self.win._wake.set()
            self.win.close()
        except Exception:
            pass
        for p in self._patches:
            p.stop()

    def _crashing(self, obj, attr, times):
        """让目标循环先抛 times 次异常，随后正常返回。"""
        state = {"n": 0}

        def _boom():
            state["n"] += 1
            if state["n"] <= times:
                raise RuntimeError("boom")
            return None

        setattr(obj, attr, _boom)
        return state


class TestBackoffIsInterruptible(_WinCase):

    def test_quote_wrapper_returns_immediately_when_quit_wakes_it(self):
        w = self.win
        self._crashing(w, "_quote_loop", 2)
        slept = []
        w._sleep = lambda s: (slept.append(s), False)[1]   # 模拟"退出中"被唤醒
        w.running = True
        t0 = time.monotonic()
        w._quote_loop_wrapper()
        self.assertLess(time.monotonic() - t0, 2.0,
                        "退出时不得硬等 30 秒退避（旧 time.sleep 行为）")
        self.assertIn(30.0, slept, "第二次崩溃应进入 30s 退避并被唤醒")

    def test_minute_wrapper_returns_immediately_when_quit_wakes_it(self):
        w = self.win
        self._crashing(w, "_minute_loop", 2)
        slept = []
        w._sleep = lambda s: (slept.append(s), False)[1]
        w.running = True
        t0 = time.monotonic()
        w._minute_loop_wrapper()
        self.assertLess(time.monotonic() - t0, 2.0,
                        "退出时不得硬等 30 秒退避")
        self.assertIn(30.0, slept)

    def test_first_crash_does_not_back_off(self):
        """首次崩溃应立即重启，不能先睡 30 秒 —— 否则一次偶发异常就停摆半分钟。"""
        for wrapper, loop in (("_quote_loop_wrapper", "_quote_loop"),
                              ("_minute_loop_wrapper", "_minute_loop")):
            w = self.win
            self._crashing(w, loop, 1)
            slept = []
            w._sleep = lambda s: (slept.append(s), True)[1]
            w.running = True
            getattr(w, wrapper)()
            self.assertEqual(slept, [],
                             "%s：首次崩溃不应退避，实睡 %r" % (wrapper, slept))

    def test_backoff_then_recovers(self):
        """退避过后若循环恢复正常，wrapper 应正常结束（不是无限重启）。"""
        w = self.win
        state = self._crashing(w, "_quote_loop", 2)
        slept = []
        w._sleep = lambda s: (slept.append(s), True)[1]
        w.running = True
        t0 = time.monotonic()
        w._quote_loop_wrapper()
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(state["n"], 3, "崩溃 2 次后第 3 次正常返回即结束")
        self.assertEqual(slept.count(30.0), 1, "期间只应退避一次")


class TestNoBlockingSleepInLoops(unittest.TestCase):
    """源码层面锁死：循环路径里不得再出现不可唤醒的 time.sleep。"""

    def test_wrappers_use_wakeable_sleep(self):
        # 只匹配真实调用 "time.sleep(" —— 说明性注释里提到它不算。
        for name in ("_quote_loop_wrapper", "_minute_loop_wrapper"):
            src = inspect.getsource(getattr(m.FloatWindow, name))
            self.assertNotIn("time.sleep(", src,
                             "%s 中仍有 time.sleep 调用 —— 退出时无法唤醒" % name)

    def test_quote_loop_uses_wakeable_sleep(self):
        src = inspect.getsource(m.FloatWindow._quote_loop)
        self.assertNotIn("time.sleep(", src,
                         "_quote_loop 中仍有 time.sleep 调用 —— 退出时无法唤醒")


if __name__ == "__main__":
    unittest.main()
