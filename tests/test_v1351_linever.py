# -*- coding: utf-8 -*-
"""v1.3.52 —— 首屏快照写入失效折线缓存 回归测试。

背景：本应用用 self._line_ver（版本号自增）驱动分时绘制缓存失效。写
self.minute_points 的 4 处里，切股清空（约 1479）、行情更新（约 1971/2030）
都跟了 _line_ver += 1，唯独 _seed_first_screen（首屏快照冷启动预填）漏了。

漏掉在 v1.3.52 之前不会炸，因为当时只有折线走了缓存、而首屏写入发生在
__init__ 里（缓存此时还是空的）。但均价线路径缓存（键含 _line_ver）一旦
加上，首屏快照恢复的旧图就会被当成新图缓存住，切股后可能显示上一只股票
的残影。

覆盖两处：
1. 源码级：_seed_first_screen 函数体里必须出现 self._line_ver += 1。
2. 行为级：构造真实主窗（构造期用 SF_SEED_FIRST_SCREEN=0 关掉预填，避免
   干扰），手工把 _line_ver 归零并写入同代码快照，调用 _seed_first_screen
   后版本号必须递增、且分时点确实被填进去了。

运行方式（在 StockFloat 目录）：
    QT_QPA_PLATFORM=offscreen ./.venv/bin/python -m unittest discover -s tests \
        -p "test_v1351_linever.py" -v
"""
import inspect
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

import sf_config
import sf_quote
import stock_float_window as m


