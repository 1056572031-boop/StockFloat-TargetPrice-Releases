"""诊断 v3 的采集、封装与校验：取不到值也要留一条说得清的理由。

这一层坏了的表现是"用户发来一份完整诊断，里面缺了最关键的那一类"，或更糟——
"看起来齐全，其实把取不到当成了没问题"。所以每条判据都问同一件事：
四类快照是否**总是存在**，失败时是否**点名**，旧版包是否**不会被升级**。
"""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PyQt6.QtWidgets import QWidget

import sf_feedback_analyzer
import sf_feedback_diagnostics as collect3
import sf_feedback_v3 as v3
import sf_notify
from sf_feedback import (FeedbackProtocolError, PAYLOAD_V1, PAYLOAD_V2, PAYLOAD_V3,
                         PrivacyRejected, build_archive)
from sf_feedback_bundle import read_diagnostics
from sf_feedback_capture import read_upload_archive
from sf_feedback_v3 import V3_DIAGNOSTIC_FILES
from tests._base import SFCase
from tests.test_feedback_full_bundle import diagnostic_bytes, full_packet, v3_diagnostic_bytes, v3_packet
import tests.test_coverage_window_diagnostics as diagnostic_fixtures

SNAPSHOT_NAMES = sorted(V3_DIAGNOSTIC_FILES)


