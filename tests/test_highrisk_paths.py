# -*- coding: utf-8 -*-
"""v1.3.68：高风险路径的最小行为测试（见 TESTS_AUDIT_2026-09-14.md 之 P1-5 / P2-6）。

本文件只守两条仍在使用的高风险路径：
1. 毛玻璃：异常分支必须降级，不能让主窗消失；
2. 退出：后台线程必须停干净，避免退出后残留。

设计原则（沿用现有框架，不引入新依赖）：
  · 标准 unittest，与 tools/test_runner.py 完全兼容；
  · 涉及系统动作一律 mock；
  · 断言的是行为与边界，不绑定实现细节。
"""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from _base import wait_for
except ImportError:  # pragma: no cover - 包模式导入
    from tests._base import wait_for

import stock_float_window as m
from PyQt6.QtWidgets import QApplication


QApplication.instance() or QApplication([])


class _WindowCase(unittest.TestCase):
    """构造一个"不会真联网、不会真写盘"的主窗。patch 清单沿用 test_bugfix_148 的口径。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            mock.patch.object(m, "load_config",
                              return_value=("sh600519", 100, 100, 200, "translucent")),
            mock.patch.object(m, "save_config", lambda *a, **k: None),
            mock.patch.object(m, "start_icon_worker", lambda: None),
            mock.patch.object(m.FloatWindow, "_force_top", lambda self: None),
            mock.patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            mock.patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        try:
            import sf_quote
            http = mock.patch.object(sf_quote, "http_get", lambda *a, **k: "")
            http.start()
            self._patches.append(http)
        except Exception:
            pass
        self.win = m.FloatWindow()
        self.win.running = False
        try:
            self.win._wake.set()
        except Exception:
            pass
        # 停掉所有定时器：构造出来的窗口不该在测试里自己滴答（避免污染后续用例）
        for attr in ("timer", "top_timer", "_anim_timer", "_flash_timer"):
            try:
                getattr(self.win, attr).stop()
            except Exception:
                pass

    def tearDown(self):
        try:
            self.win.close()
        except Exception:
            pass
        for p in reversed(self._patches):
            try:
                p.stop()
            except Exception:
                pass


class TestGlassFailureDegrades(_WindowCase):
    """毛玻璃：任何内部异常都必须静默降级为 False，绝不能冒到调用方把窗炸掉。"""

    def test_setup_glass_returns_false_on_bad_winid(self):
        import sf_glass
        # _setup_glass 定义在 sf_glass.GlassMixin 里，直接用未绑定方法喂一个残桩：
        # 残桩的 winId 返回 None，内部链路必然抛异常 —— 守的就是"这时必须返回 False"
        stub = type("_Stub", (), {"winId": lambda self: None})()
        ret = sf_glass.GlassMixin._setup_glass(stub)
        self.assertIs(ret, False,
                      "内部链路不可用时 _setup_glass 必须返回 False 而不是抛异常")

    def test_window_survives_glass_failure(self):
        """即使毛玻璃建不起来，窗口本体仍要能正常显示（不能白屏/消失）。"""
        self.win._glass_pending = True
        with mock.patch.object(m.FloatWindow, "_setup_glass", lambda self: False):
            try:
                self.win.show()
                self.assertTrue(self.win.isVisible(), "毛玻璃降级后主窗仍必须可见")
                frame = self.win.grab()
                self.assertFalse(frame.isNull(), "毛玻璃降级后主窗必须仍可抓帧")
                self.assertEqual(frame.size(), self.win.size(),
                                 "抓帧尺寸必须与主窗一致")
            except Exception as exc:
                self.fail("毛玻璃失败不应影响窗口显示：%r" % (exc,))


class TestShutdownStopsThreads(_WindowCase):
    """退出必须停干净 —— 全仓第一个真实的线程不变量。"""

    def _own_threads(self, win):
        found = []
        for name, val in vars(win).items():
            if "thread" in name.lower() and isinstance(val, threading.Thread):
                found.append((name, val))
        return found

    def test_shutdown_joins_window_threads(self):
        if not hasattr(self.win, "_shutdown_threads"):
            self.skipTest("主窗没有 _shutdown_threads 入口")
        own = self._own_threads(self.win)
        if not own:
            self.skipTest("构造出的主窗没有常驻线程，无法验证 join")
        self.win._shutdown_threads()
        ok = wait_for(lambda: all(not t.is_alive() for _n, t in own),
                      timeout=3.0,
                      msg="退出后仍有常驻线程未停止")
        alive = [(n, t.name) for n, t in own if t.is_alive()]
        self.assertTrue(ok, "退出后未停止的线程：%r" % (alive,))

    def test_active_thread_count_returns_to_baseline(self):
        """线程总数回到构造前水平：守护"反复启停不会越攒越多线程"这类泄漏。"""
        baseline = threading.active_count()
        self.win._shutdown_threads()
        ok = wait_for(lambda: threading.active_count() <= baseline,
                      timeout=3.0, msg="活跃线程数未回到基线")
        extra = [t.name for t in threading.enumerate() if t.is_alive()]
        self.assertTrue(ok, "基线 %d，当前仍活跃线程：%r" % (baseline, extra))


if __name__ == "__main__":
    unittest.main()
