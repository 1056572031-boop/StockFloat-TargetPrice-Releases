# -*- coding: utf-8 -*-
"""v1.2.95 第5轮走查：诊断导出的**运行时**验证 + 脱敏与崩溃报告纯函数。

此前 `_export_diagnostics` 只有 inspect.getsource 的源码断言 —— zip 组装、
配置脱敏、崩溃报告读取全都没有真正跑起来过，等于没验证。
本组用例第一次让它在离屏环境真实执行，并检查产物内容。

为什么值得锁死：诊断包是发给别人排障的，一旦把用户自建行情接口里的
token/sign 原样打进去就不可收回。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sf_constants
import sf_crashreport
import sf_quote
import stock_float_window as m
from sf_config import redact_config


def _fake_http_get(url, **kw):
    if "hq.sinajs.cn" in url:
        return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


_SECRET = "SUPERSECRETTOKEN123"

_REAL_POPEN = subprocess.Popen


def _fake_popen(args, *a, **k):
    """放行真实子进程（platform.platform() 依赖它），只吞掉"在访达中显示"。"""
    if args and args[0] == "open":
        return None
    return _REAL_POPEN(args, *a, **k)


class _ExportCase(unittest.TestCase):
    """真实跑一次诊断导出，把 zip 重定向到临时目录（绝不污染用户桌面）。"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sf_diag_")
        self.cfg_path = os.path.join(self.tmp, "config.json")
        self.log_path = os.path.join(self.tmp, "debug.log")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write('{"code":"sh600519","my_token":"%s",'
                    '"api_urls":{"main":"https://x.example/q?token=%s"}}'
                    % (_SECRET, _SECRET))
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("[10:00:00] INFO: hello 诊断\n")
            # v1.3.41：日志里同样可能带账号、链接参数与授权信息，导出前必须脱敏
            f.write("[10:00:01] INFO: GET https://u:pw@q.example/x?token=%s&keep=1\n"
                    % _SECRET)
            f.write("[10:00:02] INFO: Authorization: Bearer %s\n" % _SECRET)

        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", _fake_http_get),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch.object(m.FloatWindow, "_quote_loop_wrapper", lambda self: None),
            patch.object(m.FloatWindow, "_minute_loop_wrapper", lambda self: None),
            patch.object(m, "CONFIG_PATH", self.cfg_path),
            patch.object(m, "LOG_PATH", self.log_path),
            # 只拦"打开访达"这一次调用：platform.platform() 内部也会走
            # subprocess，一并打成 None 会让 meta 生成失败（那正是本轮要
            # 锁死的问题，不能让测试桩把它掩盖掉）。
            patch.object(subprocess, "Popen", _fake_popen),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        self.win._anim_timer.stop()

    def tearDown(self):
        try:
            self.win._exit_persisted = True  # discipline-ok(R5): 关窗必须仍在隔离桩内，不能保存用户配置
            self.win._anim_timer.stop()
            self.win.running = False
            self.win._wake.set()
            self.win.close()
        except Exception:
            pass
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _export(self):
        real_expanduser = os.path.expanduser
        desktop = os.path.join(self.tmp, "Desktop")
        os.makedirs(desktop, exist_ok=True)
        with patch.object(m.os.path, "expanduser", side_effect=lambda p: (
                os.path.join(desktop, os.path.basename(p))
                if p.startswith("~/Desktop/") else real_expanduser(p))) as exp:
            out = self.win._export_diagnostics()
        requested = exp.call_args_list[0].args[0]
        # 导出文件名前缀从产品常量现推（v1.4.55 全局改名 Skyrocket，写死的旧名
        # 会让这一族用例整批红，而报错内容完全看不出是改名）。
        self.assertRegex(requested, r"^~/Desktop/" + sf_constants.APP_DISPLAY_NAME
                           + r"-诊断-\d{8}_\d{6}\.zip$")
        self.assertIsNotNone(out, "诊断包未生成")
        self.assertTrue(os.path.exists(out), "诊断包未生成")
        return zipfile.ZipFile(out)