class EnvironmentAndPermissionsTests(SFCase):
    def setUp(self):
        super().setUp()
        self.app = self.ensure_qapp()
        self.widget = self.track_widget(QWidget())
        self.widget.resize(320, 200)
        self.widget.show()
        self.swallow = mock.Mock()
        self.track_patch(mock.patch.object(collect3, "log_swallow", self.swallow))

    def snapshots(self, window=None, *, feedback_context=None):
        return collect3.v3_snapshots(window if window is not None else self.widget,
                                     feedback_context=feedback_context)

    def data(self, result, name):
        self.assertTrue(v3.validate_snapshot(name, result[name]), "%s 不是一条合法快照" % name)
        self.assertTrue(result[name]["available"], "%s 本轮应取到值" % name)
        return result[name]["data"]

    def test_a_real_window_yields_bounded_environment_and_permission_records(self):
        result = self.snapshots(window=SimpleNamespace(
            width=lambda: 320, height=lambda: 200, isVisible=lambda: True,
            palette=self.widget.palette, face_guard_field_evidence=lambda: {"state": "running"}))
        environment = self.data(result, "environment.json")
        self.assertEqual(sorted(environment), sorted(
            ["macos_version", "machine_model_family", "hardware_arch", "process_arch", "is_rosetta",
             "appearance", "screen_count", "window", "locale", "region", "memory_bucket_gib"]))
        self.assertGreaterEqual(environment["screen_count"], 1)
        self.assertIn(environment["appearance"], ("dark", "light"))
        self.assertEqual(environment["window"], {"width": 320, "height": 200, "visible": True})
        self.assertIsInstance(environment["memory_bucket_gib"], int)
        permissions = self.data(result, "permissions.json")
        self.assertEqual(permissions["camera"], "running")
        self.assertIn(permissions["notifications"],
                      [sf_notify.delivery_stats().get("authorization"), "unknown"])
        self.assertFalse(self.swallow.called)

    def test_a_window_that_cannot_answer_is_left_as_unknown_instead_of_guessing(self):
        """读不到尺寸/取不到外观：外观退回 unknown，窗口那一栏留 None，各留一条站点。"""
        def raiser(*_args):
            raise RuntimeError("fixture")

        window = SimpleNamespace(width=raiser, height=raiser, isVisible=lambda: True,
                                 palette=raiser, face_guard_field_evidence=lambda: {})
        result = self.snapshots(window=window)
        environment = self.data(result, "environment.json")
        self.assertIsNone(environment["window"])
        self.assertEqual(environment["appearance"], "unknown")
        self.assertGreaterEqual(environment["screen_count"], 1)
        self.assertIn("sf_feedback_diagnostics.environment.qt",
                      [call.args[0] for call in self.swallow.call_args_list])
        self.assertIn("sf_feedback_diagnostics.environment.window",
                      [call.args[0] for call in self.swallow.call_args_list])

    def test_the_appearance_word_follows_the_window_lightness_rule(self):
        """深浅档只由窗口色明度决定：用两个人色调色板把那条界两侧都钉住。

        不拿本机此刻的系统外观当真值 —— 那是把被测环境当判据（换台机器就只是在量别的东西）。
        """
        for lightness, expected in ((10, "dark"), (200, "light")):
            colour = SimpleNamespace(lightness=lambda value=lightness: value)
            surface = SimpleNamespace(color=lambda body=colour: body)
            palette = SimpleNamespace(window=lambda body=surface: body)
            window = SimpleNamespace(width=lambda: 1, height=lambda: 1, isVisible=lambda: False,
                                     palette=lambda body=palette: body,
                                     face_guard_field_evidence=lambda: {})
            with self.subTest(lightness=lightness):
                environment = self.data(self.snapshots(window=window), "environment.json")
                self.assertEqual(environment["appearance"], expected)

    def test_locale_and_memory_probes_that_fail_are_named_as_unknown(self):
        def raiser(*_args, **_kwargs):
            raise OSError("fixture")

        with mock.patch.object(collect3, "locale", SimpleNamespace(getlocale=raiser)), \
                mock.patch.object(collect3, "os", SimpleNamespace(sysconf=raiser)):
            environment = self.data(self.snapshots(), "environment.json")
        self.assertEqual((environment["locale"], environment["region"]), ("unknown", "unknown"))
        self.assertIsNone(environment["memory_bucket_gib"])

    def test_a_notification_or_camera_query_that_throws_degrades_to_unknown(self):
        def delivery_raises():
            raise OSError("no bundle")

        with mock.patch.object(sf_notify, "delivery_stats", delivery_raises):
            window = SimpleNamespace(width=lambda: 1, height=lambda: 1, isVisible=lambda: False,
                                     palette=self.widget.palette,
                                     face_guard_field_evidence=lambda: None)
            permissions = self.data(self.snapshots(window=window), "permissions.json")
        self.assertEqual(permissions["camera"], "unknown")
        self.assertEqual(permissions["notifications"], "unknown")
        self.assertIn("sf_feedback_diagnostics.permissions.notifications",
                      [call.args[0] for call in self.swallow.call_args_list])

    def test_preferences_are_taken_from_the_window_when_it_offers_them(self):
        result = self.snapshots(window=SimpleNamespace(
            width=lambda: 1, height=lambda: 1, isVisible=lambda: False,
            palette=self.widget.palette, face_guard_field_evidence=lambda: "not-a-dict",
            _feature_snapshot=lambda: {"face_guard": True}))
        self.assertEqual(self.data(result, "feedback_preferences.json"),
                         {"features": {"face_guard": True}})
        self.assertEqual(self.data(result, "permissions.json")["camera"], "unknown")

    def test_a_non_mapping_feature_snapshot_is_reported_as_an_empty_set(self):
        """功能开关读不出形状时给空表，而不是把怪东西上传上去。"""
        window = SimpleNamespace(width=lambda: 1, height=lambda: 1, isVisible=lambda: False,
                                 palette=self.widget.palette,
                                 face_guard_field_evidence=lambda: {},
                                 _feature_snapshot=lambda: ["not", "a", "mapping"])
        self.assertEqual(self.data(self.snapshots(window=window),
                                   "feedback_preferences.json"), {"features": {}})

    def test_a_window_without_the_feature_port_gets_an_empty_set(self):
        window = SimpleNamespace(width=lambda: 1, height=lambda: 1, isVisible=lambda: False,
                                 palette=self.widget.palette, face_guard_field_evidence=lambda: {})
        self.assertEqual(self.data(self.snapshots(window=window),
                                   "feedback_preferences.json"), {"features": {}})

    def test_feedback_context_defaults_to_manual_and_never_disappears(self):
        result = self.snapshots()
        self.assertEqual(self.data(result, "feedback_context.json"), {"trigger": "manual"})
        given = self.snapshots(feedback_context={"trigger": "crash_prompt", "session_id": "s" * 32})
        self.assertEqual(self.data(given, "feedback_context.json")["trigger"], "crash_prompt")

    def test_an_environment_probe_that_cannot_be_wrapped_is_declared_unavailable(self):
        """连包装都失败：三类快照各自点名 query_failed，第四类照样带上。

        feedback_context.json 不走 try —— 它是**用户自己点出来的上下文**，
        包装失败等于这次反馈没有理由，宁可整条链路报错也不能悄悄缺件。
        """
        real = collect3.available_snapshot

        def wrap(name, data):
            if name != "feedback_context.json":
                raise RuntimeError("fixture")
            return real(name, data)

        with mock.patch.object(collect3, "available_snapshot", wrap):
            result = self.snapshots()
        for name in ("environment.json", "permissions.json", "feedback_preferences.json"):
            self.assertEqual((result[name]["available"], result[name]["reason"]), (False, "query_failed"))
        self.assertTrue(result["feedback_context.json"]["available"])
        self.assertEqual(sorted(result), SNAPSHOT_NAMES)

    def test_a_platform_probe_that_throws_loses_only_the_environment_record(self):
        def raiser(*_args, **_kwargs):
            raise RuntimeError("fixture")

        stub = SimpleNamespace(mac_ver=raiser, machine=collect3.platform.machine)
        with mock.patch.object(collect3, "platform", stub):
            result = self.snapshots()
        self.assertEqual((result["environment.json"]["available"],
                          result["environment.json"]["reason"]), (False, "query_failed"))
        self.assertTrue(result["permissions.json"]["available"])


