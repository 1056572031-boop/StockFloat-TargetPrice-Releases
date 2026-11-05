# -*- coding: utf-8 -*-
"""2026-11-02 第 50 轮走查（w2-config 批）：配置读写链路的回归判据。

覆盖：
  F-C-01  主配置"读不出来"（权限/IO）不是"损坏"：不得用旧备份覆盖主配置
  F-C-02  带 BOM 的配置，自定义行情接口（api_urls）照常加载；读不出来要留 WARNING
  F-C-08  CONFIG_PATH 是符号链接时，保存不得把链接换成普通文件

两条读不出来的造法：① 打桩 builtins.open 只对主配置抛 PermissionError（任何账号/平台都成立）；
② 真 chmod 000（root 或系统仍放行读取时如实跳过并打印理由）。
"""
import builtins
import contextlib
import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import sf_config
import sf_quote
from sf_constants import BG_MODE_DEFAULT, DEFAULT_STOCK, MIN_H
from tests._base import SFCase

NEW = {"code": "sh600519", "x": 500, "y": 600, "h": 300, "version": 1,
       "alerts": [{"direction": "above", "target": 2000.0,
                   "session_date": "2026-11-02", "code": "sh600519"},
                  {"direction": "below", "target": 1500.0,
                   "session_date": "2026-11-02", "code": "sh600519"}]}
OLD = {"code": "sh600519", "x": 10, "y": 20, "h": 218, "version": 1, "alerts": []}
DEFAULTS = (DEFAULT_STOCK, 120, 120, MIN_H, BG_MODE_DEFAULT)


