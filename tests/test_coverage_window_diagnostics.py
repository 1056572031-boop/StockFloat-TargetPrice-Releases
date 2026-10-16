# -*- coding: utf-8 -*-
"""诊断包与崩溃留痕的分支补测（遗留：`stock_float_window.py` 607 行未覆盖的第一批）。

为什么挑这三块先补（本轮为扩大自动化覆盖而加）
------------------------------------------------
`AGENTS §5.1` 把"出事后可查"列为第三道防线，而这一整条链此前只验过**主干**：
`tests/test_v1295_diagnostics.py` 真跑过一次导出、`tests/test_v123.py` 验过主线程崩溃
会写 `UNCAUGHT`。剩下的全是"出岔子的那几条分支"，而它们恰恰只在真出事时才被用到：

  · `_export_diagnostics` 的 20 来条分支：目标价分桶、runtime/storage 三份快照、
    崩溃报告的 7 天优先与家目录脱敏、快照超限、缺件清单、整体失败时的可见反馈。
    这些坏了的表现是"用户把包发过来了，里面少了最关键的那件东西"——事后才发现。
  · `_install_global_excepthook` 的**子线程适配器**：`threading.excepthook` 的签名
    与 `sys.excepthook` 不同（v1.2.71 就是栽在这里，钩子自己抛 TypeError，
    图标线程/编辑器线程的崩溃既不进 debug.log 也不进统一日志，空转了几十个版本）。
  · `_bundle_version`：装机版报告里那个版本号取错，整份崩溃报告的归属版本就都错了。

判据一律走**公开输出**：zip 里的文件名与内容、临时日志里的行、函数返回值。
不读源码做字符串断言（AGENTS §6 第 2 条）。落盘全部重定向到临时 HOME，
不碰用户桌面、不碰 `~/Library/Logs/DiagnosticReports`。
"""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_constants
import sf_quote
import stock_float_window as m
from tests._base import SFCase, wait_for

_QAPP = None


def _ensure_qapp():
    """QApplication 必须由模块级引用持有，否则离屏下会中途 abort（同 test_v1443 的做法）。"""
    global _QAPP
    if _QAPP is None:
        _QAPP = SFCase.ensure_qapp()
    return _QAPP


_DEV_VERSION = sf_constants.APP_VERSION    # 回退口径的期望值：唯一来源，不写死数字
_bundle_version = m._bundle_version        # discipline-ok(R5): 被测对象本身就是私有函数
# 种进临时 Info.plist 的两个值**故意不像版本号**：这条用例判的是"哪个键赢"，
# 不是某个具体版本。写成 9.9.9 会让用例变成"断言版本号"（AGENTS §6 第 1 条禁的正是它），
# 而读回的值本来就是字符串透传，用什么都成立。
PLIST_SHORT = "short-key-wins"
PLIST_BUILD = "build-key-fallback"
_SECRET = "SUPERSECRETTOKEN123"
_REAL_POPEN = subprocess.Popen


def _fake_popen(args, *a, **k):
    """只吞掉"在访达中显示诊断包"，其余子进程放行。

    为什么不全打成 None：`platform.platform()` 内部也要走 subprocess，一并拦掉会让
    meta 生成失败 —— 那正是本文件要锁死的东西，不能让自家测试桩把它掩盖掉。
    """
    if args and args[0] == "open":
        return None
    return _REAL_POPEN(args, *a, **k)


def _fake_http_get(url, **kw):
    return ""