class SnapshotContractTests(SFCase):
    """四份快照的形状契约：非法的一律拒，不许"差不多就行"。"""

    def test_unavailable_snapshots_need_a_known_name_and_a_listed_reason(self):
        self.assertEqual(v3.unavailable_snapshot("environment.json", "no_bundle"),
                         {"schema": "environment.v1", "available": False, "reason": "no_bundle"})
        for name, reason in (("unknown.json", "query_failed"), ("permissions.json", "camera_denied")):
            with self.subTest(name=name, reason=reason):
                with self.assertRaises(ValueError):
                    v3.unavailable_snapshot(name, reason)

    def test_available_snapshots_need_a_known_name_and_a_mapping(self):
        self.assertEqual(v3.available_snapshot("permissions.json", {"camera": "unknown"}),
                         {"schema": "permissions.v1", "available": True, "data": {"camera": "unknown"}})
        for name, data in (("unknown.json", {}), ("environment.json", ["not-a-mapping"])):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    v3.available_snapshot(name, data)

    def test_validation_rejects_wrong_name_wrong_shape_and_free_form_reasons(self):
        good = v3.available_snapshot("environment.json", {"locale": "zh_CN"})
        self.assertTrue(v3.validate_snapshot("environment.json", good))
        missing = v3.unavailable_snapshot("environment.json", "query_failed")
        self.assertTrue(v3.validate_snapshot("environment.json", missing))
        broken = [
            ("名字不在名单里", "unknown.json", good),
            ("值不是字典", "environment.json", "text"),
            ("schema 串到别族", "environment.json",
             {"schema": "permissions.v1", "available": True, "data": {}}),
            ("available 不是布尔", "environment.json",
             {"schema": "environment.v1", "available": "true", "data": {}}),
            ("多带一个键", "environment.json",
             {"schema": "environment.v1", "available": True, "data": {}, "extra": 1}),
            ("取不到却不给理由", "environment.json", {"schema": "environment.v1", "available": False}),
            ("理由是没登记过的词", "environment.json",
             {"schema": "environment.v1", "available": False, "reason": "camera_denied"}),
        ]
        for name, snapshot_name, value in broken:
            with self.subTest(shape=name):
                self.assertFalse(v3.validate_snapshot(snapshot_name, value))

    def test_an_export_is_only_v3_when_the_schema_says_three(self):
        self.assertTrue(v3.is_v3_export({"diagnostic_schema_version": 3}))
        self.assertFalse(v3.is_v3_export({"diagnostic_schema_version": 2}))
        self.assertFalse(v3.is_v3_export(None))


