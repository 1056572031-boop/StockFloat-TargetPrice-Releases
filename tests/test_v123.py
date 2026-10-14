# -*- coding: utf-8 -*-
"""实时股价 v1.2.23 新功能回归测试。

覆盖 4 项优化：
1. 非交易时段行情轮询拉长到 60 分钟（POLL_INTERVAL_OFF_MARKET=3600）
2. 配置保存自动留最近 1 份备份快照（.bak）+ 主配置损坏时自动从快照恢复
3. 添加/替换重复股票时明确提示（不再静默替换）
4. 全局异常钩子增强：崩溃信息（时间戳/线程/堆栈/上下文）写入日志

运行方式（在 StockFloat 目录）：
    python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import traceback
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

import sf_config
import sf_logging
import sf_quote
import stock_float_window as m
from sf_editor import EditorWindow
from sf_constants import DEFAULT_STOCK, POLL_INTERVAL_OFF_MARKET, CONFIG_SCHEMA_VERSION


def _fake_http_get(url, **kw):
    """离线行情/分时 mock：避免构造窗口时触发真实网络请求。"""
    if "hq.sinajs.cn" in url:
        return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
    if "web.ifzq.gtimg.cn" in url:
        return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                '"data":["0930 1510.0 100 1000", "1000 1511.0 200 2000"]}}}}')
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


class TestConfigBackup(unittest.TestCase):
    """需求2：save_config 自动留最近 1 份 .bak 快照；load_config 损坏自动恢复。"""

    def setUp(self):
        self._td = tempfile.mkdtemp(prefix="stockfloat_bak_")
        self._p = os.path.join(self._td, "config.json")
        self._bak = self._p + ".bak"
        self._patch_path = patch.object(sf_config, "CONFIG_PATH", self._p)
        self._patch_bak = patch.object(sf_config, "CONFIG_BACKUP_PATH", self._bak)
        self._patch_split = patch.object(
            sf_config, "SPLIT_SOURCE_CONFIG_PATH", os.path.join(self._td, "missing-formal.json"))
        self._patch_legacy = patch.object(
            sf_config, "LEGACY_CONFIG_PATH", os.path.join(self._td, "missing-legacy.json"))
        self._patch_path.start()
        self._patch_bak.start()
        self._patch_split.start()
        self._patch_legacy.start()

    def tearDown(self):
        self._patch_legacy.stop()
        self._patch_split.stop()
        self._patch_bak.stop()
        self._patch_path.stop()

    def _write(self, d, path=None):
        p = path or self._p
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f)

    def test_save_creates_backup_snapshot(self):
        """保存前旧配置被备份为 .bak，内容为上一份完整配置"""
        self._write({"code": "sh600000", "x": 1, "y": 2, "h": 240, "bg": "glass"})
        sf_config.save_config("sh600519", 10, 20, 240, "glass")
        self.assertTrue(os.path.exists(self._bak), ".bak 快照未生成")
        with open(self._bak, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["code"], "sh600000", ".bak 应保留保存前的旧配置")

    def test_backup_keeps_only_latest_one(self):
        """连续保存只保留最近 1 份快照（每次覆盖上一份）"""
        self._write({"code": "sh600000", "x": 1, "y": 2, "h": 240, "bg": "glass"})
        sf_config.save_config("sh600519", 1, 2, 240, "glass")
        with open(self._bak, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["code"], "sh600000")
        # 第二次保存后 .bak 应为第一次保存的配置
        sf_config.save_config("sh601318", 1, 2, 240, "glass")
        with open(self._bak, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["code"], "sh600519", ".bak 只保留最近 1 份快照")

    def test_load_recovers_from_backup_on_corrupt(self):
        """主配置损坏（JSON 解析失败）→ 自动从 .bak 恢复并写回主配置"""
        self._write({"code": "sh600000", "x": 7, "y": 8, "h": 240, "bg": "glass"})
        sf_config.save_config("sh600000", 7, 8, 240, "glass")   # 生成有效 .bak
        with open(self._p, "w", encoding="utf-8") as f:         # 破坏主配置
            f.write("{not-valid-json!!")
        code, x, y, h, bg = sf_config.load_config()
        self.assertEqual((code, x, y, h, bg), ("sh600000", 7, 8, 240, "glass"))
        # 恢复后主配置应被修复回写，不再损坏
        with open(self._p, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["code"], "sh600000")

    def test_load_no_backup_returns_default(self):
        """主配置损坏且无可用 .bak → 返回默认配置且不抛异常"""
        self._write({"code": "sh600000", "x": 7, "y": 8, "h": 240, "bg": "glass"})
        with open(self._p, "w", encoding="utf-8") as f:
            f.write("{broken")
        code, x, y, h, bg = sf_config.load_config()
        self.assertEqual(code, DEFAULT_STOCK)


class TestPollOffMarket(unittest.TestCase):
    """需求1：非交易时段行情轮询降频到 60 分钟，盘中不受影响。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", _fake_http_get),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()
        self.win.timer.stop()
        self.win.top_timer.stop()

    def tearDown(self):
        self.win.close()
        for p in self._patches:
            p.stop()

    def test_off_market_interval_constant_is_60min(self):
        """常量 = 3600 秒（60 分钟）"""
        self.assertEqual(POLL_INTERVAL_OFF_MARKET, 3600.0)

    def test_poll_interval_off_market_returns_3600(self):
        """非交易时段 _poll_interval 返回 3600s（不再 60s）"""
        self.win._is_trading_time = lambda now=None: False
        self.assertEqual(self.win._poll_interval(), POLL_INTERVAL_OFF_MARKET)

    def test_poll_interval_trading_unaffected(self):
        """盘中轮询间隔保持熔断状态机（NORMAL=0.1s），不受降频影响"""
        self.win._is_trading_time = lambda now=None: True
        self.win._poll_state = m.POLL_NORMAL
        self.win._poll_fail = 0
        self.assertEqual(self.win._poll_interval(), m.POLL_INTERVAL_NORMAL)

    def test_minute_loop_code_uses_off_market_constant(self):
        """分时线程非交易分支使用 60 分钟常量（源码级断言，防回归到 60s）"""
        import inspect
        src = inspect.getsource(m.FloatWindow._minute_loop)
        self.assertIn("else POLL_INTERVAL_OFF_MARKET", src)
        self.assertNotIn("else 60.0", src)