class TestDiagnosticsExport(_ExportCase):

    def test_zip_contains_expected_entries(self):
        with self._export() as z:
            names = z.namelist()
        self.assertIn("diagnostics.txt", names)
        self.assertIn("config.json", names)
        self.assertIn("debug.log", names)

    def test_meta_contains_version(self):
        with self._export() as z:
            meta = z.read("diagnostics.txt").decode("utf-8", "replace")
        self.assertIn(m.APP_VERSION, meta, "诊断信息必须带版本号")

    def test_runtime_snapshots_are_exported_without_sensitive_endpoints(self):
        with self._export() as z:
            names = z.namelist()
            network = z.read("network.json").decode("utf-8", "replace")
            runtime = z.read("runtime.json").decode("utf-8", "replace")
            targets = z.read("targets.json").decode("utf-8", "replace")
            storage = z.read("storage.json").decode("utf-8", "replace")
        for name in ("network.json", "runtime.json", "targets.json", "storage.json"):
            self.assertIn(name, names)
        self.assertIn("monitor", network)
        self.assertIn("quote_failures", runtime)
        self.assertIn("pending", targets)
        self.assertIn("invalid", targets)
        self.assertIn("config_backup", storage)
        self.assertNotIn(_SECRET, network + runtime + targets + storage)

    def test_target_summary_categories_are_mutually_exclusive(self):
        self.win._alerts = [
            {"state": "hit", "session_date": "2999-01-01"},
            {"state": "pending", "session_date": "2999-01-01"},
            {"state": "pending", "session_date": "2000-01-01"},
            "invalid",
        ]
        with self._export() as z:
            targets = json.loads(z.read("targets.json").decode("utf-8", "replace"))
        self.assertEqual(targets["total"], 4)
        self.assertEqual(targets["hit"], 1)
        self.assertEqual(targets["pending"], 1)
        self.assertEqual(targets["expired"], 1)
        self.assertEqual(targets["invalid"], 1)
        self.assertEqual(sum(targets[key] for key in ("hit", "pending", "expired", "invalid")), 4)

    def test_target_store_prune_is_readable_in_the_bundle(self):
        """v1.4.60（复核 F7）：存储超上限剪掉的休眠条目必须能在诊断包里问出来。

        这事的三个去处被本轮规则各自堵住了：界面不许加说明行/弹窗（v1.4.52 用户要求）、
        日志会被 5MB×3 代与 7 天轮转挤掉、菜单里也不展示 —— 那"我某天设的目标价怎么
        不见了"就只能靠这一份 targets.json。判据取包内 JSON 的键名（公开产物），
        不读源码。
        """
        pruned = {"at": "2026-10-10 14:00:00", "dropped": 2,
                  "pruned_codes": ["sz000001", "sh600000"]}
        with patch.object(m, "store_prune_report", lambda: dict(pruned)), \
                patch.object(m, "load_alerts",
                             lambda code=None: [{"code": "sh600519"}] * 3):
            with self._export() as z:
                targets = json.loads(z.read("targets.json").decode("utf-8", "replace"))
        self.assertEqual(targets["stored_total"], 3, "存储总条数要可读（含别的股票留着的）")
        self.assertEqual(targets["store_prune"], pruned,
                         "剪了多少条、剪掉哪几只、什么时候剪的，三样都得在包里")

    def test_secret_is_redacted(self):
        """诊断包要外发，任何 token/密钥都不得原样出现。"""
        with self._export() as z:
            cfg = z.read("config.json").decode("utf-8", "replace")
        self.assertNotIn(_SECRET, cfg,
                        "明文密钥出现在了诊断包里，外发即泄露")
        self.assertIn("REDACTED", cfg)

    def test_log_content_is_included(self):
        with self._export() as z:
            log = z.read("debug.log").decode("utf-8", "replace")
        self.assertIn("hello 诊断", log)

    def test_log_secret_is_redacted(self):
        """日志文件里的账号、链接参数、授权信息同样不得原样外发。"""
        with self._export() as z:
            log = z.read("debug.log").decode("utf-8", "replace")
        self.assertNotIn(_SECRET, log, "明文密钥出现在导出的日志里，外发即泄露")
        self.assertNotIn("u:pw@", log, "URL 里的用户名密码必须被抹掉")
        self.assertIn("REDACTED", log)
        # 脱敏只针对凭据，不得把整行排障信息一起吞掉
        self.assertIn("q.example", log)
        self.assertNotIn("keep=1", log)

    def test_last_failures_summary_is_redacted_before_export(self):
        path = os.path.join(self.tmp, "last_failures.json")
        home = os.path.expanduser("~")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"failures": {"probe": {
                "exc": "token=do-not-export %s/private" % home}}}, f)
        with patch.object(m, "last_failures_path", return_value=path):
            with self._export() as z:
                summary = z.read("last_failures.json").decode("utf-8", "replace")
        self.assertNotIn("do-not-export", summary)
        self.assertNotIn(home, summary)
        self.assertIn("REDACTED", summary)
        json.loads(summary)

    def test_crash_report_hides_home_directory(self):
        """崩溃报告里的 /Users/<账号>/ 路径必须脱敏，不能外发系统用户名。

        用 HOME 指向临时目录来模拟真实家目录，避免改动全局 os.path 行为。
        """
        diag_dir = os.path.join(self.tmp, "Library", "Logs", "DiagnosticReports")
        os.makedirs(diag_dir, exist_ok=True)
        with open(os.path.join(diag_dir, "StockFloatTargetPrice_2026-09-11-101500.ips"),
                  "w", encoding="utf-8") as f:
            f.write("Process: StockFloatTargetPrice\n"
                    "Exception Type: EXC_BAD_ACCESS (SIGSEGV)\n"
                    "Crashed Thread: 0\n"
                    "Thread 0 Crashed:\n"
                    "0 StockFloat %s/Applications/X\n" % self.tmp)
        with patch.dict(os.environ, {"HOME": self.tmp}):
            with self._export() as z:
                crash = [n for n in z.namelist() if n.startswith("crash_reports/")]
                self.assertTrue(crash, "崩溃报告未纳入诊断包")
                body = z.read(crash[0]).decode("utf-8", "replace")
        self.assertNotIn(self.tmp, body, "本机完整路径仍出现在崩溃报告里")
        self.assertIn("~/Applications", body)

    def test_storage_errors_redact_home_path_and_crash_read_failure_is_reported(self):
        home = os.path.expanduser("~")
        real_stat = m.os.stat
        def fake_stat(path):
            if path == m.CONFIG_BACKUP_PATH:
                raise OSError("cannot read " + home + "/secret")
            return real_stat(path)
        with patch.object(m.os, "stat", side_effect=fake_stat), \
             patch.object(m.os, "listdir", side_effect=OSError("cannot scan " + home + "/crash")):
            with self._export() as z:
                storage = z.read("storage.json").decode("utf-8", "replace")
                meta = z.read("diagnostics.txt").decode("utf-8", "replace")
        self.assertNotIn(home, storage + meta)
        self.assertIn("crash_reports", meta)

    def test_missing_config_and_log_do_not_break_export(self):
        """配置文件/日志缺失时仍要能导出，不能整个失败。"""
        os.remove(self.cfg_path)
        os.remove(self.log_path)
        with self._export() as z:
            names = z.namelist()
        self.assertIn("diagnostics.txt", names)
        self.assertNotIn("config.json", names)