class PackageRejectionTests(SFCase):
    """封装/复检侧的四条拒绝：混版本、缺快照、假快照、旧包升级。"""

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="v3-package-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def snapshot_set(self, **overrides):
        values = {
            "environment.json": {"schema": "environment.v1", "available": True, "data": {}},
            "permissions.json": {"schema": "permissions.v1", "available": True, "data": {}},
            "feedback_preferences.json": {"schema": "feedback_preferences.v1", "available": True,
                                          "data": {}},
            "feedback_context.json": {"schema": "feedback_context.v1", "available": True, "data": {}},
        }
        values.update(overrides)
        return {name: json.dumps(value, ensure_ascii=False).encode() for name, value in values.items()}

    def test_a_v3_export_without_the_four_members_is_rejected(self):
        content = diagnostic_bytes(change_export=lambda e: e.update(diagnostic_schema_version=3))
        with self.assertRaisesRegex(PrivacyRejected, "缺少固定快照"):
            read_diagnostics(content)

    def test_a_v3_export_with_a_malformed_snapshot_is_rejected(self):
        content = diagnostic_bytes(self.snapshot_set(
            **{"permissions.json": {"schema": "permissions.v1", "available": False,
                                    "reason": "camera_denied"}}),
            change_export=lambda e: e.update(diagnostic_schema_version=3))
        with self.assertRaisesRegex(PrivacyRejected, "快照格式无效"):
            read_diagnostics(content)

    def test_an_old_export_cannot_sneak_v3_members_in(self):
        content = diagnostic_bytes(self.snapshot_set())
        with self.assertRaisesRegex(PrivacyRejected, "混入v3快照"):
            read_diagnostics(content)

    def test_a_v3_package_is_read_back_with_its_snapshots_summarised(self):
        evidence = sf_feedback_analyzer.diagnostic_evidence(v3_diagnostic_bytes())
        self.assertEqual(evidence["diagnostic_schema_version"], 3)
        self.assertEqual(sorted(evidence["v3_snapshots"]), SNAPSHOT_NAMES)
        self.assertEqual(evidence["v3_snapshots"]["permissions.json"],
                         {"schema": "permissions.v1", "available": False, "reason": "no_bundle"})
        self.assertNotIn("data", evidence["v3_snapshots"]["permissions.json"])

    def test_a_v2_package_reports_no_v3_snapshots_at_all(self):
        evidence = sf_feedback_analyzer.diagnostic_evidence(diagnostic_bytes())
        self.assertEqual(evidence["v3_snapshots"], {})
        self.assertEqual(evidence["diagnostic_schema_version"], 1)

    def test_the_payload_version_must_match_what_is_actually_inside(self):
        path = self.root / "mismatched.zip"
        with self.assertRaisesRegex(FeedbackProtocolError, "does not match archive members"):
            build_archive(path, {"problem.txt": "SYNTHETIC ONLY"}, app_version="fixture",
                          bundle_id="com.skyrocket.app", run_id="fixture",
                          diagnostic_payload_version=PAYLOAD_V3)
        with self.assertRaisesRegex(FeedbackProtocolError, "does not match archive members"):
            build_archive(path, {"problem.txt": "SYNTHETIC ONLY", "diagnostics.zip": b"PK"},
                          app_version="fixture", bundle_id="com.skyrocket.app", run_id="fixture",
                          diagnostic_payload_version=PAYLOAD_V1)
        self.assertFalse(path.exists())

    def test_a_package_whose_diagnosis_is_a_different_generation_is_refused(self):
        """外层说 v3、里面那包还是 v2：不能拼成一份"看起来齐全"的 v3 上传。"""
        path, envelope = v3_packet(self.root, content=diagnostic_bytes())
        with self.assertRaises(PrivacyRejected) as raised:
            read_upload_archive(path, envelope)
        self.assertIn("完整诊断版本与反馈策略不一致", str(raised.exception.__cause__),
                      "对外只给一句通用拒绝，真实理由必须挂在 cause 上")
        good_path, _good = v3_packet(self.root)
        self.assertEqual(read_upload_archive(good_path)["manifest"]["diagnostic_payload_version"],
                         PAYLOAD_V3)
        old_path, _old, _inner = full_packet(self.root)
        self.assertEqual(read_upload_archive(old_path)["manifest"]["diagnostic_payload_version"],
                         PAYLOAD_V2)


