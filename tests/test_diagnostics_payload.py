# -*- coding: utf-8 -*-
"""诊断包本轮新增现场的判据（40 版逐版核查之后补的那七桶）。

判据守的是什么
--------------
`outputs/诊断导出逐版核查-v1.4.27至v1.4.66-2026-10-12.md` 逐版对账的结论：过去 40 个
版本位里诊断包只长了「自动隐藏窗口」和「玻璃层」两条线，其余六份 JSON 的字段自
基线起没动过 —— 39 个实发版本里有 29 个引入了**现在远程读不出来**的状态。本轮把
七桶现场接进包里，这个文件锁的就是那些新现场，尤其锁两件事：

1. **取不到值必须留痕，不许静默缺键**。装机版里任何一个 getter 都可能抛错，如果
   异常被就地吞掉、那个文件干脆不写，"这一项没跑成"和"这一项一切正常"在包里就
   同形了 —— 台账 G11（mypy 层静默归零）/ G24（零输入当通过）同一族缺陷的第三种
   长相。故每个新现场都有一条"打断它 → 包仍出、export_issues 有名字"的反向判据。
2. **外发的包里不许出现家目录与账号名**。新增现场跨了十几个模块，其中
   `sf_universe.stats()["path"]` 就带着 `~/Library/...`；诊断包的用途正是发给别人。

埋点类判据一律**真的调用一次**被测路径（AGENTS §6 第 9/12 条、以及"字面锁抓不到
签名错配的静默零计数"那一坑）：通知计数走真 `deliver()`、分时重试走真
`_minute_loop`、渲染计数走真 `grab()` 抓帧，而不是断言字段被赋值。

不写版本号断言（§6 第 1 条）：期望值全部从 `sf_constants` / 产品唯一实现现取。
"""

import ast
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

import sf_calendar
import sf_diag
import sf_glass
import sf_constants
import sf_notify
import sf_quote
import sf_ui_render
import sf_universe
import sf_update
import stock_float_window as m
from tests._base import SFCase, wait_for
from tests.test_coverage_window_diagnostics import DiagnosticsBranchCase

_APP = None


def _ensure_app():
    """QApplication 必须由模块级引用持有，否则离屏下包装器回收时进程 abort（134）。"""
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _q(**over):
    """三态行情构造：与 test_render 同形（价格/涨跌幅是字符串，_apply_quote 依赖）。"""
    q = {"name": "贵州茅台", "code": "600519", "price": "403.50", "pct": "0.00",
         "change": "0.00", "pre_close": "403.50", "high": "403.50", "low": "403.50",
         "volume": "12345", "limit_up": "443.85", "limit_down": "363.15"}
    q.update(over)
    return q


# `residue_reason` 的"当前装机版本"入参用这个哨兵值：断言里出现产品版本号是 §6
# 第 1 条禁的东西，而这里要测的只是"版本段匹不匹配"这一件事本身。
SENTINEL_VERSION = "8.8.8"
RETRY_WINDOW_CLOSED = 0   # 快速重试窗口"关着"= 剩余次数为零（断言里别裸写 0，AGENTS §6 第 6 条）


class _LoopStop(Exception):
    """结果喂完就从循环里退出来，末态正好停在这一次转移上。"""


class _Clock:
    """按循环自己请求的睡眠量前进的虚拟时钟（AGENTS §6 第 13 条）。"""

    def __init__(self):
        self.t = 1000.0        # 起点非零：QuoteLoopSchedule.last 初值是 0
        self.stopped = False

    def monotonic(self):
        return self.t

    def sleep(self, sec):
        if self.stopped:
            return False
        self.t += float(sec)
        return True


