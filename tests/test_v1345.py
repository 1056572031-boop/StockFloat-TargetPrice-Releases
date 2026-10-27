# -*- coding: utf-8 -*-
"""实时股价 v1.3.45 —— 冷启动首帧稳定性 + 启动提速 回归测试。

覆盖四处改动（每处都同时验证「改了」和「没改坏」）：

1. 主窗几何时序（消除冷启动变形）
   原顺序 setFixedWidth → setMinimumHeight → setMaximumHeight → resize，
   会让窗口连穿三个尺寸：Qt 默认 480 高 → setMaximumHeight(420) 把 480 钳到
   420 → resize 才落到 224。中间那个 184×420 是纯粹的钳制产物，一旦被系统
   画一次，用户看到的就是「窗口先很高、再被压扁」。
   改为先 resize 到最终尺寸、再上 min/max 约束。

2. 首屏数据快照（last_quote.json）
   冷启动首帧必然无数据，原来先画占位符 "--"、等行情到了再整屏换掉。
   现在把最后一条行情落盘，启动即预填。测试重点在**副作用隔离**：
   缓存价不得参与目标价/涨跌闪烁判定（_real_price=0 + _price_is_fallback）。

3. 字体度量预热 warm_font_metrics：存在且可在无窗口时安全调用。

4. 启动关键路径减负：pyobjc 延迟导入、UI 首拍间隔 16ms、崩溃检查后移。
"""
import ast
import inspect
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

import sf_config
import sf_constants
import sf_notify
import sf_quote
import sf_ui_render
import stock_float_window as m


