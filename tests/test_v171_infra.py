# -*- coding: utf-8 -*-
"""实时股价 v1.2.72 第4轮走查：日志轮转 / 诊断脱敏 / 配置保留 的回归测试。

覆盖三项修复（均属"从没走查过的基础设施层"）：
A. 日志轮转：保留多代（不再是单代覆盖）、轮转与写入持同一把锁、检查按间隔节流；
   log_line 显式 encoding="utf-8"，中文不再是"日志莫名少一截"的隐患。
B. 诊断导出脱敏 redact_config：敏感键整体脱敏、URL 查询串里 token/sign 的
   **值**被抹掉，而接口模板（如 ?secid=%s）保持原样不破坏排障信息。
C. save_config 保留未知字段（不再每次保存静默删数据）；
   _backup_config 校验主配置可解析（坏配置不再覆盖好备份）。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_config
import sf_logging
from sf_constants import LOG_ROTATE_KEEP


# =====================================================================
# A. 日志轮转与写入
# =====================================================================


class TestDiagnosticLogRedaction(unittest.TestCase):
    def test_redacts_url_headers_and_credentials(self):
        raw = ("GET https://alice:pass@example.com/a?token=abc&keep=1 "
               "Authorization: Bearer secret-value Cookie=session=xyz api_key=key-123")
        out = sf_config.redact_log_text(raw)
        for secret in ("alice", "pass", "abc", "secret-value", "xyz", "key-123"):
            self.assertNotIn(secret, out)
        self.assertIn("example.com", out)
        self.assertNotIn("keep=1", out)
        self.assertIn("***REDACTED***", out)

class TestLogRotation(unittest.TestCase):
    """轮转保留多代、写满不丢更早的现场。"""

    def setUp(self):
        self._td = tempfile.mkdtemp(prefix="stockfloat_log171_")
        self._log = os.path.join(self._td, "debug.log")
        self._patches = [
            patch.object(sf_logging, "LOG_PATH", self._log),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._td, ignore_errors=True)

    def _rotate(self):
        """直接驱动内部轮转（绕过节流），返回轮转后存在的文件名集合。"""
        sf_logging._LAST_ROTATE_CHECK = 0.0
        with open(self._log, "w", encoding="utf-8") as f:
            f.write("x" * 10)
        with sf_logging._LOG_LOCK:
            with patch.object(sf_logging, "LOG_MAX_BYTES", 5):
                sf_logging._maybe_rotate_log()
        return sorted(n for n in os.listdir(self._td))

    def test_keeps_multiple_generations(self):
        """连续轮转 3 次后应存在 .1/.2/.3 三代，而非只留一代被反复覆盖。"""
        names = set()
        for _ in range(3):
            names |= set(self._rotate())
        for i in range(1, LOG_ROTATE_KEEP + 1):
            self.assertIn("debug.log.%d" % i, names,
                          "应保留第 %d 代日志（原实现只有 .1，故障前现场会被冲掉）" % i)

    def test_does_not_exceed_keep(self):
        """轮转 6 次后不得出现第 4 代（超出保留上限的最老一代须丢弃）。"""
        names = set()
        for _ in range(6):
            names |= set(self._rotate())
        self.assertNotIn("debug.log.%d" % (LOG_ROTATE_KEEP + 1), names)

    def test_rotation_check_is_throttled(self):
        """未到检查间隔时不重复 stat + listdir（原实现每条日志都做一次）。"""
        real_listdir = os.listdir
        calls = []

        def spy(path):
            calls.append(path)
            return real_listdir(path)

        with open(self._log, "w", encoding="utf-8") as f:
            f.write("x" * 10)
        sf_logging._LAST_ROTATE_CHECK = 0.0
        try:
            with patch.object(sf_logging.os, "listdir", spy), \
                 patch.object(sf_logging, "LOG_MAX_BYTES", 5), \
                 patch.object(sf_logging, "LOG_ROTATE_CHECK_INTERVAL", 3600.0), \
                 patch.object(sf_logging.time, "monotonic", return_value=7200.0):
                sf_logging._maybe_rotate_log()      # 首次：执行（超限→轮转）
                n1 = len(calls)
                sf_logging._maybe_rotate_log()      # 节流窗口内：应整段跳过
                n2 = len(calls)
        finally:
            sf_logging._LAST_ROTATE_CHECK = 0.0
        self.assertEqual(n1, 1, "首次检查应执行一次 listdir")
        self.assertEqual(n2, 1, "节流窗口内不应重复 listdir")

    def test_log_line_writes_utf8_cn(self):
        """中文日志可正常落盘（显式 utf-8，不依赖启动环境的 locale）。"""
        with patch.object(sf_logging, "_nslog_line", lambda s: None):
            sf_logging.log_line("测试中文 涨跌幅 100.0%", "INFO")
        with open(self._log, "r", encoding="utf-8") as f:
            body = f.read()
        self.assertIn("测试中文", body)
        self.assertIn("100.0%", body)

    def test_concurrent_writes_all_land(self):
        """多线程并发写日志不丢行（轮转与写入共用同一把锁）。"""
        with patch.object(sf_logging, "_nslog_line", lambda s: None):
            def worker(n):
                for i in range(20):
                    sf_logging.log_line("t%d-%d" % (n, i), "INFO")
            ts = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        with open(self._log, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln]
        self.assertEqual(len(lines), 80, "并发写日志不得丢行")


# =====================================================================
# B. 诊断导出脱敏
# =====================================================================
class TestRedactConfig(unittest.TestCase):
    """诊断包是要发给别人的，凭据必须出不去、排障信息必须留得下。"""

    def test_sensitive_keys_fully_redacted(self):
        out = sf_config.redact_config({"token": "abc", "api_key": "k",
                                       "code": "sh600519"})
        self.assertEqual(out["token"], "***REDACTED***")
        self.assertEqual(out["api_key"], "***REDACTED***")
        self.assertEqual(out["code"], "sh600519")

    def test_url_token_value_masked(self):
        out = sf_config.redact_config(
            {"api_urls": {"quote": "https://x.com/q?token=SECRET&code=1"}})
        self.assertNotIn("SECRET", json.dumps(out, ensure_ascii=False))
        self.assertIn("https://x.com/", out["api_urls"]["quote"])
        self.assertNotIn("code=1", out["api_urls"]["quote"])

    def test_nested_secret_is_redacted(self):
        """嵌套认证字段也必须脱敏，不能只处理配置第一层。"""
        src = {"provider": {"auth": {"token": "INNER_SECRET"}},
               "items": [{"password": "INNER_PASSWORD"}]}
        out = sf_config.redact_config(src)
        dump = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("INNER_SECRET", dump)
        self.assertNotIn("INNER_PASSWORD", dump)
        self.assertEqual(out["provider"]["auth"], "***REDACTED***")
        self.assertEqual(out["items"][0]["password"], "***REDACTED***")

    def test_url_template_not_corrupted(self):
        """保留来源和模板占位提示，但不凭字段名猜测第三方 URL 是否含凭据。"""
        tpl = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get"
               "?secid=%s&fields1=f1&fields2=f51")
        out = sf_config.redact_config({"api_urls": {"minute": tpl}})
        self.assertIn("push2his.eastmoney.com", out["api_urls"]["minute"])
        self.assertIn("%s", out["api_urls"]["minute"])
        self.assertNotIn("secid=", out["api_urls"]["minute"])

    def test_non_url_string_untouched(self):
        out = sf_config.redact_config({"code": "sh600519", "bg": "glass"})
        self.assertEqual(out, {"code": "sh600519", "bg": "glass"})

    def test_does_not_mutate_input(self):
        src = {"token": "abc"}
        sf_config.redact_config(src)
        self.assertEqual(src["token"], "abc", "脱敏须返回副本，不修改入参")


# =====================================================================
# C. 配置保留与备份保护
# =====================================================================
class _ConfigTestCase(unittest.TestCase):
    """临时目录隔离配置路径 + 捕获 log_line。"""

    def setUp(self):
        self._td = tempfile.mkdtemp(prefix="stockfloat_cfg171_")
        self._p = os.path.join(self._td, "config.json")
        self._bak = self._p + ".bak"
        self.logs = []

        def _fake_log(msg, level="INFO"):
            self.logs.append((msg, level))

        self._patches = [
            patch.object(sf_config, "CONFIG_PATH", self._p),
            patch.object(sf_config, "CONFIG_BACKUP_PATH", self._bak),
            patch.object(sf_config, "SPLIT_SOURCE_CONFIG_PATH",
                         os.path.join(self._td, "missing-formal.json")),
            patch.object(sf_config, "LEGACY_CONFIG_PATH",
                         os.path.join(self._td, "missing-legacy.json")),
            patch.object(sf_config, "log_line", _fake_log),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._td, ignore_errors=True)

    def _write(self, d):
        with open(self._p, "w", encoding="utf-8") as f:
            json.dump(d, f)

    def _read(self):
        with open(self._p, "r", encoding="utf-8") as f:
            return json.load(f)


class TestSaveConfigPreservesFields(_ConfigTestCase):
    """保存配置不再静默丢弃未知字段。"""

    def test_unknown_field_survives_save(self):
        self._write({"code": "sh600519", "x": 1, "y": 2,
                     "custom_flag": True, "future_key": {"a": 1}})
        self.assertTrue(sf_config.save_config("sh600519", 10, 20, 200, "glass"))
        out = self._read()
        self.assertEqual(out["custom_flag"], True, "未知字段不得被保存流程丢弃")
        self.assertEqual(out["future_key"], {"a": 1})

    def test_known_fields_still_updated(self):
        self._write({"code": "sh600519", "x": 1, "y": 2, "custom_flag": True})
        self.assertTrue(sf_config.save_config("sz000001", 33, 44, 240, "solid"))
        out = self._read()
        self.assertEqual(out["code"], "sz000001")
        self.assertEqual((out["x"], out["y"], out["h"], out["bg"]), (33, 44, 240, "solid"))

    def test_os_major_and_api_urls_preserved(self):
        self._write({"code": "sh600519", "os_major": "15",
                     "api_urls": {"quote": "https://x/q"}})
        self.assertTrue(sf_config.save_config("sh600519", 1, 2, 200, "glass"))
        out = self._read()
        self.assertEqual(out["os_major"], "15")
        self.assertEqual(out["api_urls"], {"quote": "https://x/q"})

    def test_non_dict_top_level_not_carried_over(self):
        """主配置顶层是数组时按损坏处理：不沿用坏结构，但要留下日志。"""
        with open(self._p, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
        self.assertTrue(sf_config.save_config("sh600519", 1, 2, 200, "glass"))
        out = self._read()
        self.assertIsInstance(out, dict, "保存后必须恢复为对象结构")
        self.assertEqual(out["code"], "sh600519")
        self.assertTrue(any("顶层结构非法" in m for m, _ in self.logs))


class TestBackupNeverOverwrittenByBadConfig(_ConfigTestCase):
    """坏配置不得覆盖好备份 —— 备份是损坏时唯一的兜底。"""

    def test_bad_config_does_not_clobber_good_backup(self):
        good = {"code": "sh600519", "x": 5, "y": 6, "h": 200, "bg": "glass"}
        self._write(good)
        self.assertTrue(sf_config.save_config("sh600519", 5, 6, 200, "glass"))
        self.assertTrue(os.path.exists(self._bak))
        # 主配置被写坏（模拟崩溃/断电留下半截 JSON）
        with open(self._p, "w", encoding="utf-8") as f:
            f.write('{"code": "sh60')
        sf_config._backup_config()
        with open(self._bak, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), good,
                             "主配置损坏时不得用坏数据覆盖既有好备份")
        self.assertTrue(any("SKIPPED" in m for m, _ in self.logs),
                        "跳过备份必须留 WARNING，不能静默")

    def test_good_config_still_backed_up(self):
        self._write({"code": "sh600519", "x": 1, "y": 2})
        sf_config._backup_config()
        with open(self._bak, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["code"], "sh600519")

    def test_restore_writeback_failure_is_visible(self):
        """从好备份恢复时，主配置写回失败必须留下 WARNING，而非静默。"""
        good = {"code": "sz000001", "x": 7, "y": 8, "h": 200, "bg": "solid"}
        with open(self._bak, "w", encoding="utf-8") as f:
            json.dump(good, f)
        with open(self._p, "w", encoding="utf-8") as f:
            f.write('{"broken":')
        real_replace = os.replace
        def fail_main_replace(src, dst):
            if dst == self._p:
                raise OSError("simulated writeback failure")
            return real_replace(src, dst)
        with patch.object(sf_config.os, "replace", side_effect=fail_main_replace):
            got = sf_config.load_config()
        self.assertEqual(got[0], "sz000001", "恢复读取仍应返回备份中的有效配置")
        self.assertTrue(any("写回主配置失败" in m for m, _ in self.logs),
                        "恢复写回失败必须可见，便于排查权限/磁盘问题")


if __name__ == "__main__":
    unittest.main()