class TestExcepthookCrashLog(unittest.TestCase):
    """需求4：全局异常钩子把崩溃信息（时间戳/线程/堆栈/上下文）写入日志。"""

    def setUp(self):
        self._td = tempfile.mkdtemp(prefix="stockfloat_crash_")
        self._log = os.path.join(self._td, "debug.log")
        self._patch_log = patch.object(m, "LOG_PATH", self._log)
        self._patch_log_sfl = patch.object(sf_logging, "LOG_PATH", self._log)
        self._patch_log.start()
        self._patch_log_sfl.start()
        self._orig_sys = sys.excepthook
        self._orig_thr = getattr(threading, "excepthook", None)

    def tearDown(self):
        sys.excepthook = self._orig_sys
        if self._orig_thr is not None:
            threading.excepthook = self._orig_thr
        self._patch_log.stop()
        self._patch_log_sfl.stop()

    def _trigger_crash(self):
        m._install_global_excepthook()
        try:
            raise ValueError("boom-test")
        except ValueError:
            etype, val, tb = sys.exc_info()
            sys.excepthook(etype, val, tb)

    def test_crash_written_with_timestamp_thread_and_stack(self):
        """崩溃日志含 UNCAUGHT 标记、时间戳、thread、异常类型与堆栈"""
        self._trigger_crash()
        self.assertTrue(os.path.exists(self._log), "崩溃日志文件未生成")
        with open(self._log, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("UNCAUGHT", content)
        self.assertIn("thread=main", content)
        self.assertIn("ValueError: boom-test", content)
        self.assertIn("_trigger_crash", content,
                      "堆栈应包含触发崩溃的调用帧")
        # 时间戳形如 2026-08-26 00:31:00
        self.assertIn("UNCAUGHT 20", content, "应含时间戳（YYYY- 前缀）")

    def test_crash_log_has_version_context(self):
        """日志含版本号上下文，便于定位崩溃来源版本"""
        self._trigger_crash()
        with open(self._log, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("v%s" % m.APP_VERSION, content)


class TestEditorDuplicate(unittest.TestCase):
    """需求3：添加/替换重复股票给出明确提示，不做静默替换。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _make(self, code="sh600519"):
        applied = []

        class FakeAnchor:
            def x(self):
                return 0

            def y(self):
                return 0

        ed = EditorWindow(code, applied.append, FakeAnchor(), 200)
        return ed, applied

    def test_is_dup_detects_same_stock(self):
        """归一化后同一代码判定为重复（带前缀/纯数字两种形式）"""
        ed, _ = self._make("sh600519")
        self.assertTrue(ed._is_dup("sh600519"))
        self.assertTrue(ed._is_dup("600519"))
        self.assertFalse(ed._is_dup("sz300750"))
        ed.close()

    def test_choose_candidate_dup_does_not_apply(self):
        """重复股票点击候选：不触发应用回调，并给出提示文本"""
        ed, applied = self._make("sh600519")
        ed._choose_candidate("600519")
        self.assertEqual(applied, [], "重复股票不应触发替换")
        self.assertIn("已在显示中", ed.hint.text())
        ed.close()

    def test_choose_candidate_new_applies(self):
        """非重复股票正常应用替换"""
        ed, applied = self._make("sh600519")
        ed._choose_candidate("sz300750")
        self.assertEqual(applied, ["sz300750"])
        ed.close()

    def test_do_search_dup_direct_code_greyed(self):
        """直接输入当前股票 6 位代码：确定按钮置灰，回车不执行、不替换（v1.2.31 新交互）"""
        ed, applied = self._make("sh600519")
        ed.entry.setText("600519")   # 触发 textChanged → ok 置灰
        self.assertFalse(ed.ok.isEnabled(), "输入与当前相同的代码时确定按钮应置灰")
        ed.do_search()
        self.assertEqual(applied, [], "置灰状态下回车不应触发替换")
        ed.close()

    def test_do_search_new_code_enables_ok(self):
        """回归：输入新代码后确定按钮解除置灰、可正常替换（v1.2.33 修复 textChanged 漏接信号）"""
        ed, applied = self._make("sh600519")
        # 初始置灰——这是 bug 暴露点：v1.2.31 漏接 textChanged 导致永远置灰
        self.assertFalse(ed.ok.isEnabled(), "初始状态确定按钮应置灰")
        ed.entry.setText("000858")   # 输入新代码 → 应解除置灰
        self.assertTrue(ed.ok.isEnabled(), "输入新代码后确定按钮应解除置灰")
        with patch("sf_universe.match_confident", return_value=[("sz000858", "五粮液")]):
            ed.do_search()
        self.assertEqual(applied, ["sz000858"], "输入新代码后回车应触发替换")
        ed.close()

    def test_do_search_new_name_enables_ok(self):
        """回归：输入股票名称（非数字）后确定按钮解除置灰（v1.2.33 修复）"""
        ed, applied = self._make("sh600519")
        ed.entry.setText("茅台")
        self.assertTrue(ed.ok.isEnabled(), "输入股票名称后确定按钮应解除置灰")
        ed.close()



if __name__ == "__main__":
    unittest.main(verbosity=2)