class _ConfigDirCase(SFCase):
    def setUp(self):
        super().setUp()
        self._td = tempfile.TemporaryDirectory(prefix="sf_r50_cfg_")
        self.addCleanup(self._td.cleanup)
        self.dir = self._td.name
        self.cfg = os.path.join(self.dir, "config.json")
        self.bak = self.cfg + ".bak"
        none = os.path.join(self.dir, "__none__", "x.json")
        # 迁移来源指向不存在的路径：绝不读到本机真实旧配置。
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", self.cfg))
        self.track_patch(mock.patch.object(sf_config, "CONFIG_BACKUP_PATH", self.bak))
        self.track_patch(mock.patch.object(sf_config, "SPLIT_SOURCE_CONFIG_PATH", none))
        self.track_patch(mock.patch.object(sf_config, "LEGACY_CONFIG_PATH", none))
        self.logs = []
        self.track_patch(mock.patch.object(
            sf_config, "log_line",
            side_effect=lambda msg, level="INFO": self.logs.append((level, msg))))

    def _put(self, main, backup):
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump(main, f)
        with open(self.bak, "w", encoding="utf-8") as f:
            json.dump(backup, f)

    def _bytes(self, path):
        with open(path, "rb") as f:
            return f.read()

    def _deny_reads_of_main(self):
        """只对主配置的读取抛 PermissionError，其余文件照常。"""
        real_open = builtins.open
        target = os.path.realpath(self.cfg)

        def guarded(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and os.path.realpath(file) == target:
                raise PermissionError(errno.EACCES, "Permission denied", str(file))
            return real_open(file, *args, **kwargs)

        return mock.patch("builtins.open", guarded)


class TestNotificationOutbox(_ConfigDirCase):
    def setUp(self):
        super().setUp()
        self.alert = {"direction": "above", "target": 100, "session_date": "2099-12-31",
                      "code": "sh600519", "state": "hit"}
        self.event = {"id": "event-1", "message": "hit"}
        self._put(dict(OLD, custom={"keep": True}, target_flash="legacy", target_notification_outbox=[
            {"id": "old", "message": "pending"}]), OLD)

    def test_startup_recovery_preserves_successfully_saved_pending_event(self):
        self.assertTrue(sf_config.save_alerts([self.alert], "sh600519", outbox_events=[self.event]))
        with open(self.cfg, "w", encoding="utf-8") as stream:
            stream.write("{broken")
        sf_config.load_config()
        self.assertIn(self.event, sf_config.load_target_notification_outbox())

    def test_normal_save_after_corruption_preserves_successfully_saved_pending_event(self):
        self.assertTrue(sf_config.save_alerts([self.alert], "sh600519", outbox_events=[self.event]))
        with open(self.cfg, "w", encoding="utf-8") as stream:
            stream.write("{broken")
        self.assertTrue(sf_config.save_config("sh600519", 100, 100))
        self.assertIn(self.event, sf_config.load_target_notification_outbox())

    def test_atomic_append_and_preserve_other_fields(self):
        self.assertTrue(sf_config.save_alerts([self.alert], "sh600519", outbox_events=[self.event]))
        saved = json.loads(self._bytes(self.cfg))
        self.assertEqual(saved["alerts"], [self.alert])
        self.assertEqual(saved["custom"], {"keep": True})
        self.assertEqual(saved["target_flash"], "legacy")
        self.assertEqual(sf_config.load_target_notification_outbox(),
                         [{"id": "old", "message": "pending"}, self.event])

    def test_invalid_or_full_outbox_rejects_without_changing_main(self):
        cases = [None, {}, [{"id": "x"}, {"id": "x"}],
                 [{"id": str(i)} for i in range(1024)]]
        for outbox in cases:
            with self.subTest(outbox_type=type(outbox), size=len(outbox) if outbox else 0):
                self._put(dict(OLD, target_notification_outbox=outbox), OLD)
                before = self._bytes(self.cfg)
                self.assertFalse(sf_config.save_alerts([self.alert], outbox_events=[self.event]))
                self.assertEqual(self._bytes(self.cfg), before)

    def test_invalid_events_and_duplicate_ids_are_rejected(self):
        for events in ({}, [None], [{}], [{"id": ""}], [{"id": "old"}],
                       [{"id": "new"}, {"id": "new"}]):
            with self.subTest(events=events):
                before = self._bytes(self.cfg)
                self.assertFalse(sf_config.save_alerts([self.alert], outbox_events=events))
                self.assertEqual(self._bytes(self.cfg), before)

    def test_write_failure_before_replace_preserves_original(self):
        before = self._bytes(self.cfg)
        with mock.patch.object(sf_config.os, "replace", side_effect=OSError("write failed")):
            self.assertFalse(sf_config.save_alerts([self.alert], outbox_events=[self.event]))
        self.assertEqual(self._bytes(self.cfg), before)

    def test_directory_fsync_failure_keeps_hit_and_event_in_replaced_file(self):
        original_fsync = os.fsync
        def fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory sync failed")
            return original_fsync(fd)
        with mock.patch.object(sf_config.os, "fsync", side_effect=fsync):
            self.assertFalse(sf_config.save_alerts([self.alert], outbox_events=[self.event]))
        saved = json.loads(self._bytes(self.cfg))
        self.assertEqual(saved["alerts"], [self.alert])
        self.assertEqual(saved["target_notification_outbox"][-1], self.event)

    def test_resync_confirms_exact_pending_event_after_uncertain_commit(self):
        original_fsync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory sync failed")
            return original_fsync(fd)
        with mock.patch.object(sf_config.os, "fsync", side_effect=fail_directory):
            self.assertFalse(sf_config.save_alerts([self.alert], outbox_events=[self.event]))
        with mock.patch.object(sf_config.os, "fsync", wraps=original_fsync) as sync:
            self.assertTrue(sf_config.sync_target_notification_outbox([self.event]))
        self.assertGreaterEqual(sync.call_count, 2)
        saved = json.loads(self._bytes(self.cfg))
        self.assertEqual(saved["alerts"], [self.alert])
        self.assertEqual(saved["target_notification_outbox"][-1], self.event)
        self.assertEqual(saved["custom"], {"keep": True})

    def test_resync_rejects_event_until_backup_is_durable(self):
        original_write = sf_config._atomic_write_json  # discipline-ok(R5): 故障注入恢复副本原子写，验证同步确认不会绕过备份持久化。

        def fail_backup(path, payload):
            if os.path.abspath(path) == os.path.abspath(self.bak):
                raise OSError("backup unavailable")
            return original_write(path, payload)

        with mock.patch.object(sf_config, "_atomic_write_json", side_effect=fail_backup):
            self.assertFalse(sf_config.save_alerts(
                [self.alert], "sh600519", outbox_events=[self.event]))
        self.assertEqual(sf_config.load_target_notification_outbox(),
                         [{"id": "old", "message": "pending"}, self.event])
        backup = json.loads(self._bytes(self.bak))
        self.assertNotIn(self.event, backup.get("target_notification_outbox", []))
        with mock.patch.object(sf_config, "_atomic_write_json",
                               side_effect=OSError("backup unavailable")):
            self.assertFalse(sf_config.sync_target_notification_outbox([self.event]))

    def test_resync_repairs_backup_before_confirming_event(self):
        original_write = sf_config._atomic_write_json  # discipline-ok(R5): 故障注入恢复副本原子写，验证一次失败后重试可修复副本。
        failed = {"value": True}

        def fail_once(path, payload):
            if (failed["value"] and
                    os.path.abspath(path) == os.path.abspath(self.bak)):
                failed["value"] = False
                raise OSError("backup unavailable")
            return original_write(path, payload)

        with mock.patch.object(sf_config, "_atomic_write_json", side_effect=fail_once):
            self.assertFalse(sf_config.save_alerts(
                [self.alert], "sh600519", outbox_events=[self.event]))
        self.assertTrue(sf_config.sync_target_notification_outbox([self.event]))
        with open(self.cfg, "w", encoding="utf-8") as stream:
            stream.write("{broken")
        sf_config.load_config()
        self.assertIn(self.event, sf_config.load_target_notification_outbox())

    def test_sync_empty_or_future_schema_rejects(self):
        self.assertFalse(sf_config.sync_target_notification_outbox([]))
        self._put(dict(OLD, version=sf_config.CONFIG_SCHEMA_VERSION + 1,
                       target_notification_outbox=[self.event]), OLD)
        self.assertFalse(sf_config.sync_target_notification_outbox([self.event]))

    def test_sync_backup_symlink_rejects_after_matching_event(self):
        self._put(dict(OLD, target_notification_outbox=[self.event]), OLD)
        os.unlink(self.bak)
        victim = os.path.join(self.dir, "backup-victim.json")
        with open(victim, "w", encoding="utf-8") as stream:
            json.dump(OLD, stream)
        os.symlink(victim, self.bak)
        self.assertFalse(sf_config.sync_target_notification_outbox([self.event]))
        self.assertEqual(json.loads(self._bytes(victim)), OLD)

    def test_ack_invalid_or_future_schema_rejects(self):
        self.assertFalse(sf_config.ack_target_notification_outbox(["", 1]))
        self._put(dict(OLD, version=sf_config.CONFIG_SCHEMA_VERSION + 1,
                       target_notification_outbox=[self.event]), OLD)
        self.assertFalse(sf_config.ack_target_notification_outbox([self.event["id"]]))

    def test_save_outbox_rejects_backup_symlink_after_main_write(self):
        victim = os.path.join(self.dir, "backup-victim.json")
        with open(victim, "w", encoding="utf-8") as stream:
            json.dump(OLD, stream)
        os.unlink(self.bak)
        os.symlink(victim, self.bak)
        self.assertFalse(sf_config.save_alerts(
            [self.alert], "sh600519", outbox_events=[self.event]))
        self.assertEqual(json.loads(self._bytes(victim)), OLD)

    def test_resync_rejects_missing_or_changed_event_without_writing(self):
        before = self._bytes(self.cfg)
        self.assertFalse(sf_config.sync_target_notification_outbox([self.event]))
        self.assertFalse(sf_config.sync_target_notification_outbox([{"id": "old", "message": "changed"}]))
        self.assertEqual(self._bytes(self.cfg), before)

    def test_resync_failure_does_not_report_success(self):
        with mock.patch.object(sf_config.os, "fsync", side_effect=OSError("sync failed")):
            self.assertFalse(sf_config.sync_target_notification_outbox([{"id": "old", "message": "pending"}]))

    def test_acknowledge_by_id_is_idempotent_and_preserves_fields(self):
        self.assertTrue(sf_config.save_alerts([self.alert], outbox_events=[self.event]))
        self.assertTrue(sf_config.ack_target_notification_outbox(["event-1", "missing"]))
        before = self._bytes(self.cfg)
        self.assertTrue(sf_config.ack_target_notification_outbox(["event-1"]))
        self.assertEqual(self._bytes(self.cfg), before)
        saved = json.loads(before)
        self.assertEqual(saved["alerts"], [self.alert])
        self.assertEqual(saved["custom"], {"keep": True})
        self.assertEqual(saved["target_notification_outbox"], [{"id": "old", "message": "pending"}])

    def test_unreadable_or_corrupt_main_never_uses_backup(self):
        before = self._bytes(self.cfg)
        with self._deny_reads_of_main():
            with self.assertRaises(sf_config.ConfigUnreadableError):
                sf_config.load_target_notification_outbox()
            self.assertFalse(sf_config.save_alerts([self.alert], outbox_events=[self.event]))
            self.assertFalse(sf_config.ack_target_notification_outbox(["old"]))
        self.assertEqual(self._bytes(self.cfg), before)
        with open(self.cfg, "w", encoding="utf-8") as stream:
            stream.write("{broken")
        before = self._bytes(self.cfg)
        with self.assertRaises(sf_config.ConfigUnreadableError):
            sf_config.load_target_notification_outbox()
        self.assertFalse(sf_config.save_alerts([self.alert], outbox_events=[self.event]))
        self.assertFalse(sf_config.ack_target_notification_outbox(["old"]))
        self.assertEqual(self._bytes(self.cfg), before)

    def test_capacity_boundary_and_legacy_save(self):
        events = [{"id": str(i)} for i in range(1023)]
        self._put(dict(OLD, target_notification_outbox=events), OLD)
        self.assertTrue(sf_config.save_alerts([self.alert], outbox_events=[self.event]))
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1024)
        self.assertTrue(sf_config.save_alerts([self.alert]))
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1024)

    def test_ack_retry_requires_sync_after_uncertain_deletion(self):
        original_fsync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory sync failed")
            return original_fsync(fd)
        with mock.patch.object(sf_config.os, "fsync", side_effect=fail_directory):
            self.assertFalse(sf_config.ack_target_notification_outbox(["old"]))
            self.assertEqual(sf_config.load_target_notification_outbox(), [])
            self.assertFalse(sf_config.ack_target_notification_outbox(["old"]))
        self.assertTrue(sf_config.ack_target_notification_outbox(["old"]))

    def test_ack_write_failure_preserves_pending_event(self):
        before = self._bytes(self.cfg)
        with mock.patch.object(sf_config.os, "replace", side_effect=PermissionError("denied")):
            self.assertFalse(sf_config.ack_target_notification_outbox(["old"]))
        self.assertEqual(self._bytes(self.cfg), before)
        self.assertEqual(sf_config.load_target_notification_outbox(),
                         [{"id": "old", "message": "pending"}])

    def test_missing_and_malformed_outbox_read(self):
        self._put(OLD, OLD)
        self.assertEqual(sf_config.load_target_notification_outbox(), [])
        self._put(dict(OLD, target_notification_outbox="bad"), OLD)
        with self.assertRaises(sf_config.ConfigUnreadableError):
            sf_config.load_target_notification_outbox()
        self.assertFalse(sf_config.ack_target_notification_outbox(["old"]))