class DiagnosticsBranchCase(SFCase):
    """真跑导出，只把"出岔子的那几条分支"逐条走一遍。"""

    def setUp(self):
        _ensure_qapp()
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="sf_diag_branch_")
        self.home = os.path.join(self.tmp, "home")
        # 真用户的 HOME 里一定有 Desktop（导出物默认落在其 Skyrocket 子目录）；
        # 临时 HOME 不建它，导出会 FileNotFoundError 返回 None —— 那是夹具缺件，
        # 不是被测行为。
        os.makedirs(os.path.join(self.home, "Desktop", "Skyrocket"), exist_ok=True)
        self.cfg_path = os.path.join(self.tmp, "config.json")
        self.log_path = os.path.join(self.tmp, "debug.log")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write('{"code":"sh600519","api_urls":{"main":"https://x.example/q?token=%s"}}'
                    % _SECRET)
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("[10:00:00] INFO: hello 诊断\n")

        self._patches = [
            mock.patch.dict(os.environ, {"HOME": self.home}),
            mock.patch.object(m, "load_config",
                              return_value=("sh600519", 100, 100, 200, "translucent")),
            mock.patch.object(m, "save_config", lambda *a, **k: None),
            mock.patch.object(m, "start_icon_worker", lambda: None),
            mock.patch.object(sf_quote, "http_get", _fake_http_get),
            mock.patch.object(m.FloatWindow, "_force_top", lambda self: None),
            mock.patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            mock.patch.object(m.FloatWindow, "_quote_loop_wrapper", lambda self: None),
            mock.patch.object(m.FloatWindow, "_minute_loop_wrapper", lambda self: None),
            mock.patch.object(m, "CONFIG_PATH", self.cfg_path),
            mock.patch.object(m, "CONFIG_BACKUP_PATH", self.cfg_path + ".bak"),
            mock.patch.object(m, "LAST_QUOTE_PATH", os.path.join(self.tmp, "last_quote.json")),
            mock.patch.object(m, "LOG_PATH", self.log_path),
            mock.patch.object(m, "DESKTOP_OUTPUT_DIR",
                              os.path.join(self.home, "Desktop", "Skyrocket")),
            mock.patch.object(subprocess, "Popen", _fake_popen),
            # "在访达里显示"这条路径不是被检对象，拦掉即可
            mock.patch.object(m.QToolTip, "showText", lambda *a, **k: None),
        ]
        for p in self._patches:
            self.track_patch(p)
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()  # discipline-ok(R5): 夹具要把唤醒闸放开，这是 FloatWindow 唯一的入口，没有公开开关
        for timer in (self.win.timer, self.win.top_timer, self.win._anim_timer,  # discipline-ok(R5): 停掉帧驱动/动画定时器是夹具职责，这几个句柄都是私有的
                      self.win._flash_timer):  # discipline-ok(R5): 同上
            try:
                timer.stop()
            except Exception:
                pass
        self.win.show()
        # 目标价：四种桶各来一条，分桶判据才有正反两面
        self.win._alerts = [  # discipline-ok(R5): 目标价列表就是私有状态；诊断包读的是它，没有别的注入点
            {"code": "sh600000", "state": "pending"},
            {"code": "sh600001", "state": "hit"},
            {"code": "sh600002", "state": "pending", "expired": True},
            "不是一条记录",
        ]
        # 分桶判据要的是 expired() 的回答，这里给一个确定答案：
        # "过期与否"本身由 targets 的用例负责，本文件只管诊断包怎么归类。
        self.track_patch(mock.patch.object(m, "expired", lambda alert, now: bool(
            isinstance(alert, dict) and alert.get("expired"))))

    def tearDown(self):
        self.win._exit_persisted = True      # discipline-ok(R5): 关窗发生在 patch 还活着的时候，别写用户配置（AGENTS §6 第 11 条）
        try:
            self.win.close()
        except Exception:
            pass
        super().tearDown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _export(self):
        """真导一次，返回 zip 内的 {文件名: 文本}。"""
        out = self.win._export_diagnostics()  # discipline-ok(R5): 被测对象本身就是 GlassMixin/FloatWindow 的私有入口
        self.assertIsNotNone(out, "导出必须成功（返回 zip 路径）")
        self.assertTrue(out.startswith(os.path.join(self.home, "Desktop", "Skyrocket")),
                        "产物必须落在被重定向的临时 HOME 里，不许写到真桌面")
        with zipfile.ZipFile(out) as z:
            return {name: z.read(name).decode("utf-8", "replace") for name in z.namelist()}

    # ---- targets.json：四类桶 ----

    def test_every_alert_lands_in_exactly_one_bucket(self):
        body = json.loads(self._export()["targets.json"])
        self.assertEqual(body["total"], 4)
        self.assertEqual(body["pending"], 1)
        self.assertEqual(body["hit"], 1, "已命中的要单独数，否则看不出「当天该响的响过没有」")
        self.assertEqual(body["expired"], 1)
        self.assertEqual(body["invalid"], 1,
                         "非 dict 的脏数据不许静默丢掉 —— 它正是「目标价不见了」那一类")
        self.assertIn("enabled", body)

    # ---- runtime.json：看不见的保护必须有现场 ----

    def test_runtime_snapshot_carries_counters_geometry_and_face_guard(self):
        self.win._quote_fail = 3  # discipline-ok(R5): 计数器与最小化状态只能直接置：诊断包读的就是这两个内部量
        self.win._minimized = True  # discipline-ok(R5): 同上
        body = json.loads(self._export()["runtime.json"])
        self.assertEqual(body["quote_failures"], 3)
        self.assertTrue(body["minimized"])
        self.assertEqual(body["window"]["width"], self.win.width())
        self.assertEqual(body["window"]["height"], self.win.height())
        # 人脸守卫是"没在跑也看不出区别"的功能：诊断包里没有它的现场，
        # 用户报"没生效"就只能让他复现一遍。
        self.assertIn("face_guard", body)
        for key in ("available", "enabled_by_user", "camera_held", "state", "stalled"):
            self.assertIn(key, body["face_guard"],
                          "face_guard 少一个字段，远程就少一条能判的口径：%s" % key)

    # ---- storage.json：存在 / 不存在 / 读不动，三种回答要各自正确 ----

    def test_storage_answers_for_missing_and_unreadable_files(self):
        real_stat = os.stat    # 先拿真函数：桩里再调 os.stat 就是调自己（实测递归爆栈）

        def fake_stat(path, *a, **k):
            if str(path).endswith("last_quote.json"):
                raise PermissionError(13, "Operation not permitted")
            return real_stat(path)

        with mock.patch("os.stat", side_effect=fake_stat):
            body = json.loads(self._export()["storage.json"])
        self.assertTrue(body["config"]["exists"])
        self.assertGreater(body["config"]["size_bytes"], 0)
        self.assertFalse(body["config_backup"]["exists"],
                         "没有备份副本要如实说没有，别写成 error 让人以为读到了坏文件")
        self.assertIn("error", body["last_quote_cache"],
                      "读不动（权限/磁盘）必须留下 error 文字，而不是整件消失")
        self.assertNotIn(self.home, body["last_quote_cache"]["error"],
                         "诊断包是外发给别人的：家目录与账号名不许出现在错误文字里")

    def test_network_getter_failure_is_recorded_instead_of_looking_like_empty_state(self):
        with mock.patch.object(m, "get_last_http_status",
                               side_effect=RuntimeError("cannot read %s/token" % self.home)):
            files = self._export()
        network = json.loads(files["network.json"])
        self.assertEqual(network["last_http_status"], "")
        meta = files["diagnostics.txt"]
        self.assertIn("network.last_http_status", meta)
        self.assertNotIn(self.home, meta)

    def test_nonfinite_snapshot_value_becomes_strict_json_null(self):
        with mock.patch.object(m, "get_last_http_status", return_value=float("nan")):
            files = self._export()
        body = json.loads(files["network.json"],
                          parse_constant=lambda bad: (_ for _ in ()).throw(
                              ValueError("non-standard JSON constant: " + bad)))
        self.assertIsNone(body["last_http_status"])
        self.assertIn("non-finite number", files["diagnostics.txt"])

    def test_repeated_exports_are_collision_safe_and_each_archive_is_complete(self):
        clock_type = m.datetime.datetime
        frozen = clock_type.fromtimestamp(0)
        with mock.patch.object(m.datetime, "datetime", wraps=clock_type) as clock:
            clock.now.return_value = frozen  # discipline-ok(R4): now 是固定时钟替身，断言不依赖当前日期
            first = self.win._export_diagnostics()  # discipline-ok(R5): 被测对象本身就是私有入口
            second = self.win._export_diagnostics()  # discipline-ok(R5): 固定时钟，强制触发同秒重名
        self.assertNotEqual(first, second, "重复导出不许覆盖上一份诊断包")
        for path in (first, second):
            self.assertTrue(os.path.exists(path))
            with zipfile.ZipFile(path) as archive:
                self.assertIsNone(archive.testzip(), "原子发布后的 ZIP 必须完整可读")
                self.assertIn("diagnostics.txt", archive.namelist())

    def test_write_failure_leaves_no_archive_or_temporary_file(self):
        desktop = os.path.join(self.home, "Desktop", "Skyrocket")
        before = set(os.listdir(desktop))
        with mock.patch.object(zipfile.ZipFile, "writestr", side_effect=OSError("disk full")):
            result = self.win._export_diagnostics()  # discipline-ok(R5): 被测对象本身就是私有入口
        self.assertIsNone(result)
        self.assertEqual(set(os.listdir(desktop)), before)

    def test_publication_failure_preserves_an_existing_export(self):
        desktop = os.path.join(self.home, "Desktop", "Skyrocket")
        original = self.win._export_diagnostics()  # discipline-ok(R5): 被测对象本身就是私有入口
        with open(original, "rb") as stream:
            original_bytes = stream.read()
        before = set(os.listdir(desktop))
        with mock.patch.object(m.os, "link", side_effect=PermissionError("publish denied")):
            result = self.win._export_diagnostics()  # discipline-ok(R5): 被测对象本身就是私有入口
        self.assertIsNone(result)
        self.assertEqual(set(os.listdir(desktop)), before)
        with open(original, "rb") as stream:
            self.assertEqual(stream.read(), original_bytes)

    def test_runtime_and_glass_failures_are_isolated_to_the_failed_field(self):
        with mock.patch.object(self.win, "face_guard_field_evidence", side_effect=RuntimeError("guard")), \
                mock.patch.object(self.win, "glass_diagnostics", side_effect=RuntimeError("glass")):
            files = self._export()
        self.assertIsNone(json.loads(files["runtime.json"])["face_guard"])
        self.assertIn("runtime.face_guard", files["diagnostics.txt"])
        self.assertIn("glass.snapshot", files["diagnostics.txt"])
        self.assertIn("popup_space", json.loads(files["glass.json"]))

    def test_export_catalog_matches_the_real_entries_and_every_json_is_strict(self):
        files = self._export()
        catalog = json.loads(files["export.json"])
        self.assertEqual({entry["name"] for entry in catalog["files"]},
                         set(files) - {"export.json"})
        for entry in catalog["files"]:
            self.assertEqual(entry["size_bytes"], len(files[entry["name"]].encode("utf-8")))
        for name, text in files.items():
            if name.endswith(".json"):
                json.loads(text, parse_constant=lambda bad: (_ for _ in ()).throw(ValueError(bad)))
        self.assertGreaterEqual(catalog["collection_ms"], 0)

    def test_log_read_limit_keeps_the_latest_complete_lines(self):
        limit = 1024
        with open(self.log_path, "w", encoding="utf-8") as stream:
            stream.write("old log\n" * limit)
            stream.write("latest diagnostic marker\n")
        with mock.patch.object(m, "LOG_MAX_BYTES", limit):
            files = self._export()
        self.assertIn("latest diagnostic marker", files["debug.log"])
        self.assertLessEqual(len(files["debug.log"].encode("utf-8")), limit)
        self.assertIn("debug.log: source truncated", files["diagnostics.txt"])

    def test_curated_json_keys_and_error_values_use_the_same_privacy_boundary(self):
        secret = "diagnostic-secret-probe"
        data = {self.home + "/cache": "failed token=%s %s/cache" % (secret, self.home),
                "api_key": "named-secret-probe"}
        with mock.patch.object(m, "_notify_delivery_stats", return_value=data):
            files = self._export()
        self.assertNotIn(self.home, files["notify.json"])
        self.assertNotIn(secret, files["notify.json"])
        self.assertNotIn("named-secret-probe", files["notify.json"])
        self.assertIn("cache", files["notify.json"])

    def test_expanded_redaction_still_keeps_the_latest_log_line(self):
        limit = 512
        with open(self.log_path, "w", encoding="utf-8") as stream:
            stream.write("INFO token=x\n" * limit)
            stream.write("latest diagnostic marker\n")
        with mock.patch.object(m, "LOG_MAX_BYTES", limit):
            files = self._export()
        self.assertIn("latest diagnostic marker", files["debug.log"])
        self.assertLessEqual(len(files["debug.log"].encode("utf-8")), limit)
        self.assertIn("debug.log: text truncated", files["diagnostics.txt"])

    def test_corrupt_failure_summary_is_retained_as_text_not_as_invalid_json(self):
        path = os.path.join(self.tmp, "last_failures.json")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write('{"exc": "token=broken-secret"')
        with mock.patch.object(m, "last_failures_path", return_value=path):
            files = self._export()
        self.assertNotIn("last_failures.json", files)
        self.assertIn("last_failures.invalid.txt", files)
        self.assertNotIn("broken-secret", files["last_failures.invalid.txt"])
        self.assertIn("last_failures.json: invalid JSON", files["diagnostics.txt"])

    def test_primary_screen_marker_identifies_the_real_qt_primary_screen(self):
        body = json.loads(self._export()["runtime.json"])
        primary = m.QApplication.instance().primaryScreen()
        self.assertEqual([screen["name"] for screen in body["screens"] if screen["primary"]],
                         [primary.name()])

    # ---- 崩溃报告：进包、脱敏、限量 ----

    def _plant_crashes(self, count=3, age_sec=60.0):
        diag = os.path.join(self.home, "Library", "Logs", "DiagnosticReports")
        os.makedirs(diag, exist_ok=True)
        names = []
        for i in range(count):
            name = "StockFloatTargetPrice-%03d.ips" % i
            path = os.path.join(diag, name)
            body = {
                "app_name": "StockFloatTargetPrice",
                "exception": {"type": "EXC_BAD_ACCESS", "signal": "SIGSEGV",
                              "codes": "0x1"},
                "termination": {"indicator": "SIGSEGV", "namespace": "SIGNAL"},
                "legacyInfo": {"exception": "EXC_BAD_ACCESS"},
                "threads": [{"triggered": True, "id": 1, "name": "com.apple.main-thread",
                             "frames": [{"imageIndex": 0,
                                         "symbol": "-[ContentView drawRect:]"}]}],
                "usedImages": [{"index": 0, "name": "StockFloatTargetPrice",
                                "path": "%s/实时股价.app/Contents/MacOS/StockFloatTargetPrice"
                                % self.home, "base": "0x1000"}],
            }
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(body, ensure_ascii=False))
            stamp = time.time() - age_sec
            os.utime(path, (stamp, stamp))
            names.append(name)
        return names

    def test_plain_text_crash_report_leaves_the_home_path_out(self):
        """.crash 纯文本报告里"Application Specific Information"那一行会带完整的
        /Users/<账号>/... 路径 —— 抽取器保留这一行，所以家目录必须在这里就被换掉。
        """
        diag = os.path.join(self.home, "Library", "Logs", "DiagnosticReports")
        os.makedirs(diag, exist_ok=True)
        path = os.path.join(diag, "StockFloatTargetPrice-plain.crash")
        with open(path, "w", encoding="utf-8") as f:
            f.write("Exception Type:  EXC_CRASH (SIGABRT)\n")
            f.write("Application Specific Information: dyld: in %s/x.dylib\n" % self.home)
        text = "".join(v for k, v in self._export().items()
                       if k.startswith("crash_reports/"))
        self.assertIn("dyld", text, "关键行没进包，等于带了一份没正文的报告")
        self.assertNotIn(self.home, text, "家目录（含账号名）不许外发")
        self.assertIn("~", text, "换成 ~ 之后路径结构要留着，排障要看是哪个模块崩的")

    def test_recent_crash_reports_go_in_with_home_redacted(self):
        names = self._plant_crashes()
        body = self._export()
        packed = sorted(n.split("/")[-1] for n in body if n.startswith("crash_reports/"))
        self.assertTrue(packed, "崩溃报告正文必须进包：只列文件名+时间的诊断包回答不了「为什么崩」")
        self.assertLessEqual(len(packed), 2,
                             "每份上限 200KB，一次取几十份会把包撑大、也没人看得完")
        text = "".join(v for k, v in body.items() if k.startswith("crash_reports/"))
        self.assertNotIn(self.home, text,
                         "报告里带完整本机路径，外发等于把系统账号交给陌生人")
        index = body["diagnostics.txt"]
        self.assertTrue(any(n.split(".")[0] in index for n in names),
                        "清单里要能对上是哪几份，正文与索引不能各说各话")

    def test_a_crash_report_that_cannot_be_read_is_recorded_not_fatal(self):
        names = self._plant_crashes(count=1)
        path = os.path.join(self.home, "Library", "Logs", "DiagnosticReports", names[0])
        real_open = open

        def deny(f, *a, **k):
            if str(f) == path:
                raise PermissionError(13, "Operation not permitted")
            return real_open(f, *a, **k)

        with mock.patch("builtins.open", side_effect=deny):
            body = self._export()
        self.assertIn("diagnostics.txt", body, "一份读不动的报告不能让整个诊断包消失")
        self.assertIn("crash_reports/%s" % names[0], body["diagnostics.txt"],
                      "读不动要写进 export_issues：否则下一轮会以为「这台机器没崩过」")

    # ---- 快照过大 / 平台取不到：宁可少一件，不可没包 ----

    def test_an_oversized_snapshot_is_dropped_but_the_bundle_survives(self):
        real_dumps = json.dumps

        def bloated(value, *a, **k):
            text = real_dumps(value, *a, **k)
            if "monitor" in str(list(value.keys())[:3]):
                return text + ("x" * (300 * 1024))
            return text

        with mock.patch.object(m, "json", mock.Mock(dumps=bloated)):
            body = self._export()
        self.assertIn("diagnostics.txt", body)
        self.assertIn("network.json", body["diagnostics.txt"],
                      "超限那份要进缺失清单，不能悄悄丢")
        self.assertIn("snapshot exceeds 256KB", body["diagnostics.txt"])

    def test_platform_probe_failing_still_writes_the_key_meta(self):
        """`platform.platform()` 在打包后的 .app 里会真 fork 子进程问系统信息，
        完全可能失败。v1.2.95 的教训是：一个字段取不到，不能把最关键的那件
        （版本 / 数据源 / 状态码）整个带走。"""
        with mock.patch.object(m.platform, "platform", side_effect=OSError("fork failed")):
            text = self._export()["diagnostics.txt"]
        self.assertIn("APP_VERSION=", text)
        self.assertIn("system=unknown", text, "取不到要写成 unknown，而不是整件不见")

    def test_the_whole_export_failing_still_gives_the_user_a_line(self):
        out = []
        self.track_patch(mock.patch.object(m, "log_line",
                                           side_effect=lambda msg, *a: out.append(msg)))
        with mock.patch("zipfile.ZipFile", side_effect=OSError("disk gone")):
            self.assertIsNone(self.win._export_diagnostics())  # discipline-ok(R5): 被测对象本身就是私有入口
        self.assertTrue(any("diagnostics export error" in line for line in out),
                        "整条导出失败必须留一行 ERROR —— 用户点了菜单却什么都没拿到，"
                        "没有日志就是「永远查不到」那一类")