class PayloadCase(DiagnosticsBranchCase):
    """真导一次包，逐桶验新增现场 + 两条诚实性红线。"""

    # ---- 桶 1：分时恢复现场（台账 S52）----

    def test_minute_block_reflects_a_real_write(self):
        """真走一次 `_apply_minute_points` 之后，包里必须说得出"数据是哪天的、多旧、
        在不在飞、重试窗口开没开"，而不是只有"失败过几次"。"""
        self.win._apply_minute_points([("0931", "10.0", "10.0"), ("0932", "10.1", "10.0")])  # discipline-ok(R5): 分时写入的唯一入口就是它
        body = json.loads(self._export()["runtime.json"])["minute"]
        self.assertEqual(body["points"], 2)
        self.assertFalse(body["stale"])
        self.assertEqual(body["day"], self.win._minute_day)  # discipline-ok(R5): 包里读的就是这个内部字段，没有公开口
        self.assertIsInstance(body["last_ok_age_sec"], float)
        self.assertLess(body["last_ok_age_sec"], 60.0,
                        "刚写完曲线，数据龄就该是秒级；写成分钟说明取的是别的时刻")
        self.assertEqual(body["retry_left"], 0)
        self.assertFalse(body["retry_hot"])
        self.assertIsInstance(body["interval_sec"], float)
        self.assertIn(body["source"], ("", "primary", "fallback"))

    def test_minute_stale_and_in_flight_are_visible(self):
        """「数据暂缺 · 重试中」在界面上会消失，包里必须留得住。"""
        self.win._minute_stale = True    # discipline-ok(R5): 滞后标记没有公开写入口，诊断包读的就是它
        body = json.loads(self._export()["runtime.json"])["minute"]
        self.assertTrue(body["stale"],
                        "滞后态读不出来，就等于「重试了半小时」和「刚恢复」在包里同形")

    # ---- 桶 3：身份与 OTA ----

    def test_identity_block_names_this_generation(self):
        body = json.loads(self._export()["runtime.json"])["identity"]
        self.assertEqual(body["bundle_id"], sf_constants.BUNDLE_ID)
        self.assertEqual(body["display_name"], sf_constants.APP_DISPLAY_NAME)
        self.assertIn(body["release_stage"], ("beta", "stable"))
        self.assertTrue(body["manifest_url"], "读的是哪份清单必须能远程确认")
        # 这一项**不能按本机环境断言**（AGENTS §5.5 那条 CI 教训：本机没有 bundle 身份，
        # 而 GitHub 的 macOS runner 上 `NSBundle` 给得到值 —— 写死 "" 在另一边就是
        # 无谓的红）。形状判据：要么空（源码/探针进程如实说"问不出身份"），
        # 要么必须是两代身份之一 —— 旧副本被拉起来时正是要在包里看得见这件事。
        running = body["running_bundle_id"]
        self.assertIsInstance(running, str)
        if running:
            self.assertIn(running, (sf_constants.BUNDLE_ID,)
                          + tuple(sf_constants.LEGACY_BUNDLE_IDS),
                          "读到的运行身份既不是新 id 也不是旧 id，等于没说")

    def test_diagnostics_txt_carries_stage_and_identity_lines(self):
        """sf_constants 的注释一直声称发版阶段会进诊断包，这一条把它钉成事实。"""
        meta = self._export()["diagnostics.txt"]
        self.assertIn("RELEASE_STAGE=%s" % sf_constants.RELEASE_STAGE, meta)
        self.assertIn("BUNDLE_ID=%s" % sf_constants.BUNDLE_ID, meta)
        self.assertIn("VERSION_LABEL=", meta)

    def test_update_block_exposes_inflight_and_last_result(self):
        self.win._update_downloading = True   # discipline-ok(R5): 这些标记本身就是内部状态，无公开读口
        self.win._update_transition_pending = True  # discipline-ok(R5): 这两个标记本身就是内部状态
        res = json.loads(self._export()["runtime.json"])["update"]
        self.assertTrue(res["downloading"])
        self.assertTrue(res["transition_pending"])
        self.assertIn("last_result", res)
        self.assertIn("stale_resets", res)
        self.assertIn("install_result", res)

    def test_inventory_counts_a_hidden_rollback_package(self):
        """台账 S38 原文：「诊断包里没有 /Applications 占用这一项」。"""
        apps = os.path.join(self.tmp, "Applications")
        rollback = os.path.join(apps, ".Skyrocket.app.rollback-1700000000")
        os.makedirs(os.path.join(rollback, "Contents", "MacOS"), exist_ok=True)
        with open(os.path.join(rollback, "Contents", "MacOS", "Skyrocket"), "wb") as f:
            f.write(b"x" * 4096)
        inv = sf_update.update_storage_inventory(apps_dir=apps)
        self.assertEqual(len(inv["rollback_apps"]), 1)
        self.assertGreaterEqual(inv["rollback_bytes"], 4096)
        # 普通应用目录不许被算进去
        os.makedirs(os.path.join(apps, "Something.app"), exist_ok=True)
        self.assertEqual(len(sf_update.update_storage_inventory(apps_dir=apps)["rollback_apps"]), 1)

    def test_peek_install_result_does_not_steal_the_user_notice(self):
        """consume 在 finally 里 unlink；诊断导出若复用它，用户下次启动那条提示就没了。"""
        staging = os.path.join(self.tmp, "updates")
        os.makedirs(staging, exist_ok=True)
        path = os.path.join(staging, "update-result.json")
        with mock.patch.object(sf_update, "UPDATE_STAGING_DIR", staging):
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"status": "rolled_back", "message": "签名校验未通过"}, f)
            first = sf_update.peek_install_result()
            self.assertEqual(first["status"], "rolled_back")
            self.assertTrue(os.path.exists(path), "peek 必须只读：文件还在原位")
            self.assertEqual(sf_update.peek_install_result()["status"], "rolled_back")
            self.assertIsNotNone(sf_update.consume_install_result())
            self.assertFalse(os.path.exists(path), "consume 照旧要删掉（只提示一次的语义不变）")
            self.assertIsNone(sf_update.peek_install_result())

    def test_install_result_reaches_the_package_without_deleting(self):
        staging = os.path.join(self.tmp, "updates")
        os.makedirs(staging, exist_ok=True)
        path = os.path.join(staging, "update-result.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"status": "installed", "message": "已完成"}, f)
        with mock.patch.object(sf_update, "UPDATE_STAGING_DIR", staging):
            res = json.loads(self._export()["runtime.json"])["update"]
            self.assertEqual(res["install_result"]["status"], "installed")
            self.assertTrue(os.path.exists(path))

    # ---- 桶 2：提醒到底发出去了没（台账 S34）----

    def test_notify_counts_follow_a_real_delivery(self):
        """真的走一次 deliver()，而不是给计数器赋值。"""
        before = sf_notify.delivery_stats()["counts"].get("osascript", 0)
        with mock.patch.object(sf_notify, "_deliver_impl", lambda *a, **k: "osascript"):
            self.assertEqual(sf_notify.deliver("标题", "", "正文"), "osascript")
            self.assertEqual(sf_notify.deliver("标题", "", "正文"), "osascript")
        stats = sf_notify.delivery_stats()
        self.assertEqual(stats["counts"]["osascript"], before + 2)
        self.assertEqual(stats["last_channel"], "osascript")
        self.assertGreater(stats["last_at"], 0.0)

    def test_notify_wrapper_records_an_exception_as_error(self):
        """impl 抛错：计一次 error 并把异常原样上抛 —— 记账不许改变对外行为。"""
        def _boom(*a, **k):
            raise RuntimeError("投递炸了")
        before = sf_notify.delivery_stats()["counts"].get("error", 0)
        with mock.patch.object(sf_notify, "_deliver_impl", _boom):
            with self.assertRaises(RuntimeError):
                sf_notify.deliver("标题", "", "正文")
        self.assertEqual(sf_notify.delivery_stats()["counts"]["error"], before + 1)

    def test_notify_json_reports_no_bundle_instead_of_a_lie(self):
        """源码态问 UN 会抛 ObjC 异常直接 abort；包里必须写 "no_bundle"。"""
        with mock.patch.object(sf_notify, "has_bundle_identity", lambda: False):
            self.assertEqual(sf_notify.delivery_stats()["authorization"], "no_bundle")
        body = json.loads(self._export()["notify.json"])
        self.assertIn("counts", body)
        self.assertIn("degraded_osascript", body)
        self.assertIn("un_available", body)

    # ---- 桶 4：证券字典 ----

    def test_universe_rejects_counted_and_path_anonymised(self):
        before = sf_universe.stats()["rejects"].get("dict_no_match", 0)
        sf_universe.note_reject("dict_no_match")
        self.assertEqual(sf_universe.stats()["rejects"]["dict_no_match"], before + 1)
        # 把字典缓存路径**种到家目录里**再导一次：真机上它就是 `~/Library/...`，
        # 而测试环境默认把它重定向到系统临时目录 —— 不种这一枚，脱敏判据永远
        # 断言不到自己（实测：摘掉脱敏那行，不种路径的用例照样绿）。
        planted = os.path.join(self.home, "Library", "Application Support",
                               "StockFloatTargetPrice", "securities.json")
        with mock.patch.object(sf_universe, "UNIVERSE_PATH", planted):
            body = json.loads(self._export()["universe.json"])
        self.assertGreaterEqual(body["rejects"]["dict_no_match"], before + 1)
        self.assertIn("index_ready", body)
        self.assertIn("stale", body)
        self.assertNotIn(self.home, json.dumps(body, ensure_ascii=False),
                         "字典缓存路径含家目录（里面有账号名），外发前必须换成 ~")
        self.assertTrue(body["path"].startswith("~/"),
                        "脱敏要留下目录结构（~ 前缀），整条抹掉等于把排障信息也删了")
        self.assertIn("securities.json", body["path"])

    # ---- 桶 5 / 6 / 7：渲染降级、几何、守卫剩下的三小块 ----

    def test_render_block_reports_fonts_and_paint_endstate(self):
        body = json.loads(self._export()["runtime.json"])["render"]
        self.assertIn("fonts", body)
        self.assertIn("bundled", body["fonts"])
        self.assertIn("resolved", body["fonts"])
        for key in ("badge_degraded_drawn", "badge_degraded_blocked",
                    "bottom_shrink", "bottom_dropped"):
            self.assertIn(key, body["paint"])

    def test_gesture_tools_and_screens_blocks_shape(self):
        body = json.loads(self._export()["runtime.json"])
        self.assertIn("sys_drag", body["gesture"])
        self.assertIn("resizing", body["gesture"])
        for key in ("panel", "editor"):
            self.assertIn(key, body["tools"])
            self.assertIn("present", body["tools"][key])
        self.assertIsInstance(body["screens"], list)
        self.assertGreaterEqual(len(body["screens"]), 1,
                                "一块屏幕都没有 = 这块判据自己失效了，不是被测对象正常")
        one = body["screens"][0]
        for key in ("name", "geometry", "available", "primary"):
            self.assertIn(key, one)
        self.assertEqual(len(one["geometry"]), 4)
        self.assertIsInstance(body["snap_margin"], int)
        self.assertEqual(body["snap_margin"], m.SNAP_MARGIN)

    def test_glass_json_carries_follow_lag_and_force_top_flag(self):
        body = json.loads(self._export()["glass.json"])
        self.assertIsInstance(body["follow_lag_px"], float)
        self.assertIsInstance(body["force_top_forced"], bool)
        # 空窗时长（1.4.33 那条"这次空窗几秒"）：开着的场次 + 最近几场各自的时长
        self.assertIn("gap", body)
        self.assertIn("open", body["gap"])
        self.assertIsInstance(body["gap"]["last"], list)

    def test_storage_updates_carries_the_prune_endstate(self):
        """清理末态跟着包走：只在日志里说不算告知（AGENTS §5.2 那条口径）。"""
        report = {"at": 1.0, "moved": 2, "moved_bytes": 4096,
                  "held_back": [{"name": "Skyrocket-8.8.8.zip", "why": "current-version"}],
                  "errors": []}
        with mock.patch.object(sf_update, "staging_prune_report", lambda: dict(report)):
            body = json.loads(self._export()["storage.json"])["updates"]
        self.assertEqual(body["prune"]["moved"], 2)
        self.assertEqual(body["prune"]["moved_bytes"], 4096)
        self.assertEqual(body["prune"]["held_back"][0]["why"], "current-version",
                         "为什么还占着空间，也要能从包里读出来")

    def test_face_guard_new_evidence_keys(self):
        fg = json.loads(self._export()["runtime.json"])["face_guard"]
        for key in ("camera_access", "guard_timer_active", "last_stop", "applied_profile",
                    "hide_streak", "restore_streak", "two_since_age", "one_since_age",
                    "fed_age"):
            self.assertIn(key, fg, "少一个键，远程就少一条能判的口径：%s" % key)
        self.assertIn(fg["camera_access"],
                      ("authorized", "denied", "restricted", "undetermined",
                       "unsupported", "disabled", "no_bundle")
                      + tuple(x for x in () if x.startswith("unknown")))

    def test_guard_timer_active_follows_the_real_timer(self):
        """AGENTS §5.2 要求"凡是停掉一张回调表、又指望它自己醒的写法，必须有一条
        断言那张表活性的判据"—— 装机版远程读不到就等于判据只在测试里活着。"""
        timer = self.win._guard_timer    # discipline-ok(R5): 被测对象本身就是这张 QTimer
        timer.stop()
        self.assertFalse(json.loads(
            self._export()["runtime.json"])["face_guard"]["guard_timer_active"])
        timer.start(50)
        try:
            self.assertTrue(json.loads(
                self._export()["runtime.json"])["face_guard"]["guard_timer_active"],
                            "表在跑而包里说没跑，比没这条键更糟")
        finally:
            timer.stop()

    def test_stop_reason_is_the_only_answer_for_why_it_went_off(self):
        self.win._stop_face_guard(reason="stalled", wait=True)  # discipline-ok(R5): 被测对象就是这次停表
        rec = self.win._face_guard_stop_evidence()  # discipline-ok(R5): 判据是"停表末态"这份诊断，公开输出只有 zip
        self.assertEqual(rec["reason"], "stalled")
        self.assertGreaterEqual(rec["age_sec"], 0.0)

    def test_features_snapshot_reports_the_real_drag_mode(self):
        """1.4.37 那一族：拖拽路线此前只在 STARTUP 行的日志里，会被轮转挤掉。"""
        body = json.loads(self._export()["runtime.json"])["features"]
        self.assertIsInstance(body, dict)
        self.assertIn("system_drag", body)
        self.assertIsInstance(body["system_drag"], bool)

    def test_calendar_snapshot_shape(self):
        with mock.patch.object(m, "calendar_sync_status",
                          lambda yms=None: {"2026-11": "degraded", "2026-12": "ready"}):
            body = json.loads(self._export()["calendar.json"])
        self.assertEqual(body["2026-11"], "degraded")

    def test_targets_codes_histogram_shows_ownership(self):
        with mock.patch.object(m, "load_alerts", lambda code=None: [
                {"code": "sh600000"}, {"code": "sh600000"}, {"code": "sz000001"},
                {"no_code": True}, "脏数据"]):
            body = json.loads(self._export()["targets.json"])
        self.assertEqual(body["codes"]["sh600000"], 2)
        self.assertEqual(body["codes"]["sz000001"], 1)
        self.assertEqual(body["codes"][""], 1, "无主条目要能看出来，它归顶层股票")
        self.assertEqual(body["codes"]["?"], 1, "非 dict 的脏数据单独计数")

    # ---- 诚实性：取不到值不许静默 ----

    def test_a_broken_getter_leaves_a_named_line_and_still_ships_the_package(self):
        with mock.patch.object(m, "_notify_delivery_stats",
                          side_effect=RuntimeError("UN 现场取不到")):
            files = self._export()
        self.assertNotIn("notify.json", files, "取不到的那一份不该硬写一个空壳进包")
        self.assertIn("notify.json", files["diagnostics.txt"].split("export_issues:")[-1],
                      "缺件必须在 export_issues 里点得名")

    def test_a_broken_field_is_named_not_dropped(self):
        """同一个 JSON 里的字段取不到 → 留 default 占位 + 点名，两种失败不许同形。"""
        with mock.patch.object(m.FloatWindow, "_tool_window_geometries",
                          side_effect=RuntimeError("工具窗读不动")):
            files = self._export()
        body = json.loads(files["runtime.json"])
        self.assertIsNone(body["tools"])
        tail = files["diagnostics.txt"].split("export_issues:")[-1]
        self.assertIn("runtime.tools", tail)

    def test_broken_inventory_is_named_in_storage_issues(self):
        with mock.patch.object(m, "_update_inventory", side_effect=PermissionError(13, "不给了")):
            files = self._export()
        self.assertEqual(json.loads(files["storage.json"])["updates"], {})
        self.assertIn("storage.updates", files["diagnostics.txt"].split("export_issues:")[-1])

    def test_no_home_directory_anywhere_in_the_package(self):
        """整包扫描：新接进来的十几个现场里，任何一处漏脱敏都要在这里红。"""
        for name, text in self._export().items():
            if name.startswith("crash_reports/"):
                continue
            self.assertNotIn(self.home, text, "%s 里出现了家目录" % name)

    def _assert_archive_canaries_are_scrubbed(self, files, canaries):
        """统一检查 ZIP 成员名与正文，避免只测某个 JSON 桶而漏掉日志/附件。"""
        self.assertIn("diagnostics.txt", files)
        self.assertIn("runtime.json", files)
        self.assertIn("hello 诊断", files["debug.log"],
                      "非敏感日志证据也必须保留，不能把整份日志删掉蒙混")
        self.assertIn("RELEASE_STAGE=", files["diagnostics.txt"])
        self.assertIn("diagnostic-safe", files["notify.json"])
        for canary in canaries:
            for name, text in files.items():
                self.assertNotIn(canary, name, "%s 的 ZIP 成员名泄露金丝雀" % name)
                self.assertNotIn(canary, text, "%s 的 ZIP 正文泄露金丝雀" % name)

    def test_real_zip_scrubs_runtime_config_log_and_identity_canaries(self):
        """真实导出后逐个扫描所有文本成员：路径、用户名、token、邮箱和 cookie 都不能出包。"""
        username = "diag_canary_user_7f3"
        home = self.home
        token = "diag-token-7f3"
        email = "diag-email-7f3@example.invalid"
        cookie = "diag-cookie-7f3"
        with open(self.cfg_path, "w", encoding="utf-8") as stream:
            json.dump({"code": "sh600519", "api_urls": {"token": token},
                       "email": email, "cookie": cookie,
                       "safe_label": "diagnostic-safe"}, stream)
        with open(self.log_path, "w", encoding="utf-8") as stream:
            stream.write("[10:00:00] INFO: hello 诊断\n")
            stream.write("user=%s path=%s token=%s cookie=%s\n" %
                         (username, home, token, cookie))
        stats = {"counts": {"safe": 1}, "diagnostic_safe": "diagnostic-safe",
                 "path": os.path.join(home, "Library", "state.json"),
                 "token": token, "email": email, "cookie": cookie}
        with mock.patch.dict(os.environ, {"USER": username, "USERNAME": username}), \
             mock.patch.object(m, "_notify_delivery_stats", return_value=stats):
            files = self._export()
        self._assert_archive_canaries_are_scrubbed(
            files, (username, home, token, email, cookie))
        self.assertIn("diagnostic-safe", files["notify.json"])

    def test_archive_scan_catches_a_non_persisted_scrub_mutation(self):
        """负向变异：若非落盘文本脱敏被删，统一 ZIP 判据必须立即失败。"""
        username = "diag_mutation_user_9c2"
        token = "diag-mutation-token-9c2"
        with open(self.log_path, "w", encoding="utf-8") as stream:
            stream.write("hello 诊断 user=%s token=%s\n" % (username, token))
        with mock.patch.dict(os.environ, {"USER": username, "USERNAME": username}), \
             mock.patch.object(sf_diag, "scrub_text", side_effect=lambda text, needles: str(text)), \
             mock.patch.object(m, "_notify_delivery_stats", return_value={
                 "diagnostic_safe": "diagnostic-safe", "token": token}):
            files = self._export()
        with self.assertRaises(AssertionError):
            self._assert_archive_canaries_are_scrubbed(files, (username, token))

    # ---- 渲染计数：真画一帧才算数 ----

    def _paint(self, quote):
        """真渲染一帧：停牌判定只在交易时段生效（sf_quote.quote_status 里那条）。"""
        app = _ensure_app()
        with mock.patch.object(sf_quote, "_is_trading_time", return_value=True):
            self.win._apply_quote(quote)  # discipline-ok(R5): 注入行情的唯一真实入口
            self.win._need_redraw = True  # discipline-ok(R5): 重绘标记是内部脏位，没有公开 setter
            self.win.update()
            app.processEvents()
            self.win.grab()
            app.processEvents()

    def test_badge_counter_moves_only_when_the_degraded_badge_is_really_painted(self):
        """台账 S49：降级态「停牌」徽标那条支路死了 20+ 个版本而零痕迹。
        计数器必须由真渲染推动，且"要画但被几何挡下"另记一个数。"""
        # 计数是模块级末态，同进程里别的用例早就画过若干帧 —— 先按零，判据看的是
        # "真渲染一帧之后产品自己把它推上去"，不是相对大小。
        self._paint(_q(price="0", pct="0.00", change="0.00", volume="0"))
        self.assertGreaterEqual(sf_ui_render.paint_diagnostics()["badge_degraded_drawn"], 1,
                                "画了停牌徽标而计数没动 —— 这条埋点等于没做")
        self.assertEqual(sf_ui_render.paint_diagnostics()["badge_degraded_blocked"], 0,
                         "200px 宽度下放得下，不该同时记一次「被几何挡下」")

    def test_bottom_shrink_counter_records_the_real_step(self):
        """S39：宽不够时缩字号、再不够就丢段。两个数都必须来自真那一帧。"""
        # 哨兵必须是 None：真实档位是**负数**（9pt→8pt 记 -1），拿 -1 当"还没画过"
        # 会和真值撞车 —— 这条先按零，再让真那一帧去覆盖它。
        sf_ui_render._PAINT_DIAG["bottom_shrink"] = None     # discipline-ok(R5): 前置归零，判据看产品写入
        sf_ui_render._PAINT_DIAG["bottom_dropped"] = None    # discipline-ok(R5): 同上
        self._paint(_q(high="16888.88", low="16666.66", price="16777.77",
                       pct="0.00", change="0.00"))
        diag = sf_ui_render.paint_diagnostics()
        self.assertIsNotNone(diag["bottom_shrink"], "画过一帧而末态还是 None，说明没接线")
        self.assertLess(diag["bottom_shrink"], 0,
                        "8 个字符的高/低在窗口宽度里放不下，必须落下缩档档位（S39 的现场）")
        self.assertGreaterEqual(diag["bottom_dropped"], 0)


class LoopInstrumentCase(SFCase):
    """循环里那两处"只有跑起来才存在"的留痕：分时重试窗口、玻璃层跟随掉队。"""

    def setUp(self):
        super().setUp()
        _ensure_app()
        self.tmp = tempfile.mkdtemp(prefix="sf_diag_loop_")
        self.track_patch(mock.patch.object(m, "load_config", return_value=(
            "sh600519", 100, 100, 200, "translucent")))
        self.track_patch(mock.patch.object(m, "save_config", lambda *a, **k: True))
        self.track_patch(mock.patch.object(sf_quote, "http_get", lambda *a, **k: ""))
        self.track_patch(mock.patch.object(m, "start_icon_worker", lambda: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_force_top", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_setup_reopen_handler",
                                           lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_quote_loop_wrapper",
                                            lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_minute_loop_wrapper",
                                            lambda self: None))
        self.win = m.FloatWindow()
        self.win.running = False
        self.win._wake.set()   # discipline-ok(R5): 唤醒闸只有这个内部事件可置，没有公开开关
        for name in ("timer", "top_timer", "_anim_timer", "_flash_timer"):
            t = getattr(self.win, name, None)
            if t is not None:
                t.stop()

    def tearDown(self):
        self.win._exit_persisted = True   # discipline-ok(R5): §6 第 11 条，关窗要在 patch 还活着时
        try:
            self.win.close()
        except Exception:
            pass
        super().tearDown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _drive_minute_loop(self, results, base_interval=1.0):
        clk = _Clock()
        state = {"n": 0}

        def fake_fetch():
            idx = state["n"]
            state["n"] += 1
            if idx >= len(results):
                # 结果取完就从循环里抛出去：循环末态必须正好是**最后一次给定结果**
                # 造成的那次转移，多喂一拍就会把它改掉（本仓 §6 第 13 条的坑）
                raise _LoopStop()
            return results[idx]

        self.win._minute_interval = base_interval   # discipline-ok(R5): 常规周期没有 setter
        with mock.patch.object(m.time, "monotonic", side_effect=lambda: clk.monotonic()), \
             mock.patch.object(m, "seconds_to_session_change", return_value=0.0), \
             mock.patch.object(self.win, "_is_trading_time", return_value=True), \
             mock.patch.object(self.win, "_take_force", lambda name: False), \
             mock.patch.object(self.win, "_fetch_minute", side_effect=fake_fetch), \
             mock.patch.object(self.win, "_sleep", side_effect=lambda s: clk.sleep(s)):
            self.win.running = True
            try:
                self.win._minute_loop()   # discipline-ok(R5): 被测对象本身就是这条常驻循环
            except _LoopStop:
                pass
            finally:
                self.win.running = False
        return state["n"]

    def test_retry_window_state_is_readable_from_the_instance(self):
        """连打若干次失败之后，重试窗口的剩余次数与 hot 位必须能在实例上读到。

        这两个量此前只是 `_minute_loop` 的局部变量 —— 于是"停在重试中"这件事
        在诊断包里根本不存在（台账 S52 的取证缺口）。
        """
        self.win._minute_retry_left = 0        # discipline-ok(R5): 归零是前置，判据看循环自己写完
        self.win._minute_retry_hot = False    # discipline-ok(R5): 同上
        self._drive_minute_loop([False] * 10)
        self.assertTrue(self.win._minute_retry_hot,     # discipline-ok(R5): 判据对象就是这条留痕本身
                        "失败 10 次之后 hot 位没落下，说明循环没写这个字段")
        self.assertEqual(self.win._minute_retry_left,        # discipline-ok(R5): 这条留痕本身没有公开读口
                         RETRY_WINDOW_CLOSED,
                         "窗口耗尽之后该回落常规周期，不该还挂着剩余次数")

    def test_the_window_counts_down_one_per_failed_fetch(self):
        """剩余次数必须由循环自己数出来：三次连续失败 = 窗口用掉三格。"""
        self._drive_minute_loop([False, False, False])
        # 第 1 拍失败：开窗，retry_left = MINUTE_RETRY_MAX
        # 第 2、3 拍失败：各扣一格 → 上限 - 2
        self.assertEqual(self.win._minute_retry_left,        # discipline-ok(R5): 判据对象就是这条留痕
                         m.MINUTE_RETRY_MAX - 2,
                         "快速重试窗口没按 MINUTE_RETRY_MAX 起算、或每拍没扣一次")
        self.assertTrue(self.win._minute_retry_hot)  # discipline-ok(R5): 判据对象就是这条留痕

    def test_a_success_clears_the_retry_window(self):
        self._drive_minute_loop([False, False, True])
        self.assertEqual(self.win._minute_retry_left,        # discipline-ok(R5): 同上，判据对象就是这条留痕
                         RETRY_WINDOW_CLOSED,
                         "恢复之后还挂着「还在重试」，包里就会一直说在重试 —— "
                         "而这正是用户唯一看不见分时的那一刻")
        self.assertFalse(self.win._minute_retry_hot)  # discipline-ok(R5): 同上

    def test_follow_tick_accumulates_the_lag_px(self):
        """掉队幅度必须由真实的"这一拍比上一拍跑了多远"算出来。"""
        frames = [(0, 0, 184, 100), (30, 0, 184, 100), (75, 0, 184, 100)]
        calls = {"i": 0}

        class _Glass:
            def setFrame_display_(self, rect, display):
                pass

        self.win._glass_window = _Glass()      # discipline-ok(R5): 跟随逻辑只认这个句柄在不在
        self.win._glass_follow_lag_px = 0.0    # discipline-ok(R5): 前置归零，判据看真算出来的值
        with mock.patch.object(self.win, "_content_real_bounds",
                               side_effect=lambda *a, **k: frames[min(calls["i"], len(frames) - 1)]
                               if (calls.__setitem__("i", calls["i"] + 1) or True) else None):
            for _ in range(3):
                self.win._glass_follow_tick()   # discipline-ok(R5): 被测对象本身就是这一拍
        self.assertAlmostEqual(self.win._glass_follow_lag_px,  # discipline-ok(R5): 掉队最大值就是这条埋点本身
                               45.0, places=3,
                               msg="最大掉队应是第三拍那 45px，不是累计和也不是首拍")

    def test_follow_begin_resets_the_lag(self):
        self.win._glass_follow_lag_px = 42.0   # discipline-ok(R5): 判据就是"新会话起点归零"
        with mock.patch.object(self.win, "_glass_follow_end", lambda: None), \
             mock.patch.dict("sys.modules", {"CoreFoundation": None}):
            self.win._glass_follow_begin()     # discipline-ok(R5): 被测对象本身就是这次跟随
        self.assertEqual(self.win._glass_follow_lag_px, 0.0)  # discipline-ok(R5): 同上


class QuoteSourceAndRejectCase(SFCase):
    """两个"最后一次是谁"的留痕：分时主备源、编辑器拒绝原因。"""

    def test_minute_source_records_which_endpoint_answered(self):
        with mock.patch.object(sf_quote, "_fetch_minute_primary",
                               return_value=[("0931", "10.0", "10.0")]), \
             mock.patch.object(sf_quote, "_is_trading_time", lambda *a: True):
            sf_quote.fetch_minute("sh600000")
        self.assertEqual(sf_quote.get_last_minute_source(), "primary")

        def _primary_booms(code):
            raise sf_quote.QuoteDataError("主源断了")
        with mock.patch.object(sf_quote, "_fetch_minute_primary", _primary_booms), \
             mock.patch.object(sf_quote, "fetch_minute_fallback",
                               return_value=[("0931", "10.0", "10.0")]), \
             mock.patch.object(sf_quote, "_is_trading_time", lambda *a: True):
            sf_quote.fetch_minute("sh600000")
        self.assertEqual(sf_quote.get_last_minute_source(), "fallback")

    def test_editor_reject_reasons_are_counted_separately(self):
        """三种拒绝在界面上是同一行字，在包里必须是三个数（否则又回到"只能复现"）。"""
        before = dict(sf_universe.stats()["rejects"])
        for reason in ("dict_no_match", "probe_says_missing", "probe_empty_name"):
            sf_universe.note_reject(reason)
        rejects = sf_universe.stats()["rejects"]
        for reason in ("dict_no_match", "probe_says_missing", "probe_empty_name"):
            self.assertEqual(rejects[reason], before.get(reason, 0) + 1, reason)

class StagingResidueCase(SFCase):
    """更新 staging 残留：判据是一张表，清理只 mv 不删。

    起因是诊断包第一次真跑就读出本机 updates/ 里 80,382,786 字节残留，其中一份
    26MB 的 `.part-*` 是中断下载的孤儿 —— v1.4.59 的剪枝只管「应用程序」里的回退包。
    """

    OLD = 7200.0          # 距今两小时：过了年龄门槛
    FRESH = 30.0          # 距今 30 秒：可能正在写，一个都不许碰

    def test_every_residue_shape_is_named(self):
        for name, why in (
                ("Skyrocket-8.8.8.zip.part", "part"),
                ("StockFloatTargetPrice-1.3.79.zip.part-20559-1789543250595644000",
                 "legacy-part"),
                ("Skyrocket-8.8.8.zip.resume.json", "resume"),
                ("Skyrocket-8.8.8.zip.resume.json.tmp-993-1789543", "state-tmp"),
                ("StockFloatTargetPrice-1.3.78.zip", "old-zip"),
                ("Skyrocket-8.8.7.zip", "old-zip")):
            self.assertEqual(sf_update.residue_reason(name, self.OLD, SENTINEL_VERSION), why,
                             "%s 该被认成 %s" % (name, why))

    def test_nothing_current_or_fresh_is_touched(self):
        self.assertIsNone(sf_update.residue_reason("Skyrocket-%s.zip" % SENTINEL_VERSION,
                                                   self.OLD, SENTINEL_VERSION),
                          "当前版本的完整包是「装失败还能手动重装」的唯一凭据，不许挪")
        for name in ("Skyrocket-8.8.8.zip.part", "Skyrocket-8.8.8.zip.resume.json"):
            self.assertIsNone(sf_update.residue_reason(name, self.FRESH, SENTINEL_VERSION),
                              "%s 还在年龄门槛内就被挪走 = 掐断这一次下载" % name)
        for name in ("update-result.json", "update_helper-1789543250.sh", "readme.txt"):
            self.assertIsNone(sf_update.residue_reason(name, 10 ** 7, SENTINEL_VERSION),
                              "%s 不在清理名单里：认不出来源的一个都不许碰" % name)

    def test_prune_moves_files_and_leaves_the_bytes_on_disk(self):
        tmp = tempfile.mkdtemp(prefix="sf_staging_")
        staging = os.path.join(tmp, "updates")
        dest = os.path.join(tmp, "reclaim")
        os.makedirs(staging)
        names = ("Skyrocket-8.8.8.zip.part",
                 "StockFloatTargetPrice-1.3.79.zip.part-20559-1",
                 "Skyrocket-8.8.8.zip.resume.json",
                 "StockFloatTargetPrice-1.3.78.zip",
                 "Skyrocket-8.8.8.zip",
                 "update-result.json")
        for name in names:
            path = os.path.join(staging, name)
            with open(path, "wb") as f:
                f.write(b"x" * 1024)
            old = time.time() - 7200
            os.utime(path, (old, old))
        with mock.patch.object(sf_update, "UPDATE_STAGING_DIR", staging):
            report = sf_update.prune_staging_residue(installed_version=SENTINEL_VERSION,
                                                     dest_root=dest)
        self.assertEqual(len(report["moved"]), 4, report)
        self.assertEqual(report["moved_bytes"], 4 * 1024)
        self.assertEqual(report["errors"], [])
        self.assertTrue(os.path.exists(os.path.join(staging, "Skyrocket-8.8.8.zip")))
        self.assertTrue(os.path.exists(os.path.join(staging, "update-result.json")))
        # **一个文件都没被删**：挪走的每一份都还在回收目录里，字节数一模一样
        for item in report["moved"]:
            landed = os.path.join(dest, item["name"])
            self.assertTrue(os.path.exists(landed), "%s 不见了" % item["name"])
            self.assertEqual(os.path.getsize(landed), item["size"])
        shutil.rmtree(tmp, ignore_errors=True)

    def test_prune_leaves_a_report_for_the_diagnostics_package(self):
        tmp = tempfile.mkdtemp(prefix="sf_staging2_")
        staging = os.path.join(tmp, "updates")
        os.makedirs(staging)
        path = os.path.join(staging, "Skyrocket-8.8.8.zip.part")
        with open(path, "wb") as f:
            f.write(b"y" * 2048)
        old = time.time() - 7200
        os.utime(path, (old, old))
        with mock.patch.object(sf_update, "UPDATE_STAGING_DIR", staging):
            self.assertEqual(sf_update.staging_prune_report(), {},
                             "这个进程还没扫过，报告必须是空 —— 空与「没残留」是两件事")
            sf_update.prune_staging_residue(installed_version=SENTINEL_VERSION,
                                            dest_root=os.path.join(tmp, "reclaim"))
            report = sf_update.staging_prune_report()
        self.assertEqual(report["moved"], 1)
        self.assertEqual(report["moved_bytes"], 2048)
        shutil.rmtree(tmp, ignore_errors=True)


class GapEpisodeCase(SFCase):
    """空窗持续时长：从第一条失效留痕起表，到"这一拍真的正常"结表。"""

    def _host(self):
        class _Host(sf_glass.GlassMixin):
            pass
        return _Host()

    def test_the_first_retop_log_opens_the_clock(self):
        host = self._host()
        host._log_retop("glass above main", " desync=42")  # discipline-ok(R5): 被测对象本身就是这条计时器
        opened = host._gap_evidence()["open"]  # discipline-ok(R5): 判据对象就是这份内部计时现场
        self.assertIsNotNone(opened, "留痕了却没起表，恢复时就量不出时长")
        self.assertEqual(opened["reason"], "glass above main")

    def test_a_repeated_log_of_the_same_fault_does_not_restart_the_clock(self):
        """同一种失效连打两次，起点必须是第一次那一刻 —— 否则每 5 秒压一条日志
        就会把一场持续 30 秒的空窗说成"刚刚发生"。"""
        host = self._host()
        host._log_retop("glass above main")  # discipline-ok(R5): 被测对象本身就是这条计时器
        since = host._gap_open["since"]  # discipline-ok(R5): 起点时刻只有内部那一位
        host._log_retop("glass above main")  # discipline-ok(R5): 同上
        self.assertEqual(host._gap_open["since"], since)  # discipline-ok(R5): 同上

    def test_two_windows_never_share_one_episode_table(self):
        """两个窗口各记各的空窗场次。

        类层默认值写成 `[]` 就会踩这一条（本轮真的差一点写进去）：第二只窗口的场次会
        接在第一只后面，而诊断包读的是"这一只窗口"。
        """
        a, b = self._host(), self._host()
        a._log_retop("glass above main")  # discipline-ok(R5): 被测对象本身就是这条计时器
        a._note_gap_closed()  # discipline-ok(R5): 同上
        self.assertEqual(len(a._gap_evidence()["last"]), 1)  # discipline-ok(R5): 同上
        self.assertEqual(b._gap_evidence()["last"], [],  # discipline-ok(R5): 同上
                         "两张表共用了一张 —— 类层放了可变默认值")
        b._log_retop("order probe blind")  # discipline-ok(R5): 被测对象本身就是这条计时器
        b._note_gap_closed()  # discipline-ok(R5): 同上
        self.assertEqual([r["reason"] for r in a._gap_evidence()["last"]],  # discipline-ok(R5): 同上
                         ["glass above main"], "反向也一样：b 的场次不许进 a")

    def test_closing_writes_a_duration_and_keeps_the_last_three(self):
        # 四种**不同**原因：`_log_retop` 按原因分槽压重，同名第二次会被压掉而不是一场
        # 新故障 —— 用同名原因造场次，测的会是压重逻辑而不是这里的计数。
        host = self._host()
        reasons = ("glass above main", "order probe blind", "active space changed",
                   "desync over tolerance")
        for reason in reasons:
            host._log_retop(reason)  # discipline-ok(R5): 被测对象本身就是这条计时器
            host._note_gap_closed()  # discipline-ok(R5): 同上
        self.assertIsNone(host._gap_evidence()["open"])  # discipline-ok(R5): 同上
        last = host._gap_evidence()["last"]  # discipline-ok(R5): 同上
        self.assertEqual(len(last), 3, "只留最近 3 场，第 4 场要把最旧那条挤掉")
        self.assertEqual([rec["reason"] for rec in last], list(reasons[1:][::-1]),
                         "留下的必须是最近三场，且最新的在最前")
        self.assertNotIn(reasons[0], [rec["reason"] for rec in last],
                         "最旧那场没被挤掉 —— 列表会随运行时长无限增长")
        for rec in last:
            self.assertGreaterEqual(rec["duration_sec"], 0.0)

    def test_the_healthy_branch_of_force_top_is_what_closes_the_clock(self):
        """结表必须发生在 `_force_top` 那条"这一拍体检全过"的早退路上。

        上面几条只证明记录器本身对；这条把真链路跑一遍 —— 否则"记了时长但没人结表"
        会让每一场空窗都显示成"还在持续"，比根本没有这个数更误导。
        """
        host = self._host()
        host._force_top_forced = False  # discipline-ok(R5): 要驱动的就是这条内部开关
        host._top_state_ok = lambda window, gw: True  # discipline-ok(R5): 稳态体检替身
        host._active_space_changed = lambda: False  # discipline-ok(R5): Space 没变
        host._steady_state_fault = lambda window, gw: None  # discipline-ok(R5): 顺序与错位都正常
        host._log_retop("glass above main")  # discipline-ok(R5): 被测对象本身就是这条计时器
        with mock.patch.object(sf_glass, "content_ns_window", lambda self: object()), \
             mock.patch.object(sf_glass, "native_cocoa_active", lambda: True):
            sf_glass.GlassMixin._force_top(host)  # discipline-ok(R5): 被测对象就是这一拍
        self.assertIsNone(host._gap_evidence()["open"],  # discipline-ok(R5): 同上
                          "体检全过却没结表：这场空窗会被一直说成还在发生")
        self.assertEqual(len(host._gap_evidence()["last"]), 1)  # discipline-ok(R5): 同上

    def test_a_healthy_tick_with_no_open_episode_records_nothing(self):
        """没起表就不许凭空造出一场「恢复」 —— 那会把正常的每一拍都写成一次故障。"""
        host = self._host()
        host._note_gap_closed()  # discipline-ok(R5): 被测对象本身就是结表动作
        self.assertEqual(host._gap_evidence()["last"], [])  # discipline-ok(R5): 同上


class HintClearanceCase(SFCase):
    """滞后提示与白色虚线的净空：选位与留痕必须共用同一个实现。"""

    def test_the_position_picker_uses_the_shared_clearance_function(self):
        """选位函数必须**调用**那个共用的净空实现，而不是自己再抄一份公式。

        为什么用 AST 而不是行为断言（§6 第 2 条要求写清理由）：两处各抄一份公式时，
        行为在抄的那一瞬间仍然完全一致 —— 没有任何可调用判据能看出差别，能看到的只有
        "下一次只改一处"。本轮实测正是如此：把公式抄回 `stale_hint_y` 里，
        当时全部判据照旧绿。所以锁的是"只有一个实现"这个形状本身。
        """
        tree = ast.parse(pathlib.Path(sf_ui_render.__file__).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "stale_hint_y")
        called = {node.func.id for node in ast.walk(fn)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertIn("hint_line_gap_px", called,
                      "净空公式被抄回选位函数里了：两份定义会漂移，而留痕与选位从此无关")

    def test_missing_line_is_counted_separately_from_a_tight_gap(self):
        """虚线取不到时退回贴顶，那不能记成「压线」，也不能记成「净空 0」。"""
        self.assertEqual(sf_ui_render.hint_line_gap_px(100.0, 20.0, None), float("inf"))

    def test_pure_fn_agrees_with_the_position_picker(self):
        """`stale_hint_y` 选出来的位置，净空必须由同一个函数算得出非负值。

        判据不写死数字：拿"下跌日虚线贴在图带顶"那一族几何（v1.4.34 的事故现场）逐档试。
        """
        for rect_top, rect_bottom, line_y in ((300.0, 380.0, 302.0), (300.0, 380.0, 340.0),
                                              (300.0, 380.0, 376.0)):
            hint_h = 20.0
            y = sf_ui_render.stale_hint_y(rect_top, rect_bottom, hint_h, line_y)
            self.assertGreaterEqual(sf_ui_render.hint_line_gap_px(y, hint_h, line_y), 0.0)


class PaintEvidenceCase(DiagnosticsBranchCase):
    """渲染末态：必须由真那一帧推上去，且窗口用夹具那只（本类绝不自己建 FloatWindow）。

    为什么这条要写在注释里：曾经有一版把真渲染判据写在没挡 `start_icon_worker` 的类里，
    自己另建了一只窗口 —— 实测那一条判据把用户机器上的正式版从 /Applications 拉起来
    跑了 5 分钟（dock 图标链路真去 `open` 那个 bundle）。窗口不是被测对象。
    """

    def test_the_real_paint_records_the_hint_clearance(self):
        before = sf_ui_render.paint_diagnostics()["stale_hint_frames"]
        img = QImage(self.win.width(), self.win.height(), QImage.Format.Format_ARGB32)
        painter = QPainter(img)
        try:
            rect = QRectF(10, 200, self.win.width() - 20, 90)
            self.win._draw_chart_stale(painter, rect, None)  # discipline-ok(R5): 真渲染入口只有这个私有方法
            self.win._draw_chart_stale(painter, rect, {"pre_close": None})  # discipline-ok(R5): 同上
            self.win._draw_chart_stale(painter, rect, {"pre_close": "10.0"})  # discipline-ok(R5): 同上
        finally:
            painter.end()
        after = sf_ui_render.paint_diagnostics()
        self.assertGreaterEqual(after["stale_hint_frames"], before + 3,
                                "画了三帧提示而计数没动：这条埋点等于没接线")
        self.assertGreaterEqual(after["stale_hint_line_missing"], 2,
                                "m=None 与没有 pre_close 都算「虚线取不到」，要分开数")
        self.assertIsInstance(after["stale_hint_tight_frames"], int)

    def test_the_real_paint_records_the_stretch_endstate(self):
        """拉伸在飞的末态进包：动画 from/to + minimumHeight（1.4.34 那一族）。"""
        driver = self.win._anim_driver  # discipline-ok(R5): 被测对象就是驱动器的这两个字段
        driver.layout_anim_from = 224    # discipline-ok(R5): 动画没有公开写入口，包里读的就是这一对
        driver.layout_anim_to = 120     # discipline-ok(R5): 同上
        body = json.loads(self._export()["runtime.json"])["stretch"]
        self.assertEqual(body["anim_from"], 224)
        self.assertEqual(body["anim_to"], 120)
        self.assertGreater(body["min_height"], 0)

    def test_source_mode_never_touches_the_real_updates_dir(self):
        """源码态与测试**一个文件都不许碰**用户目录（清理动作唯一的安全闸）。

        这条判据守的不是清理逻辑，是"什么时候不许跑"：本仓测试跑的是真用户目录，
        没有这道门，一次回归就能把某人正在重传的半包挪走。
        """
        calls = []
        self.track_patch(mock.patch.object(
            sf_update, "prune_staging_residue",
            lambda *a, **k: calls.append(1)))
        import sys as _sys
        with mock.patch.object(_sys, "frozen", False, create=True):
            self.win._prune_update_staging()  # discipline-ok(R5): 被测对象本身就是这条闸门
        self.app.processEvents() if hasattr(self, "app") else QApplication.processEvents()
        self.assertEqual(calls, [], "源码态也去清理了 —— 那正是要拦的情形")
        with mock.patch.object(_sys, "frozen", True, create=True), \
             mock.patch.object(m, "log_line", lambda *a, **k: None):
            self.win._prune_update_staging()  # discipline-ok(R5): 同上
            wait_for(lambda: calls == [1], 3.0)
        self.assertEqual(calls, [1], "冻结产物里这条自愈清理根本没跑起来")

    def test_the_glass_package_carries_the_gap_episodes(self):
        with mock.patch.object(sf_glass.GlassMixin, "_gap_evidence",
                               lambda self: {"open": {"reason": "glass above main",
                                                      "age_sec": 4.0},
                                             "last": [{"reason": "order probe blind",
                                                       "duration_sec": 12.71}]}):
            body = json.loads(self._export()["glass.json"])["gap"]
        self.assertEqual(body["open"]["reason"], "glass above main")
        self.assertEqual(body["last"][0]["duration_sec"], 12.71,
                         "上一场空窗持续了多久，是这一组键唯一要回答的问题")


    # ---- 近两天逐版核对补上的两处现场（v1.4.70 弹层补 Space / v1.4.71 分时区拖动）----

    def test_glass_carries_the_popup_space_watch_list(self):
        """真注册一块弹层进续补名单，包里必须说得出「在等第几拍、表在不在跑」。

        这条判据存在理由是：v1.4.70 修完只在本机量到"0.27 秒看见菜单"，而"那台机器上
        补齐了没有"远程读不出来 —— 补齐是**按节拍续补**的，读不到拍数就等于读不到成败。

        两个实测坑决定了这条判据长这个样子：
        ① `native_cocoa_active` 的桩**只能在注册那一下开着**，导出前必须关掉 —— 开着导出
           会让 `glass_diagnostics()` 也走真 cocoa 分支，离屏下当场 SIGSEGV(rc=139)；
        ② 续补那一拍 `popup_space_tick` 必须按住，理由同上（它是真伸手进 AppKit 的地方）。
        """
        from PyQt6.QtWidgets import QLabel
        probe = QLabel("弹层替身")
        self.track_widget(probe)
        self.track_patch(mock.patch.object(sf_glass, "popup_space_tick", lambda *a: None))
        with mock.patch.object(sf_glass, "native_cocoa_active", lambda: True), \
             mock.patch.object(sf_glass, "join_active_space",
                               lambda w: "queued:shown-not-on-space"):
            sf_glass.watch_active_space(probe)     # discipline-ok(R5): 这条链的唯一公开入口就是这个函数
        self.addCleanup(sf_glass.clear_popup_space_watch)
        body = json.loads(self._export()["glass.json"])["popup_space"]
        self.assertGreaterEqual(body["pending"], 1, "名单里明明有一块弹层，包里说没有")
        self.assertEqual([rec[0] for rec in body["tries"]], ["QLabel"],
                         "tries 里要带得上是哪种弹层，否则只知道数量不知道是谁")
        self.assertTrue(body["timer_running"], "续补表没在跑，pending 就永远是假数字")
        self.assertEqual(body["max_ticks"], sf_glass.POPUP_SPACE_MAX_TICKS,
                         "上限必须取自那份唯一实现，不许这里再写一遍")

    def test_render_bucket_says_whether_menu_icons_were_drawn(self):
        """"v1.4.69 那批给菜单行加了行首图标，失效形状是「行首一片空白」——包里此前只说
        字体与绘制降级，答不出"图标出图了没有、哪一行没出"。这条补的就是那一问。

        刻意**不**调 `menu_images()`：取诊断不许顺手把图标全建一遍（副作用边界，
        与自动采集层那条"一个函数都不调"同口径）。所以这里读的是缓存本身，
        三种长相要分得开：没建过 / 建了但有行取不到图 / 全建成功。
        """
        import sf_icons
        body = json.loads(self._export()["runtime.json"])["render"]["menu_icons"]
        self.assertEqual(body["registered"], len(sf_icons.MENU_ICON_KEYS),
                         "登记了几行要说得出，否则分不清「少了一行」和「没这一行」")
        self.assertIn("built", body)
        with mock.patch.object(sf_icons, "_menu_images_cache",
                               {k: None for k in sf_icons.MENU_ICON_KEYS}):
            body = json.loads(self._export()["runtime.json"])["render"]["menu_icons"]
        self.assertTrue(body["built"], "缓存已经在手上了，不能说没建过")
        self.assertEqual(sorted(body["missing"]), sorted(sf_icons.MENU_ICON_KEYS),
                         "四行全都取不到图（例如 QtSvg 缺失）必须逐行点名")
        good = {k: object() for k in sf_icons.MENU_ICON_KEYS}
        good["diag"] = None
        with mock.patch.object(sf_icons, "_menu_images_cache", good):
            body = json.loads(self._export()["runtime.json"])["render"]["menu_icons"]
        self.assertEqual(body["missing"], ["diag"],
                         "只少一行时说成「四行都少」，读的人就会去查错的东西")

    def test_no_popup_watch_reports_zero_rather_than_missing(self):
        """反向：名单空着时包里要写 0，而不是没这个键 —— "查了、没有"与"没查"两种长相。"""
        sf_glass.clear_popup_space_watch()
        body = json.loads(self._export()["glass.json"])["popup_space"]
        self.assertEqual(body["pending"], 0)
        self.assertFalse(body["timer_running"], "名单空了表必须停，否则是空转功耗")

    def test_gesture_carries_the_chart_press_origin(self):
        """真按一次分时热区，包里要留得下"落进的是哪个分支、起点在哪、偏了多少"。

        v1.4.71 那条投诉（朋友机器"拖不动"）到现在还分不清三种情形：热区没命中、
        命中了但没进拖窗分支、进了拖窗但位移算错。这三件事在界面上长得一模一样。
        """
        from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtWidgets import QApplication
        from sf_ui_render import CHART_TOP
        local = QPoint(self.win.width() // 2, CHART_TOP + 10)
        glob = self.win.mapToGlobal(local)
        QApplication.sendEvent(self.win, QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(local), QPointF(glob),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        body = json.loads(self._export()["runtime.json"])["gesture"]
        self.assertTrue(body["press_chart"], "按下确实落在分时热区，包里说没落进")
        self.assertFalse(body["drag"], "刚按下还没过移动阈值，此刻不该是拖动态")
        press = body["chart_press"]
        self.assertEqual(press["origin"], [glob.x(), glob.y()],
                         "起点必须是真的那一个：偏移算错正是「拖不动」的候选根因之一")
        self.assertEqual(press["offset"],
                         [glob.x() - self.win.x(), glob.y() - self.win.y()],
                         "offset 是「起点减窗口左上角」，读的人靠它判断跟手性")