# =====================================================================
# 1. 几何时序
# =====================================================================
class TestGeometryOrder(unittest.TestCase):
    """构造期只允许出现一次尺寸落定，不得有被 min/max 钳出的中间态。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _build_with_geometry_log(self):
        """构造一个 FloatWindow，记录每次几何变更前后的尺寸。"""
        log = []

        def hook(name):
            fn = fn_map[name]

            def wrapper(self, *a, **k):
                before = (self.width(), self.height())
                r = fn(self, *a, **k)
                log.append((name, before, (self.width(), self.height())))
                return r
            return wrapper

        # 先取原始实现再打桩，避免 hook 之间互相拿到已被替换的版本
        fn_map = {meth: getattr(m.FloatWindow, meth)
                  for meth in ("setFixedWidth", "setMinimumHeight",
                               "setMaximumHeight", "resize", "move")}
        shadowed = {meth: m.FloatWindow.__dict__.get(meth)
                    for meth in fn_map}
        for meth in fn_map:
            setattr(m.FloatWindow, meth, hook(meth))
        try:
            with patch.object(m, "load_config",
                              return_value=("sh600519", 100, 100, 224, "translucent")), \
                 patch.object(m, "save_config", lambda *a, **k: None), \
                 patch.object(sf_quote, "http_get", lambda *a, **k: ""), \
                 patch.object(m, "start_icon_worker", lambda: None), \
                 patch.object(m.FloatWindow, "_force_top", lambda self: None), \
                 patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None):
                win = m.FloatWindow()
        finally:
            # 恢复必须"还原到继承链"，不能把父类方法写成子类属性 —— 那样留给
            # 后续用例的是一个未绑定的 C++ 槽，直接 TypeError。
            for meth, orig_attr in shadowed.items():
                if orig_attr is None:
                    if meth in m.FloatWindow.__dict__:
                        delattr(m.FloatWindow, meth)
                else:
                    setattr(m.FloatWindow, meth, orig_attr)
        win.running = False
        try:
            win._wake.set()
        except Exception:
            pass
        for t in ("timer", "top_timer", "_anim_timer", "_flash_timer"):
            try:
                getattr(win, t).stop()
            except Exception:
                pass
        return win, log

    def test_no_clamped_intermediate_size(self):
        """构造过程中不得出现「被 max/min 钳出来的第三个尺寸」。

        判定口径：记录每次几何调用**之前**的尺寸。优化前序列是
        640×480 → 184×480 → 184×480 → 184×420 → 184×224（420 是钳制产物）；
        优化后应为 640×480 → 184×480 → 184×224，此后不再变化。
        """
        win, log = self._build_with_geometry_log()
        try:
            befores = [b for _n, b, _a in log]
            # 去掉连续的重复项，得到"真实走过的尺寸"
            path = []
            for s in befores:
                if not path or path[-1] != s:
                    path.append(s)
            self.assertNotIn(
                [184, 420], [list(s) for s in path],
                "构造期出现 184×420 钳制中间态（冷启动变形的根因），实际路径：%s"
                % (path,))
            # 落定之后不应再有任何尺寸变化
            self.assertEqual(win.height(), 224, "已保存的用户高度应保持不变")
            self.assertEqual(win.minimumHeight(), m.MIN_H)
            self.assertEqual(win.maximumHeight(), 420)
        finally:
            win.close()

    def test_resize_precedes_minmax(self):
        """源码级防回归：resize/move 必须排在 setMinimum/MaximumHeight 之前。"""
        src = inspect.getsource(m.FloatWindow.__init__)
        i_resize = src.find("self.resize(W, startup_h)")
        i_move = src.find("self.move(x, y)")
        i_min = src.find("self.setMinimumHeight(MIN_H)")
        i_max = src.find("self.setMaximumHeight(MAX_H)")
        self.assertGreater(i_resize, 0, "找不到 resize 调用")
        self.assertGreater(i_move, i_resize)
        self.assertGreater(i_min, i_move, "setMinimumHeight 必须排在 move 之后")
        self.assertGreater(i_max, i_min, "setMaximumHeight 必须排在 setMinimumHeight 之后")


# =====================================================================
# 2. 首屏数据快照
# =====================================================================
class _SnapshotBase(unittest.TestCase):
    """把首屏快照路径指到临时目录，并显式打开预填（run_tests.sh 默认关闭）。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])
        cls._tmpdir = tempfile.mkdtemp(prefix="sf_v1345_")

    def setUp(self):
        self._env = {
            "SF_LAST_QUOTE_PATH": os.path.join(self._tmpdir, "last_quote.json"),
            "SF_SEED_FIRST_SCREEN": "1",
        }
        self._old = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        if os.path.exists(self._env["SF_LAST_QUOTE_PATH"]):
            os.remove(self._env["SF_LAST_QUOTE_PATH"])

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _make_win(self):
        with patch.object(m, "load_config",
                          return_value=("sh600519", 100, 100, 224, "translucent")), \
             patch.object(m, "save_config", lambda *a, **k: None), \
             patch.object(sf_quote, "http_get", lambda *a, **k: ""), \
             patch.object(m, "start_icon_worker", lambda: None), \
             patch.object(m.FloatWindow, "_force_top", lambda self: None), \
             patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None):
            win = m.FloatWindow()
        win.running = False
        try:
            win._wake.set()
        except Exception:
            pass
        for t in ("timer", "top_timer", "_anim_timer", "_flash_timer"):
            try:
                getattr(win, t).stop()
            except Exception:
                pass
        return win