class ExportedPackageMembersTests(SFCase):
    """只有反馈采集这一条路会把四类快照放进包；手工导出不得被悄悄升级。"""

    def run_export(self, feedback_context):
        case = diagnostic_fixtures.DiagnosticsBranchCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        out = case.win._export_diagnostics(feedback_context=feedback_context)  # discipline-ok(R5): 诊断导出只有这个私有入口
        self.assertIsNotNone(out)
        with zipfile.ZipFile(out) as archive:
            names = set(archive.namelist())
            export = json.loads(archive.read("export.json"))
            members = {name: json.loads(archive.read(name)) for name in SNAPSHOT_NAMES if name in names}
        return names, export, members

    def test_a_feedback_export_carries_the_four_snapshots_and_declares_schema_three(self):
        names, export, members = self.run_export({"trigger": "crash_prompt"})
        self.assertTrue(set(SNAPSHOT_NAMES) <= names, "四类快照必须都在包里")
        self.assertEqual(export["diagnostic_schema_version"], 3)
        self.assertEqual(sorted(members), SNAPSHOT_NAMES)
        for name, value in members.items():
            self.assertTrue(v3.validate_snapshot(name, value), "%s 没通过自己的契约" % name)
        self.assertEqual(members["feedback_context.json"]["data"]["trigger"], "crash_prompt")
        self.assertIn("window", members["environment.json"]["data"])
        self.assertIn("camera", members["permissions.json"]["data"])

    def test_feedback_export_answers_queue_cleanup_and_session_questions(self):
        """有过上传/清理中断/未正常退出时，feedback.json 必须能回答三句用户问题。"""
        from datetime import datetime, timezone

        import sf_feedback as protocol
        import sf_run_session
        from sf_feedback_outbox import FeedbackOutbox, read_diagnostics

        case = diagnostic_fixtures.DiagnosticsBranchCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        root = Path(case.tmp)
        feedback = root / "support" / "feedback"
        archive = root / "source.zip"
        protocol.build_archive(archive, {"state.json": "fixture"}, app_version="fixture",
                               bundle_id="com.skyrocket.app", run_id="fixture")
        store = FeedbackOutbox(feedback / "outbox.sqlite3", feedback / "objects")
        row = store.enqueue(archive, app_version="fixture", consent_policy_version="fixture")
        clock = datetime(2026, 10, 24, 13, 0, tzinfo=timezone.utc)
        claimed = store.claim(row["outbox_id"], kind="submit", statuses=("locally_verified",),
                              target_status="submitting", token="a" * 32, now=clock)
        store.finish_claim(row["outbox_id"], token=claimed["lease_token"],
                           fence=claimed["lease_fence"], status="response_unknown", error="TimeoutError",
                           now=clock)
        current = store.get(row["outbox_id"])
        store.persist_receipt(row["outbox_id"], {
            "case_id": "fixture-case", "archive_sha256": current["archive_sha256"],
            "size_bytes": current["archive_size"], "received_at": "2026-10-24T13:00:00Z",
            "storage_status": "stored", "validation_status": "validated",
            "retention_until": None, "retention_policy": "manual_deletion"})
        Path(current["archive_path"]).unlink()
        (feedback / "objects" / "orphan.zip").write_bytes(b"orphan")

        ledger = sf_run_session.RuntimeSessionLedger(
            sf_run_session.session_path_for_log(case.log_path))
        ledger.begin(app_version="fixture", bundle_id="com.skyrocket.app", pid=1234,
                     session_id="a" * 32, started_at="2026-10-24T13:00:00+00:00")
        ledger.mark_frame_ready("a" * 32)
        ledger.mark_healthy("a" * 32)
        ledger.begin(app_version="fixture", bundle_id="com.skyrocket.app", pid=1235,
                     session_id="b" * 32, started_at="2026-10-24T13:01:00+00:00")

        out = case.win._export_diagnostics()  # discipline-ok(R5): 只读导出入口没有公开替代品
        with zipfile.ZipFile(out) as archive_zip:
            feedback_data = json.loads(archive_zip.read("feedback.json"))
        self.assertTrue(feedback_data["queue"]["present"])
        self.assertEqual(feedback_data["queue"]["by_status"]["receipt_persisted"], 1)
        self.assertEqual(feedback_data["queue"]["last_errors"][0]["error"], "TimeoutError")
        self.assertEqual(feedback_data["queue"]["archive_missing"], 1)
        self.assertEqual(feedback_data["queue"]["orphan_objects"], 1)
        self.assertTrue(feedback_data["session"]["pending_unasked"])
        self.assertIsNotNone(feedback_data["session"]["current"])
        self.assertIsNotNone(feedback_data["session"]["pending"])

    def test_read_diagnostics_is_read_only_and_does_not_touch_store_mtime(self):
        import time
        import sf_feedback as protocol
        from sf_feedback_outbox import FeedbackOutbox, read_diagnostics as read_outbox_diagnostics

        with tempfile.TemporaryDirectory(prefix="feedback-readonly-", dir="/tmp") as directory:
            root = Path(directory)
            database = root / "outbox.sqlite3"
            objects = root / "objects"
            store = FeedbackOutbox(database, objects)
            archive = root / "source.zip"
            protocol.build_archive(archive, {"state.json": "fixture"}, app_version="fixture",
                                   bundle_id="com.skyrocket.app", run_id="fixture")
            # The summary reader only needs the persisted table shape; seed one row
            # through the real store, then make the archive an orphan deliberately.
            row = store.enqueue(archive, app_version="fixture", consent_policy_version="fixture")

            def footprint():
                # 红线盯的是"目录本身的修改时间 + 目录里有哪些文件"：只比库文件会漏掉 -wal/-shm 的新建。
                return (directory_state(root), database.stat().st_mtime_ns, objects.stat().st_mtime_ns)

            def directory_state(path):
                return (path.stat().st_mtime_ns, sorted(item.name for item in path.iterdir()))

            before = footprint()
            time.sleep(0.01)
            summary = read_outbox_diagnostics(database, objects)
            after = footprint()
            self.assertEqual(before, after, "只读摘要不许改库、目录修改时间，也不许新建 -wal/-shm")
            self.assertTrue(summary["present"])
            self.assertEqual(summary["by_status"]["locally_verified"], 1)
            self.assertEqual(summary["orphan_objects"], 0)
            self.assertEqual(row["status"], "locally_verified")

    def test_read_diagnostics_on_a_machine_that_never_used_feedback_creates_nothing(self):
        from sf_feedback_outbox import read_diagnostics as read_outbox_diagnostics

        with tempfile.TemporaryDirectory(prefix="feedback-absent-", dir="/tmp") as directory:
            root = Path(directory)
            before = sorted(item.name for item in root.iterdir())
            summary = read_outbox_diagnostics(root / "feedback" / "outbox.sqlite3",
                                              root / "feedback" / "objects")
            self.assertEqual(sorted(item.name for item in root.iterdir()), before,
                             "没用过反馈的机器：导出诊断不许为它新建目录或库")
            self.assertFalse(summary["present"])
            self.assertEqual(summary["by_status"], {})
            self.assertEqual(summary["recent"], 0)
            self.assertIsNone(summary["orphan_objects"])

    def test_read_diagnostics_still_sees_rows_that_only_live_in_the_wal(self):
        import sqlite3
        import sf_feedback as protocol
        from sf_feedback_outbox import FeedbackOutbox, read_diagnostics as read_outbox_diagnostics

        with tempfile.TemporaryDirectory(prefix="feedback-wal-", dir="/tmp") as directory:
            root = Path(directory)
            database = root / "outbox.sqlite3"
            objects = root / "objects"
            store = FeedbackOutbox(database, objects)
            archive = root / "source.zip"
            protocol.build_archive(archive, {"state.json": "fixture"}, app_version="fixture",
                                   bundle_id="com.skyrocket.app", run_id="fixture")
            store.enqueue(archive, app_version="fixture", consent_policy_version="fixture")
            # 写入者仍开着连接且不做检查点：这一行此刻只存在于 -wal 里。
            writer = sqlite3.connect(database)
            self.addCleanup(writer.close)
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE outbox SET status='cleanup_pending', last_error='TimeoutError'")
            writer.commit()
            self.assertTrue((root / "outbox.sqlite3-wal").exists(), "夹具必须真的留下 WAL")
            summary = read_outbox_diagnostics(database, objects)
            self.assertEqual(summary["by_status"], {"cleanup_pending": 1},
                             "有 -wal 时必须读它；按 immutable 打开会漏掉这一行")

    def test_a_manual_export_stays_v2_and_ships_no_snapshot_members(self):
        names, export, members = self.run_export(None)
        self.assertEqual(members, {})
        self.assertNotIn("diagnostic_schema_version", export)