class _WindowBase(unittest.TestCase):
    """构造一个真实 FloatWindow 并停掉定时器，用完即关。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])
        cls._tmpdir = tempfile.mkdtemp(prefix="sf_v1351_")

    def setUp(self):
        from tests._base import fixed_clock
        import datetime
        clock = fixed_clock(datetime.datetime(2026, 9, 12, 16, 30),
                            module_names=("sf_quote", "stock_float_window"))
        clock.__enter__()
        self.addCleanup(clock.__exit__, None, None, None)
        self._env = {
            # 快照落到临时目录，避免污染用户真实的 last_quote.json，
            # 也避免别的用例写进去的快照让本用例串味。
            "SF_LAST_QUOTE_PATH": os.path.join(self._tmpdir, "last_quote.json"),
            # 构造期先关掉首屏预填：本用例要自己控制预填时机与前置 _line_ver。
            "SF_SEED_FIRST_SCREEN": "0",
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
        """构造真实主窗并隔离后台行情线程。

        行情线程会异步写 minute_points 并自增 _line_ver，不隔离的话本用例
        断言的基线会被它踩掉（联网环境下尤其明显）。这里把取数函数整段
        no-op 掉，保证整个用例期间只有被测代码会动这两处状态。
        """
        for target, ret in ((m.FloatWindow, "_fetch_quote"), (m.FloatWindow, "_fetch_minute")):
            p = patch.object(target, ret, lambda self: None)
            p.start()
            self.addCleanup(p.stop)
        with patch.object(m, "load_config",
                          return_value=("sh600519", 100, 100, 224, "translucent")), \
             patch.object(m, "save_config", lambda *a, **k: None), \
             patch.object(sf_quote, "http_get", lambda *a, **k: ""), \
             patch.object(m, "start_icon_worker", lambda: None), \
             patch.object(m.FloatWindow, "_force_top", lambda self: None), \
             patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None):
            win = m.FloatWindow()
        self.addCleanup(win.close)
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

    def _write_snapshot(self, minute):
        sf_config.save_last_quote(
            "sh600519",
            {"name": "贵州茅台", "code": "600519", "price": "1688.00",
             "pct": "1.23", "high": "1700.00", "low": "1660.00"},
            minute, "2026-09-12")


class TestSeedBumpsLineVer(_WindowBase):
    """首屏快照写入必须和另外 3 处一样失效折线缓存。"""

    def test_source_level_increment_present(self):
        """源码级防回归：_seed_first_screen 里必须有 _line_ver 自增。"""
        src = inspect.getsource(m.FloatWindow._seed_first_screen)
        self.assertIn("self._line_ver += 1", src,
                      "_seed_first_screen 写入 minute_points 后未自增 _line_ver，"
                      "首屏快照恢复的旧图会被缓存住")

    def test_increment_sits_after_minute_points_write(self):
        """自增必须排在 minute_points 赋值之后（顺序反了等于没失效）。"""
        src = inspect.getsource(m.FloatWindow._seed_first_screen)
        i_write = src.find("self.minute_points =")
        i_bump = src.find("self._line_ver += 1")
        self.assertGreater(i_write, 0, "找不到 minute_points 赋值")
        self.assertGreater(i_bump, i_write,
                           "_line_ver 自增必须在 minute_points 赋值之后")

    def test_seed_bumps_line_ver(self):
        """行为级：预填首屏后 _line_ver 必须 +1，且分时点确实写进去了。"""
        self._write_snapshot([("0930", 1680.0, None), ("1000", 1688.0, None)])
        win = self._make_win()
        try:
            # 构造期预填已关闭，这里才打开，并显式把版本号归零做基线
            os.environ["SF_SEED_FIRST_SCREEN"] = "1"
            win._line_ver = 0
            win._seed_first_screen()

            self.assertTrue(getattr(win, "_seeded_from_cache", False),
                            "同代码快照应当完成首屏预填")
            self.assertEqual(len(win.minute_points), 2,
                             "快照里的分时点应被写进 minute_points")
            self.assertEqual(win._line_ver, 1,
                             "写入分时数据后 _line_ver 必须自增，否则折线/均价线"
                             "缓存不会失效，切股后可能残留上一只股票的图")
        finally:
            win.close()

    def test_seed_bumps_only_once_per_call(self):
        """每次预填只自增一次（多次调用应线性累加，不跳过也不重复）。"""
        self._write_snapshot([("0930", 1680.0, None)])
        win = self._make_win()
        try:
            os.environ["SF_SEED_FIRST_SCREEN"] = "1"
            win._line_ver = 7
            win._seed_first_screen()
            self.assertEqual(win._line_ver, 8)
            win._seed_first_screen()
            self.assertEqual(win._line_ver, 9)
        finally:
            win.close()

    def test_no_minute_points_leaves_ver_untouched(self):
        """快照里没有分时点时没写 minute_points，版本号不应凭空跳。"""
        self._write_snapshot([])
        win = self._make_win()
        try:
            os.environ["SF_SEED_FIRST_SCREEN"] = "1"
            win._line_ver = 3
            win._seed_first_screen()
            self.assertTrue(getattr(win, "_seeded_from_cache", False))
            self.assertEqual(win.minute_points, [])
            self.assertEqual(win._line_ver, 3,
                             "未改动 minute_points 时不应自增版本号")
        finally:
            win.close()


class TestReexportContract(_WindowBase):
    """v1.3.52 登记的 8 个再导出符号：正文零使用，但不得删除。"""

    NAMES = ("RED", "GREEN", "FG_DIM", "_migrate_legacy_config", "_drop_flash_due",
             "POLL_DEGRADED", "POLL_INTERVAL_DEGRADED", "POLL_INTERVAL_NORMAL")

    def test_symbols_still_reexported(self):
        """测试以 m.符号 直访；删掉导入就是一片 AttributeError。"""
        missing = [n for n in self.NAMES if not hasattr(m, n)]
        self.assertEqual(missing, [],
                         "再导出符号缺失（v1.3.52 契约，禁止当死代码删）：%r" % (missing,))

    def test_values_match_host_modules(self):
        """再导出必须与宿主模块同源，不能是各写一份的副本。"""
        import sf_constants
        import sf_config
        import sf_ui_render
        import sf_utils
        expect = {
            "RED": sf_ui_render.RED,
            "GREEN": sf_ui_render.GREEN,
            "FG_DIM": sf_constants.FG_DIM,
            "_migrate_legacy_config": sf_config._migrate_legacy_config,
            "_drop_flash_due": sf_utils._drop_flash_due,
            "POLL_DEGRADED": sf_constants.POLL_DEGRADED,
            "POLL_INTERVAL_DEGRADED": sf_constants.POLL_INTERVAL_DEGRADED,
            "POLL_INTERVAL_NORMAL": sf_constants.POLL_INTERVAL_NORMAL,
        }
        for name, val in expect.items():
            self.assertIs(getattr(m, name), val,
                          "%s 与宿主模块不是同一个对象" % name)

    def test_contract_is_documented_in_source(self):
        """契约必须写在代码里，不能只靠人记住（v1.3.46 就是靠人记住才没删）。"""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "stock_float_window.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("再导出契约", src, "缺少再导出契约注释块")


if __name__ == "__main__":
    unittest.main(verbosity=2)