class GlobalCrashHookCase(SFCase):
    """未捕获异常的兜底：主线程、子线程、以及"钩子自己坏了"三种都要有现场。"""

    def setUp(self):
        super().setUp()
        _ensure_qapp()
        self.tmp = tempfile.mkdtemp(prefix="sf_hook_")
        self.log_path = os.path.join(self.tmp, "debug.log")
        self._originals = (sys.excepthook, threading.excepthook)
        self.track_patch(mock.patch.object(m, "LOG_PATH", self.log_path))
        m._install_global_excepthook()  # discipline-ok(R5): 被测对象本身就是私有函数

    def tearDown(self):
        sys.excepthook, threading.excepthook = self._originals
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def _logged(self):
        # 一条都还没写过时文件是不存在的 —— 读不到要当成"空日志"，
        # 否则"钩子压根没跑"会被报成 FileNotFoundError，看不出是判据没成立。
        if not os.path.exists(self.log_path):
            return ""
        with open(self.log_path, "r", encoding="utf-8") as f:
            return f.read()

    def _raise(self):
        try:
            raise ValueError("boom in slot")
        except ValueError:
            return sys.exc_info()

    def test_main_thread_crash_writes_context_and_stack(self):
        etype, value, tb = self._raise()
        sys.excepthook(etype, value, tb)
        text = self._logged()
        self.assertIn("UNCAUGHT", text)
        self.assertIn("ValueError", text)
        self.assertIn("boom in slot", text)
        self.assertIn("_raise", text, "堆栈本体必须进去：只有类型名的崩溃日志没法定位")
        self.assertIn("thread=main", text)

    def test_worker_thread_crash_uses_the_same_recorder(self):
        """子线程这一条是历史空转点：`threading.excepthook` 只传**一个**
        `ExceptHookArgs`，签名不匹配时钩子自己抛 TypeError，原始异常全丢。

        所以要**真起一条线程去崩**，而不是在主进程里手工造一个 ExceptHookArgs：
        钩子里的线程名取自 `threading.current_thread()`，主线程替它调用只会得到
        "thread=main"，测不到"崩在哪条线程"这半边判据（这条我第一版就写错了）。
        """
        def worker():
            raise RuntimeError("boom in icon worker")

        before = self._logged()
        t = threading.Thread(target=worker, name="icon worker")
        t.start()
        t.join()
        self.assertTrue(wait_for(lambda: "icon worker" in self._logged(), timeout=3.0,
                                 msg="子线程崩溃没进日志"),
                        "图标线程/编辑器线程崩了也必须进同一份日志")
        text = self._logged()
        self.assertNotEqual(text, before, "日志必须真的多了一行")
        self.assertIn("boom in icon worker", text)
        self.assertIn("thread=icon worker", text,
                      "线程名要进去，否则只知道「崩过」，不知道是哪条线程崩的")

    def test_current_stock_code_is_part_of_the_context(self):
        etype, value, tb = self._raise()
        host = type("Ref", (), {"code": "sh600000"})()
        with mock.patch.object(m, "_win_ref", host, create=True):
            sys.excepthook(etype, value, tb)
        self.assertIn("code=sh600000", self._logged(),
                      "崩在哪只票是复现的第一现场；取不到时宁可不写这一段")

    def test_a_failed_file_write_still_reaches_the_system_log(self):
        """写不进文件（磁盘满 / 权限）时，独立那条统一日志必须仍然发出去。

        为什么拦 `open` 而不是把 LOG_PATH 指到不存在的目录：钩子自己会
        `os.makedirs(..., exist_ok=True)`，那条路实测**写得进去**，
        变成测了一个没发生的失败。
        """
        etype, value, tb = self._raise()
        lines = []
        real_open = open

        def deny(path, *a, **k):
            if str(path).endswith("debug.log"):
                raise OSError(28, "No space left on device")
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", side_effect=deny), \
             mock.patch.object(m, "log_line",
                               side_effect=lambda msg, *a: lines.append(msg)):
            sys.excepthook(etype, value, tb)
        self.assertTrue(any("file log failed" in line for line in lines),
                        "落盘失败不能等于「这次崩溃没发生过」：统一日志那一条必须仍然写出去")

    def test_the_hook_never_raises_back_into_the_crashing_process(self):
        etype, value, tb = self._raise()
        with mock.patch.object(m, "log_line", side_effect=RuntimeError("logger gone")):
            try:
                sys.excepthook(etype, value, tb)
            except Exception as exc:                    # noqa: BLE001
                self.fail("钩子自己抛出来 = 崩溃现场二次丢失，还多一条脏 stderr：%r" % (exc,))


