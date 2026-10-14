# -*- coding: utf-8 -*-
"""实时股价 v1.2.41 运行时调度纯逻辑单测（上帝类拆分第三步 sf_runtime 侧验证）。

覆盖 sf_runtime：
1. NapTokenManager：App Nap 令牌生命周期（begin/end 幂等、状态未变化零系统调用、
   令牌回收后可再激活、系统 API 失败静默降级、stop 收尾幂等）
2. RuntimeState：缩 Dock / 恢复窗口的 _minimized 门控 + 按交易状态切换 App Nap；
   注入 fake 管理器验证状态转换，不触碰 Qt / Foundation
3. restore_ui_interval：恢复窗口后 UI 轮询间隔决策（交易 50ms / 非交易 1000ms）
4. 模块导入不依赖 Qt（无任何 PyQt6 / Foundation import）

运行方式（在 StockFloat 目录）：
    python3 unittest discover -s tests 或 python3 -m unittest tests/test_runtime.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sf_constants import UI_WATCHDOG_INTERVAL_MS
from sf_runtime import NapTokenManager, RuntimeState, restore_ui_interval


class FakeProcessInfo(object):
    """可注入的 NSProcessInfo 替身：记录 begin/end 调用、可抛异常模拟系统失败。"""

    def __init__(self, fail_begin=False, fail_end=False):
        self.fail_begin = fail_begin
        self.fail_end = fail_end
        self.begins = 0
        self.ends = []
        self.live_tokens = []
        self._seq = 0
        self.last_options = None
        self.last_reason = None

    def beginActivityWithOptions_reason_(self, options, reason):
        self.begins += 1
        self.last_options = options
        self.last_reason = reason
        if self.fail_begin:
            raise RuntimeError("beginActivity failed (simulated)")
        self._seq += 1
        tok = ("token", self._seq)
        self.live_tokens.append(tok)
        return tok

    def endActivity_(self, token):
        self.ends.append(token)
        if self.fail_end:
            raise RuntimeError("endActivity failed (simulated)")
        if token in self.live_tokens:
            self.live_tokens.remove(token)


class TestNapTokenManager(unittest.TestCase):
    """App Nap 令牌生命周期（原 _update_nap_policy 纯决策 + 系统调用封装）。"""

    def _manager(self, **kw):
        info = FakeProcessInfo(**kw)
        return NapTokenManager(ns_process_info=info), info

    def test_initial_state_inactive_without_calls(self):
        mgr, info = self._manager()
        self.assertFalse(mgr.active())
        self.assertIsNone(mgr._token)
        self.assertEqual(info.begins, 0)
        self.assertEqual(info.ends, [])

    def test_activate_begins_once_and_holds_token(self):
        mgr, info = self._manager()
        tok = mgr.set_trading(True)
        self.assertEqual(info.begins, 1)
        self.assertEqual(info.ends, [])
        self.assertIsNotNone(tok)
        self.assertTrue(mgr.active())
        # 持有的令牌与系统侧记录一致
        self.assertEqual(mgr._token, info.live_tokens[0])

    def test_activate_passes_options_and_reason(self):
        info = FakeProcessInfo()
        mgr = NapTokenManager(ns_process_info=info, activity_options=0x1,
                              reason="自定义原因")
        mgr.set_trading(True)
        self.assertEqual(info.last_reason, "自定义原因")
        self.assertEqual(info.last_options, 0x1)   # 注入的 options 原样透传

    def test_default_options_loads_foundation_constant(self):
        """未注入 options 时懒加载 Foundation.NSActivityUserInitiated（回退位 1<<0）。"""
        try:
            import Foundation
        except Exception:
            self.skipTest("Foundation 不可用，跳过默认 options 断言")
        expected = Foundation.NSActivityUserInitiatedAllowingIdleSystemSleep
        info = FakeProcessInfo()
        mgr = NapTokenManager(ns_process_info=info)
        mgr.set_trading(True)
        self.assertEqual(info.last_options, expected)
        self.assertFalse(info.last_options & Foundation.NSActivityIdleSystemSleepDisabled)

    def test_activate_idempotent_no_second_begin(self):
        mgr, info = self._manager()
        mgr.set_trading(True)
        tok_keep = mgr.set_trading(True)   # 已持有令牌，状态未变化 → 不重复 begin
        self.assertEqual(info.begins, 1)
        self.assertIsNotNone(tok_keep)
        self.assertTrue(mgr.active())

    def test_deactivate_ends_and_releases_token(self):
        mgr, info = self._manager()
        tok_held = mgr.set_trading(True)
        tok = mgr.set_trading(False)
        self.assertIsNone(tok)
        self.assertFalse(mgr.active())
        self.assertEqual(info.ends, [tok_held])   # end 回收的是先前持有的令牌
        self.assertEqual(info.live_tokens, [])

    def test_deactivate_idempotent_no_repeat_end(self):
        mgr, info = self._manager()
        mgr.set_trading(False)   # 未持有令牌 → 不调 endActivity
        mgr.set_trading(False)
        self.assertEqual(info.ends, [])
        self.assertEqual(info.begins, 0)

    def test_reactivate_after_release_begins_again(self):
        mgr, info = self._manager()
        mgr.set_trading(True)
        mgr.set_trading(False)
        mgr.set_trading(True)
        self.assertEqual(info.begins, 2)
        self.assertTrue(mgr.active())

    def test_begin_failure_silently_degrades(self):
        mgr, info = self._manager(fail_begin=True)
        tok = mgr.set_trading(True)
        self.assertIsNone(tok)
        self.assertFalse(mgr.active())
        self.assertEqual(info.begins, 1)

    def test_end_failure_keeps_token_then_release_recovers(self):
        mgr, info = self._manager(fail_end=True)
        mgr.set_trading(True)
        tok = mgr.set_trading(False)     # endActivity 抛异常被吞，令牌保留
        self.assertIsNotNone(tok)
        self.assertTrue(mgr.active())
        # 下一轮正常系统即回收（fail_end 仍为 True 则再吞，此处关闭后验证释放路径）
        info.fail_end = False
        tok2 = mgr.set_trading(False)
        self.assertIsNone(tok2)
        self.assertFalse(mgr.active())

    def test_stop_releases_token_idempotent(self):
        mgr, info = self._manager()
        mgr.set_trading(True)
        mgr.stop()
        self.assertFalse(mgr.active())
        self.assertEqual(len(info.ends), 1)
        before = len(info.ends)
        mgr.stop()
        self.assertEqual(len(info.ends), before)   # 幂等，不重复 end


class TestRuntimeState(unittest.TestCase):
    """缩 Dock / 恢复窗口 运行状态机（App Nap 门控 + _minimized 状态）。"""

    def _state(self, **kw):
        info = FakeProcessInfo(**kw)
        return RuntimeState(nap_manager=NapTokenManager(ns_process_info=info)), info

    def test_initial_not_minimized_and_inactive(self):
        st, _ = self._state()
        self.assertFalse(st.minimized)
        self.assertFalse(st.nap_active)
        self.assertIsNone(st.nap_token)

    def test_enter_dock_sets_minimized(self):
        st, info = self._state()
        st.enter_dock()
        self.assertTrue(st.minimized)

    def test_exit_dock_clears_minimized(self):
        st, _ = self._state()
        st.enter_dock()
        st.exit_dock()
        self.assertFalse(st.minimized)

    def test_minimized_property_rw(self):
        st, _ = self._state()
        st.minimized = True
        self.assertTrue(st.minimized)
        st.minimized = False
        self.assertFalse(st.minimized)

    def test_dock_gate_disables_app_nap(self):
        # 缩 Dock 场景：先交易激活，再缩 Dock（update_nap False）→ App Nap 停用
        st, info = self._state()
        st.update_nap(True)
        self.assertTrue(st.nap_active)
        st.enter_dock()
        st.update_nap(False)
        self.assertFalse(st.nap_active)
        self.assertEqual(len(info.ends), 1)

    def test_restore_reenables_nap_per_trading(self):
        # 恢复场景：缩 Dock 时停用；恢复且交易 → 重新激活；恢复且非交易 → 保持停用
        st, info = self._state()
        st.enter_dock()
        st.update_nap(False)
        st.exit_dock()
        st.update_nap(True)
        self.assertTrue(st.nap_active)
        self.assertEqual(info.begins, 1)
        #
        st2, _ = self._state()
        st2.enter_dock()
        st2.exit_dock()
        st2.update_nap(False)
        self.assertFalse(st2.nap_active)
        self.assertEqual(st2.minimized, False)

    def test_update_nap_idempotent_across_poll_cycles(self):
        # 每轮轮询都调 update_nap(True) → 仅首次 begin
        st, info = self._state()
        for _ in range(5):
            st.update_nap(True)
        self.assertEqual(info.begins, 1)
        for _ in range(5):
            st.update_nap(False)
        self.assertEqual(len(info.ends), 1)
        self.assertFalse(st.nap_active)

    def test_shutdown_releases_token(self):
        st, info = self._state()
        st.update_nap(True)
        st.shutdown()
        self.assertFalse(st.nap_active)
        self.assertIsNone(st.nap_token)
        before = len(info.ends)
        st.shutdown()
        self.assertEqual(len(info.ends), before)

    def test_default_manager_created_when_not_injected(self):
        st = RuntimeState()
        self.assertIsInstance(st._nap, NapTokenManager)
        self.assertFalse(st.nap_active)


class TestRestoreUiInterval(unittest.TestCase):
    """恢复窗口后 UI 兜底看门狗间隔（原 _restore_window 内 50/1000 字面量）。"""

    def test_trading_restores_to_watchdog(self):
        """v1.4.10：恢复窗口不再回到 50ms 高频轮询。

        数据交付已改为"入队即 emit ui_wakeup"，定时器只剩兜底巡检职责；
        恢复窗口后立刻回到 50ms 只会把每秒 20 次空转唤醒重新请回来。
        """
        self.assertEqual(restore_ui_interval(True), UI_WATCHDOG_INTERVAL_MS)

    def test_off_market_low_frequency(self):
        self.assertEqual(restore_ui_interval(False), 1000)


class TestModuleImportIndependence(unittest.TestCase):
    """模块可独立于 Qt / Foundation 导入（纯逻辑，无渲染依赖）。"""

    def test_no_qt_import(self):
        """模块级不得 import Qt；Foundation 仅作方法内懒加载（顶层无 import）。"""
        import sf_runtime
        src = open(os.path.join(os.path.dirname(sf_runtime.__file__), "sf_runtime.py"),
                   encoding="utf-8").read()
        for line in src.splitlines():
            self.assertFalse(line.startswith(("import PyQt6", "from PyQt6")),
                             "top-level PyQt6 import found: %r" % line)
            self.assertFalse(line.startswith(("import Foundation", "from Foundation")),
                             "top-level Foundation import found: %r" % line)


if __name__ == "__main__":
    unittest.main()