class TestConcurrentConfigWrites(_ConfigDirCase):
    def test_other_process_respects_transaction_and_exit_releases_lock(self):
        self._put(OLD, OLD)
        script = ("import sf_config,sys; "
                  "ctx=sf_config.config_transaction(sys.argv[1]); ctx.__enter__(); "
                  "print('locked',flush=True); sys.stdin.read()")
        child = subprocess.Popen([sys.executable, "-c", script, self.cfg],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with mock.patch.object(sf_config, "_CONFIG_LOCK_TIMEOUT", 0.1):  # discipline-ok(R5): 覆盖锁竞争超时分支。
                self.assertFalse(sf_config.save_face_guard_pref(True))
            self.assertNotIn("face_guard", json.loads(self._bytes(self.cfg)))
        finally:
            child.communicate("", timeout=5)
        self.assertEqual(child.returncode, 0)
        self.assertTrue(sf_config.save_face_guard_pref(True))
        self.assertTrue(json.loads(self._bytes(self.cfg))["face_guard"])

    def test_legacy_writer_change_invalidates_stock_revision(self):
        first = {"code": "sh600519", "direction": "above", "target": 100.0,
                 "session_date": "2099-12-31", "state": "pending"}
        self._put(OLD, OLD)
        self.assertTrue(sf_config.save_alerts([first], code="sh600519"))
        revision = sf_config.alerts_revision("sh600519")
        with open(self.cfg, encoding="utf-8") as stream:
            payload = json.load(stream)
        payload["alerts"] = [dict(first, target=110.0)]
        with open(self.cfg, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        self.assertNotEqual(sf_config.alerts_revision("sh600519"), revision)

    def test_pruning_another_stock_changes_its_revision(self):
        first = {"code": "sh600519", "direction": "above", "target": 100.0,
                 "session_date": "2099-12-31", "state": "pending"}
        second = dict(first, code="sz000001", target=110.0)
        self._put(OLD, OLD)
        self.assertTrue(sf_config.save_alerts([first], code="sh600519"))
        revision = sf_config.alerts_revision("sh600519")
        with mock.patch.object(sf_config, "ALERT_STORE_MAX_CODES", 0):
            self.assertTrue(sf_config.save_alerts([second], code="sz000001"))
        self.assertEqual(sf_config.load_alerts("sh600519"), [])
        self.assertNotEqual(sf_config.alerts_revision("sh600519"), revision)

    def test_pruning_other_stock_preserves_unaffected_stock_revision(self):
        alert = {"direction": "above", "target": 100.0, "session_date": "2099-12-31", "state": "pending"}
        first = dict(alert, code="sh600519")
        stale = dict(alert, code="sz000002")
        second = dict(alert, code="sz000001")
        self._put(OLD, OLD)
        self.assertTrue(sf_config.save_alerts([stale], code="sz000002"))
        self.assertTrue(sf_config.save_alerts([first], code="sh600519"))
        revision = sf_config.alerts_revision("sh600519")
        with mock.patch.object(sf_config, "ALERT_STORE_MAX_CODES", 1):
            self.assertTrue(sf_config.save_alerts([second], code="sz000001"))
        self.assertEqual(sf_config.load_alerts("sh600519"), [first])
        self.assertNotEqual(sf_config.load_alerts("sz000002"), [stale])
        self.assertEqual(sf_config.alerts_revision("sh600519"), revision)

    def test_stamping_other_legacy_stock_preserves_active_hit_revision(self):
        legacy = {"direction": "below", "target": 90.0,
                  "session_date": "2099-12-31", "state": "pending"}
        current = {"code": "sh600519", "direction": "above", "target": 100.0,
                   "session_date": "2099-12-31", "state": "pending"}
        self._put({"code": "sz000001", "alerts": [legacy, current]}, OLD)
        revision = sf_config.alerts_revision("sh600519")
        self.assertTrue(sf_config.save_alerts([dict(current, state="hit")], code="sh600519"))
        self.assertEqual(sf_config.alerts_revision("sh600519"), revision)
        self.assertEqual(sf_config.load_alerts("sz000001")[0]["target"], 90.0)

    def test_legacy_owner_stamping_preserves_stock_revision(self):
        alert = {"direction": "above", "target": 100.0, "session_date": "2099-12-31", "state": "pending"}
        self._put(dict(OLD, alerts=[alert]), OLD)
        revision = sf_config.alerts_revision("sh600519")
        self.assertTrue(sf_config.save_alerts([dict(alert, code="sz000001")], code="sz000001"))
        self.assertEqual(sf_config.alerts_revision("sh600519"), revision)

    def test_target_tick_lock_rejects_non_owned_regular_file(self):
        path = self.cfg + ".target-ticks.lock"
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("")
        with mock.patch.object(sf_config.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(PermissionError):
                with sf_config.target_tick_transaction():
                    pass

    def test_outbox_unavailable_configuration_is_not_read(self):
        self._put(OLD, OLD)
        with mock.patch.object(sf_config, "config_unavailable", return_value=True):
            with self.assertRaises(sf_config.ConfigUnreadableError):
                sf_config.load_target_notification_outbox()
        self.assertEqual(json.loads(self._bytes(self.cfg)), OLD)

    def test_outbox_stat_failure_is_unreadable(self):
        self._put(OLD, OLD)
        real_stat = os.stat

        def denied(path, *args, **kwargs):
            if path == self.cfg:
                raise PermissionError("stat denied")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(sf_config.os, "stat", side_effect=denied), \
                mock.patch.object(sf_config, "config_unavailable", return_value=False):
            with self.assertRaises(sf_config.ConfigUnreadableError):
                sf_config.load_target_notification_outbox()

    def test_lock_symlink_cannot_modify_target(self):
        self._put(OLD, OLD)
        victim = os.path.join(self.dir, "keep")
        with open(victim, "w", encoding="utf-8") as stream:
            stream.write("KEEP")
        os.symlink(victim, self.cfg + ".lock")
        self.assertFalse(sf_config.save_face_guard_pref(True))
        with open(victim, encoding="utf-8") as stream:
            self.assertEqual(stream.read(), "KEEP")
        self.assertEqual(json.loads(self._bytes(self.cfg)), OLD)

    def test_concurrent_preferences_preserve_both_changes(self):
        self._put(OLD, OLD)
        first_read = threading.Event()
        second_done = threading.Event()
        read_base = sf_config._base_dict_for_write  # discipline-ok(R5): 控制真实读改写的竞态时序。
        results = []

        def delayed_read(*args):
            result = read_base(*args)
            if threading.current_thread().name == "first-save":
                first_read.set()
                second_done.wait(0.2)
            return result

        def first():
            results.append(sf_config.save_config("sh600519", 501, 20))

        def second():
            try:
                results.append(sf_config.save_face_guard_pref(True))
            finally:
                second_done.set()

        with mock.patch.object(sf_config, "_base_dict_for_write", side_effect=delayed_read):
            a = threading.Thread(target=first, name="first-save")
            b = threading.Thread(target=second, name="second-save")
            a.start()
            self.assertTrue(first_read.wait(3))
            b.start()
            a.join(5)
            b.join(5)
        self.assertFalse(a.is_alive())
        self.assertFalse(b.is_alive())
        self.assertEqual(results, [True, True])
        with open(self.cfg, encoding="utf-8") as stream:
            saved = json.load(stream)
        self.assertEqual(saved["x"], 501)
        self.assertIs(saved.get("face_guard"), True)


    def test_lock_directory_is_rejected_without_touching_config(self):
        self._put(OLD, OLD)
        os.mkdir(self.cfg + ".lock")
        self.assertFalse(sf_config.save_face_guard_pref(True))
        self.assertEqual(json.loads(self._bytes(self.cfg)), OLD)
        os.rmdir(self.cfg + ".lock")

    def test_legacy_migration_read_error_is_logged(self):
        legacy = os.path.join(self.dir, "legacy.json")
        self.track_patch(mock.patch.object(sf_config, "LEGACY_CONFIG_PATH", legacy))
        with open(legacy, "w", encoding="utf-8") as stream:
            json.dump(OLD, stream)
        real_open = builtins.open
        def broken(file, *args, **kwargs):
            if os.path.realpath(file) == os.path.realpath(legacy):
                raise OSError("legacy read failed")
            return real_open(file, *args, **kwargs)
        with mock.patch("builtins.open", broken):
            self.assertEqual(sf_config.load_config(), DEFAULTS)
        self.assertFalse(os.path.exists(self.cfg))

    def test_load_config_lock_failure_falls_back_without_writing(self):
        self._put(NEW, OLD)
        with mock.patch.object(sf_config, "config_transaction",
                               side_effect=TimeoutError("busy")):
            self.assertEqual(sf_config.load_config(), DEFAULTS)
        self.assertTrue(sf_config.config_unavailable())
        self.assertEqual(json.loads(self._bytes(self.cfg)), NEW)

    def test_alerts_revision_rejects_unreadable_and_bad_backup(self):
        self._put(NEW, OLD)
        sf_config._untrusted_config_path = self.cfg  # discipline-ok(R5): 测试恢复哨兵状态。
        self.addCleanup(setattr, sf_config, "_untrusted_config_path", None)
        with self.assertRaises(sf_config.ConfigUnreadableError):
            sf_config.alerts_revision()
        sf_config._untrusted_config_path = None  # discipline-ok(R5): 清除测试哨兵以验证损坏备份分支。
        with open(self.cfg, "w", encoding="utf-8") as stream:
            stream.write("broken")
        with open(self.bak, "w", encoding="utf-8") as stream:
            stream.write("also broken")
        with self.assertRaises(sf_config.ConfigUnreadableError):
            sf_config.alerts_revision()

    def test_alerts_revision_uses_backup_when_main_is_not_a_dict(self):
        self._put([], {"alerts_revision": "backup-rev"})
        self.assertEqual(sf_config.alerts_revision(), "backup-rev")


    def test_nested_transaction_reuses_thread_lock(self):
        self._put(OLD, OLD)
        with sf_config.config_transaction(self.cfg):
            with sf_config.config_transaction(self.cfg):  # discipline-ok(R5): 验证同线程可重入锁。
                self.assertTrue(os.path.exists(self.cfg + ".lock"))

    def test_lock_cleanup_after_body_exception(self):
        self._put(OLD, OLD)
        with self.assertRaises(RuntimeError):
            with sf_config.config_transaction(self.cfg):
                raise RuntimeError("body failed")
        self.assertTrue(sf_config.save_face_guard_pref(True))

    def test_lock_open_failure_releases_thread_lock(self):
        self._put(OLD, OLD)
        with mock.patch.object(sf_config.os, "open", side_effect=OSError("open failed")):
            with self.assertRaises(OSError):
                with sf_config.config_transaction(self.cfg):
                    pass
        self.assertTrue(sf_config.save_face_guard_pref(True))

    def test_owned_lock_file_is_reused_after_save(self):
        self._put(OLD, OLD)
        self.assertTrue(sf_config.save_face_guard_pref(True))
        self.assertTrue(os.path.isfile(self.cfg + ".lock"))

    def test_config_transaction_accepts_nested_path_without_config_file(self):
        path = os.path.join(self.dir, "nested", "config.json")
        with sf_config.config_transaction(path):
            self.assertTrue(os.path.isdir(os.path.dirname(path)))


    def test_thread_lock_acquire_timeout_is_reported(self):
        self._put(OLD, OLD)
        blocker = mock.Mock()
        blocker.acquire.return_value = False
        self.track_patch(mock.patch.object(sf_config, "_config_thread_lock", blocker))
        with self.assertRaises(TimeoutError):
            with sf_config.config_transaction(self.cfg):
                pass

    def test_lock_owner_mismatch_is_rejected(self):
        self._put(OLD, OLD)
        with open(self.cfg + ".lock", "w", encoding="utf-8") as stream:
            stream.write("lock")
        fake = mock.Mock(st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid() + 1)
        with mock.patch.object(sf_config.os, "fstat", return_value=fake):
            self.assertFalse(sf_config.save_face_guard_pref(True))

    def test_migration_destination_stat_error_stays_default(self):
        real_stat = sf_config.os.stat
        calls = []
        def broken(path):
            calls.append(path)
            if path == self.cfg and len(calls) == 1:
                raise OSError("destination stat failed")
            return real_stat(path)
        with mock.patch.object(sf_config.os, "stat", side_effect=broken):
            self.assertEqual(sf_config.load_config(), DEFAULTS)
        self.assertIn(self.cfg, calls)
        self.assertFalse(os.path.exists(self.cfg))

    def test_save_base_stat_error_preserves_config(self):
        self._put(OLD, OLD)
        def broken(path):
            if path == self.cfg:
                raise OSError("stat failed")
            return os.stat(path)
        with mock.patch.object(sf_config.os, "stat", side_effect=broken):
            with self.assertRaises(sf_config.ConfigUnreadableError):
                sf_config._base_dict_for_write(self.cfg, self.bak)  # discipline-ok(R5): 直接覆盖底稿异常分支。
        self.assertEqual(json.loads(self._bytes(self.cfg)), OLD)


    def test_migration_dangling_destination_link_stays_default(self):
        os.symlink(os.path.join(self.dir, "missing.json"), self.cfg)
        self.assertEqual(sf_config.load_config(), DEFAULTS)

    def test_migration_stat_error_logs_and_stays_default(self):
        original = sf_config.os.stat
        calls = []
        def broken(path):
            calls.append(path)
            if path == self.cfg and len(calls) == 1:
                raise OSError("destination stat failed")
            return original(path)
        with mock.patch.object(sf_config.os, "stat", side_effect=broken):
            sf_config._migrate_legacy_config()  # discipline-ok(R5): 直接覆盖迁移异常分支。
        self.assertTrue(any("migration skipped" in message for _, message in self.logs))


    def test_save_base_missing_config_uses_empty_dict(self):
        self.assertEqual(sf_config._base_dict_for_write(self.cfg, self.bak), {})  # discipline-ok(R5): 直接覆盖首次启动底稿分支。


    def test_save_base_dangling_symlink_is_rejected(self):
        os.symlink(os.path.join(self.dir, "missing.json"), self.cfg)
        with self.assertRaises(sf_config.ConfigUnreadableError):
            sf_config._base_dict_for_write(self.cfg, self.bak)  # discipline-ok(R5): 直接覆盖悬空链接保护分支。


class TestUnreadableMainConfigIsNotCorruption(_ConfigDirCase):
    """F-C-01：读不出来（OSError）≠ 内容损坏。主配置此刻只是读不到，数据还在。"""

    def test_backup_symlink_cannot_overwrite_other_file(self):
        self._put(NEW, OLD)
        os.unlink(self.bak)
        victim = os.path.join(self.dir, "victim.json")
        with open(victim, "w", encoding="utf-8") as stream:
            stream.write("KEEP")
        os.symlink(victim, self.bak)
        sf_config.save_config("sh600519", 4, 5, 300)
        self.assertEqual(self._bytes(victim), b"KEEP")

    def test_dangling_config_symlink_is_unavailable(self):
        os.symlink(os.path.join(self.dir, "missing.json"), self.cfg)
        sf_config.load_config()
        self.assertTrue(sf_config.config_unavailable())

    def test_unreadable_startup_exposes_unavailable_status(self):
        self._put(NEW, OLD)
        with self._deny_reads_of_main():
            sf_config.load_config()
        self.assertTrue(sf_config.config_unavailable())

    def test_unreadable_destination_does_not_migrate_legacy(self):
        self._put(NEW, OLD)
        legacy = os.path.join(self.dir, "legacy.json")
        with open(legacy, "w", encoding="utf-8") as stream:
            json.dump(OLD, stream)
        before = self._bytes(self.cfg)
        real_exists = os.path.exists
        def exists(path):
            if os.fspath(path) == self.cfg:
                return False
            return real_exists(path)
        with mock.patch.object(sf_config, "SPLIT_SOURCE_CONFIG_PATH", legacy), \
                mock.patch.object(sf_config.os.path, "exists", side_effect=exists), \
                self._deny_reads_of_main():
            sf_config.load_config()
        self.assertEqual(self._bytes(self.cfg), before)

    def test_exists_failure_does_not_erase_configuration_on_save(self):
        self._put(NEW, OLD)
        before = self._bytes(self.cfg)
        real_exists = os.path.exists
        def exists(path):
            if os.fspath(path) == self.cfg:
                return False
            return real_exists(path)
        with mock.patch.object(sf_config.os.path, "exists", side_effect=exists), \
                self._deny_reads_of_main():
            self.assertFalse(sf_config.save_config("sh600519", 1, 2, 300))
        self.assertEqual(self._bytes(self.cfg), before)

    def test_load_config_does_not_restore_over_an_unreadable_main(self):
        self._put(NEW, OLD)
        before_main, before_bak = self._bytes(self.cfg), self._bytes(self.bak)
        with self._deny_reads_of_main():
            result = sf_config.load_config()
        self.assertEqual(result, DEFAULTS, "读不出来时本次只用默认值，不拿旧备份顶替")
        self.assertEqual(self._bytes(self.cfg), before_main,
                         "主配置字节必须原封不动（旧实现会用旧备份覆盖它）")
        self.assertEqual(self._bytes(self.bak), before_bak)
        self.assertTrue(any(lv == "WARNING" and "读取失败" in msg for lv, msg in self.logs),
                        "必须留一条 WARNING 说明是读取失败、不是损坏：%r" % (self.logs,))

    def test_recovered_main_is_not_overwritten_by_startup_defaults(self):
        self._put(NEW, OLD)
        with self._deny_reads_of_main():
            code, x, y, height, bg = sf_config.load_config()
        self.assertIs(sf_config.save_config(code, x, y, height, bg), False)
        self.assertEqual(json.loads(self._bytes(self.cfg))["x"], NEW["x"])
        self.assertEqual(json.loads(self._bytes(self.bak))["x"], OLD["x"])

    def test_every_save_path_refuses_while_main_is_unreadable(self):
        self._put(NEW, OLD)
        before_main, before_bak = self._bytes(self.cfg), self._bytes(self.bak)
        alerts = [{"direction": "above", "target": 1.0, "session_date": "2026-11-02",
                   "code": "sh600519"}]
        with self._deny_reads_of_main():
            self.assertIs(sf_config.save_config("sh600519", 1, 2, 300), False)
            self.assertIs(sf_config.save_alerts(alerts, "sh600519"), False)
            self.assertIs(sf_config.save_face_guard_pref(True), False)
        self.assertEqual(self._bytes(self.cfg), before_main,
                         "读不出来时任何 save_* 都不许覆盖主配置")
        self.assertEqual(self._bytes(self.bak), before_bak,
                         "读不出来时备份快照也不许被当成'当前主配置'覆盖")

    def test_unreadable_startup_does_not_activate_backup_targets(self):
        backup = dict(OLD, alerts=[{"direction": "above", "target": 99.0,
                                    "session_date": "2026-11-02", "code": "sh600519"}])
        self._put(NEW, backup)
        with self._deny_reads_of_main():
            sf_config.load_config()
            self.assertEqual(sf_config.load_alerts("sh600519"), [])

    def test_runtime_unreadable_config_does_not_use_stale_backup_targets(self):
        backup = dict(OLD, alerts=[{"direction": "above", "target": 99.0,
                                    "session_date": "2026-11-02", "code": "sh600519"}])
        self._put(NEW, backup)
        with self._deny_reads_of_main():
            self.assertEqual(sf_config.load_alerts("sh600519"), [])

    def test_load_alerts_rejects_stale_backup_when_main_unreadable(self):
        backup = dict(OLD, alerts=[{"direction": "above", "target": 99.0,
                                    "session_date": "2026-11-02", "code": "sh600519"}])
        self._put(NEW, backup)
        before_main = self._bytes(self.cfg)
        with self._deny_reads_of_main():
            got = sf_config.load_alerts("sh600519")
        self.assertEqual(got, [], "读取失败时不能将旧备份当成当前目标价")
        self.assertEqual(self._bytes(self.cfg), before_main, "load_alerts 只读不写回")

    def test_unreadable_main_does_not_enable_guard_from_stale_backup(self):
        self._put(dict(NEW, face_guard=False), dict(OLD, face_guard=True))
        with self._deny_reads_of_main():
            self.assertIs(sf_config.load_face_guard_pref(), False)

    def test_face_guard_pref_stays_off_when_main_is_unreadable(self):
        """旧备份不能证明用户最近选择，配置不可读时禁止启动摄像头。"""
        backup = dict(OLD, face_guard=True)
        self._put(NEW, backup)
        before_main, before_bak = self._bytes(self.cfg), self._bytes(self.bak)
        with self._deny_reads_of_main():
            self.assertIs(sf_config.load_face_guard_pref(), False)
        self.assertEqual((self._bytes(self.cfg), self._bytes(self.bak)), (before_main, before_bak))
        self.assertTrue(any(lv == "WARNING" and "读取失败" in msg
                            for lv, msg in self.logs),
                        "不可读配置必须留告警：%r" % (self.logs,))

    def test_face_guard_pref_with_unreadable_main_and_no_backup_warns_and_stays_off(self):
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump(dict(NEW, face_guard=True), f)
        with self._deny_reads_of_main():
            self.assertIs(sf_config.load_face_guard_pref(), False)
        self.assertTrue(any(lv == "WARNING" and "读取失败" in msg for lv, msg in self.logs))

    def test_face_guard_pref_with_unreadable_main_and_backup_warns_and_stays_off(self):
        self._put(dict(NEW, face_guard=True), dict(OLD, face_guard=True))
        real_open = builtins.open

        def deny_both(file, *args, **kwargs):
            if os.fspath(file) in (self.cfg, self.bak):
                raise PermissionError(errno.EACCES, "Permission denied", str(file))
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", deny_both):
            self.assertIs(sf_config.load_face_guard_pref(), False)
        self.assertTrue(any(lv == "WARNING" and "人脸守卫开关未知" in msg
                            for lv, msg in self.logs))

    def test_missing_main_is_still_the_quiet_first_launch_path(self):
        """FileNotFoundError 仍是首次启动的正常路径：不记'读取失败'，保存照常成功。"""
        with open(self.bak, "w", encoding="utf-8") as f:
            json.dump(OLD, f)
        self.assertEqual(sf_config.load_config(), DEFAULTS)
        self.assertFalse(any("读取失败" in msg for _lv, msg in self.logs), self.logs)
        self.assertTrue(sf_config.save_config("sh600519", 7, 8, 300))
        self.assertEqual(json.loads(self._bytes(self.cfg))["x"], 7)

    def test_genuinely_corrupt_main_still_restores_from_backup(self):
        """对照：真损坏（读到了但不是 JSON）仍按既有口径从备份恢复并写回。"""
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write("{not json")
        with open(self.bak, "w", encoding="utf-8") as f:
            json.dump(NEW, f)
        self.assertEqual(sf_config.load_config()[:3], ("sh600519", 500, 600))
        self.assertEqual(json.loads(self._bytes(self.cfg))["x"], 500, "恢复后写回主配置")

    def test_json_reader_keeps_none_contract_for_other_callers(self):
        """sf_credentials / sf_diagnostics 只认 dict-or-None：旧入口不许泄露哨兵对象。"""
        self._put(NEW, OLD)
        with self._deny_reads_of_main():
            self.assertIsNone(sf_config._read_json_or_none(self.cfg))  # discipline-ok(R5): 外部调用点依赖的旧契约，没有公开替代入口
            self.assertIs(sf_config._read_json_checked(self.cfg),  # discipline-ok(R5): 三态读取是本次修复的核心，只有它能区分读不出来与损坏
                          sf_config._UNREADABLE)  # discipline-ok(R5): 哨兵对象本身就是被测契约

    def test_real_chmod_000_main_is_not_overwritten(self):
        """真文件权限版：与打桩版互为印证；root 或系统仍放行读取时如实跳过。"""
        self._put(NEW, OLD)
        before_main = self._bytes(self.cfg)
        os.chmod(self.cfg, 0)
        self.addCleanup(lambda: os.path.exists(self.cfg) and os.chmod(self.cfg, 0o600))
        try:
            with open(self.cfg, "rb"):
                self.skipTest("本环境对 chmod 000 的文件仍放行读取（root？），打桩版已覆盖该行为")
        except PermissionError:
            pass
        self.assertEqual(sf_config.load_config(), DEFAULTS)
        self.assertIs(sf_config.save_config("sh600519", 1, 2, 300), False)
        os.chmod(self.cfg, 0o600)
        self.assertEqual(self._bytes(self.cfg), before_main)


class TestSymlinkedConfigStaysALink(_ConfigDirCase):
    """F-C-08：config.json 是符号链接（dotfiles 管理）时，保存更新真实文件、保留链接。"""

    def _link_setup(self):
        real_dir = os.path.join(self.dir, "real_store")
        os.makedirs(real_dir)
        target = os.path.join(real_dir, "config_real.json")
        with open(target, "w", encoding="utf-8") as f:
            json.dump({"code": "sh600000", "x": 1, "y": 1, "h": 300, "version": 1}, f)
        os.symlink(target, self.cfg)
        return target

    def test_credentials_migration_preserves_config_symlink(self):
        import sf_credentials
        from tests.test_quality_credentials import MemoryKeychain
        victim = os.path.join(self.dir, "external.json")
        payload = dict(NEW, api_urls={"quote": "https://example.invalid/private"})
        with open(victim, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        os.symlink(victim, self.cfg)
        sf_credentials.resolve_config_urls(self.cfg, payload, keychain=MemoryKeychain())
        self.assertTrue(os.path.islink(self.cfg))
        self.assertEqual(json.loads(self._bytes(victim)), payload)

    def test_config_symlink_does_not_overwrite_external_target(self):
        victim = os.path.join(self.dir, "external.json")
        with open(victim, "w", encoding="utf-8") as stream:
            json.dump(NEW, stream)
        before = self._bytes(victim)
        os.symlink(victim, self.cfg)
        sf_config.save_config("sh600519", 77, 88, 300)
        self.assertEqual(self._bytes(victim), before)
        self.assertFalse(os.path.exists(self.bak))

    def test_save_config_preserves_symlink_and_target(self):
        target = self._link_setup()
        before = self._bytes(target)
        self.assertFalse(sf_config.save_config("sh600519", 77, 88, 300))
        self.assertTrue(os.path.islink(self.cfg))
        self.assertEqual(self._bytes(target), before)

    def test_save_alerts_and_face_guard_preserve_symlink_and_target(self):
        target = self._link_setup()
        before = self._bytes(target)
        alerts = [{"direction": "above", "target": 5.0, "session_date": "2026-11-02",
                   "code": "sh600519"}]
        self.assertFalse(sf_config.save_alerts(alerts, "sh600519"))
        self.assertFalse(sf_config.save_face_guard_pref(True))
        self.assertTrue(os.path.islink(self.cfg))
        self.assertEqual(self._bytes(target), before)

    def test_plain_file_path_behaves_as_before(self):
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"code": "sh600000", "x": 1, "y": 1, "h": 300, "version": 1}, f)
        self.assertTrue(sf_config.save_config("sh600519", 9, 9, 300))
        self.assertFalse(os.path.islink(self.cfg))
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o600)


class ConfigUnavailableFeedback(SFCase):
    def test_startup_schedules_feedback_only_when_unavailable(self):
        import stock_float_window as window
        from tests.test_minute_recovery_latency import _Host
        from PyQt6.QtCore import QTimer
        class Host(_Host):
            def runTest(self):
                pass
        host = Host()
        host.setUp()
        try:
            with mock.patch.object(sf_config, "config_unavailable", return_value=True), \
                    mock.patch.object(QTimer, "singleShot") as timer:
                host.win._init_targets()  # discipline-ok(R5): 触发真实启动恢复入口。
            self.assertTrue(any(call.args[1] == host.win._show_config_unavailable  # discipline-ok(R5): 检查实际异步通知是否安排。
                                for call in timer.call_args_list))
        finally:
            host.tearDown()

    def test_target_read_failure_during_startup_schedules_feedback(self):
        from tests.test_minute_recovery_latency import _Host
        import stock_float_window as window
        from PyQt6.QtCore import QTimer
        class Host(_Host):
            def runTest(self):
                pass
        host = Host()
        host.setUp()
        try:
            with mock.patch.object(sf_config, "config_unavailable", side_effect=[False, True]), \
                    mock.patch.object(window, "load_alerts", return_value=[]), \
                    mock.patch.object(QTimer, "singleShot") as timer:
                host.win._init_targets()  # discipline-ok(R5): 检查目标读取后故障的提示。
            self.assertTrue(any(call.args[1] == host.win._show_config_unavailable  # discipline-ok(R5): 检查提示已被调度。
                                for call in timer.call_args_list))
        finally:
            host.tearDown()

    def test_save_failure_explains_suspended_targets_and_restart(self):
        from tests.test_minute_recovery_latency import _Host
        import stock_float_window as window
        from PyQt6.QtWidgets import QToolTip
        class Host(_Host):
            def runTest(self):
                pass
        host = Host()
        host.setUp()
        try:
            with mock.patch.object(sf_config, "config_unavailable", return_value=True), \
                    mock.patch.object(QToolTip, "showText") as shown:
                host.win._after_save_config(False)  # discipline-ok(R5): 验证真实窗口保存失败反馈。
            message = shown.call_args.args[1]
            self.assertIn("目标价", message)
            self.assertIn("重新启动", message)
        finally:
            host.tearDown()

    def _save_failure_message(self, refusal):
        from tests.test_minute_recovery_latency import _Host
        from PyQt6.QtWidgets import QToolTip
        class Host(_Host):
            def runTest(self):
                pass
        host = Host()
        host.setUp()
        try:
            with mock.patch.object(sf_config, "config_unavailable", return_value=False), \
                    mock.patch.object(sf_config, "last_alert_refusal", return_value=refusal), \
                    mock.patch.object(QToolTip, "showText") as shown:
                host.win._after_save_config(False)  # discipline-ok(R5): 验证真实窗口保存失败反馈。
            return shown.call_args.args[1]
        finally:
            host.tearDown()

    def test_store_limit_refusal_is_not_blamed_on_disk_or_permissions(self):
        """复核 B1：目标价条数/股票数到上限而被拒时，不能让用户去查磁盘空间和文件权限。"""
        message = self._save_failure_message("store_limit")
        self.assertIn("上限", message)
        self.assertNotIn("磁盘", message)
        self.assertNotIn("权限", message)

    def test_other_save_failures_still_point_at_disk_and_permissions(self):
        message = self._save_failure_message("")
        self.assertIn("磁盘空间", message)
        self.assertIn("权限", message)


class TestApiUrlsFromBomConfig(SFCase):
    """F-C-02：带 BOM 的配置里的 api_urls 要照常生效；读不出来留 WARNING。"""

    def setUp(self):
        super().setUp()
        self._td = tempfile.TemporaryDirectory(prefix="sf_r50_api_")
        self.addCleanup(self._td.cleanup)
        self.cfg = os.path.join(self._td.name, "config.json")
        # 先登记原值：load_api_urls 会改写模块全局，patch 停止时还原。
        for name in ("QUOTE_URL", "QUOTE_URL_FALLBACK", "SINA_REFERER"):
            self.track_patch(mock.patch.object(sf_quote, name, getattr(sf_quote, name)))
        self.track_patch(mock.patch.object(sf_quote, "CONFIG_PATH", self.cfg))
        self.track_patch(mock.patch(
            "sf_credentials.resolve_config_urls",
            side_effect=lambda _path, config: config["api_urls"]))
        self.logs = []
        self.track_patch(mock.patch.object(
            sf_quote, "log_line",
            side_effect=lambda msg, level="INFO": self.logs.append((level, msg))))

    def _write(self, raw_bytes):
        with open(self.cfg, "wb") as f:
            f.write(raw_bytes)

    def test_bom_config_still_overrides_api_urls(self):
        payload = {"code": "sh600000", "api_urls": {"quote": "https://example.invalid/q=%s"}}
        for label, prefix in (("无 BOM", b""), ("UTF-8 BOM", b"\xef\xbb\xbf")):
            with self.subTest(label):
                self._write(prefix + json.dumps(payload).encode("utf-8"))
                changed = sf_quote.load_api_urls()
                self.assertEqual(changed, ["quote"], "%s 的配置也必须加载自定义接口" % label)
                self.assertEqual(sf_quote.QUOTE_URL, "https://example.invalid/q=%s")

    def test_unreadable_config_leaves_a_warning_not_silence(self):
        self._write(b"{broken")
        self.assertEqual(sf_quote.load_api_urls(), [])
        self.assertTrue(any(lv == "WARNING" and "api_urls" in msg for lv, msg in self.logs),
                        "配置存在但读不出对象时要留 WARNING：%r" % (self.logs,))

    def test_missing_config_stays_silent(self):
        self.assertEqual(sf_quote.load_api_urls(), [])
        self.assertEqual(self.logs, [], "配置文件不存在是正常场景，不刷日志")

    def test_lock_timeout_while_resolving_urls_leaves_a_warning(self):
        """复核 B3：解析接口地址时配置锁超时，此前被外层 except 静默吞掉，注释却说
        “细节由该模块自己记日志”，实际一行都没有。自定义接口没生效必须能在日志里查到。"""
        self._write(json.dumps({"api_urls": {"quote": "https://example.invalid/q=%s"}}).encode("utf-8"))
        with mock.patch("sf_credentials.resolve_config_urls", side_effect=TimeoutError("configuration lock timed out")):
            self.assertEqual(sf_quote.load_api_urls(), [])
        self.assertTrue(any(lv == "WARNING" and "api_urls" in msg for lv, msg in self.logs),
                        "自定义接口没加载必须留 WARNING：%r" % (self.logs,))


class TestLoadConfigLockTimeout(_ConfigDirCase):
    """复核 B3：启动时配置锁被别人短暂占住，不能把本次运行永久判成配置不可信。"""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, sf_config, "_untrusted_config_path", None)
        sf_config._untrusted_config_path = None  # discipline-ok(R5): 保证用例从可信状态开始。
        self._put(dict(OLD, code="sh600519"), OLD)

    def _flaky_transaction(self, failures):
        real = sf_config.config_transaction
        state = {"left": failures, "timeouts": []}

        @contextlib.contextmanager
        def wrapper(path=None, timeout=None):
            state["timeouts"].append(timeout)
            if state["left"] > 0:
                state["left"] -= 1
                raise TimeoutError("configuration lock timed out")
            with real(path, timeout):
                yield
        return wrapper, state

    def test_one_transient_timeout_does_not_poison_the_run(self):
        wrapper, state = self._flaky_transaction(1)
        with mock.patch.object(sf_config, "config_transaction", wrapper):
            code = sf_config.load_config()[0]
        self.assertEqual(code, "sh600519", "重试之后必须读到用户真实配置，而不是默认股票")
        self.assertFalse(sf_config.config_unavailable(), "一次短暂超时不能把本次运行判成不可信")
        self.assertEqual(len(state["timeouts"]), 2)
        self.assertGreater(state["timeouts"][1], sf_config._CONFIG_LOCK_TIMEOUT,  # discipline-ok(R5): 锁超时没有公开读取口，要比较的就是它
                           "重试必须比首次等得更久，否则等于原样再撞一次")

    def test_a_lock_that_never_frees_still_marks_the_config_untrusted(self):
        wrapper, _state = self._flaky_transaction(99)
        with mock.patch.object(sf_config, "config_transaction", wrapper):
            code = sf_config.load_config()[0]
        self.assertEqual(code, sf_config.DEFAULT_STOCK)
        self.assertTrue(sf_config.config_unavailable(), "重试用尽仍拿不到锁，保持原来的保护行为")

    def test_a_non_timeout_failure_marks_untrusted_without_a_second_wait(self):
        calls = []

        @contextlib.contextmanager
        def broken(path=None, timeout=None):
            calls.append(timeout)
            raise PermissionError("configuration lock is not an owned regular file")
            yield
        with mock.patch.object(sf_config, "config_transaction", broken):
            code = sf_config.load_config()[0]
        self.assertEqual(code, sf_config.DEFAULT_STOCK)
        self.assertTrue(sf_config.config_unavailable())
        self.assertEqual(len(calls), 1, "权限这类故障再等也没用，不能白等第二轮")