class TestRedaction(unittest.TestCase):

    def test_nested_auth_is_redacted(self):
        """递归脱敏：嵌套多层的认证信息同样要拦住（诊断包外发不可收回）。"""
        d = {"provider": {"auth": {"token": "abc123"}},
             "list": [{"secret": "s1"}],
             "url": "https://x.example/q?token=zzz&sign=yyy",
             "code": "sh600519"}
        r = redact_config(d)
        self.assertNotIn("abc123", str(r))
        self.assertNotIn("s1", str(r))
        self.assertNotIn("zzz", str(r))
        self.assertEqual(r["code"], "sh600519", "非敏感字段必须原样保留")

    def test_input_not_mutated(self):
        d = {"token": "keepme"}
        redact_config(d)
        self.assertEqual(d["token"], "keepme", "脱敏不得修改入参")


class TestCrashReportHelpers(unittest.TestCase):

    def test_name_filter(self):
        self.assertTrue(sf_crashreport.is_crash_report_name("StockFloatTargetPrice-2026-09-07-123456.ips"))
        self.assertFalse(sf_crashreport.is_crash_report_name("SomeOtherApp-2026.ips"))
        self.assertFalse(sf_crashreport.is_crash_report_name("notes.txt"))

    def test_extract_handles_empty_and_junk(self):
        """空串/乱码不得抛异常，也不得产出超限内容。"""
        for text in ("", "not a crash report at all", "\x00\x01\x02"):
            out = sf_crashreport.extract_crash_key_sections(text)
            self.assertIsInstance(out, str)
            self.assertLessEqual(len(out.encode("utf-8")),
                                 sf_crashreport.MAX_SEGMENT_BYTES + 1024)

    def test_extract_respects_size_limit(self):
        body = "Exception Type: EXC_BAD_ACCESS\n" + ("x" * 500000)
        out = sf_crashreport.extract_crash_key_sections(body)
        self.assertLessEqual(len(out.encode("utf-8")),
                             sf_crashreport.MAX_SEGMENT_BYTES * 2,
                             "崩溃报告关键段必须裁剪到上限内")


if __name__ == "__main__":
    unittest.main()