class TestLastQuoteSnapshot(_SnapshotBase):

    def test_cache_path_isolated_from_config(self):
        """快照必须独立于 config.json —— 行情每秒在变，不能高频重写用户配置。"""
        self.assertNotEqual(sf_constants.LAST_QUOTE_PATH, sf_constants.CONFIG_PATH)
        self.assertTrue(sf_constants.LAST_QUOTE_PATH.endswith("last_quote.json"))
        self.assertGreaterEqual(sf_constants.LAST_QUOTE_MIN_WRITE_INTERVAL, 5.0,
                                "快照写盘必须有节流，不能跟着 8 次/秒的行情写盘")

    def test_save_load_roundtrip(self):
        q = {"name": "贵州茅台", "code": "600519", "price": "1688.00",
             "pct": "1.23", "high": "1700.00", "low": "1660.00"}
        self.assertTrue(sf_config.save_last_quote(
            "sh600519", q, [("0930", 1680.0, None), ("1000", 1688.0, None)], "2026-09-12"))
        snap = sf_config.load_last_quote()
        self.assertIsInstance(snap, dict)
        self.assertEqual(snap["code"], "sh600519")
        self.assertEqual(snap["quote"]["price"], "1688.00")
        self.assertEqual(snap["minute"][1][1], 1688.0)

    def test_load_returns_none_when_missing(self):
        self.assertIsNone(sf_config.load_last_quote())

    def test_seed_fills_first_screen(self):
        """有同代码快照时，构造完成即有价格 —— 首屏不再从 "--" 起步。"""
        sf_config.save_last_quote(
            "sh600519",
            {"name": "贵州茅台", "code": "600519", "price": "1688.00",
             "pct": "1.23", "high": "1700.00", "low": "1660.00"},
            [("0930", 1680.0, None)], "2026-09-12")
        win = self._make_win()
        try:
            self.assertTrue(getattr(win, "_seeded_from_cache", False),
                            "有同代码快照时应完成首屏预填")
            self.assertEqual(win._disp_price, 1688.0)
            self.assertIsNotNone(win.quote)
            self.assertEqual(win.quote["name"], "贵州茅台")
        finally:
            win.close()

    def test_seed_does_not_leak_into_decisions(self):
        """缓存价不得参与目标价/涨跌判定：_real_price 必须置 0。"""
        sf_config.save_last_quote(
            "sh600519", {"name": "贵州茅台", "code": "600519", "price": "1688.00",
                         "pct": "9.99"}, [], "2026-09-12")
        win = self._make_win()
        try:
            self.assertEqual(win._real_price, 0.0,
                             "缓存价不能作为判定价，否则开盘前会误触发「跌到 X」")
            self.assertTrue(win._price_is_fallback)
            self.assertEqual(win._current_price(), 0.0,
                             "_current_price 必须读不到缓存价")
        finally:
            win.close()

    def test_seed_skipped_when_code_differs(self):
        """换过股票就不能用别人的缓存价。"""
        sf_config.save_last_quote(
            "sz000001", {"name": "平安银行", "code": "000001",
                         "price": "11.00", "pct": "1.00"}, [], "2026-09-12")
        win = self._make_win()
        try:
            self.assertFalse(getattr(win, "_seeded_from_cache", False))
            self.assertIsNone(win._disp_price)
        finally:
            win.close()

    def test_seed_zeroes_pct_across_days(self):
        """涨跌幅属于某个交易日：隔夜快照只复用价格，涨跌幅归零不误导。"""
        sf_config.save_last_quote(
            "sh600519", {"name": "贵州茅台", "code": "600519",
                         "price": "1688.00", "pct": "9.99"}, [], "2000-01-01")
        win = self._make_win()
        try:
            self.assertTrue(getattr(win, "_seeded_from_cache", False))
            self.assertEqual(win._disp_price, 1688.0, "价格是最后已知事实，可复用")
            self.assertEqual(win._disp_pct, 0.0, "隔夜涨跌幅不得沿用")
        finally:
            win.close()

    def test_write_is_throttled(self):
        """8 次/秒的行情不能变成 8 次/秒的写盘。"""
        win = self._make_win()
        try:
            path = self._env["SF_LAST_QUOTE_PATH"]
            win._maybe_cache_first_screen()
            from tests._base import wait_for
            self.assertTrue(wait_for(lambda: os.path.exists(path)), "后台首屏缓存未落盘")
            self.assertTrue(os.path.exists(path))
            st1 = os.stat(path).st_mtime_ns
            win._maybe_cache_first_screen()
            win._maybe_cache_first_screen()
            st2 = os.stat(path).st_mtime_ns
            self.assertEqual(st1, st2, "节流窗口内不得重复写盘")
        finally:
            win.close()