class TestHasPendingTicks(_ConfigDirCase):
    """复核 B1：丢弃过期休眠条目前，必须确认这只股票没有在途行情；读不出来按"有"处理。"""

    def _store_with_tick(self, path, code):
        from sf_target_tick_store import TargetTickStore
        quote = sf_quote.QuoteData({"code": code, "price": "10"}, "")
        quote.source_time = None
        quote.received_at = None
        with TargetTickStore(path) as store:
            store.save(code, quote)

    def test_a_live_publisher_counts_as_pending(self):
        with mock.patch.object(sf_config, "target_tick_publisher_active", return_value=True):
            self.assertTrue(sf_config._has_pending_ticks("sh600519"))  # discipline-ok(R5): 判定函数没有公开入口，要测的就是它的出口

    def test_default_queue_sits_next_to_the_config_file(self):
        default_queue = os.path.join(self.dir, "target_ticks.sqlite")
        with mock.patch.dict(os.environ):
            os.environ.pop("SF_TARGET_TICKS_PATH", None)
            self.assertFalse(sf_config._has_pending_ticks("sh600519"), "队列文件还不存在就是没有在途行情")  # discipline-ok(R5): 判定函数没有公开入口，要测的就是它的出口
            self._store_with_tick(default_queue, "sh600519")
            self.assertTrue(sf_config._has_pending_ticks("sh600519"), "默认路径要落在配置文件旁边")  # discipline-ok(R5): 判定函数没有公开入口，要测的就是它的出口
            self.assertFalse(sf_config._has_pending_ticks("sz000001"), "别的股票的行不算")  # discipline-ok(R5): 判定函数没有公开入口，要测的就是它的出口

    def test_an_unreadable_queue_is_treated_as_pending(self):
        broken = os.path.join(self.dir, "broken.sqlite")
        with open(broken, "wb") as stream:
            stream.write(b"")
        with mock.patch.dict(os.environ, {"SF_TARGET_TICKS_PATH": broken}), \
                mock.patch("sf_target_tick_store.TargetTickStore", side_effect=OSError("database is locked")):
            self.assertTrue(sf_config._has_pending_ticks("sh600519"),  # discipline-ok(R5): 判定函数没有公开入口，要测的就是它的出口
                            "读不出来时宁可多留一条过期条目，也不能丢在途行情要用的目标价")


if __name__ == "__main__":
    unittest.main()