class BundleVersionCase(SFCase):
    """装机版报告里的版本号：取错一次，整份崩溃报告的归属版本就都错了。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="sf_bundle_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def _write_plist(self, mapping):
        macos = os.path.join(self.tmp, "Real.app", "Contents", "MacOS")
        os.makedirs(macos, exist_ok=True)
        with open(os.path.join(macos, "..", "Info.plist"), "wb") as f:
            plistlib.dump(mapping, f)
        return os.path.join(macos, "stub")

    def test_short_version_string_wins(self):
        exe = self._write_plist({"CFBundleShortVersionString": PLIST_SHORT,
                                 "CFBundleVersion": PLIST_BUILD})
        with mock.patch.object(sys, "executable", exe):
            self.assertEqual(_bundle_version(), PLIST_SHORT,
                             "对外版本要读 CFBundleShortVersionString，读成 build 号会让人对不上版本")

    def test_bundle_version_is_the_fallback(self):
        exe = self._write_plist({"CFBundleVersion": PLIST_BUILD})
        with mock.patch.object(sys, "executable", exe):
            self.assertEqual(_bundle_version(), PLIST_BUILD,
                             "短版本号缺失时回退 CFBundleVersion，不能报空")

    def test_development_run_falls_back_to_the_single_source_of_truth(self):
        """开发态没有 Info.plist：必须回退到 `sf_constants.APP_VERSION`（唯一来源）。"""
        exe = os.path.join(self.tmp, "MacOS", "python")
        os.makedirs(os.path.dirname(exe), exist_ok=True)
        with mock.patch.object(sys, "executable", exe):
            self.assertEqual(_bundle_version(), _DEV_VERSION)

    def test_a_broken_plist_does_not_take_the_version_down(self):
        macos = os.path.join(self.tmp, "Bad.app", "Contents", "MacOS")
        os.makedirs(macos, exist_ok=True)
        with open(os.path.join(macos, "..", "Info.plist"), "wb") as f:
            f.write(b"\x00not a plist\x00")
        with mock.patch.object(sys, "executable", os.path.join(macos, "stub")):
            self.assertEqual(_bundle_version(), _DEV_VERSION,
                             "plist 读不动也要有版本号：空的版本字段会让崩溃报告无法归版")