# =====================================================================
# 3. 字体度量预热
# =====================================================================
class TestFontWarmup(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_warm_font_metrics_exists_and_is_safe(self):
        self.assertTrue(callable(getattr(sf_ui_render, "warm_font_metrics", None)),
                        "缺少 warm_font_metrics")
        sf_ui_render.load_bundled_fonts()
        sf_ui_render.warm_font_metrics()      # 不得抛异常
        sf_ui_render.warm_font_metrics()      # 幂等：重复调用安全

    def test_called_before_window_shown(self):
        """源码级：warm_font_metrics 必须出现在启动入口、且早于 win.show()。"""
        with open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "stock_float_window.py"),
                  encoding="utf-8") as f:
            src = f.read()
        tail = src[src.find('if __name__ == "__main__":'):]
        self.assertIn("warm_font_metrics()", tail)
        self.assertLess(tail.find("warm_font_metrics()"), tail.find("win.show()"),
                        "预热必须早于窗口露出")


# =====================================================================
# 4. 启动关键路径减负
# =====================================================================
class TestStartupPath(unittest.TestCase):

    def test_pyobjc_not_imported_at_module_level(self):
        """sf_notify 顶层不得再同步 import pyobjc（实测 54~62ms）。

        用 AST 精确断言：import 必须发生在函数体内，而不是模块级。
        """
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "sf_notify.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        bad = []
        for node in tree.body:                      # 只看模块级语句
            if isinstance(node, ast.ImportFrom) and node.module in (
                    "UserNotifications", "Foundation"):
                bad.append(node.module)
            # 兼容模块级 try/except 包裹的写法（历史版本就是这么写的）
            if isinstance(node, ast.Try):
                for sub in node.body:
                    if isinstance(sub, ast.ImportFrom) and sub.module in (
                            "UserNotifications", "Foundation"):
                        bad.append(sub.module)
        self.assertEqual(bad, [],
                         "sf_notify 模块级仍在 import pyobjc：%r（应延迟到函数内）" % (bad,))
        self.assertTrue(callable(getattr(sf_notify, "_ensure_un", None)),
                        "缺少延迟导入入口 _ensure_un")

    def test_lazy_import_names_still_patchable(self):
        """延迟导入后模块级名字必须保留（测试桩要能替换）。"""
        for name in ("UNUserNotificationCenter", "UNMutableNotificationContent",
                     "UNNotificationRequest", "NSObject", "NSString",
                     "dispatch_get_main_queue", "dispatch_async"):
            self.assertTrue(hasattr(sf_notify, name), "缺少可替换的模块级名字 %s" % name)

    def test_ui_wakeup_replaces_fast_poll_timer(self):
        """首屏数据不等定时器 —— v1.4.10 起改由"入队即唤醒"保证。

        原用例断言源码里含 `self.timer.start(16)`，守的是"启动后不要白等 50ms"。
        现在 UI 定时器退成 1 秒兜底看门狗，16ms 这个字面量本身已无意义；
        真正要守的行为（数据一到就上屏）由 tests/test_power_v1410.py 用
        真实窗口 + 信号计数来断言。
        """
        self.assertEqual(m.UI_WATCHDOG_INTERVAL_MS, 1000,
                         "UI 定时器已退为兜底看门狗，间隔必须是有限值")

    def test_crash_check_moved_after_show(self):
        """崩溃报告检查是纯诊断，不得挡在首帧前面。"""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "stock_float_window.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        tail = src[src.find('if __name__ == "__main__":'):]
        self.assertLess(tail.find("win.show()"), tail.find("_check_recent_crashes"),
                        "崩溃检查必须排到窗口露出之后")



if __name__ == "__main__":
    unittest.main()
