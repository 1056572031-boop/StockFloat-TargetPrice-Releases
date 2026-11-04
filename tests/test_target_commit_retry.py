"""目标价命中事务：保存失败可重试，只有持久化成功才通知。"""
import json
import os
import tempfile
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest import mock

import sf_config
from sf_config import load_alerts as real_load_alerts
import stock_float_window as window
import targets
from tests.test_minute_recovery_latency import _Host

real_init_targets = window.FloatWindow._init_targets  # discipline-ok(R5): 构造期须替换，但恢复测试需调用真实入口。


class TargetCommitRetry(_Host):
    """真窗口编排 + 真 evaluate_tick；只替换持久化的一次写失败和通知出口。"""

    def setUp(self):
        # 不修改事务入口；仅隔离构造期历史目标恢复、授权和通知副作用。
        self.construction_guards = ExitStack()
        self.addCleanup(self.construction_guards.close)
        for target, name, options in (
                (window, "load_alerts", {"return_value": []}),
                (sf_config, "load_alerts", {"return_value": []}),
                (window.FloatWindow, "_init_targets", {}),
                (window.FloatWindow, "_request_notify_auth", {}),
                (window, "_notify_deliver", {})):
            self.construction_guards.enter_context(mock.patch.object(target, name, **options))
        super().setUp()
        directory = tempfile.TemporaryDirectory(prefix="sf_target_retry_")
        self.addCleanup(directory.cleanup)
        self.tmp_path = directory.name
        # 跨进程锁与发布登记都挂在 CONFIG_PATH 旁边：不按用例隔离，前面用例留下的登记
        # 文件会让后面的 has_pending 误判为"仍有发布者"。用 construction_guards 栈是
        # 为了让补丁撑到窗口关闭之后（它在 addCleanup 阶段才收）。
        config = os.path.join(self.tmp_path, "config.json")
        self.construction_guards.enter_context(mock.patch.object(sf_config, "CONFIG_PATH", config))
        self.construction_guards.enter_context(mock.patch.object(sf_config, "CONFIG_BACKUP_PATH", config + ".bak"))

    def test_empty_panel_apply_persists_when_runtime_empty(self):
        import target_panel
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0)
        alert["code"] = "sh600183"
        config_path = os.path.join(self.tmp_path, "config.json")
        backup_path = os.path.join(self.tmp_path, "backup.json")
        with open(config_path, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "code": "sh600183", "alerts": [alert]}, stream)
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", config_path))
        self.track_patch(mock.patch.object(sf_config, "CONFIG_BACKUP_PATH", backup_path))
        self.track_patch(mock.patch.object(target_panel, "load_alerts", real_load_alerts))
        self.track_patch(mock.patch.object(window, "load_alerts", real_load_alerts))
        self.track_patch(mock.patch.object(sf_config, "load_alerts", real_load_alerts))
        self.track_patch(mock.patch.object(window.FloatWindow, "_after_save_config"))
        self.win.code = "sh600183"
        self.win._alerts = []  # discipline-ok(R5): 模拟启动恢复写盘失败后的空运行态。
        self.win._open_target_panel()  # discipline-ok(R5): 经主窗真实接线构造面板。
        panel = self.win._target_panel  # discipline-ok(R5): 驱动真实面板用户操作。
        panel._remove_one(targets.TARGET_DIR_ABOVE, 100.0)  # discipline-ok(R5): 模拟逐条删除。
        panel._apply()  # discipline-ok(R5): 模拟立即生效。
        self.assertEqual(real_load_alerts("sh600183"), [])

    def test_empty_panel_apply_does_not_delete_new_runtime_target(self):
        import target_panel
        original = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0)
        newer = targets.make_alert(targets.TARGET_DIR_ABOVE, 110.0)
        original["code"] = newer["code"] = "sh600183"
        config_path = os.path.join(self.tmp_path, "config.json")
        backup_path = os.path.join(self.tmp_path, "backup.json")
        with open(config_path, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "code": "sh600183", "alerts": [original]}, stream)
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", config_path))
        self.track_patch(mock.patch.object(sf_config, "CONFIG_BACKUP_PATH", backup_path))
        self.track_patch(mock.patch.object(target_panel, "load_alerts", real_load_alerts))
        self.track_patch(mock.patch.object(window, "load_alerts", real_load_alerts))
        self.track_patch(mock.patch.object(sf_config, "load_alerts", real_load_alerts))
        self.track_patch(mock.patch.object(window.FloatWindow, "_after_save_config"))
        self.win.code = "sh600183"
        self.win._alerts = [original]  # discipline-ok(R5): 建立运行态初始目标。
        self.win._open_target_panel()  # discipline-ok(R5): 经主窗真实接线构造面板。
        panel = self.win._target_panel  # discipline-ok(R5): 驱动真实面板用户操作。
        self.assertTrue(sf_config.save_alerts([original, newer], code=self.win.code))
        self.win._alerts = [original, newer]  # discipline-ok(R5): 模拟面板打开后新增条目。
        panel._remove_one(targets.TARGET_DIR_ABOVE, 100.0)  # discipline-ok(R5): 模拟逐条删除。
        panel._apply()  # discipline-ok(R5): 模拟立即生效。
        self.assertIn(110.0, [a["target"] for a in real_load_alerts(self.win.code)])

    def test_empty_panel_apply_write_failure_keeps_disk_and_unsaved_state(self):
        import target_panel
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0)
        alert["code"] = "sh600183"
        config_path = os.path.join(self.tmp_path, "config.json")
        backup_path = os.path.join(self.tmp_path, "backup.json")
        with open(config_path, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "code": "sh600183", "alerts": [alert]}, stream)
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", config_path))
        self.track_patch(mock.patch.object(sf_config, "CONFIG_BACKUP_PATH", backup_path))
        self.track_patch(mock.patch.object(target_panel, "load_alerts", real_load_alerts))
        self.track_patch(mock.patch.object(window, "load_alerts", real_load_alerts))
        self.track_patch(mock.patch.object(sf_config, "load_alerts", real_load_alerts))
        self.track_patch(mock.patch.object(window.FloatWindow, "_after_save_config"))
        self.track_patch(mock.patch.object(sf_config, "_atomic_write_json", side_effect=OSError("failed")))
        self.win.code = "sh600183"
        self.win._alerts = []  # discipline-ok(R5): 模拟运行态为空但磁盘仍有目标。
        self.win._open_target_panel()  # discipline-ok(R5): 驱动主窗真实面板接线。
        panel = self.win._target_panel  # discipline-ok(R5): 驱动面板删除与保存。
        panel._remove_one(targets.TARGET_DIR_ABOVE, 100.0)  # discipline-ok(R5): 模拟逐条删除。
        panel._apply()  # discipline-ok(R5): 模拟立即生效。
        self.assertTrue(panel.has_unsaved_changes())
        self.assertEqual([a["target"] for a in real_load_alerts(self.win.code)], [100.0])

    def _open_saved_panel(self, alerts):
        import target_panel
        config_path = os.path.join(self.tmp_path, "config.json")
        with open(config_path, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "code": "sh600183", "alerts": alerts}, stream)
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", config_path))
        self.track_patch(mock.patch.object(sf_config, "CONFIG_BACKUP_PATH", config_path + ".bak"))
        self.track_patch(mock.patch.object(target_panel, "load_alerts", real_load_alerts))
        self.track_patch(mock.patch.object(window, "load_alerts", real_load_alerts))
        self.track_patch(mock.patch.object(sf_config, "load_alerts", real_load_alerts))
        self.track_patch(mock.patch.object(window.FloatWindow, "_after_save_config"))
        self.win.code = "sh600183"
        self.win._alerts = [dict(a) for a in alerts]  # discipline-ok(R5): 建立打开面板时的运行态。
        self.win._open_target_panel()  # discipline-ok(R5): 经实际主窗接线打开面板。
        return self.win._target_panel  # discipline-ok(R5): 驱动面板删除操作。

    def test_empty_apply_preserves_target_added_before_persist(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-31", "state": "pending"}
        new = dict(old, target=110.0)
        panel = self._open_saved_panel([old])
        persist = self.win._persist_alerts  # discipline-ok(R5): 注入并发写入到真实持久化入口前。
        def concurrent_write(alerts, expected=None):
            self.assertTrue(sf_config.save_alerts([old, new], code=self.win.code))
            return persist(alerts, expected=expected)
        self.track_patch(mock.patch.object(self.win, "_persist_alerts", side_effect=concurrent_write))
        panel._remove_one("above", 100.0)  # discipline-ok(R5): 模拟删除旧目标。
        panel._apply()  # discipline-ok(R5): 提交并发删除。
        self.assertIn(110.0, [a["target"] for a in real_load_alerts(self.win.code)])

    def test_empty_apply_preserves_identical_target_recreated_on_disk(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-31", "state": "pending"}
        panel = self._open_saved_panel([old])
        self.assertTrue(sf_config.save_alerts([], code=self.win.code))
        self.assertTrue(sf_config.save_alerts([dict(old)], code=self.win.code))
        panel._remove_one("above", 100.0)  # discipline-ok(R5): 模拟旧面板删除。
        panel._apply()  # discipline-ok(R5): 提交过期操作。
        self.assertEqual(len(real_load_alerts(self.win.code)), 1)

    def test_clear_button_preserves_recreated_disk_target(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-31", "state": "pending"}
        panel = self._open_saved_panel([old])
        self.assertTrue(sf_config.save_alerts([], code=self.win.code))
        self.assertTrue(sf_config.save_alerts([dict(old)], code=self.win.code))
        panel._clear()  # discipline-ok(R5): 模拟第一次点击清空。
        panel._clear()  # discipline-ok(R5): 模拟确认清空。
        self.assertEqual(len(real_load_alerts(self.win.code)), 1)

    def test_partial_apply_preserves_recreated_disk_target(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-31", "state": "pending"}
        other = dict(old, target=110.0)
        panel = self._open_saved_panel([old, other])
        self.assertTrue(sf_config.save_alerts([other], code=self.win.code))
        self.assertTrue(sf_config.save_alerts([dict(old), other], code=self.win.code))
        panel._remove_one("above", 100.0)  # discipline-ok(R5): 删除旧面板的一条目标。
        panel._apply()  # discipline-ok(R5): 保存剩余目标。
        self.assertEqual({a["target"] for a in real_load_alerts(self.win.code)}, {100.0, 110.0})

    def test_other_stock_edit_does_not_block_current_panel_delete(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-31", "state": "pending"}
        panel = self._open_saved_panel([old])
        other = dict(old, code="sz000001", target=110.0)
        self.assertTrue(sf_config.save_alerts([other], code="sz000001"))
        panel._remove_one("above", 100.0)  # discipline-ok(R5): 模拟用户删除当前股票目标。
        panel._apply()  # discipline-ok(R5): 提交真实面板操作。
        self.assertEqual(real_load_alerts("sh600183"), [])
        self.assertEqual([a["target"] for a in real_load_alerts("sz000001")], [110.0])

    def test_other_stock_hit_does_not_block_current_delete(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-31", "state": "pending"}
        other = dict(old, code="sz000001", target=110.0)
        panel = self._open_saved_panel([old, other])
        self.win._alerts = [old]  # discipline-ok(R5): 当前窗口只持有当前股票的目标。
        self.assertTrue(sf_config.save_alerts([dict(other, state="hit")], code="sz000001"))
        panel._remove_one("above", 100.0)  # discipline-ok(R5): 删除当前股票目标。
        panel._apply()  # discipline-ok(R5): 验证其他股票命中不阻止删除。
        self.assertEqual(real_load_alerts(self.win.code), [])

    def test_backup_restore_changes_old_panel_revision(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-31", "state": "pending"}
        panel = self._open_saved_panel([old])
        self.assertTrue(sf_config.save_alerts([], code=self.win.code))
        self.assertTrue(sf_config.save_alerts([dict(old)], code=self.win.code))
        with open(sf_config.CONFIG_BACKUP_PATH, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "code": self.win.code, "alerts": [old]}, stream)
        with open(sf_config.CONFIG_PATH, "w", encoding="utf-8") as stream:
            stream.write("broken")
        sf_config.load_config()
        panel._remove_one("above", 100.0)  # discipline-ok(R5): 对旧面板执行删除。
        panel._apply()  # discipline-ok(R5): 恢复后的旧快照不得接受旧操作。
        self.assertEqual(len(real_load_alerts(self.win.code)), 1)

    def test_corrupt_config_direct_panel_apply_preserves_backup_target(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-31", "state": "pending"}
        panel = self._open_saved_panel([old])
        self.assertTrue(sf_config.save_alerts([], code=self.win.code))
        self.assertTrue(sf_config.save_alerts([dict(old)], code=self.win.code))
        with open(sf_config.CONFIG_BACKUP_PATH, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "code": self.win.code, "alerts": [old]}, stream)
        with open(sf_config.CONFIG_PATH, "w", encoding="utf-8") as stream:
            stream.write("broken")
        panel._remove_one("above", 100.0)  # discipline-ok(R5): 旧面板提交时主配置已损坏。
        panel._apply()  # discipline-ok(R5): 不得删除备份中的目标。
        with open(sf_config.CONFIG_BACKUP_PATH, encoding="utf-8") as stream:
            self.assertEqual(len(json.load(stream)["alerts"]), 1)
        self.assertEqual(len(real_load_alerts(self.win.code)), 1)

    def test_other_setting_recovery_invalidates_old_panel(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-31", "state": "pending"}
        panel = self._open_saved_panel([old])
        self.assertTrue(sf_config.save_alerts([], code=self.win.code))
        self.assertTrue(sf_config.save_alerts([dict(old)], code=self.win.code))
        with open(sf_config.CONFIG_BACKUP_PATH, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "code": self.win.code, "alerts": [old]}, stream)
        with open(sf_config.CONFIG_PATH, "w", encoding="utf-8") as stream:
            stream.write("broken")
        self.assertTrue(sf_config.save_face_guard_pref(True))
        panel._remove_one("above", 100.0)  # discipline-ok(R5): 其他设置保存后旧面板仍不得删除恢复的目标。
        panel._apply()  # discipline-ok(R5): 不得删除恢复的目标。
        self.assertEqual(len(real_load_alerts(self.win.code)), 1)

    def test_partial_apply_keeps_hit_written_by_other_instance(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-31", "state": "pending"}
        other = dict(old, target=110.0)
        panel = self._open_saved_panel([old, other])
        self.assertTrue(sf_config.save_alerts([dict(old, state="hit"), other], code=self.win.code))
        panel._remove_one("above", 110.0)  # discipline-ok(R5): 保留已触发目标。
        panel._apply()  # discipline-ok(R5): 保存前保留另一实例的命中状态。
        saved = real_load_alerts(self.win.code)
        self.assertEqual([(a["target"], a["state"]) for a in saved], [(100.0, "hit")])

    def test_empty_apply_refuses_when_targets_disabled(self):
        self.track_patch(mock.patch.object(window.FloatWindow, "_targets_enabled", return_value=False))
        self.assertFalse(self.win._apply_empty_targets([], 2))  # discipline-ok(R5): 验证禁用时的真实删除入口。

    def test_empty_apply_reports_invalid_original(self):
        self.assertFalse(self.win._apply_empty_targets([{"target": "bad"}], 2))  # discipline-ok(R5): 验证异常数据不会触发保存。

    def test_empty_apply_after_target_hit_deletes_target(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-31", "state": "pending"}
        panel = self._open_saved_panel([old])
        hit = dict(old, state="hit")
        self.assertTrue(self.win._persist_alerts([hit]))  # discipline-ok(R5): 模拟后台命中后的真实持久化。
        self.win._alerts = [hit]  # discipline-ok(R5): 同步命中后的运行状态。
        panel._remove_one("above", 100.0)  # discipline-ok(R5): 驱动逐条删除。
        panel._apply()  # discipline-ok(R5): 提交删除。
        self.assertEqual(real_load_alerts(self.win.code), [])

    def test_empty_apply_removes_distinct_historical_duplicates(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-30", "state": "pending"}
        duplicate = dict(old, session_date="2099-12-31")
        panel = self._open_saved_panel([old, duplicate])
        panel._remove_one("above", 100.0)  # discipline-ok(R5): 删除界面合并显示的槽位。
        panel._apply()  # discipline-ok(R5): 提交删除。
        self.assertEqual(real_load_alerts(self.win.code), [])

    def test_empty_apply_refuses_recreated_same_slot(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2026-10-30", "state": "pending"}
        panel = self._open_saved_panel([old])
        new = dict(old, session_date="2026-11-02")
        self.assertTrue(sf_config.save_alerts([new], code=self.win.code))
        self.win._alerts = [new]  # discipline-ok(R5): 模拟后台跨日重建同槽。
        panel._remove_one("above", 100.0)  # discipline-ok(R5): 模拟删除打开时看到的条目。
        panel._apply()  # discipline-ok(R5): 提交面板编辑。
        self.assertEqual(real_load_alerts(self.win.code), [new])
        self.assertTrue(panel.has_unsaved_changes())
        self.assertTrue(panel.status_label.text())

    def test_empty_apply_refuses_identical_target_recreated_through_window(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-31", "state": "pending"}
        panel = self._open_saved_panel([old])
        self.assertTrue(self.win._persist_alerts([]))  # discipline-ok(R5): 模拟主窗删除并保存。
        self.assertTrue(self.win._persist_alerts([dict(old)]))  # discipline-ok(R5): 同日同价重建。
        self.win._alerts = [dict(old)]  # discipline-ok(R5): 保持运行态与重建后的磁盘一致。
        panel._remove_one("above", 100.0)  # discipline-ok(R5): 删除面板打开时的旧条目。
        panel._apply()  # discipline-ok(R5): 提交陈旧快照。
        self.assertEqual(real_load_alerts(self.win.code), [old])
        self.assertTrue(panel.has_unsaved_changes())

    def test_empty_apply_refuses_divergent_unrelated_disk_and_live(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2026-11-02", "state": "pending"}
        panel = self._open_saved_panel([old])
        stale = dict(old, target=110.0)
        self.assertTrue(sf_config.save_alerts([old, stale], code=self.win.code))
        panel._remove_one("above", 100.0)  # discipline-ok(R5): 删除面板原条目。
        panel._apply()  # discipline-ok(R5): 提交时不得复活磁盘多出的旧条目。
        self.assertEqual(real_load_alerts(self.win.code), [old, stale])
        self.assertTrue(panel.has_unsaved_changes())
        self.assertTrue(panel.status_label.text())

    def test_empty_apply_rejects_changed_same_slot_and_unrelated_state(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-31", "state": "pending"}
        panel = self._open_saved_panel([old])
        changed = dict(old, session_date="2099-12-30")
        self.assertTrue(sf_config.save_alerts([changed], code=self.win.code))
        self.win._alerts = [changed]  # discipline-ok(R5): 模拟同价位的新目标。
        self.assertFalse(self.win._apply_empty_targets_locked([old], 2))  # discipline-ok(R5): 验证事务内部的身份比较。
        other = dict(old, target=110.0)
        self.assertTrue(sf_config.save_alerts([old, other], code=self.win.code))
        self.win._alerts = [old]  # discipline-ok(R5): 构造磁盘与内存的无关目标分歧。
        self.assertFalse(self.win._apply_empty_targets_locked([old], 2))  # discipline-ok(R5): 不得抹除其他目标。
        self.assertEqual([a["target"] for a in real_load_alerts(self.win.code)], [100.0, 110.0])
        self.assertIsNotNone(panel)

    def test_panel_transaction_reports_lock_failure(self):
        with mock.patch.object(sf_config, "config_transaction", side_effect=TimeoutError("busy")), \
                mock.patch.object(window, "log_line") as log:
            self.assertFalse(self.win._panel_targets_transaction(self.win.code, None, lambda _: True))  # discipline-ok(R5): 验证真实事务异常出口。
            self.assertFalse(self.win._apply_empty_targets([], 2))  # discipline-ok(R5): 验证删除事务异常出口。
        self.assertEqual(log.call_count, 2)

    def test_show_config_unavailable_uses_existing_error_indicator(self):
        with mock.patch.object(self.win, "_after_save_config") as feedback:
            self.win._show_config_unavailable()  # discipline-ok(R5): 验证定时器对应的通知出口。
        feedback.assert_called_once_with(False)

    def test_locked_empty_apply_rejects_disabled_and_invalid_data(self):
        with mock.patch.object(window.FloatWindow, "_targets_enabled", return_value=False):
            self.assertFalse(self.win._apply_empty_targets_locked([], 2))  # discipline-ok(R5): 禁用时不可删除。
        with mock.patch.object(window, "log_line") as log:
            self.assertFalse(self.win._apply_empty_targets_locked([{"target": "bad"}], 2))  # discipline-ok(R5): 无效目标不能保存。
        self.assertTrue(log.called)

    def test_received_quote_with_unreadable_revision_does_not_notify(self):
        self.win._alerts = [{"target": 100}]  # discipline-ok(R5): 模拟目标存在。
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        with mock.patch.object(sf_config, "alerts_revision", side_effect=OSError("unreadable")), \
                mock.patch.object(window, "_notify_deliver") as notify:
            self.win._evaluate_received_quote({"code": self.win.code, "price": "101"})  # discipline-ok(R5): 验证读取版本失败。
        notify.assert_not_called()

    def test_unreadable_initial_revision_requeues_received_quote(self):
        self.win._alerts = [{"target": 100}]  # discipline-ok(R5): 模拟目标存在。
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        quote = {"code": self.win.code, "price": "101"}
        with mock.patch.object(sf_config, "alerts_revision", side_effect=OSError("unreadable")):
            self.win._evaluate_received_quote(quote)  # discipline-ok(R5): 首次版本读取失败时行情仍须留待重试。
        self.assertEqual(self.win._target_ticks.take(self.win.code), [quote])  # discipline-ok(R5): 检查实际重试缓冲是否保留行情。

    def test_unversioned_retry_does_not_trigger_recreated_target(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        with mock.patch.object(sf_config, "alerts_revision", side_effect=OSError("unreadable")):
            self.win._evaluate_received_quote({"code": self.win.code, "price": "101"})  # discipline-ok(R5): 行情到达时版本不可读。
        self.assertTrue(sf_config.save_alerts([], code=self.win.code))
        self.assertTrue(sf_config.save_alerts([old], code=self.win.code))
        self.win._target_retry_blocked = False  # discipline-ok(R5): 模拟下一次轮询。
        for tick in self.win._target_ticks.take(self.win.code):  # discipline-ok(R5): 重放版本未知的行情。
            self.win._evaluate_received_quote(tick)  # discipline-ok(R5): 不得触发重建后的目标。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")

    def test_poll_does_not_replay_unversioned_quote_against_recreated_target(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        quote = {"code": self.win.code, "price": "101"}
        self.win.running = True
        self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 驱动生产者同时放入行情缓冲和显示队列。
        with mock.patch.object(sf_config, "alerts_revision", side_effect=OSError("unreadable")) as revision:
            self.win._poll()  # discipline-ok(R5): 实际轮询消费两个队列。
            self.assertTrue(revision.called)
        self.assertTrue(sf_config.save_alerts([], code=self.win.code))
        self.assertTrue(sf_config.save_alerts([old], code=self.win.code))
        self.win._poll()  # discipline-ok(R5): 再次轮询不得把旧行情交给重建目标。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")

    def test_old_unknown_revision_warns_and_preserves_queue(self):
        import sf_quote
        from PyQt6.QtWidgets import QToolTip
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离持久队列。
        quote = sf_quote.QuoteData({"code": "sh600183", "price": "101"}, "20261015100000")
        quote.received_at = now
        quote.target_revision = None
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.add_ordered("sh600183", quote)
        self.win.code = "sh600519"
        self.win.running = True
        self.ensure_qapp().processEvents()
        with mock.patch.object(QToolTip, "showText") as show:
            self.win._poll()  # discipline-ok(R5): 切股后处理未知版本的队首。
            self.ensure_qapp().processEvents()
        self.assertTrue(any("目标价" in str(call.args[1]) and "暂停" in str(call.args[1])
                            for call in show.call_args_list))
        with TargetTickStore(path) as store:
            self.assertEqual(len(store.read("sh600183")), 1)

    def test_unknown_revision_after_lock_recovery_cannot_trigger_new_target(self):
        import sf_quote
        from contextlib import contextmanager
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 110.0, now=now)
        old["code"] = "sh600183"
        new = dict(old, target=100.0)
        self._open_saved_panel([old])
        path = os.path.join(self.tmp_path, "unknown-after-lock.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离持久队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        quote.received_at = now
        @contextmanager
        def denied(**kwargs):
            raise PermissionError("lock unavailable")
            yield
        with mock.patch.object(sf_config, "target_tick_transaction", denied), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 接收版本未知行情。
        self.assertTrue(sf_config.save_alerts([new], code=self.win.code))
        self.win._alerts = [new]  # discipline-ok(R5): 模拟新目标已生效。
        with mock.patch.object(window, "_notify_deliver") as deliver:
            self.win._poll()  # discipline-ok(R5): 锁恢复后实际消费队列。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")
        self.assertEqual(sf_config.load_target_notification_outbox(), [])
        deliver.assert_not_called()
        with TargetTickStore(path) as store:
            rows = store.read(self.win.code)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].quote.target_revision)

    def test_persistent_unknown_revision_is_not_acknowledged(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        quote.received_at = now
        with mock.patch.object(sf_config, "alerts_revision", side_effect=OSError("unavailable")):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 接收时版本未知。
        self.win._poll()  # discipline-ok(R5): 不得将未知版本当成功确认。
        with TargetTickStore(path) as store:
            self.assertEqual(len(store.read(self.win.code)), 1)
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")

    def test_persistent_unknown_revision_is_retained_when_no_current_alerts(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        self._open_saved_panel([])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离队列。
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        quote.received_at = now
        quote.target_revision = None
        ticks.add_ordered(self.win.code, quote)
        self.win._poll()  # discipline-ok(R5): 当前无目标不能证明历史行情无目标。
        with TargetTickStore(path) as store:
            self.assertEqual(len(store.read(self.win.code)), 1)

    def test_persistent_plain_quote_does_not_trigger_new_target(self):
        from sf_quote_delivery import TargetTickBuffer
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 110.0, now=now)
        old["code"] = "sh600183"
        new = dict(old, target=100.0)
        self._open_saved_panel([old])
        self.win._target_ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))  # discipline-ok(R5): 使用隔离队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        self.win._q_put(("quote", (self.win.code, {"code": self.win.code, "price": "101"})))  # discipline-ok(R5): 模拟旧目标期间接收普通行情。
        self.assertTrue(sf_config.save_alerts([new], code=self.win.code))
        self.win._alerts = [new]  # discipline-ok(R5): 模拟新目标生效。
        self.win._poll()  # discipline-ok(R5): 旧行情不能触发新目标。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")

    def test_queued_quote_does_not_trigger_target_created_later(self):
        import sf_quote
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 110.0, now=now)
        old["code"] = "sh600183"
        new = dict(old, target=100.0)
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.track_patch(mock.patch.object(sf_quote, "_beijing_now", return_value=now))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 模拟先接收真实行情。
        self.assertTrue(sf_config.save_alerts([new], code=self.win.code))
        self.win._alerts = [new]  # discipline-ok(R5): 模拟用户创建新目标。
        self.win._poll()  # discipline-ok(R5): 旧行情不得触发新目标。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")

    def test_quote_received_during_revision_failure_is_displayed_without_alert(self):
        import sf_quote
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(sf_quote, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        with mock.patch.object(sf_config, "alerts_revision", side_effect=OSError("unreadable")), \
                mock.patch.object(self.win, "_apply_quote") as display, \
                mock.patch.object(window, "_notify_deliver") as notify:
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 版本读取失败时接收真实行情。
            self.win._poll()  # discipline-ok(R5): 验证行情仍能显示且不触发提醒。
        display.assert_called_once_with(quote)
        notify.assert_not_called()
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")

    def test_tick_with_unavailable_config_preserves_memory(self):
        old = {"code": "sh600183", "direction": "above", "target": 100.0,
               "session_date": "2099-12-31", "state": "pending"}
        self._open_saved_panel([old])
        with mock.patch.object(sf_config, "alerts_revision", return_value=(0, 0, ())), \
                mock.patch.object(sf_config, "config_unavailable", return_value=True), \
                mock.patch.object(window, "_notify_deliver") as notify:
            self.win._evaluate_targets(101.0)  # discipline-ok(R5): 不可用配置不应清空目标。
        self.assertEqual(self.win._alerts, [old])  # discipline-ok(R5): 验证窗口仍保留原目标。
        notify.assert_not_called()

    def test_unavailable_config_after_revision_retries_received_quote(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        revision = sf_config.alerts_revision(self.win.code)
        quote = {"code": self.win.code, "price": "101"}
        with mock.patch.object(sf_config, "alerts_revision", return_value=revision), \
                mock.patch.object(sf_config, "config_unavailable", return_value=True):
            self.win._evaluate_received_quote(quote)  # discipline-ok(R5): 版本取得后配置不可用。
        pending = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 核验待重试行情。
        self.assertEqual(pending, [quote])
        self.win._target_retry_blocked = False  # discipline-ok(R5): 开始下一轮。
        self.win._evaluate_received_quote(pending[0])  # discipline-ok(R5): 配置恢复后重试。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_tick_preserves_newer_disk_target(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        new = targets.make_alert(targets.TARGET_DIR_ABOVE, 110.0, now=now)
        old["code"] = new["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_start_rainbow"))
        self.assertTrue(sf_config.save_alerts([old, new], code=self.win.code))
        self.win._evaluate_targets(101.0)  # discipline-ok(R5): 驱动旧运行态收到行情。
        saved = real_load_alerts(self.win.code)
        self.assertEqual([(a["target"], a["state"]) for a in saved],
                         [(100.0, "hit"), (110.0, "pending")])

    def test_tick_does_not_restore_deleted_target(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        notifications = []
        self.track_patch(mock.patch.object(window, "_notify_deliver", side_effect=lambda *a, **k: notifications.append(a)))
        self.assertTrue(sf_config.save_alerts([], code=self.win.code))
        self.win._evaluate_targets(101.0)  # discipline-ok(R5): 模拟旧运行态收到行情。
        self.assertEqual(real_load_alerts(self.win.code), [])
        self.assertEqual(notifications, [])

    def test_tick_does_not_repeat_disk_hit(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        notifications = []
        self.track_patch(mock.patch.object(window, "_notify_deliver", side_effect=lambda *a, **k: notifications.append(a)))
        self.assertTrue(sf_config.save_alerts([dict(old, state="hit")], code=self.win.code))
        self.win._evaluate_targets(101.0)  # discipline-ok(R5): 模拟旧运行态收到行情。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")
        self.assertEqual(notifications, [])

    def test_tick_does_not_restore_expired_disk_target(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        expired_alert = dict(old, target=90.0, session_date="2026-10-14")
        self._open_saved_panel([old, expired_alert])
        self.win._alerts = [old]  # discipline-ok(R5): 模拟启动清理失败后保留未过期目标。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.win._evaluate_targets(101.0)  # discipline-ok(R5): 驱动真实行情命中。
        self.assertEqual([a["target"] for a in real_load_alerts(self.win.code)], [100.0])
        self.assertEqual([a["target"] for a in self.win._alerts], [100.0])  # discipline-ok(R5): 验证运行态不复活。

    def test_locked_config_does_not_block_ui_and_retries_crossing(self):
        import threading
        import time
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        held, release = threading.Event(), threading.Event()
        def hold_config():
            with sf_config.config_transaction():
                held.set()
                release.wait(3)
        worker = threading.Thread(target=hold_config)
        worker.start()
        try:
            self.assertTrue(held.wait(2))
            started = time.monotonic()
            self.win._evaluate_received_quote({"code": self.win.code, "price": "101"})  # discipline-ok(R5): 模拟锁竞争时的真实行情入口。
            self.assertLess(time.monotonic() - started, 0.25)
        finally:
            release.set()
            worker.join(3)
        ticks = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 短暂越线不得丢失。
        self.assertEqual([q["price"] for q in ticks], ["101"])
        self.win._target_retry_blocked = False  # discipline-ok(R5): 模拟下一拍开始。
        self.win._evaluate_received_quote(ticks[0])  # discipline-ok(R5): 下一拍重试实际行情。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_corrupt_config_does_not_discard_runtime_target(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        with open(sf_config.CONFIG_PATH, "w", encoding="utf-8") as stream:
            stream.write("{")
        self.win._evaluate_targets(101.0)  # discipline-ok(R5): 配置损坏时驱动行情。
        self.assertEqual([a["target"] for a in self.win._alerts], [100.0])  # discipline-ok(R5): 不能把读错当成空配置。

    def test_unexpected_evaluation_error_retries_crossing(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        quote = {"code": self.win.code, "price": "101"}
        with mock.patch.object(window, "evaluate_tick", side_effect=RuntimeError("evaluation failure")):
            self.win._evaluate_received_quote(quote)  # discipline-ok(R5): 模拟命中计算失败。
        pending = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 真实待重试队列。
        self.assertEqual(pending, [quote])
        self.win._target_retry_blocked = False  # discipline-ok(R5): 模拟下一轮。
        self.win._evaluate_received_quote(pending[0])  # discipline-ok(R5): 恢复后重试原行情。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_failed_write_requeues_short_crossing(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        with mock.patch.object(sf_config, "_atomic_write_json", side_effect=OSError("disk busy")):
            self.win._evaluate_received_quote({"code": self.win.code, "price": "101"})  # discipline-ok(R5): 模拟越线时保存失败。
        ticks = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 恢复后仍需处理短暂越线。
        self.assertEqual([q["price"] for q in ticks], ["101"])

    def test_crossing_with_none_name_uses_current_code(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.win.quote = {"name": "旧名称", "code": "600183"}  # discipline-ok(R5): 模拟上一条行情。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        with mock.patch.object(window, "_notify_deliver", return_value="pending"):
            self.win._evaluate_received_quote({"code": self.win.code, "name": None, "price": "101"})  # discipline-ok(R5): 驱动异常名称行情。
        events = sf_config.load_target_notification_outbox()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "sh600183 目标价")

    def test_crossing_notification_uses_incoming_quote_name(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.win.quote = {"name": "旧名称", "code": "600183"}
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        with mock.patch.object(window, "_notify_deliver", return_value="pending") as deliver:
            self.win._evaluate_received_quote({"code": self.win.code, "name": "新名称", "price": "101"})  # discipline-ok(R5): 验证实时行情的通知名称。
        events = sf_config.load_target_notification_outbox()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "新名称 目标价")
        self.assertEqual(deliver.call_args.args[0], "新名称 目标价")
        self.assertEqual(deliver.call_args.args[3], events[0]["semantic_key"])

    def test_crossing_persists_notification_before_delivery_confirmation(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        with mock.patch.object(window, "_notify_deliver", return_value="pending"):
            self.win._evaluate_received_quote({"code": self.win.code, "price": "101"})  # discipline-ok(R5): 驱动真实命中事务。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")
        events = sf_config.load_target_notification_outbox()
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["id"])
        self.assertTrue(events[0]["body"])

    def test_crossing_notification_is_acknowledged_only_on_success(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        callbacks = []
        def deliver(*args, **kwargs):
            callbacks.append(kwargs["on_complete"])
            return "un"
        with mock.patch.object(window, "_notify_deliver", side_effect=deliver):
            self.win._evaluate_received_quote({"code": self.win.code, "price": "101"})  # discipline-ok(R5): 触发实际通知。
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1)
        callbacks[0](True)
        self.win._drain_target_notification_outbox()  # discipline-ok(R5): 主线程处理投递确认。
        self.assertEqual(sf_config.load_target_notification_outbox(), [])

    def test_success_callback_schedules_prompt_main_thread_ack(self):
        import threading
        self._open_saved_panel([])
        self.win._target_notification_initial_timer.stop()  # discipline-ok(R5): 排除启动定时器造成的假阳性。
        event = {"id": "prompt-ack", "code": "other", "title": "old stock",
                 "body": "crossed", "semantic_key": "prompt-ack"}
        self.assertTrue(sf_config.save_alerts([], self.win.code, outbox_events=[event]))
        callbacks = []
        def deliver(*args, **kwargs):
            callbacks.append(kwargs["on_complete"])
            return "un"
        with mock.patch.object(window, "_notify_deliver", side_effect=deliver):
            self.win._drain_target_notification_outbox()  # discipline-ok(R5): 启动待发送通知。
            worker = threading.Thread(target=lambda: callbacks[0](True))
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.ensure_qapp().processEvents()
        self.assertEqual(sf_config.load_target_notification_outbox(), [])

    def test_failed_notification_callback_keeps_pending_and_waits_before_retry(self):
        self._open_saved_panel([])
        event = {"id": "pending-fail", "code": "other", "title": "old stock",
                 "body": "crossed", "semantic_key": "pending-fail"}
        self.assertTrue(sf_config.save_alerts([], self.win.code, outbox_events=[event]))
        callbacks = []
        def deliver(*args, **kwargs):
            callbacks.append(kwargs["on_complete"])
            return "un"
        with mock.patch.object(window, "_notify_deliver", side_effect=deliver), \
                mock.patch.object(window.time, "monotonic", return_value=100.0):
            self.win._drain_target_notification_outbox()  # discipline-ok(R5): 触发首次发送。
            callbacks[0](False)
            self.win._drain_target_notification_outbox()  # discipline-ok(R5): 失败不能立即反复发送。
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(sf_config.load_target_notification_outbox(), [event])
        with mock.patch.object(window, "_notify_deliver", side_effect=deliver), \
                mock.patch.object(window.time, "monotonic", return_value=131.0):
            self.win._drain_target_notification_outbox()  # discipline-ok(R5): 退避到期后重试。
        self.assertEqual(len(callbacks), 2)

    def test_outbox_retry_runs_without_targets_or_new_quotes(self):
        self._open_saved_panel([])
        event = {"id": "pending-startup", "code": "other", "title": "old stock",
                 "body": "crossed", "semantic_key": "pending-startup"}
        self.assertTrue(sf_config.save_alerts([], self.win.code, outbox_events=[event]))
        delivered = []
        with mock.patch.object(window, "_notify_deliver", side_effect=lambda *args, **kwargs: delivered.append(args)):
            self.win._target_notification_retry_timer.timeout.emit()  # discipline-ok(R5): 模拟启动后的定时恢复。
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][0], "old stock")
        self.assertEqual(sf_config.load_target_notification_outbox(), [event])

    def test_uncertain_old_stock_commit_does_not_swallow_new_stock_crossing(self):
        import stat
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        real_fsync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory sync failed")
            return real_fsync(fd)
        with mock.patch.object(sf_config.os, "fsync", side_effect=fail_directory):
            self.win._evaluate_received_quote({"code": "sh600183", "price": "101"})  # discipline-ok(R5): 旧股票写入结果不确定。
        new = targets.make_alert(targets.TARGET_DIR_ABOVE, 200.0, now=now)
        new["code"] = "sh600519"
        self.assertTrue(sf_config.save_alerts([new], code="sh600519"))
        self.win.code = "sh600519"
        self.win._alerts = [new]  # discipline-ok(R5): 模拟切股后的运行状态。
        self.win._target_retry_blocked = False  # discipline-ok(R5): 模拟下一轮。
        self.win._evaluate_received_quote({"code": "sh600519", "price": "201"})  # discipline-ok(R5): 新股票首次越线。
        self.assertEqual(real_load_alerts("sh600519")[0]["state"], "hit")

    def test_directory_sync_failure_does_not_lose_crossing_notification(self):
        import stat
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        notifications = []
        self.track_patch(mock.patch.object(window, "_notify_deliver", side_effect=lambda *a, **k: notifications.append(a)))
        real_fsync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory sync failed")
            return real_fsync(fd)
        with mock.patch.object(sf_config.os, "fsync", side_effect=fail_directory):
            self.win._evaluate_received_quote({"code": self.win.code, "price": "101"})  # discipline-ok(R5): 文件替换后目录同步失败。
        self.win._target_retry_blocked = False  # discipline-ok(R5): 模拟下一轮消费。
        for tick in self.win._target_ticks.take(self.win.code):  # discipline-ok(R5): 重试原始行情。
            self.win._evaluate_received_quote(tick)  # discipline-ok(R5): 检查最终通知。
        self.ensure_qapp().processEvents()
        self.assertEqual(len(notifications), 1)

    def test_earlier_failed_crossing_cannot_be_overtaken(self):
        seen = []
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        def evaluate(price):
            seen.append(price)
            return False if price == 90.0 and seen.count(90.0) == 1 else None
        with mock.patch.object(self.win, "_evaluate_targets", side_effect=evaluate):
            self.win._evaluate_received_quote({"code": self.win.code, "price": "90"})  # discipline-ok(R5): 模拟首笔失败。
            self.win._evaluate_received_quote({"code": self.win.code, "price": "110"})  # discipline-ok(R5): 后一笔不得越过前一笔。
        self.assertNotIn(110.0, seen)

    def test_expiry_cleanup_preserves_newer_disk_target(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        old["session_date"] = "2026-10-14"
        new = dict(old, target=110.0, session_date="2026-10-15")
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_arm_clear_timer"))
        self.assertTrue(sf_config.save_alerts([old, new], code=self.win.code))
        self.win._clear_expired()  # discipline-ok(R5): 驱动旧运行态的到期清理。
        self.assertEqual([a["target"] for a in real_load_alerts(self.win.code)], [110.0])

    def test_stock_adoption_does_not_erase_concurrent_target(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        old["session_date"] = "2026-10-14"
        new = dict(old, target=110.0, session_date="2026-10-15")
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_arm_clear_timer"))
        persist = self.win._persist_alerts  # discipline-ok(R5): 在保存之前插入并发写入。
        def concurrent_save(alerts, **kwargs):
            self.assertTrue(sf_config.save_alerts([old, new], code=self.win.code))
            return persist(alerts, **kwargs)
        with mock.patch.object(self.win, "_persist_alerts", side_effect=concurrent_save):
            self.win._adopt_targets_for(self.win.code)  # discipline-ok(R5): 驱动切股接管。
        self.assertIn(110.0, [a["target"] for a in real_load_alerts(self.win.code)])

    def test_startup_uncertain_commit_waits_for_sync_before_delivery(self):
        import stat
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(self.win, "_current_price", return_value=101.0))
        self.track_patch(mock.patch.object(self.win, "_prefetch_calendar"))
        real_fsync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory sync failed")
            return real_fsync(fd)
        with mock.patch.object(window, "_notify_deliver", return_value="pending") as deliver:
            with mock.patch.object(sf_config.os, "fsync", side_effect=fail_directory):
                real_init_targets(self.win)  # discipline-ok(R5): 模拟替换成功但目录同步失败。
                self.win._target_notification_retry_timer.timeout.emit()  # discipline-ok(R5): 同步仍失败时不得发送。
                deliver.assert_not_called()
            self.win._target_notification_retry_timer.timeout.emit()  # discipline-ok(R5): 同步恢复后发送。
            deliver.assert_called_once()
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1)

    def test_startup_reminder_retries_after_initial_save_failure(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(self.win, "_current_price", return_value=101.0))
        self.track_patch(mock.patch.object(self.win, "_prefetch_calendar"))
        with mock.patch.object(sf_config, "_atomic_write_json", side_effect=OSError("busy")):
            real_init_targets(self.win)  # discipline-ok(R5): 启动恢复时保存失败。
        with mock.patch.object(window, "_notify_deliver", return_value="pending"):
            self.win._target_notification_retry_timer.timeout.emit()  # discipline-ok(R5): 恢复后定时重试。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1)

    def test_startup_recovery_defers_when_target_tick_lock_is_busy(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        expired = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now - timedelta(days=1))
        expired["code"] = "sh600183"
        expired["session_date"] = "2026-10-14"
        self._open_saved_panel([expired])
        from sf_quote_delivery import TargetTickBuffer  # discipline-ok(R5): 注入隔离持久队列验证启动锁。
        self.win._target_ticks = TargetTickBuffer(  # discipline-ok(R5): 注入隔离持久队列验证启动恢复。
            persistent_path=os.path.join(self.tmp_path, "startup-ticks.sqlite"))
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_prefetch_calendar"))
        with mock.patch.object(sf_config.fcntl, "flock",
                               side_effect=BlockingIOError("target tick lock busy")):
            real_init_targets(self.win)  # discipline-ok(R5): 启动恢复被真实目标行情锁阻塞。
        self.assertEqual(real_load_alerts(self.win.code), [expired])
        self.assertEqual(self.win._alerts, [expired])  # discipline-ok(R5): 核验锁忙时运行态保留。
        self.assertTrue(self.win._clear_timer.isActive())  # discipline-ok(R5): 核验锁忙时已安排启动重试。

    def test_startup_lock_busy_retries_recovery_after_lock_release(self):
        from sf_quote_delivery import TargetTickBuffer
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.win._target_ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "startup-retry.sqlite"))  # discipline-ok(R5): 隔离持久队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(self.win, "_current_price", return_value=101.0))
        self.track_patch(mock.patch.object(window.FloatWindow, "_prefetch_calendar"))
        with mock.patch.object(sf_config.fcntl, "flock", side_effect=BlockingIOError("busy")):
            real_init_targets(self.win)
        with mock.patch.object(window, "_notify_deliver", return_value="pending"), \
                mock.patch.object(window.FloatWindow, "_init_targets", new=real_init_targets):
            self.win._clear_timer.timeout.emit()  # discipline-ok(R5): 触发真实定时器接线。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1)

    def test_startup_pending_queue_retries_recovery_when_drained(self):
        from sf_quote_delivery import TargetTickBuffer
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "startup-pending.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(self.win, "_current_price", return_value=101.0))
        self.track_patch(mock.patch.object(window.FloatWindow, "_prefetch_calendar"))
        with mock.patch.object(ticks, "has_pending", return_value=True):
            real_init_targets(self.win)
        with mock.patch.object(window, "_notify_deliver", return_value="pending"), \
                mock.patch.object(window.FloatWindow, "_init_targets", new=real_init_targets):
            self.win._clear_timer.timeout.emit()  # discipline-ok(R5): 触发真实定时器接线。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1)

    def test_deferred_pending_queue_switch_cleans_expired_without_notification(self):
        from sf_quote_delivery import TargetTickBuffer
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now - timedelta(days=1))
        alert.update(code="sh600183", session_date="2026-10-14")
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "pending-switch.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离队列模拟待处理行情。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_prefetch_calendar"))
        self.track_patch(mock.patch.object(self.win, "_force_fetch"))
        with mock.patch.object(ticks, "has_pending", return_value=True):
            real_init_targets(self.win)
        with mock.patch.object(window.FloatWindow, "_init_targets", new=real_init_targets), \
                mock.patch.object(window, "_notify_deliver") as deliver:
            self.assertTrue(self.win.apply_stock("sz000001"))
            self.win._clear_timer.timeout.emit()  # discipline-ok(R5): 模拟切股后定时清理。
            self.assertEqual(self.win._target_startup_deferred_code, "sh600183")  # discipline-ok(R5): 检查延期股票未被切股覆盖。
            self.assertTrue(self.win.apply_stock("sh600183"))
            self.win._clear_timer.timeout.emit()  # discipline-ok(R5): 模拟切回后的定时清理。
        self.assertEqual(real_load_alerts("sh600183"), [])
        self.assertEqual(sf_config.load_target_notification_outbox(), [])
        deliver.assert_not_called()

    def test_deferred_startup_real_stock_switch_recovers(self):
        from sf_quote_delivery import TargetTickBuffer
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.win._target_ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "real-switch.sqlite"))  # discipline-ok(R5): 注入隔离队列模拟启动恢复。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_prefetch_calendar"))
        self.track_patch(mock.patch.object(self.win, "_force_fetch"))
        with mock.patch.object(sf_config.fcntl, "flock", side_effect=BlockingIOError("busy")):
            real_init_targets(self.win)
        with mock.patch.object(window.FloatWindow, "_init_targets", new=real_init_targets), \
                mock.patch.object(window, "_notify_deliver", return_value="pending"):
            self.assertTrue(self.win.apply_stock("sz000001"))
            self.win._clear_timer.timeout.emit()  # discipline-ok(R5): 模拟切股后的定时器触发。
            self.assertEqual(self.win._target_startup_deferred_code, "sh600183")  # discipline-ok(R5): 检查延期股票仍被保留。
            self.assertTrue(self.win.apply_stock("sh600183"))
            self.assertTrue(self.win._clear_timer.isActive())  # discipline-ok(R5): 检查恢复定时器已启动。
            with mock.patch.object(self.win, "_current_price", return_value=101.0):
                self.win._clear_timer.timeout.emit()  # discipline-ok(R5): 模拟恢复定时器触发。
        self.assertEqual(real_load_alerts("sh600183")[0]["state"], "hit")
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1)

    def test_deferred_startup_survives_switch_away_and_back(self):
        from sf_quote_delivery import TargetTickBuffer
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.win._target_ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "switch-retry.sqlite"))  # discipline-ok(R5): 隔离持久队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(self.win, "_current_price", return_value=101.0))
        self.track_patch(mock.patch.object(window.FloatWindow, "_prefetch_calendar"))
        with mock.patch.object(sf_config.fcntl, "flock", side_effect=BlockingIOError("busy")):
            real_init_targets(self.win)
        self.win.code = "sz000001"
        self.win._clear_timer.timeout.emit()  # discipline-ok(R5): 模拟切股后定时器到期。
        self.win.code = "sh600183"
        with mock.patch.object(window, "_notify_deliver", return_value="pending"), \
                mock.patch.object(window.FloatWindow, "_init_targets", new=real_init_targets):
            self.win._clear_timer.timeout.emit()  # discipline-ok(R5): 模拟切回后定时器到期。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1)

    def test_adopting_deferred_stock_schedules_startup_retry(self):
        from sf_quote_delivery import TargetTickBuffer
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.win._target_ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "adopt-retry.sqlite"))  # discipline-ok(R5): 注入隔离队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.win._target_startup_deferred_code = self.win.code  # discipline-ok(R5): 模拟启动恢复被延期。
        self.win._clear_timer.stop()  # discipline-ok(R5): 模拟切股后旧定时器已结束。
        self.win._adopt_targets_for(self.win.code)  # discipline-ok(R5): 驱动真实股票接管入口。
        self.assertTrue(self.win._clear_timer.isActive())  # discipline-ok(R5): 验证恢复已重新安排。
        self.assertEqual(self.win._clear_timer.interval(), 1000)  # discipline-ok(R5): 验证延期恢复使用短重试间隔。

    def test_deferred_recovery_without_price_waits_for_real_crossing(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "no-price.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离持久队列。
        self.win._target_startup_deferred_code = self.win.code  # discipline-ok(R5): 模拟待恢复股票。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_prefetch_calendar"))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_current_price", return_value=0))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        with mock.patch.object(window.FloatWindow, "_init_targets", new=real_init_targets), \
                mock.patch.object(window, "_notify_deliver", return_value="pending"):
            self.win._clear_timer.timeout.emit()  # discipline-ok(R5): 恢复时没有可信现价。
            self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")
            self.assertEqual(sf_config.load_target_notification_outbox(), [])
            quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
            quote.received_at = now
            with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 接收真实队列行情。
            self.win._poll()  # discipline-ok(R5): 消费行情并提交命中。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1)

    def test_startup_recovery_persists_reminder_before_delivery(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(self.win, "_current_price", return_value=101.0))
        self.track_patch(mock.patch.object(self.win, "_prefetch_calendar"))
        with mock.patch.object(window, "_notify_deliver", return_value="pending"):
            real_init_targets(self.win)  # discipline-ok(R5): 启动恢复已越线目标。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1)


    def test_startup_recovery_does_not_erase_concurrent_target(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        old["session_date"] = "2026-10-14"
        new = dict(old, target=110.0, session_date="2026-10-15")
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_arm_clear_timer"))
        self.track_patch(mock.patch.object(window.FloatWindow, "_prefetch_calendar"))
        persist = self.win._persist_alerts  # discipline-ok(R5): 在恢复写盘前插入并发保存。
        def concurrent_save(alerts, **kwargs):
            self.assertTrue(sf_config.save_alerts([old, new], code=self.win.code))
            return persist(alerts, **kwargs)
        with mock.patch.object(self.win, "_persist_alerts", side_effect=concurrent_save):
            real_init_targets(self.win)  # discipline-ok(R5): 执行真实启动恢复。
        self.assertIn(110.0, [a["target"] for a in real_load_alerts(self.win.code)])

    def test_startup_recovery_defers_when_the_queue_cannot_be_read(self):
        """跨进程队列读不出时按"可能还有待重放的行情"处理：保留目标、延后清理，不能静默丢弃。

        这条支路一旦判成"没有 pending"，启动那一下就会把当天还没投递的目标价
        当过期数据清掉 —— 用户看到的是"提醒没来，铃铛也空了"。
        """
        from types import SimpleNamespace
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_prefetch_calendar"))
        jammed = SimpleNamespace(persistent=True,
                                has_pending=mock.Mock(side_effect=OSError("queue unreadable")))
        self.win._target_ticks = jammed  # discipline-ok(R5): 启动恢复只认运行态队列句柄
        with mock.patch.object(window, "log_line") as logged, \
                mock.patch.object(self.win, "_request_notify_auth") as auth:
            real_init_targets(self.win)  # discipline-ok(R5): 执行真实启动恢复
        phrases = [call.args[0] for call in logged.call_args_list]
        self.assertTrue(any("target startup queue read failed" in text for text in phrases), phrases)
        jammed.has_pending.assert_called_once_with(self.win.code)
        auth.assert_called_once()
        self.assertEqual(self.win._target_startup_deferred_code, self.win.code)  # discipline-ok(R5): 延后标记只写在运行态
        self.assertEqual([a["target"] for a in self.win._alerts], [100.0])  # discipline-ok(R5): 运行态目标没有 setter
        self.assertTrue(self.win._clear_timer.isActive())  # discipline-ok(R5): 延后清理由这张表驱动，必须断言它的活性

    def test_an_unreadable_publisher_entry_still_protects_the_stock(self):
        """读不出内容的发布登记按"还活着"算：宁可多挡一次清理，也不能把别人的行情当无主删掉。

        这里用"同名目录"造出 open() 的 OSError（不是 FileNotFoundError）：
        读不出 PID 不等于没有进程在发布。
        """
        config_path = self._isolated_config()
        directory = config_path + ".target-tick-publishers"
        os.makedirs(directory, exist_ok=True)
        entry = os.path.join(directory, "sh600183.undecodable")
        os.mkdir(entry)
        self.assertTrue(sf_config.target_tick_publisher_active("sh600183"))
        self.assertTrue(os.path.isdir(entry))
        self.assertEqual(self._lease_count(), 1)

    def test_crossing_received_before_break_is_retried_after_break_starts(self):
        now = datetime(2026, 10, 15, 11, 29, 59, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        with mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True), \
                mock.patch.object(sf_config, "_atomic_write_json", side_effect=OSError("disk busy")):
            self.win._evaluate_received_quote({"code": self.win.code, "price": "101"})  # discipline-ok(R5): 午休前越线写盘失败。
        self.win._target_retry_blocked = False  # discipline-ok(R5): 模拟下一次轮询。
        ticks = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 取出待重试行情。
        with mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=False):
            self.win._evaluate_received_quote(ticks[0])  # discipline-ok(R5): 午休开始后重试已接收行情。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_retry_does_not_trigger_recreated_target(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        with mock.patch.object(sf_config, "_atomic_write_json", side_effect=OSError("disk busy")):
            self.win._evaluate_received_quote({"code": self.win.code, "price": "101"})  # discipline-ok(R5): 原目标越线但写盘失败。
        self.assertTrue(sf_config.save_alerts([], code=self.win.code))
        self.assertTrue(sf_config.save_alerts([old], code=self.win.code))
        self.win._target_retry_blocked = False  # discipline-ok(R5): 模拟下一次轮询。
        for tick in self.win._target_ticks.take(self.win.code):  # discipline-ok(R5): 重试先前行情。
            self.win._evaluate_received_quote(tick)  # discipline-ok(R5): 不得触发重建的目标。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")

    def test_retry_bookkeeping_is_bounded_during_persistent_failure(self):
        self.win._alerts = [{"target": 100}]  # discipline-ok(R5): 建立有目标价的运行态。
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        with mock.patch.object(self.win, "_evaluate_targets", return_value=False), \
                mock.patch.object(window, "log_line"):
            for price in range(1, 4201):
                self.win._evaluate_received_quote({"code": self.win.code, "price": str(price)})  # discipline-ok(R5): 持续失败超过缓冲容量。
        retained_ids = set(self.win._target_retry_quotes)  # discipline-ok(R5): 检查失败状态是否与实际缓冲一致。
        pending = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 取出全部保留行情验证身份与上限。
        self.assertEqual(retained_ids, {id(quote) for quote in pending})
        self.assertLessEqual(len(pending), 4100)

    def test_two_failed_ticks_keep_retry_identity(self):
        self.win._alerts = [{"target": 100}]  # discipline-ok(R5): 建立有目标价的运行态。
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        with mock.patch.object(self.win, "_evaluate_targets", return_value=False):
            self.win._evaluate_received_quote({"code": self.win.code, "price": "90"})  # discipline-ok(R5): 建立第一条失败行情。
            self.win._evaluate_received_quote({"code": self.win.code, "price": "110"})  # discipline-ok(R5): 保留第二条行情。
            self.win._target_retry_blocked = False  # discipline-ok(R5): 模拟下一拍。
            batch = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 取出同一批待处理行情。
            second = batch[1]
            self.win._target_retry_quotes[id(second)] = (second, "original-revision")  # discipline-ok(R5): 标记原始版本以检测身份丢失。
            self.win._target_retry_inflight = {id(tick) for tick in batch}  # discipline-ok(R5): 模拟轮询持有的整批行情。
            for tick in batch:
                self.win._evaluate_received_quote(tick)  # discipline-ok(R5): 模拟再次失败。
                self.win._target_retry_inflight.discard(id(tick))  # discipline-ok(R5): 模拟轮询逐条完成。
        ticks = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 检查两笔仍有重试身份。
        self.assertEqual(len(ticks), 2)
        self.assertEqual(self.win._target_retry_quotes[id(second)][1], "original-revision")  # discipline-ok(R5): 原始版本不能被刷新。

    def test_roll_then_failed_hit_retries_the_same_crossing(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        real_write = sf_config._atomic_write_json  # discipline-ok(R5): 仅第二次写入失败。
        writes = []
        def fail_second(path, payload):
            writes.append(1)
            if len(writes) == 2:
                raise OSError("disk busy")
            return real_write(path, payload)
        rolled = dict(old, session_date="2026-10-15", extra="rolled")
        with mock.patch.object(window, "roll_alerts_to_trading_day", side_effect=[([rolled], True), ([rolled], False)]), \
                mock.patch.object(sf_config, "_atomic_write_json", side_effect=fail_second):
            self.win._evaluate_received_quote({"code": self.win.code, "price": "101"})  # discipline-ok(R5): 顺延成功、命中保存失败。
            self.win._target_retry_blocked = False  # discipline-ok(R5): 模拟下一拍。
            tick = self.win._target_ticks.take(self.win.code)[0]  # discipline-ok(R5): 取失败行情。
            self.win._evaluate_received_quote(tick)  # discipline-ok(R5): 重试原越线。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_batch_roll_does_not_discard_later_received_crossing(self):
        import sf_quote
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        rolled = dict(old, extra="rolled")
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(sf_quote, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        for price in (99, 101):
            quote = sf_quote.QuoteData({"code": self.win.code, "price": str(price)}, "20261015100000")
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 两笔行情均在目标顺延前到达。
        with mock.patch.object(window, "roll_alerts_to_trading_day", side_effect=[([rolled], True), ([rolled], False)]):
            self.win._poll()  # discipline-ok(R5): 首笔顺延，第二笔越线。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_batch_roll_does_not_accept_external_recreated_target(self):
        import sf_quote
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        rolled = dict(old, extra="rolled")
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(sf_quote, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        for price in (99, 101):
            quote = sf_quote.QuoteData({"code": self.win.code, "price": str(price)}, "20261015100000")
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 两笔行情均早于目标重建。
        evaluate = self.win._evaluate_received_quote  # discipline-ok(R5): 在真实逐笔入口后插入外部修改。
        calls = []
        def concurrent_change(quote):
            evaluate(quote)
            calls.append(quote)
            if len(calls) == 1:
                self.assertTrue(sf_config.save_alerts([], code=self.win.code))
                self.assertTrue(sf_config.save_alerts([rolled], code=self.win.code))
        with mock.patch.object(window, "roll_alerts_to_trading_day", side_effect=[([rolled], True), ([rolled], False)]), \
                mock.patch.object(self.win, "_evaluate_received_quote", side_effect=concurrent_change):
            self.win._poll()  # discipline-ok(R5): 同一批中途重建目标。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")

    def test_roll_followed_by_failure_preserves_later_batch_crossing(self):
        import sf_quote
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        above = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        below = targets.make_alert(targets.TARGET_DIR_BELOW, 95.0, now=now)
        above["code"] = below["code"] = "sh600183"
        rolled = [dict(above, extra="rolled"), dict(below, extra="rolled")]
        self._open_saved_panel([above, below])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(sf_quote, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        for price in (98, 101, 90):
            quote = sf_quote.QuoteData({"code": self.win.code, "price": str(price)}, "20261015100000")
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 顺延前接收完整批次。
        real_write = sf_config._atomic_write_json  # discipline-ok(R5): 仅第二次写入失败。
        writes = []
        def fail_second(path, payload):
            writes.append(1)
            if len(writes) == 2:
                raise OSError("busy")
            return real_write(path, payload)
        rolls = []
        def roll_once(alerts):
            rolls.append(1)
            return (rolled, True) if len(rolls) == 1 else (alerts, False)
        with mock.patch.object(window, "roll_alerts_to_trading_day", side_effect=roll_once), \
                mock.patch.object(sf_config, "_atomic_write_json", side_effect=fail_second):
            self.win._poll()  # discipline-ok(R5): 顺延成功后命中保存失败。
            self.win._poll()  # discipline-ok(R5): 下一批重新处理剩余行情。
        self.assertEqual([alert["state"] for alert in real_load_alerts(self.win.code)], ["hit", "hit"])

    def test_switch_restore_overflow_warns_user(self):
        from PyQt6.QtWidgets import QToolTip
        from sf_quote_delivery import TargetTickBuffer
        self._open_saved_panel([])
        ticks = TargetTickBuffer()
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离内存队列模拟存储故障回退。
        self.track_patch(mock.patch.object(window.FloatWindow, "_force_fetch"))
        records = [(i, float(i), {"code": "sz000001", "price": str(i)})
                   for i in range(1, 8196)]
        self.win._target_retry_by_code = {"sz000001": [(record, None) for record in records]}  # discipline-ok(R5): 建立切股恢复积压。
        self.ensure_qapp().processEvents()
        with mock.patch.object(QToolTip, "showText") as show:
            self.assertTrue(self.win.apply_stock("sz000001"))
            self.ensure_qapp().processEvents()
        self.assertTrue(any("目标价" in str(call.args[1]) for call in show.call_args_list))

    def test_received_crossing_survives_switching_stocks(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window.FloatWindow, "_force_fetch"))
        with mock.patch.object(sf_config, "_atomic_write_json", side_effect=OSError("disk busy")):
            self.win._evaluate_received_quote({"code": self.win.code, "price": "101"})  # discipline-ok(R5): 越线时写入失败。
        self.assertTrue(self.win.apply_stock("sz000001"))
        self.assertTrue(self.win.apply_stock("sh600183"))
        ticks = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 切回后旧股待处理越线仍应存在。
        self.assertEqual([q["price"] for q in ticks], ["101"])

    def test_received_unprocessed_crossing_survives_stock_switch(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window.FloatWindow, "_force_fetch"))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        self.win._q_put(("quote", (self.win.code, {"code": self.win.code, "price": "101"})))  # discipline-ok(R5): 切股前收到尚未处理的行情。
        self.assertTrue(self.win.apply_stock("sz000001"))
        self.win._poll()  # discipline-ok(R5): 新股票轮询会丢弃旧股票显示消息。
        self.assertTrue(self.win.apply_stock("sh600183"))
        self.win._poll()  # discipline-ok(R5): 切回后仍应处理先前已接收的越线。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_late_quote_after_switch_park_survives_new_stock_poll(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window.FloatWindow, "_force_fetch"))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        ticks = self.win._target_ticks  # discipline-ok(R5): 在真实切股停放后注入在途回包。
        park = ticks.park
        def late_quote(code):
            records = park(code)
            self.win._q_put(("quote", (code, {"code": code, "price": "101"})))  # discipline-ok(R5): 模拟旧请求在停放后返回。
            return records
        with mock.patch.object(ticks, "park", side_effect=late_quote):
            self.assertTrue(self.win.apply_stock("sz000001"))
        self.win._poll()  # discipline-ok(R5): 消费新股票轮询。
        self.assertTrue(self.win.apply_stock("sh600183"))
        self.win._poll()  # discipline-ok(R5): 切回后处理旧股行情。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_real_quote_retry_survives_switch_after_becoming_stale(self):
        import sf_quote
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window.FloatWindow, "_force_fetch"))
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        with mock.patch.object(window, "quote_is_current", return_value=True), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(sf_config, "_atomic_write_json", side_effect=OSError("disk busy")):
            self.win._evaluate_received_quote(quote)  # discipline-ok(R5): 真实行情写入失败。
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=False):
            self.assertTrue(self.win.apply_stock("sz000001"))
            self.assertTrue(self.win.apply_stock("sh600183"))
        ticks = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 验证切回后的待处理行情。
        self.assertEqual(ticks, [quote])
        with mock.patch.object(window, "quote_is_current", return_value=False), \
                mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=False):
            self.win._evaluate_received_quote(ticks[0])  # discipline-ok(R5): 休市后重试已接收行情。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_retry_for_first_stock_survives_second_stock_edit(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        with mock.patch.object(sf_config, "_atomic_write_json", side_effect=OSError("busy")):
            self.win._evaluate_received_quote({"code": self.win.code, "price": "101"})  # discipline-ok(R5): 第一只股票越线但写入失败。
        other = dict(old, code="sz000001", target=110.0)
        self.assertTrue(sf_config.save_alerts([other], code="sz000001"))
        self.win._target_retry_blocked = False  # discipline-ok(R5): 模拟下一次轮询。
        for tick in self.win._target_ticks.take(self.win.code):  # discipline-ok(R5): 重试第一只股票的行情。
            self.win._evaluate_received_quote(tick)  # discipline-ok(R5): 后台其他股票变更不得影响本股。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_failed_retry_storage_is_bounded_across_stock_switches(self):
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window.FloatWindow, "_force_fetch"))
        with mock.patch.object(self.win, "_evaluate_targets", return_value=False), \
                mock.patch.object(window, "log_line") as log:
            for number in range(20):
                code = "sz%06d" % number
                self.win.apply_stock(code)
                self.win._alerts = [{"target": 100}]  # discipline-ok(R5): 每只股票均有待处理目标。
                self.win._evaluate_received_quote({"code": code, "price": "101"})  # discipline-ok(R5): 模拟每只股票保存失败。
        self.assertEqual(len(self.win._target_retry_by_code), 12)  # discipline-ok(R5): 长期失败不得无限持有行情。
        self.assertTrue(any("target retry buffer full" in str(call) for call in log.call_args_list))

    def test_new_target_not_triggered_by_quote_older_than_target(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        revision = sf_config.alerts_revision
        calls = []
        def interleaved(code=None):
            value = revision(code)
            calls.append(value)
            if len(calls) == 1:
                self.assertTrue(sf_config.save_alerts([], code=self.win.code))
                self.assertTrue(sf_config.save_alerts([old], code=self.win.code))
            return value
        with mock.patch.object(sf_config, "alerts_revision", side_effect=interleaved):
            self.win._evaluate_received_quote({"code": self.win.code, "price": "101"})  # discipline-ok(R5): 控制读取版本与持锁之间的修改。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")

    def test_tick_revision_read_failure_does_not_lose_pending_quote(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        real_revision = sf_config.alerts_revision
        calls = []
        def revision(code=None):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("temporary read failure")
            return real_revision(code)
        quote = {"code": self.win.code, "price": "101"}
        with mock.patch.object(sf_config, "alerts_revision", side_effect=revision):
            self.win._evaluate_received_quote(quote)  # discipline-ok(R5): 持锁读取版本时发生暂时错误。
        ticks = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 已接收越线不能丢失。
        self.assertEqual(ticks, [quote])

    def test_failed_crossing_survives_more_extreme_later_quotes(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        with mock.patch.object(sf_config, "_atomic_write_json", side_effect=OSError("busy")):
            self.win._evaluate_received_quote({"code": self.win.code, "price": "101"})  # discipline-ok(R5): 首次越线保存失败。
            self.win._evaluate_received_quote({"code": self.win.code, "price": "90"})  # discipline-ok(R5): 后续行情穿过低点。
            self.win._evaluate_received_quote({"code": self.win.code, "price": "110"})  # discipline-ok(R5): 后续行情穿过高点。
        ticks = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 检查失败行情及后续极值。
        self.assertEqual([q["price"] for q in ticks], ["101", "90", "110"])

    def test_failed_batch_preserves_intermediate_crossing_order(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        above = targets.make_alert(targets.TARGET_DIR_ABOVE, 102.0, now=now)
        below = targets.make_alert(targets.TARGET_DIR_BELOW, 95.0, now=now)
        above["code"] = below["code"] = "sh600183"
        self._open_saved_panel([above, below])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        notifications = []
        self.track_patch(mock.patch.object(window, "_notify_deliver", side_effect=lambda *a, **k: notifications.append(a)))
        self.win.running = True
        for price in (90, 99, 103, 91, 110):
            self.win._q_put(("quote", (self.win.code, {"code": self.win.code, "price": str(price)})))  # discipline-ok(R5): 输入有先后关系的真实行情批次。
        with mock.patch.object(sf_config, "_atomic_write_json", side_effect=OSError("busy")):
            self.win._poll()  # discipline-ok(R5): 第一笔越线保存失败。
        self.win._poll()  # discipline-ok(R5): 恢复后按收到顺序重试。
        self.assertEqual(["below" if "-below-" in entry[3] else "above" for entry in notifications],
                         ["below", "above"])

    def test_poll_requeues_quote_if_evaluator_raises(self):
        self.win._alerts = [{"target": 100}]  # discipline-ok(R5): 模拟存在目标价。
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        quote = {"code": self.win.code, "price": "101"}
        self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 驱动真实接收入口。
        with mock.patch.object(self.win, "_evaluate_received_quote", side_effect=RuntimeError("unexpected")):
            self.win._poll()  # discipline-ok(R5): 异常时已取出的行情不得丢失。
        pending = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 核验实际待重试行情。
        self.assertTrue(any(item is quote for item in pending))

    def test_exception_retry_cannot_trigger_recreated_target(self):
        import sf_quote
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 实际接收旧版本行情。
        with mock.patch.object(self.win, "_evaluate_received_quote", side_effect=RuntimeError("unexpected")):
            self.win._poll()  # discipline-ok(R5): 模拟异常导致的重试。
        self.assertTrue(sf_config.save_alerts([], code=self.win.code))
        self.assertTrue(sf_config.save_alerts([dict(alert)], code=self.win.code))
        with mock.patch.object(window, "quote_is_current", return_value=True):
            self.win._poll()  # discipline-ok(R5): 固定行情时效，验证版本保护而非过期拦截。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")

    def test_exception_retry_survives_stale_quote_and_break(self):
        import sf_quote
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 接收有效行情。
            with mock.patch.object(self.win, "_evaluate_received_quote", side_effect=RuntimeError("unexpected")):
                self.win._poll()  # discipline-ok(R5): 异常后保留行情。
        with mock.patch.object(window, "quote_is_current", return_value=False), \
                mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=False):
            self.win._poll()  # discipline-ok(R5): 休市后处理已接收的行情。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_unexpected_target_transaction_error_keeps_persistent_tick(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用独立持久队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window, "quote_is_current", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        quote.received_at = now
        self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 真实接收。
        with mock.patch.object(window, "roll_alerts_to_trading_day", side_effect=RuntimeError("unexpected")):
            self.win._poll()  # discipline-ok(R5): 非预期业务异常不能确认行情。
        self.assertEqual(len(ticks.take(self.win.code)), 1)
        self.win._poll()  # discipline-ok(R5): 恢复后重试。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_persistent_tick_acknowledged_only_after_successful_evaluation(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        self.win._target_ticks = TargetTickBuffer(persistent_path=path)  # discipline-ok(R5): 接入隔离的真实持久缓冲。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window, "quote_is_current", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 真实生产入口。
        with mock.patch.object(sf_config, "_atomic_write_json", side_effect=OSError("busy")):
            self.win._poll()  # discipline-ok(R5): 失败事务不得确认行情。
        with TargetTickStore(path) as store:
            self.assertEqual(len(store.read(self.win.code)), 1)
        self.win._poll()  # discipline-ok(R5): 成功后才确认。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")
        with TargetTickStore(path) as store:
            self.assertEqual(store.read(self.win.code), [])

    def test_persistent_ack_failure_keeps_record_for_retry(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        self.win._target_ticks = TargetTickBuffer(persistent_path=path)  # discipline-ok(R5): 使用隔离持久队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window, "quote_is_current", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 实际生产行情。
        with mock.patch.object(TargetTickStore, "acknowledge", side_effect=OSError("busy")):
            self.win._poll()  # discipline-ok(R5): 模拟目标成功但确认失败。
        with TargetTickStore(path) as store:
            self.assertEqual(len(store.read(self.win.code)), 1)
        self.win._poll()  # discipline-ok(R5): 恢复后再次确认，不重复命中。
        with TargetTickStore(path) as store:
            self.assertEqual(store.read(self.win.code), [])

    def test_persistent_ack_failure_blocks_later_crossing(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        above = targets.make_alert(targets.TARGET_DIR_ABOVE, 102.0, now=now)
        below = targets.make_alert(targets.TARGET_DIR_BELOW, 95.0, now=now)
        above["code"] = below["code"] = "sh600183"
        self._open_saved_panel([above, below])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        self.win._target_ticks = TargetTickBuffer(persistent_path=path)  # discipline-ok(R5): 使用隔离持久队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window, "quote_is_current", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        for price in (90, 103):
            quote = sf_quote.QuoteData({"code": self.win.code, "price": str(price)}, "20261015100000")
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 模拟两笔按序到达。
        with mock.patch.object(TargetTickStore, "acknowledge", side_effect=OSError("busy")):
            self.win._poll()  # discipline-ok(R5): 第一笔确认失败时不得处理第二笔。
        states = {item["direction"]: item["state"] for item in real_load_alerts(self.win.code)}
        self.assertEqual(states, {"above": "pending", "below": "hit"})
        with TargetTickStore(path) as store:
            self.assertEqual(len(store.read(self.win.code)), 2)

    def test_reopened_persistent_tick_retries_after_trading_break(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        quote.received_at = now
        quote.target_revision = sf_config.alerts_revision(self.win.code)
        with TargetTickStore(path) as store:
            store.save(self.win.code, quote)
        self.win._target_ticks = TargetTickBuffer(persistent_path=path)  # discipline-ok(R5): 模拟进程重启后的缓冲。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=False))
        self.track_patch(mock.patch.object(window, "quote_is_current", return_value=False))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        self.win._poll()  # discipline-ok(R5): 模拟休市后恢复已接收行情。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")
        with TargetTickStore(path) as store:
            self.assertEqual(store.read(self.win.code), [])

    def test_reopened_persistent_stale_at_receipt_does_not_trigger(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261014093000")
        quote.received_at = now
        quote.target_revision = sf_config.alerts_revision(self.win.code)
        with TargetTickStore(path) as store:
            store.save(self.win.code, quote)
        self.win._target_ticks = TargetTickBuffer(persistent_path=path)  # discipline-ok(R5): 注入隔离数据库中的陈旧行情。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=False))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        self.win._poll()  # discipline-ok(R5): 陈旧来源不得触发。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")

    def test_reopened_persistent_tick_does_not_trigger_recreated_target(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        quote.received_at = now
        quote.target_revision = sf_config.alerts_revision(self.win.code)
        with TargetTickStore(path) as store:
            store.save(self.win.code, quote)
        self.assertTrue(sf_config.save_alerts([], code=self.win.code))
        self.assertTrue(sf_config.save_alerts([alert], code=self.win.code))
        self.win._target_ticks = TargetTickBuffer(persistent_path=path)  # discipline-ok(R5): 从隔离数据库恢复行情。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=False))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        self.win._poll()  # discipline-ok(R5): 恢复的旧行情不能触发新目标。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")

    def test_read_failure_displays_quote_without_evaluating_target(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.win._target_ticks = TargetTickBuffer(  # discipline-ok(R5): 注入隔离持久队列。
            persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window, "quote_is_current", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101", "pct": "1",
                                    "high": "101", "low": "99", "name": "测试"}, "20261015100000")
        quote.received_at = now
        with mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 真实行情接收。
        with mock.patch.object(TargetTickStore, "read_actionable_prefix", side_effect=OSError("read failed")):
            self.win._poll()  # discipline-ok(R5): 故障下执行真实显示路径。
        self.assertEqual(self.win.quote["price"], "101")
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")
        self.win._poll()  # discipline-ok(R5): 恢复后处理原行情。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_persistent_read_failure_defers_poll_until_queue_recovers(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        self.win._target_ticks = TargetTickBuffer(persistent_path=path)  # discipline-ok(R5): 使用隔离队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window, "quote_is_current", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        display = mock.Mock()
        self.track_patch(mock.patch.object(self.win, "_apply_quote", display))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        quote.received_at = now
        self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 模拟实际接收。
        with mock.patch.object(TargetTickStore, "read", side_effect=OSError("read failed")), \
                mock.patch.object(TargetTickStore, "read_actionable_prefix", side_effect=OSError("read failed")):
            self.win._poll()  # discipline-ok(R5): 队列读取失败时不得逃逸或越序。
        display.assert_called_once_with(quote)
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")
        self.win._poll()  # discipline-ok(R5): 恢复后重新消费原行情。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_full_durable_queue_warns_when_quote_is_only_volatile(self):
        from PyQt6.QtWidgets import QToolTip
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"),
                                 persistent_max_entries=1)
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用容量受限的隔离队列。
        self.win.running = True
        self.ensure_qapp().processEvents()
        with mock.patch.object(QToolTip, "showText") as show, mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, {"price": "101"})))  # discipline-ok(R5): 填满持久队列。
            self.win._q_put(("quote", (self.win.code, {"price": "102"})))  # discipline-ok(R5): 下一笔仅暂存在内存。
            self.ensure_qapp().processEvents()
        self.assertEqual(ticks.unsaved_count(), 1)
        self.assertTrue(any("目标价" in str(call.args[1]) for call in show.call_args_list))

    def test_async_recovery_without_new_quote_rearms_storage_warning(self):
        from PyQt6.QtWidgets import QToolTip
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"),
                                 async_recovery=True, on_recovery=self.win._wake_ui)  # discipline-ok(R5): 使用窗口真实唤醒。
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离持久队列。
        self.win.running = True
        self.ensure_qapp().processEvents()
        with mock.patch.object(QToolTip, "showText") as show:
            with mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")), \
                    mock.patch.object(self.win, "_wake_ui"):
                self.win._q_put(("quote", (self.win.code, {"code": self.win.code, "name": "测试股", "price": "101", "pct": "1", "high": "101", "low": "99"})))  # discipline-ok(R5): 首次写入失败。
            self.ensure_qapp().processEvents()
            with self.assertRaises(BlockingIOError):
                ticks.take(self.win.code)
            self.assertTrue(ticks.wait_recovery(4))
            self.ensure_qapp().processEvents()
            with mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")), \
                    mock.patch.object(self.win, "_wake_ui"):
                self.win._q_put(("quote", (self.win.code, {"code": self.win.code, "name": "测试股", "price": "102", "pct": "2", "high": "102", "low": "99"})))  # discipline-ok(R5): 无成功新行情即再次故障。
            self.ensure_qapp().processEvents()
        self.assertEqual(sum("目标价" in str(call.args[1]) for call in show.call_args_list), 2)

    def test_runtime_tick_warning_resets_after_recovery(self):
        from PyQt6.QtWidgets import QToolTip
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离持久队列。
        self.win.running = True
        self.ensure_qapp().processEvents()
        with mock.patch.object(QToolTip, "showText") as show, mock.patch.object(self.win, "_wake_ui"):
            with mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")):
                self.win._q_put(("quote", (self.win.code, {"price": "101"})))  # discipline-ok(R5): 首次写入失败。
            self.ensure_qapp().processEvents()
            ticks.take(self.win.code)
            self.win._q_put(("quote", (self.win.code, {"price": "102"})))  # discipline-ok(R5): 恢复正常写入。
            with mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")):
                self.win._q_put(("quote", (self.win.code, {"price": "103"})))  # discipline-ok(R5): 再次故障。
            self.ensure_qapp().processEvents()
        self.assertEqual(sum("目标价" in str(call.args[1]) for call in show.call_args_list), 2)

    def test_runtime_tick_write_failure_warns_once_per_fault(self):
        from PyQt6.QtWidgets import QToolTip
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离的持久队列。
        self.win.running = True
        with mock.patch.object(TargetTickStore, "save", side_effect=OSError("disk busy")), \
                mock.patch.object(QToolTip, "showText") as show, \
                mock.patch.object(self.win, "_wake_ui"):
            for price in ("101", "102"):
                self.win._q_put(("quote", (self.win.code, {"code": self.win.code, "price": price})))  # discipline-ok(R5): 连续接收失败。
            self.ensure_qapp().processEvents()
        self.assertEqual(ticks.unsaved_count(), 2)
        self.assertEqual(sum("目标价" in str(call.args[1]) for call in show.call_args_list), 1)

    def test_runtime_tick_write_failure_notifies_user_and_retains_quote(self):
        from PyQt6.QtWidgets import QToolTip
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离的持久队列。
        self.win.running = True
        quote = {"code": self.win.code, "price": "101"}
        with mock.patch.object(TargetTickStore, "save", side_effect=OSError("disk busy")), \
                mock.patch.object(QToolTip, "showText") as show:
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 模拟实际接收写失败。
            self.ensure_qapp().processEvents()
        self.assertEqual(ticks.unsaved_count(), 1)
        self.assertTrue(any("目标价" in str(call.args[1]) for call in show.call_args_list))

    def test_volatile_queue_exhaustion_reports_target_gap(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import CapacityError
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        from PyQt6.QtWidgets import QToolTip
        with mock.patch.object(ticks, "add_ordered", side_effect=CapacityError("full")), \
                mock.patch.object(ticks, "retain_unsaved", side_effect=CapacityError("volatile full")), \
                mock.patch.object(window, "log_line") as log, \
                mock.patch.object(QToolTip, "showText") as show:
            with self.assertRaises(CapacityError):
                self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 接收入口的双重容量耗尽。
            self.ensure_qapp().processEvents()
        messages = [str(call.args[0]) for call in log.call_args_list]
        self.assertTrue(any("target tick gap" in message and self.win.code in message for message in messages))
        self.assertTrue(any("目标价" in str(call.args[1]) for call in show.call_args_list))

    def test_persistent_queue_consumes_equivalent_stock_code(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离持久队列。
        quote = sf_quote.QuoteData({"code": "600183", "price": "101"}, "20261015100000")
        quote.received_at = now
        quote.target_revision = sf_config.alerts_revision("sh600183")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.add_ordered("600183", quote)
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        self.win._poll()  # discipline-ok(R5): 驱动实际队列消费。
        self.assertEqual(real_load_alerts("sh600183")[0]["state"], "hit")
        with TargetTickStore(path) as store:
            self.assertEqual(store.read_global(), [])

    def test_old_stock_backlog_precedes_new_stock_notification(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        new = targets.make_alert(targets.TARGET_DIR_ABOVE, 200.0, now=now)
        new["code"] = "sh600519"
        self._open_saved_panel([old])
        self.assertTrue(sf_config.save_alerts([new], code="sh600519"))
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 独立持久队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        with mock.patch.object(self.win, "_wake_ui"):
            for code, price in (("sh600183", "99"), ("sh600183", "101"), ("sh600519", "201")):
                quote = sf_quote.QuoteData({"code": code, "price": price}, "20261015100000")
                quote.received_at = now
                self.win._q_put(("quote", (code, quote)))  # discipline-ok(R5): 使用真实入队顺序。
        self.win.code = "sh600519"
        self.win._alerts = [new]  # discipline-ok(R5): 模拟切股后目标状态。
        self.win._poll()  # discipline-ok(R5): 按序消费队列。
        self.win.timer.timeout.emit()
        events = sf_config.load_target_notification_outbox()
        self.assertEqual([event["code"] for event in events], ["sh600183", "sh600519"])

    def test_failed_durable_tick_prevents_later_batch_acknowledgement(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离持久队列。
        revision = sf_config.alerts_revision(self.win.code)
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            for price in ("99", "101"):
                quote = sf_quote.QuoteData({"code": self.win.code, "price": price}, "20261015100000")
                quote.received_at = now
                quote.target_revision = revision
                ticks.add_ordered(self.win.code, quote)
        self.win.running = True
        with mock.patch.object(self.win, "_evaluate_received_quote", side_effect=[False, None]), \
                mock.patch.object(self.win, "_apply_quote"):
            self.win._poll()  # discipline-ok(R5): 第一条失败时不得确认后续行情。
        with TargetTickStore(path) as store:
            self.assertEqual([item.quote["price"] for item in store.read(self.win.code)], ["99", "101"])

    def test_durable_tick_exception_does_not_acknowledge_later_ticks(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离持久队列。
        revision = sf_config.alerts_revision(self.win.code)
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            for price in ("99", "101"):
                quote = sf_quote.QuoteData({"code": self.win.code, "price": price}, "20261015100000")
                quote.received_at = now
                quote.target_revision = revision
                ticks.add_ordered(self.win.code, quote)
        self.win.running = True
        with mock.patch.object(self.win, "_evaluate_received_quote", side_effect=[OSError("failure"), None]), \
                mock.patch.object(self.win, "_apply_quote"):
            self.win._poll()  # discipline-ok(R5): 异常后后续行情必须保留。
        with TargetTickStore(path) as store:
            self.assertEqual([item.quote["price"] for item in store.read(self.win.code)], ["99", "101"])

    def test_pending_old_stock_target_survives_other_stock_store_prune(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        new = targets.make_alert(targets.TARGET_DIR_ABOVE, 200.0, now=now)
        new["code"] = "sh600519"
        self._open_saved_panel([old])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离持久队列。
        quote = sf_quote.QuoteData({"code": "sh600183", "price": "101"}, "20261015100000")
        quote.received_at = now
        quote.target_revision = sf_config.alerts_revision("sh600183")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.add_ordered("sh600183", quote)
        with mock.patch.dict(os.environ, {"SF_TARGET_TICKS_PATH": path}), \
                mock.patch.object(sf_config, "ALERT_STORE_MAX_TOTAL", 1):
            saved = sf_config.save_alerts([new], code="sh600519")
        with TargetTickStore(path) as store:
            self.assertEqual(len(store.read("sh600183")), 1)
        self.assertTrue(not saved or real_load_alerts("sh600183"))

    def test_durable_existing_over_limit_can_persist_hit_without_pruning(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        new = targets.make_alert(targets.TARGET_DIR_ABOVE, 200.0, now=now)
        new["code"] = "sh600519"
        self._open_saved_panel([old])
        self.assertTrue(sf_config.save_alerts([new], code="sh600519"))
        hit = dict(old, state="hit")
        event = {"id": "test-event", "code": "sh600183", "title": "test",
                 "body": "test", "semantic_key": "test"}
        with mock.patch.dict(os.environ, {"SF_TARGET_TICKS_PATH": os.path.join(self.tmp_path, "ticks.sqlite")}), \
                mock.patch.object(sf_config, "ALERT_STORE_MAX_TOTAL", 1):
            saved = sf_config.save_alerts([hit], code="sh600183", outbox_events=[event])
        self.assertTrue(saved)
        self.assertEqual(real_load_alerts("sh600183")[0]["state"], "hit")
        self.assertTrue(real_load_alerts("sh600519"))
        self.assertEqual([item["id"] for item in sf_config.load_target_notification_outbox()], ["test-event"])

    def test_concurrent_prune_does_not_discard_captured_tick_target(self):
        import threading
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        new = targets.make_alert(targets.TARGET_DIR_ABOVE, 200.0, now=now)
        new["code"] = "sh600519"
        self._open_saved_panel([old])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离持久队列。
        captured = threading.Event()
        proceed = threading.Event()
        saved = []
        actual_add = ticks.add_ordered
        def delayed_add(code, quote):
            captured.set()
            if not proceed.wait(3):
                raise TimeoutError("test synchronization failed")
            return actual_add(code, quote)
        quote = sf_quote.QuoteData({"code": "sh600183", "price": "101"}, "20261015100000")
        quote.received_at = now
        with mock.patch.dict(os.environ, {"SF_TARGET_TICKS_PATH": path}), \
                mock.patch.object(sf_config, "ALERT_STORE_MAX_TOTAL", 1), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(ticks, "add_ordered", side_effect=delayed_add), \
                mock.patch.object(self.win, "_wake_ui"):
            producer = threading.Thread(target=self.win._q_put, args=(  # discipline-ok(R5): 使用真实行情入口验证并发。
                ("quote", ("sh600183", quote)),))  # discipline-ok(R5): 线程化验证行情入队与配置保存交错。
            producer.start()
            self.assertTrue(captured.wait(3))
            writer = threading.Thread(target=lambda: saved.append(sf_config.save_alerts([new], code="sh600519")))
            writer.start()
            writer.join(0.2)
            proceed.set()
            producer.join(3)
            writer.join(3)
            self.assertFalse(producer.is_alive())
            self.assertFalse(writer.is_alive())
        with TargetTickStore(path) as store:
            self.assertEqual(len(store.read("sh600183")), 1)
        self.assertTrue(not saved[0] or real_load_alerts("sh600183"))

    def test_unknown_old_stock_does_not_block_other_stock(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        new = targets.make_alert(targets.TARGET_DIR_ABOVE, 200.0, now=now)
        new["code"] = "sh600519"
        self._open_saved_panel([old])
        self.assertTrue(sf_config.save_alerts([new], code="sh600519"))
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离持久队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        for code, price, revision in (("sh600183", "101", None),
                                      ("sh600183", "102", sf_config.alerts_revision("sh600183")),
                                      ("sh600519", "201", sf_config.alerts_revision("sh600519"))):
            quote = sf_quote.QuoteData({"code": code, "price": price}, "20261015100000")
            quote.received_at = now
            quote.target_revision = revision
            ticks.add_ordered(code, quote)
        self.win.code = "sh600519"
        self.win._alerts = [new]  # discipline-ok(R5): 模拟切换至新股票。
        self.win.running = True
        self.win._poll()  # discipline-ok(R5): 驱动真实队列消费。
        self.assertEqual(real_load_alerts("sh600519")[0]["state"], "hit")
        self.assertEqual(real_load_alerts("sh600183")[0]["state"], "pending")
        with TargetTickStore(path) as store:
            self.assertEqual(len(store.read("sh600183")), 2)
            self.assertEqual(store.read("sh600519"), [])

    def test_old_stock_tick_does_not_fire_when_targets_disabled(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离持久队列。
        quote = sf_quote.QuoteData({"code": "sh600183", "price": "101"}, "20261015100000")
        quote.received_at = now
        quote.target_revision = sf_config.alerts_revision("sh600183")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.add_ordered("sh600183", quote)
        self.win.code = "sh600519"
        self.win.running = True
        current = {"code": "sh600519", "price": "201"}
        self.win.q.put_nowait(("quote", (self.win.code, current)))  # discipline-ok(R5): 注入真实显示队列。
        with mock.patch.object(self.win, "_targets_enabled", return_value=False), \
                mock.patch.object(self.win, "_apply_quote") as display, \
                mock.patch.object(window, "_beijing_now", return_value=now), \
                mock.patch.object(targets, "_beijing_now", return_value=now):
            self.win._poll()  # discipline-ok(R5): 验证关闭目标功能的实际轮询。
        display.assert_called_once_with(current)
        self.assertEqual(real_load_alerts("sh600183")[0]["state"], "pending")
        self.assertEqual(sf_config.load_target_notification_outbox(), [])
        with TargetTickStore(path) as store:
            self.assertEqual(len(store.read("sh600183")), 1)

    def test_old_stock_revision_change_waits_for_config_sync_before_ack(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入独立持久队列。
        quote = sf_quote.QuoteData({"code": "sh600183", "price": "101"}, "20261015100000")
        quote.received_at = now
        quote.target_revision = sf_config.alerts_revision("sh600183")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.add_ordered("sh600183", quote)
        self.assertTrue(sf_config.save_alerts([], code="sh600183"))
        self.win.code = "sh600519"
        self.win.running = True
        with mock.patch.object(window.os, "fsync", side_effect=OSError("sync failed")):
            self.win._poll()  # discipline-ok(R5): 模拟版本变化后持久化不可确认。
        with TargetTickStore(path) as store:
            self.assertEqual(len(store.read("sh600183")), 1)
        self.win._poll()  # discipline-ok(R5): 同步恢复后允许确认旧行情。
        with TargetTickStore(path) as store:
            self.assertEqual(store.read("sh600183"), [])

    def test_old_stock_retry_after_replace_preserves_single_notification(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        quote = sf_quote.QuoteData({"code": "sh600183", "price": "101"}, "20261015100000")
        quote.received_at = now
        quote.target_revision = sf_config.alerts_revision("sh600183")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.add_ordered("sh600183", quote)
        self.win.code = "sh600519"
        self.win.running = True
        write = sf_config._atomic_write_json  # discipline-ok(R5): 模拟替换后同步失败。
        def write_then_fail(*args, **kwargs):
            write(*args, **kwargs)
            raise OSError("directory sync failed")
        with mock.patch.object(window, "_beijing_now", return_value=now), \
                mock.patch.object(targets, "_beijing_now", return_value=now):
            with mock.patch.object(sf_config, "_atomic_write_json", side_effect=write_then_fail):
                self.win._poll()  # discipline-ok(R5): 已写入但返回失败。
            with TargetTickStore(path) as store:
                self.assertEqual(len(store.read("sh600183")), 1)
            self.win._target_ticks = TargetTickBuffer(persistent_path=path)  # discipline-ok(R5): 模拟重启后重新打开队列。
            self.win._poll()  # discipline-ok(R5): 同步并确认旧行情。
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1)
        with TargetTickStore(path) as store:
            self.assertEqual(store.read("sh600183"), [])

    def test_old_stock_tick_is_retained_when_target_write_fails(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        self._open_saved_panel([old])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        ticks = TargetTickBuffer(persistent_path=path, persistent_max_entries=1, async_recovery=True)
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        quote = sf_quote.QuoteData({"code": "sh600183", "price": "101"}, "20261015100000")
        quote.received_at = now
        quote.target_revision = sf_config.alerts_revision("sh600183")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.add_ordered("sh600183", quote)
        self.win.code = "sh600519"
        self.win.running = True
        with mock.patch.object(window, "_beijing_now", return_value=now), \
                mock.patch.object(targets, "_beijing_now", return_value=now), \
                mock.patch.object(sf_config, "_atomic_write_json", side_effect=OSError("busy")) as write:
            self.win._poll()  # discipline-ok(R5): 驱动实际消费与写入失败。
            self.assertTrue(write.called)
        with TargetTickStore(path) as store:
            self.assertEqual(len(store.read("sh600183")), 1)
        self.assertEqual(real_load_alerts("sh600183")[0]["state"], "pending")

    def test_switch_stock_drains_old_durable_tick_before_new_crossing(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old["code"] = "sh600183"
        new = targets.make_alert(targets.TARGET_DIR_ABOVE, 200.0, now=now)
        new["code"] = "sh600519"
        self._open_saved_panel([old])
        self.assertTrue(sf_config.save_alerts([new], code="sh600519"))
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"),
                                 persistent_max_entries=1, async_recovery=True,
                                 on_recovery=self.win._wake_ui)  # discipline-ok(R5): 使用窗口真实唤醒。
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入有限容量队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window, "quote_is_current", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        for code, price in (("sh600183", "101"), ("sh600519", "201")):
            quote = sf_quote.QuoteData({"code": code, "price": price}, "20261015100000")
            quote.received_at = now
            self.win._q_put(("quote", (code, quote)))  # discipline-ok(R5): 两股行情按次序进入。
        self.win.code = "sh600519"
        self.win._alerts = [new]  # discipline-ok(R5): 模拟切股完成。
        self.win._poll()  # discipline-ok(R5): 先处理旧股以释放容量。
        self.assertTrue(ticks.wait_recovery(4))
        self.win._poll()  # discipline-ok(R5): 再处理新股。
        self.assertTrue(ticks.wait_recovery(4))
        self.win._poll()  # discipline-ok(R5): 消费恢复后的新股行情。
        self.assertEqual(real_load_alerts("sh600183")[0]["state"], "hit")
        self.assertEqual(real_load_alerts("sh600519")[0]["state"], "hit")

    def test_persistent_capacity_full_preserves_received_crossing(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"),
                                 persistent_max_entries=1)
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离容量限制。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window, "quote_is_current", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        for price in (90, 101):
            quote = sf_quote.QuoteData({"code": self.win.code, "price": str(price)}, "20261015100000")
            quote.received_at = now
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 真实接收入口。
        self.win._poll()  # discipline-ok(R5): 消费已落盘行情以释放空间。
        self.win._poll()  # discipline-ok(R5): 补写并消费后续越线行情。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_async_recovery_retries_after_temporary_failure_without_new_quote(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"),
                                 async_recovery=True, on_recovery=self.win._wake_ui)  # discipline-ok(R5): 使用窗口唤醒入口。
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入独立队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window, "quote_is_current", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        quote.received_at = now
        with mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")), \
                mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 写失败后暂存。
            self.win._poll()  # discipline-ok(R5): 首次补写仍失败。
            self.assertTrue(ticks.wait_recovery(4))
        self.win.timer.timeout.emit()
        self.win.timer.timeout.emit()
        self.assertTrue(ticks.wait_recovery(4))
        self.ensure_qapp().processEvents()
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_async_recovery_wakes_window_without_new_quote(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"),
                                 async_recovery=True, on_recovery=self.win._wake_ui)  # discipline-ok(R5): 使用窗口唤醒入口。
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入异步队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window, "quote_is_current", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        quote.received_at = now
        with mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")), \
                mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 写失败先暂存，屏蔽接收时唤醒。
        self.ensure_qapp().processEvents()
        self.win._poll()  # discipline-ok(R5): 启动异步补写。
        self.assertTrue(ticks.wait_recovery(4))
        self.ensure_qapp().processEvents()
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_shutdown_warns_if_quote_publisher_remains_blocked(self):
        import threading
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "shutdown-blocked.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离队列。
        self.win.running = True
        entered = threading.Event()
        done = threading.Event()
        def publish():
            entered.set()
            try:
                self.win._q_put(("quote", (self.win.code, sf_quote.QuoteData({"price": "101"}))))  # discipline-ok(R5): 模拟后台接收。
            finally:
                done.set()
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_wake_ui"), \
                mock.patch.object(window, "log_line") as log:
            with sf_config.target_tick_transaction():
                worker = threading.Thread(target=publish, daemon=True)
                self.win._quote_thread = worker  # discipline-ok(R5): 注入实际等待锁的后台线程。
                worker.start()
                self.assertTrue(entered.wait(2))
                self.assertFalse(done.wait(0.1))
                self.win._shutdown_threads()  # discipline-ok(R5): 实际退出流程。
                self.assertFalse(done.is_set())
                messages = [str(call.args[0]) for call in log.call_args_list]
                self.assertTrue(any("target" in text and "publisher" in text for text in messages))
            self.assertTrue(done.wait(3))
            worker.join(3)

    def test_shutdown_logs_unsaved_ticks_when_no_worker_is_running(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"),
                                 async_recovery=True)
        quote = sf_quote.QuoteData({"price": "101"}, "20261015100000")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(TargetTickStore, "save", side_effect=OSError("busy")):
            ticks.add_ordered("sh600183", quote)
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入有待写行情的隔离队列。
        with mock.patch.object(window, "log_line") as log:
            self.win._shutdown_threads()  # discipline-ok(R5): 验证真实退出路径。
        self.assertTrue(any("target tick" in str(call.args[0]) and
                            "1" in str(call.args[0]) and "unsaved" in str(call.args[0])
                            for call in log.call_args_list))

    def test_shutdown_logs_unfinished_target_recovery(self):
        ticks = mock.Mock()
        ticks.wait_recovery.return_value = False
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入超时的补写协作者。
        with mock.patch.object(window, "log_line") as log:
            self.win._shutdown_threads()  # discipline-ok(R5): 驱动退出。
        self.assertTrue(any("target tick recovery still running" in str(call.args[0])
                            for call in log.call_args_list))

    def test_shutdown_waits_for_target_recovery_worker(self):
        ticks = mock.Mock()
        ticks.wait_recovery.return_value = True
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入可观察的后台补写协作者。
        self.win._shutdown_threads()  # discipline-ok(R5): 驱动实际退出收尾。
        ticks.wait_recovery.assert_called_once()
        self.assertLessEqual(ticks.wait_recovery.call_args.args[0], 2.0)

    def test_default_target_queue_uses_isolated_config_directory(self):
        config_path = os.path.join(self.tmp_path, "config.json")
        with mock.patch.object(sf_config, "CONFIG_PATH", config_path), \
                mock.patch.dict(os.environ, {"SF_TARGET_TICKS_DISABLE": "0"}, clear=False):
            os.environ.pop("SF_TARGET_TICKS_PATH", None)
            win = self.track_widget(window.FloatWindow())
        self.assertTrue(win._target_ticks.persistent)  # discipline-ok(R5): 验证实际默认队列。
        self.assertEqual(win._target_ticks._persistent_path,  # discipline-ok(R5): 验证默认路径。
                         os.path.join(self.tmp_path, "target_ticks.sqlite"))

    def test_unavailable_persistent_store_notifies_user(self):
        from PyQt6.QtWidgets import QToolTip
        from sf_target_tick_store import TargetTickStore
        with mock.patch.dict(os.environ, {"SF_TARGET_TICKS_PATH": os.path.join(self.tmp_path, "bad.sqlite")}), \
                mock.patch.object(TargetTickStore, "__init__", side_effect=OSError("unavailable")), \
                mock.patch.object(QToolTip, "showText") as show:
            win = self.track_widget(window.FloatWindow())
            self.ensure_qapp().processEvents()
        self.assertTrue(any("目标价" in str(call.args[1]) for call in show.call_args_list))
        self.assertFalse(win._target_ticks.persistent)  # discipline-ok(R5): 验证失败回退。

    def test_unavailable_persistent_store_surfaces_warning(self):
        from sf_target_tick_store import TargetTickStore
        with mock.patch.dict(os.environ, {"SF_TARGET_TICKS_PATH": os.path.join(self.tmp_path, "bad.sqlite")}), \
                mock.patch.object(TargetTickStore, "__init__", side_effect=OSError("unavailable")), \
                mock.patch.object(window, "log_line") as log:
            win = self.track_widget(window.FloatWindow())
        messages = [str(call.args[0]) for call in log.call_args_list]
        self.assertTrue(any("target tick store unavailable" in message for message in messages))
        self.assertFalse(win._target_ticks.persistent)  # discipline-ok(R5): 验证故障回退实际模式。

    def test_stock_adoption_preserves_target_for_pending_persistent_crossing(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        received = datetime(2026, 10, 15, 14, 59, tzinfo=timezone(timedelta(hours=8)))
        later = datetime(2026, 10, 15, 15, 1, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=received)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用独立持久队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        quote.received_at = received
        quote.target_revision = sf_config.alerts_revision(self.win.code)
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.add_ordered(self.win.code, quote)
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=later))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=later))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=False))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        self.win._adopt_targets_for(self.win.code)  # discipline-ok(R5): 模拟切回股票。
        self.win._poll()  # discipline-ok(R5): 处理该股票之前收到的行情。
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1)

    def test_startup_preserves_target_for_pending_persistent_crossing(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        received = datetime(2026, 10, 15, 14, 59, tzinfo=timezone(timedelta(hours=8)))
        later = datetime(2026, 10, 15, 15, 1, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=received)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用独立持久队列模拟重启。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        quote.received_at = received
        quote.target_revision = sf_config.alerts_revision(self.win.code)
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.add_ordered(self.win.code, quote)
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=later))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=later))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=False))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        real_init_targets(self.win)
        self.win._poll()  # discipline-ok(R5): 恢复启动前收到的行情。
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1)

    def test_expiry_cleanup_retries_when_pending_queue_read_fails(self):
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离的持久队列。
        self.win._clear_timer.stop()  # discipline-ok(R5): 验证真实单次定时器重启。
        with mock.patch.object(ticks, "has_pending", side_effect=OSError("busy")):
            self.win._clear_expired()  # discipline-ok(R5): 队列不可读时不得放弃重试。
        self.assertTrue(self.win._clear_timer.isActive())  # discipline-ok(R5): 定时器没有公开替代接口。

    def test_expiry_cleanup_does_not_discard_pending_persistent_crossing(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        received = datetime(2026, 10, 15, 14, 59, tzinfo=timezone(timedelta(hours=8)))
        later = datetime(2026, 10, 15, 15, 1, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=received)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离的持久队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        quote.received_at = received
        quote.target_revision = sf_config.alerts_revision(self.win.code)
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.add_ordered(self.win.code, quote)
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=later))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=later))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=False))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        self.win._clear_expired()  # discipline-ok(R5): 到期定时器先于行情处理。
        self.win._poll()  # discipline-ok(R5): 随后处理已接收的行情。
        events = sf_config.load_target_notification_outbox()
        self.assertEqual(len(events), 1)

    def test_expiry_cleanup_preserves_tick_arriving_after_pending_check(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        received = datetime(2026, 10, 15, 14, 59, tzinfo=timezone(timedelta(hours=8)))
        later = datetime(2026, 10, 15, 15, 1, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=received)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        quote.received_at = received
        quote.target_revision = sf_config.alerts_revision(self.win.code)
        original = ticks.has_pending
        def arriving(code):
            pending = original(code)
            if not pending:
                with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
                    ticks.add_ordered(code, quote)
            return pending
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=later))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=later))
        with mock.patch.object(ticks, "has_pending", side_effect=arriving):
            self.win._clear_expired()  # discipline-ok(R5): 模拟检查后立即收到旧行情。
        self.assertTrue(real_load_alerts(self.win.code))
        self.assertTrue(original(self.win.code))

    def test_expiry_cleanup_races_real_quote_producer(self):
        import threading
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        received = datetime(2026, 10, 15, 14, 59, tzinfo=timezone(timedelta(hours=8)))
        later = datetime(2026, 10, 15, 15, 1, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=received)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        quote.received_at = received
        original = ticks.has_pending
        producer = None
        def arriving(code):
            nonlocal producer
            pending = original(code)
            if not pending and producer is None:
                producer = threading.Thread(target=self.win._q_put, args=(  # discipline-ok(R5): 使用真实行情入口验证并发。
                    ("quote", (code, quote)),))  # discipline-ok(R5): 模拟清理检查期间生产者入队。
                producer.start()
                producer.join(2)
            return pending
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=later))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=later))
        with mock.patch.object(ticks, "has_pending", side_effect=arriving), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_wake_ui"):
            self.win._clear_expired()  # discipline-ok(R5): 真实生产者在检查后入队。
            if producer is not None:
                producer.join(3)
        self.assertIsNotNone(producer)
        self.assertFalse(producer.is_alive())
        self.assertTrue(real_load_alerts(self.win.code))
        self.assertTrue(original(self.win.code))

    def test_target_tick_lock_blocks_other_process(self):
        import subprocess
        import sys
        self._open_saved_panel([])
        child = '''
import sys
import sf_config
sf_config.CONFIG_PATH = sys.argv[1]
try:
    with sf_config.target_tick_transaction(blocking=False):
        raise SystemExit(2)
except BlockingIOError:
    raise SystemExit(0)
'''
        with sf_config.target_tick_transaction():
            result = subprocess.run([sys.executable, "-c", child, sf_config.CONFIG_PATH],
                                    capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lock_failure_warns_again_when_volatile_capacity_exhausted(self):
        import sf_quote
        from contextlib import contextmanager
        from PyQt6.QtWidgets import QToolTip
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import CapacityError
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "lock-escalation.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离目标行情队列。
        self.win.running = True
        @contextmanager
        def denied(**kwargs):
            raise PermissionError("lock unavailable")
            yield
        with mock.patch.object(sf_config, "target_tick_transaction", denied), \
                mock.patch.object(QToolTip, "showText") as show, \
                mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, sf_quote.QuoteData({"price": "101"}))))  # discipline-ok(R5): 首次故障仅内存暂存。
            self.ensure_qapp().processEvents()
            with mock.patch.object(ticks, "retain_unsaved", side_effect=CapacityError("full")):
                with self.assertRaises(CapacityError):
                    self.win._q_put(("quote", (self.win.code, sf_quote.QuoteData({"price": "102"}))))  # discipline-ok(R5): 容量耗尽产生缺口。
            self.ensure_qapp().processEvents()
        self.assertEqual(show.call_count, 2)

    def test_target_lock_warning_rearms_after_lock_recovers(self):
        import sf_quote
        from contextlib import contextmanager
        from PyQt6.QtWidgets import QToolTip
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "lock-rearm.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离队列。
        self.win.running = True
        @contextmanager
        def denied(**kwargs):
            raise PermissionError("lock unavailable")
            yield
        with mock.patch.object(QToolTip, "showText") as show, \
                mock.patch.object(self.win, "_wake_ui"), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            with mock.patch.object(sf_config, "target_tick_transaction", denied):
                self.win._q_put(("quote", (self.win.code, sf_quote.QuoteData({"price": "101"}))))  # discipline-ok(R5): 首次锁故障。
                self.ensure_qapp().processEvents()
            self.win._q_put(("quote", (self.win.code, sf_quote.QuoteData({"price": "102"}))))  # discipline-ok(R5): 锁恢复后再次接收。
            with mock.patch.object(sf_config, "target_tick_transaction", denied):
                self.win._q_put(("quote", (self.win.code, sf_quote.QuoteData({"price": "103"}))))  # discipline-ok(R5): 再次锁故障。
                self.ensure_qapp().processEvents()
        self.assertEqual(sum("提醒已暂停" in str(call.args[1]) for call in show.call_args_list), 2)

    def test_target_lock_failure_warns_once_during_continuous_failure(self):
        import sf_quote
        from contextlib import contextmanager
        from PyQt6.QtWidgets import QToolTip
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "lock-warnings.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离队列。
        self.win.running = True
        @contextmanager
        def denied(**kwargs):
            raise PermissionError("lock unavailable")
            yield
        with mock.patch.object(sf_config, "target_tick_transaction", denied), \
                mock.patch.object(QToolTip, "showText") as show, \
                mock.patch.object(self.win, "_wake_ui"):
            for price in ("101", "102"):
                quote = sf_quote.QuoteData({"code": self.win.code, "price": price})
                self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 连续模拟锁故障。
                self.ensure_qapp().processEvents()
        self.assertEqual(ticks.unsaved_count(), 2)
        own_calls = [call for call in show.call_args_list if len(call.args) > 2 and call.args[2] is self.win]
        self.assertEqual(len(own_calls), 1, [call.args[1] for call in own_calls])

    def test_lock_failure_visible_warning_includes_gap_and_pause(self):
        import sf_quote
        from contextlib import contextmanager
        from PyQt6.QtWidgets import QToolTip
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "warning-text.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离队列。
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"})
        @contextmanager
        def denied(**kwargs):
            raise PermissionError("lock unavailable")
            yield
        with mock.patch.object(sf_config, "target_tick_transaction", denied), \
                mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 驱动真实告警信号。
            self.ensure_qapp().processEvents()
            text = QToolTip.text()
        QToolTip.hideText()
        self.assertIn("提醒可能遗漏", text)
        self.assertIn("提醒已暂停", text)

    def test_quote_publisher_waits_for_real_target_lock_then_delivers(self):
        import threading
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "publisher-lock.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"})
        entered = threading.Event()
        finished = threading.Event()
        errors = []
        def publish():
            entered.set()
            try:
                self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 模拟后台行情接收。
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_wake_ui"):
            with sf_config.target_tick_transaction():
                thread = threading.Thread(target=publish)
                thread.start()
                self.assertTrue(entered.wait(2))
                self.assertFalse(finished.wait(0.1))
            self.assertTrue(finished.wait(3))
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(self.win.q.empty())
        self.assertTrue(ticks.has_pending(self.win.code))

    def test_target_lock_failure_retains_tick_without_writing_database(self):
        import sf_quote
        from contextlib import contextmanager
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "lock-denied.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离队列测试锁故障。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"})
        @contextmanager
        def denied(**kwargs):
            raise PermissionError("lock unavailable")
            yield
        with mock.patch.object(sf_config, "target_tick_transaction", denied), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 驱动真实行情入口。
        self.assertEqual(ticks.unsaved_count(), 1)
        self.assertFalse(self.win.q.empty())
        self.assertIsNone(quote.target_revision)
        with TargetTickStore(ticks._persistent_path) as store:  # discipline-ok(R5): 只读核实隔离队列。
            self.assertEqual(store.read(self.win.code), [])

    def test_target_tick_lease_stays_active_when_recovery_write_fails(self):
        import sf_quote
        from contextlib import contextmanager
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", os.path.join(self.tmp_path, "config.json")))
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "recovery-denied.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"})
        @contextmanager
        def denied(**kwargs):
            raise PermissionError("target lock unavailable")
            yield
        with mock.patch.object(sf_config, "target_tick_transaction", denied), \
                mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 锁失败，暂存行情。
        self.assertTrue(sf_config.target_tick_publisher_active(self.win.code))
        with mock.patch.object(TargetTickStore, "save", side_effect=OSError("disk busy")):
            self.assertEqual(ticks.take(self.win.code), [])
        self.assertEqual(ticks.unsaved_count(), 1)
        self.assertTrue(sf_config.target_tick_publisher_active(self.win.code))
        self.assertTrue(ticks.wait_recovery(4))
        ticks.take(self.win.code)
        self.assertEqual(ticks.unsaved_count(), 0)
        self.assertFalse(sf_config.target_tick_publisher_active(self.win.code))

    def test_publisher_registration_active_handles_stale_and_malformed_entries(self):
        import sf_config
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", os.path.join(self.tmp_path, "config.json")))
        directory = sf_config.CONFIG_PATH + ".target-tick-publishers"
        os.makedirs(directory, exist_ok=True)
        stale = os.path.join(directory, "sh600183.999999-thread-token")
        malformed = os.path.join(directory, "sh600183.bad-thread-token")
        with open(stale, "w", encoding="ascii") as stream:
            stream.write("999999")
        with open(malformed, "w", encoding="ascii") as stream:
            stream.write("not-a-pid")
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError("process exited")):
            self.assertTrue(sf_config.target_tick_publisher_active(self.win.code))
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(malformed))

    def test_publisher_registration_active_treats_directory_errors_as_active(self):
        import sf_config
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", os.path.join(self.tmp_path, "config.json")))
        with mock.patch.object(os, "listdir", side_effect=PermissionError("directory denied")):
            self.assertTrue(sf_config.target_tick_publisher_active(self.win.code))
        with mock.patch.object(os, "listdir", side_effect=FileNotFoundError):
            self.assertFalse(sf_config.target_tick_publisher_active(self.win.code))

    def test_publisher_cleanup_tolerates_external_removal(self):
        import sf_config
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", os.path.join(self.tmp_path, "config.json")))
        directory = sf_config.CONFIG_PATH + ".target-tick-publishers"
        with sf_config.target_tick_publisher(self.win.code):
            entries = os.listdir(directory)
            self.assertEqual(len(entries), 1)
            os.unlink(os.path.join(directory, entries[0]))

    def test_publisher_registration_active_skips_other_stocks_and_vanished_entries(self):
        """别的股票不算这只；巡检与清理之间被人删掉的登记既不报错也不算活跃发布者。"""
        import sf_config
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", os.path.join(self.tmp_path, "config.json")))
        directory = sf_config.CONFIG_PATH + ".target-tick-publishers"
        os.makedirs(directory, exist_ok=True)
        alive = os.path.join(directory, "sh600999.%d-1-token" % os.getpid())
        gone = os.path.join(directory, "sh600183.%d-1-token" % self._dead_publisher_pid())
        for path in (alive, gone):
            with open(path, "w", encoding="ascii"):
                pass
        unlink = os.unlink

        def raced(path, *args, **kwargs):
            if str(path) == gone:
                raise FileNotFoundError(path)  # 列出来之后、清掉之前，别人先删了
            return unlink(path, *args, **kwargs)

        with mock.patch.object(os, "unlink", side_effect=raced):
            self.assertFalse(sf_config.target_tick_publisher_active("sh600000"),
                             "别的股票的登记不能算成这只股票还在发布")
            self.assertTrue(sf_config.target_tick_publisher_active("sh600999"),
                             "自己进程还持有的登记必须认账")
            self.assertFalse(sf_config.target_tick_publisher_active(self.win.code),
                             "登记刚被别人清掉时不能报错，也不能当成还在发布")
        self.assertTrue(os.path.exists(alive), "查别的股票时不许顺手清掉别人的登记")

    def test_publisher_registration_active_keeps_a_stale_entry_it_cannot_remove(self):
        """进程已确认不在了：登记文件删不掉只是没清理，不能继续挡住这只股票的过期清理。"""
        import sf_config
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", os.path.join(self.tmp_path, "config.json")))
        directory = sf_config.CONFIG_PATH + ".target-tick-publishers"
        os.makedirs(directory, exist_ok=True)
        stale = os.path.join(directory, "sh600183.999999-1-token")
        with open(stale, "w", encoding="ascii"):
            pass
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError("process exited")), \
                mock.patch.object(os, "unlink", side_effect=PermissionError("read-only directory")):
            self.assertFalse(sf_config.target_tick_publisher_active(self.win.code))
        self.assertTrue(os.path.exists(stale), "删不掉时只能留着，由下一次巡检再清")

    def _dead_publisher_pid(self):
        """取一个确认不存在的进程号，当登记名里的持有者（不靠桩，用系统真实答案）。"""
        for candidate in range(99999, 99000, -1):
            try:
                os.kill(candidate, 0)
            except ProcessLookupError:
                return candidate
            except OSError:
                continue
        self.skipTest("本机这 1000 个进程号里取不到空闲的")
        return 0

    def test_marker_created_without_content_is_cleaned_when_its_owner_is_gone(self):
        """进程在"建登记"和"写进程号"之间被杀掉：登记必须能自愈，不能永久挡住这只股票。

        持有者身份只有来自文件名才是原子的——目录项在创建那一刻就带全名，而正文是
        第二次系统调用才写进去的。用户直接杀进程、崩溃、注销都会踩中这个窗口。
        """
        import sf_config
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", os.path.join(self.tmp_path, "config.json")))
        directory = sf_config.CONFIG_PATH + ".target-tick-publishers"
        os.makedirs(directory, exist_ok=True)
        dead = self._dead_publisher_pid()
        marker = os.path.join(directory, "%s.%d-%d-%s" % (self.win.code, dead, 12345, "0" * 32))
        with open(marker, "w", encoding="ascii"):
            pass  # 只留下文件名，正文是空的——真实崩溃就停在这里
        self.assertEqual(sf_config.target_tick_publisher_status(self.win.code), "INACTIVE")
        self.assertFalse(sf_config.target_tick_publisher_active(self.win.code),
                         "空正文登记不能把这只股票永远算成有人在发布")
        self.assertFalse(os.path.exists(marker), "持有者已确认不在的登记要顺手清掉")

    def test_store_prune_names_the_expired_dormant_targets_it_drops(self):
        """休眠条目超上限要先丢已过期的，并把被剪的股票报给诊断包（只在日志说不算告知）。"""
        import sf_config
        config_path = os.path.join(self.tmp_path, "config.json")
        expired = targets.make_alert(targets.TARGET_DIR_ABOVE, 10.0)
        expired.update(code="sh600000", session_date="2026-10-15")
        dormant = [expired] + [dict(expired, code="sh6000%02d" % index) for index in range(1, 13)]
        with open(config_path, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "code": "sh600519", "alerts": dormant}, stream)
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", config_path))
        self.track_patch(mock.patch.object(sf_config, "CONFIG_BACKUP_PATH", config_path + ".bak"))
        live = targets.make_alert(targets.TARGET_DIR_ABOVE, 99.0)
        live.update(code="sh600519", session_date="2026-10-15")
        with mock.patch.object(sf_config, "_session_is_past",
                               side_effect=lambda alert: alert.get("code") == "sh600000"):
            self.assertTrue(sf_config.save_alerts([live], code="sh600519"))
        report = sf_config.store_prune_report()
        self.assertIn("sh600000", str(report), "被剪掉的股票必须能在诊断包里查到名字")
        self.assertEqual([a["code"] for a in real_load_alerts("sh600001")], ["sh600001"],
                         "未过期的休眠条目不能被一起剪掉")

    # ---------- 故障路径：锁拿不到 / 显示队列满 / 队列读不出来 ----------

    def _denied_lock(self):
        from contextlib import contextmanager

        @contextmanager
        def denied(**kwargs):
            raise PermissionError("lock unavailable")
            yield
        return mock.patch.object(sf_config, "target_tick_transaction", denied)

    def _isolated_config(self, alerts=()):
        config_path = os.path.join(self.tmp_path, "config.json")
        with open(config_path, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "code": "sh600183", "alerts": list(alerts)}, stream)
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", config_path))
        self.track_patch(mock.patch.object(sf_config, "CONFIG_BACKUP_PATH", config_path + ".bak"))
        return config_path

    def _lease_count(self):
        """数还活着的发布登记：泄漏一条，这只股票的过期清理就被永久挡住。"""
        try:
            return len(os.listdir(sf_config.CONFIG_PATH + ".target-tick-publishers"))
        except FileNotFoundError:
            return 0

    def test_revision_and_store_warnings_survive_a_broken_tooltip(self):
        """提示气泡本身画不出来时只能留一行日志，不能把行情线程或主线程带崩。"""
        from PyQt6.QtWidgets import QToolTip

        def boom(*args, **kwargs):
            raise RuntimeError("tooltip unavailable")

        with mock.patch.object(window, "log_line") as logged, \
                mock.patch.object(QToolTip, "showText", side_effect=boom):
            self.win._show_target_revision_unavailable()  # discipline-ok(R5): 被测的就是这个提示入口本身
            self.win._show_target_store_unavailable()  # discipline-ok(R5): 同上
        phrases = [call.args[0] for call in logged.call_args_list]
        self.assertTrue(any("target revision warning display failed" in text for text in phrases), phrases)
        self.assertTrue(any("target tick warning display failed" in text for text in phrases), phrases)

    def test_a_full_display_queue_still_lets_the_newest_quote_through(self):
        """UI 卡住把队列塞满时，挤掉最旧一条也要让最新行情进去，并先保住发布登记。"""
        import queue
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        self._isolated_config()
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "jam.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离持久队列只能挂在这个成员上
        while True:
            try:
                self.win.q.put_nowait(("junk", object()))
            except queue.Full:
                break
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        with self._denied_lock(), mock.patch.object(self.win, "_wake_ui"):  # discipline-ok(R5): 只挡唤醒，其余走真实现
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 被测入口本身
        items = []
        while not self.win.q.empty():
            items.append(self.win.q.get_nowait())
        self.assertIs(items[-1][1][1], quote, "最新一条必须在队列里")
        self.assertEqual(ticks.unsaved_count(), 1)

    def test_a_repeated_quote_does_not_take_a_second_lease(self):
        """同一笔行情第二次被拒时，第二条登记要当场归还；只留暂存行情那一条。"""
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        self._isolated_config()
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "twice.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离持久队列只能挂在这个成员上
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        with self._denied_lock(), mock.patch.object(self.win, "_wake_ui"):  # discipline-ok(R5): 只挡唤醒
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 被测入口本身
            self.assertTrue(sf_config.target_tick_publisher_active(self.win.code))
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 同一笔第二次
        self.assertEqual(self._lease_count(), 1,
                         "没有新数据被暂存时，第二次那条登记要当场归还，否则会一直挡住清理")
        self.assertEqual(ticks.unsaved_count(), 1, "同一笔行情只许暂存一条")

    def test_a_display_queue_that_refuses_everything_reports_full_and_releases(self):
        """显示口彻底坏了：当场报队列满，并立即归还发布登记。"""
        import queue
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        self._isolated_config()
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "dead.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离持久队列只能挂在这个成员上
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        with self._denied_lock(), mock.patch.object(self.win, "_wake_ui"):  # discipline-ok(R5): 只挡唤醒
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 先让这笔进暂存
            with mock.patch.object(self.win.q, "put_nowait", side_effect=queue.Full), \
                    mock.patch.object(self.win.q, "get_nowait", side_effect=queue.Empty):
                with self.assertRaises(queue.Full):
                    self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 同一笔第二次
        self.assertEqual(self._lease_count(), 1,
                         "显示不出去时归还第二次那条，暂存行情自己那条要继续挡住清理")

    def test_an_unexpected_error_in_the_locked_put_is_not_swallowed(self):
        """非容量类的意外必须原样抛出：静默等于目标价提醒悄悄不再工作。"""
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        self._isolated_config()
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "boom.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离持久队列只能挂在这个成员上
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        with mock.patch.object(ticks, "add_ordered", side_effect=ValueError("bad payload")), \
                mock.patch.object(self.win, "_wake_ui"):  # discipline-ok(R5): 只挡唤醒
            with self.assertRaises(ValueError):
                self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 被测入口本身
        self.assertFalse(sf_config.target_tick_publisher_active(self.win.code))

    def test_revision_lock_timeout_keeps_the_quote_visible_and_warns(self):
        """事务锁被别的进程占着：行情照旧显示，但必须说出"这笔没版本、提醒可能漏"。"""
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import RetainedCapacityError
        self._isolated_config()
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "timeout.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离持久队列只能挂在这个成员上
        paused, store_gap = [], []
        self.win.target_revision_warning.connect(lambda: paused.append(True))
        self.win.target_store_warning.connect(lambda: store_gap.append(True))
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        with mock.patch.object(sf_config, "config_transaction", side_effect=TimeoutError("held")), \
                mock.patch.object(ticks, "add_ordered", side_effect=RetainedCapacityError("store full")), \
                mock.patch.object(ticks, "unsaved_count", return_value=1), \
                mock.patch.object(self.win, "_wake_ui"):  # discipline-ok(R5): 只挡唤醒
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 被测入口本身
        self.assertEqual(paused, [True], "取不到目标版本必须告知一次")
        self.assertEqual(store_gap, [True], "没能落盘时缺口提示也要发一次")
        self.assertIsNone(quote.target_revision)

    def test_capacity_error_without_a_warning_channel_still_propagates(self):
        """轻量宿主没有信号时也不能吞掉容量异常，并且必须留下缺口日志。"""
        from contextlib import nullcontext
        from types import SimpleNamespace
        import queue
        from sf_target_tick_store import CapacityError
        self._isolated_config()
        ticks = mock.Mock(persistent=True)
        ticks.add_ordered.side_effect = CapacityError("volatile queue full")
        host = SimpleNamespace(code="sh600183", _target_ticks=ticks, q=queue.Queue(4),
                               _wake_ui=lambda: None)
        with mock.patch.object(sf_config, "config_transaction", return_value=nullcontext()), \
                mock.patch.object(window, "log_line") as logged:
            with self.assertRaises(CapacityError):
                window.FloatWindow._q_put_locked(host, ("quote", ("sh600183", {"price": "101"})))  # discipline-ok(R5): 被测入口本身，宿主是替身
        self.assertTrue(any("volatile queue exhausted" in call.args[0] for call in logged.call_args_list))

    def test_adoption_defers_cleanup_when_the_queue_cannot_be_read(self):
        """读不出暂存队列时按"可能有未落盘行情"处理：不清目标，也不报错。"""
        from sf_quote_delivery import TargetTickBuffer
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0)
        alert.update(code=self.win.code, session_date="2026-10-15")
        self._isolated_config([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "adopt.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离持久队列只能挂在这个成员上
        self.track_patch(mock.patch.object(window, "load_alerts", return_value=[alert]))
        self.track_patch(mock.patch.object(self.win, "_targets_enabled", return_value=True))
        self.track_patch(mock.patch.object(ticks, "has_pending", side_effect=OSError("store locked")))
        with mock.patch.object(window, "log_line") as logged, \
                mock.patch.object(self.win._clear_timer, "start") as started:  # discipline-ok(R5): 看门狗是否被续上只能在这里量
            self.win._adopt_targets_for_locked(self.win.code)  # discipline-ok(R5): 被测的就是这个接管入口
        self.assertTrue(any("target adoption queue read failed" in call.args[0] for call in logged.call_args_list))
        self.assertEqual(self.win._alerts, [alert],  # discipline-ok(R5): 目标是否被清空只能读运行态
                        "读不出队列时不能把用户的目标丢掉")
        started.assert_called_once_with(1000)

    def test_session_is_past_rejects_non_mapping_alert(self):
        self.assertFalse(sf_config._session_is_past(None))  # discipline-ok(R5): 验证非法输入不能当作过期目标。

    # ---------- 跨股逐笔消费、通知外发与面板清空入口的故障分支 ----------

    def _old_quote(self, price="101", revision=("rev", 1, ("{}",)), received=None):
        import sf_quote
        quote = sf_quote.QuoteData({"code": "sh600000", "price": price}, "20261015145900")
        quote.received_at = (datetime(2026, 10, 15, 14, 59, tzinfo=timezone(timedelta(hours=8)))
                             if received is None else received)
        quote.target_revision = revision
        return quote

    def _old_tick_stub(self, item):
        class Stub:
            def __init__(self):
                self.blocked_stocks = set()
                self.acknowledged = []

            def oldest_persistent(self):
                return item

            def acknowledge(self, quote):
                self.acknowledged.append(quote)
        return Stub()

    def test_a_stopped_target_window_leaves_another_stocks_tick_queued(self):
        self._isolated_config()
        ticks = self._old_tick_stub(("sh600000", self._old_quote()))
        with mock.patch.object(self.win, "_targets_enabled", return_value=False):  # discipline-ok(R5): 关掉目标价开关只能改这个内部状态
            self.assertFalse(self.win._consume_old_target_tick(ticks))  # discipline-ok(R5): 被测的就是这个跨股逐笔入口
        self.assertEqual(ticks.acknowledged, [], "功能关闭时不能替它把行情判成已处理")

    def test_an_unknown_revision_tick_pauses_the_reminder_once(self):
        self._isolated_config()
        paused = []
        self.win.target_revision_warning.connect(lambda: paused.append(True))
        ticks = self._old_tick_stub(("sh600000", self._old_quote(revision=None)))
        with mock.patch.object(self.win, "_targets_enabled", return_value=True):  # discipline-ok(R5): 打开开关
            self.assertFalse(self.win._consume_old_target_tick(ticks))  # discipline-ok(R5): 被测入口本身
        self.assertEqual(paused, [True], "版本未知必须说出提醒可能遗漏")
        self.assertEqual(ticks.acknowledged, [], "版本未知的行情必须留在队列里等")

    def test_a_blocked_stock_is_named_even_when_nothing_can_be_consumed(self):
        self._isolated_config()
        paused = []
        self.win.target_revision_warning.connect(lambda: paused.append(True))
        ticks = self._old_tick_stub(None)
        ticks.blocked_stocks = {"sh600000"}
        self.assertTrue(self.win._consume_old_target_tick(ticks))  # discipline-ok(R5): 被测入口本身
        self.assertEqual(paused, [True], "被挡住的股票要立即报出，而不是等下一次")

    def test_an_untrusted_config_leaves_another_stocks_tick_queued(self):
        self._isolated_config()
        ticks = self._old_tick_stub(("sh600000", self._old_quote()))
        with mock.patch.object(self.win, "_targets_enabled", return_value=True), \
                mock.patch.object(sf_config, "config_unavailable", return_value=True):  # discipline-ok(R5): 打开开关
            self.assertFalse(self.win._consume_old_target_tick(ticks))  # discipline-ok(R5): 被测入口本身
        self.assertEqual(ticks.acknowledged, [])

    def test_a_tick_whose_targets_already_moved_is_dropped(self):
        """目标已被别的进程改过：这条旧行情过期了，认掉它，别拿新版本去判旧价。"""
        config_path = self._isolated_config()
        ticks = self._old_tick_stub(("sh600000", self._old_quote()))
        with mock.patch.object(self.win, "_targets_enabled", return_value=True), \
                mock.patch.object(sf_config, "config_unavailable", return_value=False), \
                mock.patch.object(sf_config, "alerts_revision", return_value=("new", 2, ())), \
                mock.patch.object(sf_config, "load_alerts") as unread:  # discipline-ok(R5): 打开开关 + "没去读新清单"的观测点
            self.assertTrue(self.win._consume_old_target_tick(ticks))  # discipline-ok(R5): 被测入口本身
        self.assertEqual(ticks.acknowledged, [ticks.oldest_persistent()[1]])
        # 丢弃这条旧行情的全部后果必须只有"认掉"：既不能去读新版目标判旧价，
        # 也不能顺手把别的进程刚写的目标清单改回去。
        unread.assert_not_called()
        with open(config_path, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)["alerts"], [])
        self.assertEqual(sf_config.load_target_notification_outbox(), [])

    def test_an_out_of_session_tick_is_dropped_without_notification(self):
        """收盘后回放的旧行情不能判目标：认掉它，别在夜里提醒一条昨天的价。

        原来这条用例把 received 传成 None，而夹具把 None 当成"用默认时刻"，
        于是行情时间仍是交易时段内 —— 用例其实走的是"目标清单为空"那条支路，
        和名字说的完全不是一件事（现在按真实的盘后时刻构造前提）。
        """
        self._isolated_config()
        after_hours = datetime(2026, 10, 15, 20, 30, tzinfo=timezone(timedelta(hours=8)))
        quote = self._old_quote(received=after_hours)
        ticks = self._old_tick_stub(("sh600000", quote))
        with mock.patch.object(self.win, "_targets_enabled", return_value=True), \
                mock.patch.object(sf_config, "config_unavailable", return_value=False), \
                mock.patch.object(sf_config, "alerts_revision", return_value=quote.target_revision), \
                mock.patch.object(sf_config, "load_alerts") as unread:  # discipline-ok(R5): 打开开关 + "没去读清单"的观测点
            self.assertTrue(self.win._consume_old_target_tick(ticks))  # discipline-ok(R5): 被测入口本身
        self.assertEqual(ticks.acknowledged, [quote])
        unread.assert_not_called()
        self.assertEqual(sf_config.load_target_notification_outbox(), [])

    def test_a_tick_without_a_usable_price_is_dropped(self):
        """坏价格不能卡住整条队列：认掉它，继续跑下一条。"""
        import sf_quote
        self._isolated_config()
        quote = self._old_quote(price="abc")
        ticks = self._old_tick_stub(("sh600000", quote))
        with mock.patch.object(self.win, "_targets_enabled", return_value=True), \
                mock.patch.object(sf_config, "alerts_revision", return_value=quote.target_revision), \
                mock.patch.object(sf_quote, "_is_trading_time", return_value=True), \
                mock.patch.object(sf_quote, "quote_is_current", return_value=True):  # discipline-ok(R5): 打开开关
            self.assertTrue(self.win._consume_old_target_tick(ticks))  # discipline-ok(R5): 被测入口本身
        self.assertEqual(ticks.acknowledged, [quote])

    def test_a_zero_price_tick_is_dropped(self):
        import sf_quote
        self._isolated_config()
        quote = self._old_quote(price="0")
        ticks = self._old_tick_stub(("sh600000", quote))
        with mock.patch.object(self.win, "_targets_enabled", return_value=True), \
                mock.patch.object(sf_config, "alerts_revision", return_value=quote.target_revision), \
                mock.patch.object(sf_quote, "_is_trading_time", return_value=True), \
                mock.patch.object(sf_quote, "quote_is_current", return_value=True):  # discipline-ok(R5): 打开开关
            self.assertTrue(self.win._consume_old_target_tick(ticks))  # discipline-ok(R5): 被测入口本身
        self.assertEqual(ticks.acknowledged, [quote])

    def test_a_config_that_goes_unreadable_midway_stops_before_the_write(self):
        import sf_quote
        self._isolated_config()
        quote = self._old_quote()
        ticks = self._old_tick_stub(("sh600000", quote))
        with mock.patch.object(self.win, "_targets_enabled", return_value=True), \
                mock.patch.object(sf_config, "config_unavailable", side_effect=[False, True]), \
                mock.patch.object(sf_config, "alerts_revision", return_value=quote.target_revision), \
                mock.patch.object(sf_quote, "_is_trading_time", return_value=True), \
                mock.patch.object(sf_quote, "quote_is_current", return_value=True):  # discipline-ok(R5): 打开开关
            self.assertFalse(self.win._consume_old_target_tick(ticks))  # discipline-ok(R5): 被测入口本身
        self.assertEqual(ticks.acknowledged, [], "读不出配置时不能把这条行情认掉")

    def test_a_retry_quote_with_unknown_revision_is_blocked_and_warns(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        self._isolated_config()
        path = os.path.join(self.tmp_path, "norev.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离持久队列只能挂在这个成员上
        paused = []
        self.win.target_revision_warning.connect(lambda: paused.append(True))
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        quote.target_revision = None
        quote._target_store_record = (path, self.win.code, 1)  # discipline-ok(R5): 归属标记只有这一个内部字段
        self.assertFalse(self.win._evaluate_received_quote(quote))  # discipline-ok(R5): 被测的就是这个逐笔判定入口
        self.assertEqual(paused, [True])
        self.assertTrue(self.win._target_retry_blocked,  # discipline-ok(R5): 拦截标记没有对外读数
                        "版本未知时必须挡住重试，不能拿旧价配新目标")

    def test_a_retry_attempt_without_revision_is_retained_not_fired(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        self._isolated_config()
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "retry.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离持久队列只能挂在这个成员上
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0)
        alert.update(code=self.win.code, session_date="2026-10-15")
        self.win._alerts = [alert]  # discipline-ok(R5): 运行态目标没有 setter
        paused = []
        self.win.target_revision_warning.connect(lambda: paused.append(True))
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        quote.target_revision = None
        with mock.patch.object(self.win, "_targets_enabled", return_value=True), \
                mock.patch.object(self.win, "_is_trading_time", return_value=True), \
                mock.patch.object(window, "quote_is_current", return_value=True):  # discipline-ok(R5): 这两处都是被测入口的内部前置
            self.assertFalse(self.win._evaluate_received_quote(quote))  # discipline-ok(R5): 被测入口本身
        self.assertEqual(paused, [True])

    def test_a_startup_retry_whose_target_is_gone_is_not_resaved(self):
        self._isolated_config()
        event = {"id": "abc", "code": "sh600519", "title": "t", "body": "b", "semantic_key": "k"}
        self.win._target_startup_retry = ("sh600519", [{"target": 1.0}], [], [event])  # discipline-ok(R5): 待重试登记没有对外写入口
        with mock.patch.object(sf_config, "load_target_notification_outbox", return_value=[]), \
                mock.patch.object(sf_config, "load_alerts", return_value=[{"target": 2.0}]):
            self.win._drain_target_notification_outbox()  # discipline-ok(R5): 被测的就是这个外发入口
        self.assertIsNone(self.win._target_startup_retry,  # discipline-ok(R5): 同上
                          "目标已被别的进程改掉时，这条待重试要作废而不是覆盖")

    def test_a_failing_startup_retry_leaves_the_ledger_for_the_next_round(self):
        self._isolated_config()
        event = {"id": "abc", "code": "sh600519", "title": "t", "body": "b", "semantic_key": "k"}
        pending = ("sh600519", [{"target": 1.0}], [], [event])
        self.win._target_startup_retry = pending  # discipline-ok(R5): 待重试登记没有对外写入口
        with mock.patch.object(sf_config, "config_transaction", side_effect=OSError("disk gone")), \
                mock.patch.object(window, "log_line") as logged:
            self.win._drain_target_notification_outbox()  # discipline-ok(R5): 被测入口本身
        self.assertEqual(self.win._target_startup_retry, pending,  # discipline-ok(R5): 同上
                        "这一步失败时不能把待补发的提醒忘掉")
        self.assertTrue(any("target startup retry failed" in call.args[0] for call in logged.call_args_list))

    def _outbox_host(self):
        import queue
        from types import SimpleNamespace

        class RaisingSignal:
            def emit(self):
                raise RuntimeError("窗口已销毁")
        return SimpleNamespace(_threads_stopped=False, _target_startup_retry=None,
                               _target_notification_results=queue.Queue(),
                               _target_notification_inflight={}, _target_notification_acks=set(),
                               _restore_window=lambda: None,
                               target_notification_wakeup=RaisingSignal())

    def test_a_notification_finishing_after_the_window_died_is_still_acked(self):
        self._isolated_config()
        event = {"id": "abc", "code": "sh600519", "title": "t", "body": "b", "semantic_key": "k"}
        host = self._outbox_host()
        results = host._target_notification_results  # discipline-ok(R5): 替身宿主的结果总线就是被测对象
        acked = []

        def deliver(title, subtitle, body, key, on_click=None, on_complete=None):
            on_complete(True)

        with mock.patch.object(sf_config, "load_target_notification_outbox", return_value=[event]), \
                mock.patch.object(sf_config, "sync_target_notification_outbox", return_value=True), \
                mock.patch.object(sf_config, "ack_target_notification_outbox",
                                  side_effect=lambda ids: acked.extend(ids) or True), \
                mock.patch.object(window, "_notify_deliver", deliver):
            window.FloatWindow._drain_target_notification_outbox(host)  # discipline-ok(R5): 被测入口本身，宿主是替身
            self.assertFalse(results.empty(), "回包先进结果总线，下一次巡检再认掉")
            self.assertEqual(acked, [])
            window.FloatWindow._drain_target_notification_outbox(host)  # discipline-ok(R5): 第二次巡检负责认账
        self.assertEqual(acked, ["abc"], "窗口没了也要把这一条认掉，否则会永远重发")
        self.assertFalse(results.empty(),
                         "认掉之后同一份外发清单又发了一次，回包重新进总线等下一轮")

    def test_a_delivery_that_cannot_start_is_logged_and_retried_later(self):
        self._isolated_config()
        event = {"id": "abc", "code": "sh600519", "title": "t", "body": "b", "semantic_key": "k"}
        host = self._outbox_host()

        def boom(*args, **kwargs):
            raise RuntimeError("通知通道坏了")

        with mock.patch.object(sf_config, "load_target_notification_outbox", return_value=[event]), \
                mock.patch.object(sf_config, "sync_target_notification_outbox", return_value=True), \
                mock.patch.object(window, "_notify_deliver", boom), \
                mock.patch.object(window, "log_line") as logged:
            window.FloatWindow._drain_target_notification_outbox(host)  # discipline-ok(R5): 被测入口本身，宿主是替身
        self.assertTrue(any("target notification delivery failed" in call.args[0]
                            for call in logged.call_args_list))

    def test_a_partially_recorded_reminder_batch_is_not_resent(self):
        self._isolated_config()
        first = {"id": "a", "code": self.win.code}
        second = {"id": "b", "code": self.win.code}
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0)
        alert.update(code=self.win.code, session_date="2026-10-15")
        self.win._alerts = [alert]  # discipline-ok(R5): 运行态目标没有 setter
        self.win._target_notification_uncertain = [first, second]  # discipline-ok(R5): 上轮不确定的批次没有对外读口
        with mock.patch.object(self.win, "_targets_enabled", return_value=True), \
                mock.patch.object(sf_config, "load_target_notification_outbox", return_value=[first]):  # discipline-ok(R5): 被测入口的内部前置
            self.assertFalse(self.win._evaluate_targets(101.0))  # discipline-ok(R5): 被测的就是这个越线判定入口
        self.assertEqual(self.win._target_notification_uncertain, [first, second],  # discipline-ok(R5): 同上
                         "只落了一半的批次不能当成功，也不能当没发生")

    def test_a_rolled_over_revision_chain_is_resolved_before_the_compare(self):
        """顺延过的目标版本要顺着链比到底，否则会把自己刚写的目标当成"已过期"丢掉。"""
        self._isolated_config()
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0)
        alert.update(code=self.win.code, session_date="2026-10-15")
        self.win._alerts = [alert]  # discipline-ok(R5): 运行态目标没有 setter
        self.win._target_expected_revision = ("stamp", "rev1")  # discipline-ok(R5): 期望版本只能写进运行态
        self.win._target_batch_rolls = {"rev1": "rev2"}  # discipline-ok(R5): 顺延链是运行态账
        with mock.patch.object(self.win, "_targets_enabled", return_value=True), \
                mock.patch.object(sf_config, "load_target_notification_outbox", return_value=[]), \
                mock.patch.object(sf_config, "alerts_revision", return_value="rev2"), \
                mock.patch.object(window, "load_alerts", return_value=[alert]) as loaded:  # discipline-ok(R5): 前置开关与比对口径
            self.win._evaluate_targets(101.0)  # discipline-ok(R5): 被测入口本身
        # 版本对得上才该继续读目标清单；逗号写法会把这句说明变成一个被丢弃的元组。
        loaded.assert_called_once_with(self.win.code)

    def test_panel_apply_empty_reports_an_unreadable_stock_instead_of_deleting(self):
        panel = self._open_saved_panel([dict(code="sh600183", direction="above", target=100.0,
                                             session_date="2026-10-15")])
        panel.current_code_fn = mock.Mock(side_effect=OSError("窗口已销毁"))
        panel._apply_empty()  # discipline-ok(R5): 被测的就是这个清空入口
        self.assertIn("无法确认股票", panel.status_label.text())
        self.assertTrue(panel.isVisible())

    def test_panel_apply_empty_refuses_after_the_stock_was_switched(self):
        panel = self._open_saved_panel([dict(code="sh600183", direction="above", target=100.0,
                                             session_date="2026-10-15")])
        panel.current_code_fn = lambda: "sh600519"
        panel._apply_empty()  # discipline-ok(R5): 被测入口本身
        self.assertIn("已切换股票", panel.status_label.text())
        self.assertTrue(panel.isVisible())

    def test_panel_apply_empty_keeps_the_targets_when_saving_blows_up(self):
        panel = self._open_saved_panel([dict(code="sh600183", direction="above", target=100.0,
                                             session_date="2026-10-15")])
        panel.current_code_fn = lambda: "sh600183"
        panel.on_apply_empty = mock.Mock(side_effect=OSError("disk gone"))
        panel._apply_empty()  # discipline-ok(R5): 被测入口本身
        self.assertIn("保存失败", panel.status_label.text())
        self.assertTrue(panel.isVisible())


    def test_target_lock_failure_cross_process_cleanup_preserves_target(self):
        import subprocess
        import sys
        import sf_quote
        from contextlib import contextmanager
        from sf_quote_delivery import TargetTickBuffer
        received = datetime(2026, 10, 15, 14, 59, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=received)
        alert.update(code="sh600183", session_date="2026-10-15")
        self._open_saved_panel([alert])
        db_path = os.path.join(self.tmp_path, "race.sqlite")
        ticks = TargetTickBuffer(persistent_path=db_path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离数据库验证跨进程竞争。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        quote.received_at = received
        @contextmanager
        def denied(**kwargs):
            raise PermissionError("lock unavailable")
            yield
        child = '''
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sf_config, stock_float_window as w
from sf_quote_delivery import TargetTickBuffer
sf_config.CONFIG_PATH, sf_config.CONFIG_BACKUP_PATH, db = sys.argv[1:]
h = SimpleNamespace(code="sh600183", _target_ticks=TargetTickBuffer(persistent_path=db),
                    _clear_timer=Mock(), _target_tick_publishers=0, update=Mock(),
                    _sync_target_panel=Mock(), _arm_clear_timer=Mock())
def persist(alerts, expected):
    with sf_config.expect_alerts_unchanged(h.code, expected):
        return sf_config.save_alerts(alerts, code=h.code)
h._persist_alerts = persist
h._clear_expired_locked = lambda: w.FloatWindow._clear_expired_locked(h)
with patch.object(w, "_beijing_now", return_value=datetime(2026, 10, 15, 15, 1, tzinfo=timezone(timedelta(hours=8)))):
    w.FloatWindow._clear_expired(h)
'''
        with mock.patch.object(sf_config, "target_tick_transaction", denied), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 触发真实行情入口。
        result = subprocess.run([sys.executable, "-c", child, sf_config.CONFIG_PATH,
                                 sf_config.CONFIG_BACKUP_PATH, db_path],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        from sf_target_tick_store import TargetTickStore
        with TargetTickStore(db_path) as store:
            stored = store.read("sh600183")
        self.assertEqual(stored, [])
        self.assertEqual(ticks.unsaved_count(), 1)
        self.assertIsNone(quote.target_revision)
        self.assertFalse(self.win.q.empty())
        self.assertTrue(real_load_alerts("sh600183"), "锁失败暂存后不能让另一进程删除目标")
        self.assertTrue(sf_config.target_tick_publisher_active("sh600183"))
        ticks.take("sh600183")
        self.assertEqual(ticks.unsaved_count(), 0)
        self.assertFalse(sf_config.target_tick_publisher_active("sh600183"))
        with TargetTickStore(db_path) as store:
            recovered = store.read("sh600183")
        self.assertEqual(len(recovered), 1)
        self.assertIsNone(recovered[0].quote.target_revision)

    def test_capacity_failure_before_display_is_not_reported_as_success(self):
        from sf_target_tick_store import CapacityError
        quote = {"code": self.win.code, "price": "101"}
        self.win._quote_fail = 0  # discipline-ok(R5): 观察未送显失败计数。
        with mock.patch.object(window, "fetch_quote", return_value=quote), \
                mock.patch.object(window, "ensure_current_quote", return_value=quote), \
                mock.patch.object(self.win, "_q_put", side_effect=CapacityError("not displayed")), \
                mock.patch.object(self.win, "_on_poll_result") as result:
            self.win._fetch_quote()  # discipline-ok(R5): 验证送显前异常。
        self.assertEqual(self.win._quote_fail, 1)  # discipline-ok(R5): 未送显必须保留失败状态。
        result.assert_called_once_with(False)

    def test_target_queue_capacity_failure_is_not_network_failure(self):
        import sf_quote
        from contextlib import contextmanager
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "fetch-full.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离队列。
        for _ in range(4096):
            ticks.retain_unsaved(self.win.code, sf_quote.QuoteData({"code": self.win.code, "price": "100"}))
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        @contextmanager
        def denied(**kwargs):
            raise PermissionError("lock unavailable")
            yield
        self.win._quote_fail = 0  # discipline-ok(R5): 验证行情抓取失败计数。
        with mock.patch.object(window, "fetch_quote", return_value=quote), \
                mock.patch.object(window, "ensure_current_quote", return_value=quote), \
                mock.patch.object(window, "get_last_quote_source", return_value="tencent"), \
                mock.patch.object(sf_config, "target_tick_transaction", denied), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_on_poll_result") as result:
            self.win._fetch_quote()  # discipline-ok(R5): 数据源成功，真实队列容量耗尽。
        self.assertFalse(self.win.q.empty(), "成功的行情应当能送到显示队列")
        self.assertEqual(self.win._quote_fail, 0)  # discipline-ok(R5): 存储故障不能冒充网络故障。
        result.assert_called_once_with(True, via_fallback=False)

    def test_target_lock_failure_with_display_full_is_not_success(self):
        import queue
        import sf_quote
        from contextlib import contextmanager
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "display-only-full.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离目标行情队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"})
        @contextmanager
        def denied(**kwargs):
            raise PermissionError("lock unavailable")
            yield
        self.win._quote_fail = 0  # discipline-ok(R5): 观察显示失败计数。
        with mock.patch.object(window, "fetch_quote", return_value=quote), \
                mock.patch.object(window, "ensure_current_quote", return_value=quote), \
                mock.patch.object(window, "get_last_quote_source", return_value="tencent"), \
                mock.patch.object(sf_config, "target_tick_transaction", denied), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win.q, "put_nowait", side_effect=queue.Full), \
                mock.patch.object(self.win.q, "get_nowait", side_effect=queue.Empty), \
                mock.patch.object(self.win, "_wake_ui"), \
                mock.patch.object(self.win, "_on_poll_result") as result:
            self.win._fetch_quote()  # discipline-ok(R5): 真实入口遇到显示队列竞争。
        self.assertEqual(self.win._quote_fail, 1)  # discipline-ok(R5): 没有送显不能报告成功。
        result.assert_called_once_with(False)

    def test_target_lock_failure_and_display_queue_full_reports_failure(self):
        import queue
        import sf_quote
        from contextlib import contextmanager
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import CapacityError, DisplayQueuedCapacityError
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "display-full.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"})
        @contextmanager
        def denied(**kwargs):
            raise PermissionError("lock unavailable")
            yield
        with mock.patch.object(sf_config, "target_tick_transaction", denied), \
                mock.patch.object(ticks, "retain_unsaved", side_effect=CapacityError("full")), \
                mock.patch.object(self.win.q, "put_nowait", side_effect=queue.Full), \
                mock.patch.object(self.win.q, "get_nowait", side_effect=queue.Empty), \
                mock.patch.object(self.win, "_wake_ui"):
            with self.assertRaises(CapacityError) as caught:
                self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 显示无法入队。
        self.assertNotIsInstance(caught.exception, DisplayQueuedCapacityError)

    def test_target_lock_failure_full_queue_still_displays_quote(self):
        import sf_quote
        from contextlib import contextmanager
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import CapacityError
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "lock-full.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离队列以模拟容量耗尽。
        for _ in range(4096):
            ticks.retain_unsaved(self.win.code, sf_quote.QuoteData({"code": self.win.code, "price": "100"}))
        quote = sf_quote.QuoteData({"code": self.win.code, "name": "测试股", "price": "101.00",
                                    "pct": "1.0", "high": "101.00", "low": "99.00"}, "20261015100000")
        @contextmanager
        def denied(**kwargs):
            raise PermissionError("lock unavailable")
            yield
        warnings = []
        self.win.target_revision_warning.connect(lambda: warnings.append(True))
        from PyQt6.QtWidgets import QToolTip
        with mock.patch.object(sf_config, "target_tick_transaction", denied), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(QToolTip, "showText") as show, \
                mock.patch.object(self.win, "_wake_ui"):
            try:
                self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 验证目标锁失败下的显示入口。
            except CapacityError:
                pass
            self.ensure_qapp().processEvents()
        self.assertTrue(warnings)
        messages = [str(call.args[1]) for call in show.call_args_list]
        self.assertTrue(any("提醒可能遗漏" in message for message in messages))
        self.assertTrue(any("提醒已暂停" in message for message in messages))
        self.assertFalse(self.win.q.empty(), "目标价队列满时显示行情也必须可达")
        self.win.running = True
        self.win._disp_price = None  # discipline-ok(R5): 观察首次行情实际显示状态。
        with mock.patch.object(window, "quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_evaluate_targets") as evaluate:
            self.win._poll()  # discipline-ok(R5): 验证真实行情应用路径。
        self.assertEqual(self.win._disp_price, 101.0)  # discipline-ok(R5): 核验显示价格。
        evaluate.assert_not_called()

    def test_target_lock_permission_failure_retains_unknown_tick_and_display(self):
        import sf_quote
        from contextlib import contextmanager
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "lock-failure.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        @contextmanager
        def denied(**kwargs):
            raise PermissionError("lock unavailable")
            yield
        warnings = []
        self.win.target_revision_warning.connect(lambda: warnings.append(True))
        with mock.patch.object(sf_config, "target_tick_transaction", denied), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_wake_ui"):
            try:
                self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 执行真实接收入口。
                raised = None
            except Exception as exc:
                raised = exc
            self.ensure_qapp().processEvents()
        self.assertIsNone(raised, "锁文件不可用不能丢弃行情")
        with TargetTickStore(os.path.join(self.tmp_path, "lock-failure.sqlite")) as store:
            stored = store.read(self.win.code)
        self.assertEqual(stored, [])
        self.assertEqual(ticks.unsaved_count(), 1)
        self.assertIsNone(quote.target_revision)
        self.assertFalse(self.win.q.empty())
        self.assertTrue(warnings)

    def test_quote_is_retained_when_config_lock_times_out(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        with mock.patch.object(sf_config, "config_transaction", side_effect=TimeoutError("busy")), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 锁失败仍保留行情。
        self.assertTrue(ticks.has_pending(self.win.code))
        from sf_target_tick_store import TargetTickStore
        with TargetTickStore(os.path.join(self.tmp_path, "ticks.sqlite")) as store:
            retained = store.read(self.win.code)
            self.assertEqual(len(retained), 1)
            self.assertIsNone(retained[0].quote.target_revision)

    def test_config_lock_full_buffers_are_not_reported_as_displayed(self):
        import queue
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import CapacityError, DisplayQueuedCapacityError
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "both-full.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        with mock.patch.object(sf_config, "config_transaction", side_effect=TimeoutError("busy")), \
                mock.patch.object(ticks, "add_ordered", side_effect=CapacityError("full")), \
                mock.patch.object(self.win.q, "put_nowait", side_effect=queue.Full), \
                mock.patch.object(self.win.q, "get_nowait", side_effect=queue.Empty):
            with self.assertRaises(CapacityError) as caught:
                self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 双队列均不可用。
        self.assertNotIsInstance(caught.exception, DisplayQueuedCapacityError)

    def test_config_lock_full_buffer_minimized_display_does_not_evaluate_target(self):
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import CapacityError
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "config-minimized.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离目标行情队列。
        self.win.running = True
        self.win._minimized = True  # discipline-ok(R5): 模拟缩到 Dock。
        quote = {"code": self.win.code, "name": "测试股", "price": "101.00", "pct": "1.0",
                 "high": "101.00", "low": "99.00"}
        with mock.patch.object(sf_config, "config_transaction", side_effect=TimeoutError("busy")), \
                mock.patch.object(ticks, "add_ordered", side_effect=CapacityError("full")), \
                mock.patch.object(self.win, "_wake_ui"):
            with self.assertRaises(CapacityError):
                self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 模拟保存失败。
        with mock.patch.object(window, "quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_evaluate_received_quote") as evaluate:
            self.win._poll()  # discipline-ok(R5): 验证缩小状态的显示消费。
        self.assertEqual(self.win._disp_price, 101.0)  # discipline-ok(R5): 检查显示价格。
        evaluate.assert_not_called()

    def test_config_lock_full_buffer_display_does_not_evaluate_target(self):
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import CapacityError
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "config-display.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离目标行情队列。
        self.win.running = True
        quote = {"code": self.win.code, "name": "测试股", "price": "101.00", "pct": "1.0",
                 "high": "101.00", "low": "99.00"}
        with mock.patch.object(sf_config, "config_transaction", side_effect=TimeoutError("busy")), \
                mock.patch.object(ticks, "add_ordered", side_effect=CapacityError("full")), \
                mock.patch.object(self.win, "_wake_ui"):
            with self.assertRaises(CapacityError):
                self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 模拟保存失败。
        with mock.patch.object(window, "quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_evaluate_received_quote") as evaluate:
            self.win._poll()  # discipline-ok(R5): 实际显示消费路径。
        self.assertEqual(self.win._disp_price, 101.0)  # discipline-ok(R5): 检查显示价格。
        evaluate.assert_not_called()

    def test_config_lock_timeout_full_buffer_still_queues_display(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import CapacityError, DisplayQueuedCapacityError
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "config-full.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        with mock.patch.object(sf_config, "config_transaction", side_effect=TimeoutError("busy")), \
                mock.patch.object(ticks, "add_ordered", side_effect=CapacityError("full")), \
                mock.patch.object(self.win, "_wake_ui"):
            with self.assertRaises(DisplayQueuedCapacityError):
                self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 检查显示与目标故障分离。
        self.assertFalse(self.win.q.empty())

    def test_lock_timeout_and_full_volatile_queue_warns_about_gap(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import CapacityError
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 隔离持久队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        warnings = []
        self.win.target_store_warning.connect(lambda: warnings.append(True))
        with mock.patch.object(sf_config, "config_transaction", side_effect=TimeoutError("busy")), \
                mock.patch.object(ticks, "add_ordered", side_effect=CapacityError("full")):
            with self.assertRaises(CapacityError):
                self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 模拟无法保存。
            self.ensure_qapp().processEvents()
        self.assertEqual(warnings, [True])

    def test_lock_timeout_tick_inflight_does_not_allow_expiry_cleanup(self):
        import threading
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        received = datetime(2026, 10, 15, 14, 59, tzinfo=timezone(timedelta(hours=8)))
        later = datetime(2026, 10, 15, 15, 1, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=received)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        quote.received_at = received
        entered = threading.Event()
        resume = threading.Event()
        actual_add = ticks.add_ordered
        actual_transaction = sf_config.config_transaction
        def transaction(*args, **kwargs):
            if threading.current_thread().name == "quote-test":
                raise TimeoutError("busy")
            return actual_transaction(*args, **kwargs)
        def delayed_add(code, value):
            entered.set()
            if not resume.wait(3):
                raise TimeoutError("test synchronization failed")
            return actual_add(code, value)
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=later))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=later))
        with mock.patch.object(sf_config, "config_transaction", side_effect=transaction), \
                mock.patch.object(ticks, "add_ordered", side_effect=delayed_add), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_wake_ui"):
            producer = threading.Thread(name="quote-test", target=self.win._q_put,  # discipline-ok(R5): 驱动真实生产线程。
                                        args=(("quote", (self.win.code, quote)),))
            producer.start()
            try:
                self.assertTrue(entered.wait(3))
                self.win._clear_expired()  # discipline-ok(R5): 检查未知版本行情入队前的清理。
            finally:
                resume.set()
                producer.join(3)
            self.assertFalse(producer.is_alive())
        self.assertTrue(real_load_alerts(self.win.code))
        self.assertTrue(ticks.has_pending(self.win.code))

    def test_adoption_does_not_discard_captured_tick_inflight(self):
        import threading
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        received = datetime(2026, 10, 15, 14, 59, tzinfo=timezone(timedelta(hours=8)))
        later = datetime(2026, 10, 15, 15, 1, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=received)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        quote.received_at = received
        entered, proceed = threading.Event(), threading.Event()
        original = ticks.add_ordered
        def delayed(code, value):
            entered.set()
            if not proceed.wait(3):
                raise TimeoutError("test synchronization failed")
            return original(code, value)
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=later))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=later))
        actual_transaction = sf_config.config_transaction
        def transaction(*args, **kwargs):
            if threading.current_thread().name == "adoption-quote-test":
                raise TimeoutError("busy")
            return actual_transaction(*args, **kwargs)
        with mock.patch.object(sf_config, "config_transaction", side_effect=transaction), \
                mock.patch.object(ticks, "add_ordered", side_effect=delayed), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_wake_ui"):
            producer = threading.Thread(name="adoption-quote-test", target=self.win._q_put,  # discipline-ok(R5): 验证切股与入队交错。
                                        args=(("quote", (self.win.code, quote)),))
            producer.start()
            try:
                self.assertTrue(entered.wait(3))
                self.win._adopt_targets_for(self.win.code)  # discipline-ok(R5): 生产者尚未入队。
            finally:
                proceed.set()
                producer.join(3)
            self.assertFalse(producer.is_alive())
        self.assertTrue(real_load_alerts(self.win.code))
        self.assertTrue(ticks.has_pending(self.win.code))

    def test_expiry_cleanup_waits_for_captured_tick_to_be_queued(self):
        import threading
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        received = datetime(2026, 10, 15, 14, 59, tzinfo=timezone(timedelta(hours=8)))
        later = datetime(2026, 10, 15, 15, 1, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=received)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用独立队列。
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        quote.received_at = received
        captured = threading.Event()
        proceed = threading.Event()
        original = ticks.add_ordered
        def delayed_add(code, value):
            captured.set()
            if not proceed.wait(3):
                raise TimeoutError("producer synchronization failed")
            return original(code, value)
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=later))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=later))
        with mock.patch.object(ticks, "add_ordered", side_effect=delayed_add), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_wake_ui"):
            producer = threading.Thread(target=self.win._q_put, args=(  # discipline-ok(R5): 使用真实行情入口验证并发。
                ("quote", (self.win.code, quote)),))  # discipline-ok(R5): 验证行情捕获后尚未入队的时序。
            producer.start()
            try:
                self.assertTrue(captured.wait(3))
                self.win._clear_expired()  # discipline-ok(R5): 行情已捕获旧版本但尚未入队。
            finally:
                proceed.set()
                producer.join(3)
            self.assertFalse(producer.is_alive())
        self.assertTrue(real_load_alerts(self.win.code))
        self.assertTrue(ticks.has_pending(self.win.code))

    def test_persistent_crossing_uses_received_time_after_session_close(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        received = datetime(2026, 10, 15, 14, 59, 0, tzinfo=timezone(timedelta(hours=8)))
        later = datetime(2026, 10, 15, 15, 1, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=received)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=later))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=later))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=False))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        quote.received_at = received
        quote.target_revision = sf_config.alerts_revision(self.win.code)
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.add_ordered(self.win.code, quote)
        self.win._poll()  # discipline-ok(R5): 收盘后处理盘中收到的行情。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_main_window_persistent_quote_completes_and_acknowledges(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.win._target_ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))  # discipline-ok(R5): 注入独立持久数据库。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window, "quote_is_current", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        quote.received_at = now
        self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 真实入队路径。
        self.win._poll()  # discipline-ok(R5): 真实消费和确认路径。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")
        self.assertEqual(self.win._target_ticks.take(self.win.code), [])  # discipline-ok(R5): 验证持久队列确认。
        self.assertEqual(len(sf_config.load_target_notification_outbox()), 1)

    def test_unavailable_config_does_not_ack_persistent_tick_when_alerts_empty(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        self.win._alerts = []  # discipline-ok(R5): 模拟配置不可用导致目标未装载。
        self.win.running = True
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.add_ordered(self.win.code, quote)
        with mock.patch.object(sf_config, "config_unavailable", return_value=True):
            self.win._poll()  # discipline-ok(R5): 配置不可用时不能确认行情。
        self.assertEqual(len(ticks.take(self.win.code)), 1)

    def test_persistent_store_failure_keeps_received_quote_for_retry(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "ticks.sqlite")
        self.win._target_ticks = TargetTickBuffer(persistent_path=path)  # discipline-ok(R5): 注入隔离持久队列。
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window, "quote_is_current", return_value=True))
        self.track_patch(mock.patch("sf_quote_delivery.quote_is_current", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        quote.received_at = now
        with mock.patch.object(TargetTickStore, "save", side_effect=OSError("disk busy")):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 模拟接收时持久化失败。
        self.win._poll()  # discipline-ok(R5): 资源恢复后重试已接收行情。
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "hit")

    def test_failed_poll_preserves_all_later_received_ticks_in_order(self):
        self.win._alerts = [{"target": 100}]  # discipline-ok(R5): 建立目标价运行态。
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(self.win, "_apply_quote"))
        self.win.running = True
        for price in (101, 99, 103, 90, 110):
            self.win._q_put(("quote", (self.win.code, {"code": self.win.code, "price": str(price)})))  # discipline-ok(R5): 模拟真实接收顺序。
        with mock.patch.object(self.win, "_evaluate_targets", return_value=False):
            self.win._poll()  # discipline-ok(R5): 模拟第一笔事务失败。
        pending = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 验证重试队列中实际保留的行情。
        self.assertEqual([quote["price"] for quote in pending], ["101", "99", "103", "90", "110"])

    def test_failed_quote_is_not_queued_twice_by_display(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.win.running = True
        quote = {"code": self.win.code, "price": "101", "pct": "1", "name": "测试",
                 "high": "102", "low": "98", "pre_close": "100"}
        self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 使用真实队列和显示路径。
        with mock.patch.object(sf_config, "_atomic_write_json", side_effect=OSError("busy")):
            self.win._poll()  # discipline-ok(R5): 目标写入失败后继续真实显示。
        pending = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 验证重试队列对象身份。
        self.assertEqual(len(pending), 1)
        self.assertIs(pending[0], quote)

    def test_minimized_failed_quote_is_not_queued_twice_by_display(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.win.running = True
        self.win._minimized = True  # discipline-ok(R5): 驱动缩到 Dock 的真实轮询分支。
        quote = {"code": self.win.code, "price": "101", "pct": "1", "name": "测试",
                 "high": "102", "low": "98", "pre_close": "100"}
        self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 实际行情入队。
        with mock.patch.object(sf_config, "_atomic_write_json", side_effect=OSError("busy")):
            self.win._poll()  # discipline-ok(R5): 写入失败后仍更新显示。
        pending = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 验证重试队列对象身份。
        self.assertEqual(len(pending), 1)
        self.assertIs(pending[0], quote)

    def test_blocked_retry_full_volatile_queue_warns(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import CapacityError
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "blocked-full.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离重试队列。
        self.track_patch(mock.patch.object(sf_quote, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        quote.target_revision = ("old", "revision", ())
        self.win._target_retry_blocked = True  # discipline-ok(R5): 模拟前一笔待重试。
        warnings = []
        self.win.target_store_warning.connect(lambda: warnings.append(True))
        with mock.patch.object(ticks, "retain_unsaved", side_effect=CapacityError("full")), \
                mock.patch.object(window, "log_line") as log:
            self.assertFalse(self.win._evaluate_received_quote(quote))  # discipline-ok(R5): 驱动容量故障。
            self.ensure_qapp().processEvents()
        self.assertTrue(warnings)
        self.assertTrue(any("target tick gap" in str(call.args[0]) for call in log.call_args_list))

    def test_blocked_retry_does_not_persist_without_target_lock(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "blocked-retry.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离重试队列。
        self.track_patch(mock.patch.object(sf_quote, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        quote.target_revision = ("old", "revision", ())
        self.win._target_retry_blocked = True  # discipline-ok(R5): 模拟前一笔待重试。
        self.win._evaluate_received_quote(quote)  # discipline-ok(R5): 测试真实重试分支。
        self.assertEqual(ticks.unsaved_count(), 1)
        with TargetTickStore(path) as store:
            self.assertEqual(store.read(self.win.code), [])

    def test_blocked_real_quote_keeps_effective_retry_version(self):
        import sf_quote
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(sf_quote, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        quote.target_revision = ("old", "revision", ())
        self.win._target_batch_rolls = {quote.target_revision: ("new", "revision", ())}  # discipline-ok(R5): 模拟本批已确认的顺延。
        self.win._target_retry_blocked = True  # discipline-ok(R5): 模拟先前行情持久化失败。
        self.win._evaluate_received_quote(quote)  # discipline-ok(R5): 后续行情进入真实重试入口。
        retained = self.win._target_ticks.take(self.win.code)  # discipline-ok(R5): 验证待重试行情的有效版本。
        self.assertEqual(retained[0].target_revision, ("new", "revision", ()))

    def test_zero_quote_fallback_does_not_trigger_target(self):
        import sf_quote
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert["code"] = "sh600183"
        self._open_saved_panel([alert])
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(sf_quote, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        self.win.minute_points = [(1, 101.0)]
        self.win.running = True
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "0", "pct": "0",
                                    "name": "测试", "high": "102", "low": "98", "pre_close": "100"},
                                   "20261015100000")
        with mock.patch.object(window, "_notify_deliver") as notify:
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 零价行情经实际显示入口。
            self.win._poll()  # discipline-ok(R5): 缓冲拒收后显示分时兜底。
        notify.assert_not_called()
        self.assertEqual(real_load_alerts(self.win.code)[0]["state"], "pending")

    def test_real_tick_retries_failed_commit_without_duplicate_notification(self):
        now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        self.win.code = "sh600183"
        self.win._alerts = [dict(alert)]  # discipline-ok(R5): 将与磁盘一致的 pending 条目装入真实窗口，建立命中事务的初始状态。
        self.win.quote = {"name": "测试股票"}
        self.win.running = True

        config_path = os.path.join(self.tmp_path, "config.json")
        backup_path = os.path.join(self.tmp_path, "config.backup.json")
        os.makedirs(self.tmp_path, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "code": self.win.code, "alerts": [dict(alert)]}, stream)

        real_write = sf_config._atomic_write_json  # discipline-ok(R5): 保留真实原子写盘实现，仅使首写失败，保证重试确实持久化而非由替身模拟成功。
        writes = []

        def fail_once(path, payload):
            if os.path.abspath(path) == os.path.abspath(config_path):
                writes.append(payload)
                if len(writes) == 1:
                    raise OSError("temporary config permission failure")
            return real_write(path, payload)

        self.track_patch(mock.patch.object(window, "CONFIG_PATH", config_path, create=True))
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", config_path))
        self.track_patch(mock.patch.object(sf_config, "CONFIG_BACKUP_PATH", backup_path))
        self.track_patch(mock.patch.object(window, "load_alerts", real_load_alerts))
        self.track_patch(mock.patch.object(sf_config, "load_alerts", real_load_alerts))
        self.track_patch(mock.patch.object(sf_config, "_atomic_write_json", side_effect=fail_once))
        self.track_patch(mock.patch.object(window, "_is_trading_time", return_value=True))
        self.track_patch(mock.patch.object(window, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(targets, "_beijing_now", return_value=now))
        self.track_patch(mock.patch.object(window.FloatWindow, "_after_save_config"))
        self.track_patch(mock.patch.object(window.FloatWindow, "_sync_target_panel"))
        self.track_patch(mock.patch.object(window.FloatWindow, "_start_rainbow"))
        notifications = []
        self.track_patch(mock.patch.object(window, "_notify_deliver",
                                           side_effect=lambda *args, **kwargs: notifications.append(args)))

        self.win._evaluate_targets(101.0)  # discipline-ok(R5): 直接驱动真实目标价命中事务入口，覆盖写盘失败后再次报价时的重试。
        self.assertEqual(self.win._alerts[0]["state"], "pending",  # discipline-ok(R5): 磁盘仍为 pending 不足以证明内存未提前提交，须检查窗口内存条目。
                         "临时写盘失败后内存不能先变成 hit")
        self.assertEqual(notifications, [], "写盘失败不得提前通知")
        with open(config_path, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)["alerts"][0]["state"], "pending")

        self.win._evaluate_targets(101.0)  # discipline-ok(R5): 直接驱动真实目标价命中事务入口，覆盖写盘失败后再次报价时的重试。
        self.assertEqual(self.win._alerts[0]["state"], "hit")  # discipline-ok(R5): 与下方磁盘断言配对，确认重试成功后窗口内存也完成提交。
        self.assertEqual(len(notifications), 1, "重试成功应恰好通知一次")
        with open(config_path, encoding="utf-8") as stream:
            persisted = json.load(stream)
        self.assertEqual(persisted["alerts"][0]["state"], "hit")
        self.assertEqual(len(writes), 2, "应是一次失败后一次重试，而非重复提交")
        self.win._evaluate_targets(101.0)  # discipline-ok(R5): 同一价格第三次到达，验证已提交目标不再提醒或写盘。
        self.assertEqual(len(notifications), 1, "重复报价不能再次通知")
        self.assertEqual(len(writes), 2, "重复报价不能再次提交命中")

    def test_clear_then_add_target_in_same_panel(self):
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0)
        old["code"] = "sh600183"
        panel = self._open_saved_panel([old])
        panel._clear()  # discipline-ok(R5): 模拟清空确认。
        panel._clear()  # discipline-ok(R5): 执行真实清空。
        self.assertEqual(real_load_alerts(self.win.code), [])
        panel.edit_up.setText("110")
        panel._add(targets.TARGET_DIR_ABOVE)  # discipline-ok(R5): 模拟新增目标。
        panel._apply()  # discipline-ok(R5): 经真实面板提交。
        self.assertEqual([a["target"] for a in real_load_alerts(self.win.code)], [110.0])

    def test_clear_then_other_instance_add_is_not_overwritten(self):
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0)
        old["code"] = "sh600183"
        panel = self._open_saved_panel([old])
        panel._clear()  # discipline-ok(R5): 驱动真实面板清空确认。
        panel._clear()  # discipline-ok(R5): 提交清空。
        self.assertEqual(real_load_alerts(self.win.code), [])
        other = targets.make_alert(targets.TARGET_DIR_ABOVE, 120.0)
        other["code"] = self.win.code
        self.assertTrue(sf_config.save_alerts([other], code=self.win.code))
        panel.edit_up.setText("110")
        panel._add(targets.TARGET_DIR_ABOVE)  # discipline-ok(R5): 模拟面板新增。
        panel._apply()  # discipline-ok(R5): 提交过期面板。
        self.assertEqual([a["target"] for a in real_load_alerts(self.win.code)], [120.0])
        self.assertTrue(panel.has_unsaved_changes())

    def test_fetch_quote_does_not_report_network_failure_when_target_buffer_is_full(self):
        import queue
        from sf_quote import QuoteData
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import CapacityError

        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "fetch-full.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离目标行情队列。
        self.win.q = queue.Queue()
        self.win._quote_fail = 0  # discipline-ok(R5): 检查真实行情拉取失败计数。
        quote = QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        with mock.patch.object(window, "fetch_quote", return_value=quote), \
                mock.patch.object(window, "ensure_current_quote", return_value=quote), \
                mock.patch.object(sf_config, "target_tick_transaction") as transaction, \
                mock.patch.object(ticks, "retain_unsaved", side_effect=CapacityError("full")), \
                mock.patch.object(self.win, "_wake_ui"), \
                mock.patch.object(self.win, "_on_poll_result") as result:
            transaction.return_value.__enter__.side_effect = PermissionError("denied")
            self.win._fetch_quote()  # discipline-ok(R5): 驱动真实后台行情入口。
        self.assertEqual(self.win._quote_fail, 0)  # discipline-ok(R5): 存储失败不能计为网络失败。
        self.assertEqual(self.win.q.get_nowait()[1][1]["price"], "101")
        self.assertTrue(result.call_args.args[0])

    def test_fetch_reports_failure_when_locked_quote_cannot_be_displayed(self):
        import queue
        from sf_quote import QuoteData
        from sf_quote_delivery import TargetTickBuffer

        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "display-busy.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离行情队列。
        self.win.q = mock.Mock()
        self.win.q.put_nowait.side_effect = queue.Full
        self.win.q.get_nowait.side_effect = queue.Empty
        self.win._quote_fail = 0  # discipline-ok(R5): 核对真实行情入口的失败计数。
        quote = QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        with mock.patch.object(window, "fetch_quote", return_value=quote), \
                mock.patch.object(window, "ensure_current_quote", return_value=quote), \
                mock.patch.object(sf_config, "target_tick_transaction") as transaction, \
                mock.patch.object(ticks, "add_ordered"), \
                mock.patch.object(self.win, "_wake_ui"), \
                mock.patch.object(self.win, "_on_poll_result") as result:
            transaction.return_value.__enter__.side_effect = PermissionError("denied")
            self.win._fetch_quote()  # discipline-ok(R5): 驱动真实拉取入口。
        self.assertEqual(self.win._quote_fail, 1)  # discipline-ok(R5): 未送显不得报成功。
        result.assert_called_once_with(False)

    def test_lock_failure_and_full_tick_buffer_still_displays_quote(self):
        import queue
        from sf_quote import QuoteData
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import CapacityError

        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "full.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 使用隔离目标行情队列。
        self.win.q = queue.Queue()
        quote = QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        with mock.patch.object(sf_config, "target_tick_transaction") as transaction, \
                mock.patch.object(ticks, "retain_unsaved", side_effect=CapacityError("full")), \
                mock.patch.object(self.win, "_wake_ui"):
            transaction.return_value.__enter__.side_effect = PermissionError("denied")
            with self.assertRaises(CapacityError):
                self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 驱动真实行情入口。
        self.assertFalse(self.win.q.empty(), "目标行情存储失败时股价显示不能一起丢失")
        self.assertEqual(self.win.q.get_nowait()[1][1]["price"], "101")

    def test_real_unsafe_lock_path_defers_cross_process_cleanup(self):
        import subprocess
        import sys
        from sf_quote_delivery import TargetTickBuffer

        now = datetime(2026, 10, 15, 14, 59, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert.update(code="sh600183", session_date="2026-10-15")
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "unsafe-ticks.sqlite")
        self.win._target_ticks = TargetTickBuffer(persistent_path=path)  # discipline-ok(R5): 注入隔离队列。
        lock_path = sf_config.CONFIG_PATH + ".target-ticks.lock"
        if os.path.exists(lock_path):
            self.skipTest("fixture already created the target lock")
        os.symlink(os.path.join(self.tmp_path, "missing"), lock_path)
        child = '''
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sf_config, stock_float_window as w
from sf_quote_delivery import TargetTickBuffer
sf_config.CONFIG_PATH, sf_config.CONFIG_BACKUP_PATH, db = sys.argv[1:]
h = SimpleNamespace(code="sh600183", _target_ticks=TargetTickBuffer(persistent_path=db),
                    _clear_timer=Mock(), _target_tick_publishers=0, update=Mock(),
                    _sync_target_panel=Mock(), _arm_clear_timer=Mock())
def persist(alerts, expected):
    with sf_config.expect_alerts_unchanged(h.code, expected):
        return sf_config.save_alerts(alerts, code=h.code)
h._persist_alerts = persist
h._clear_expired_locked = lambda: w.FloatWindow._clear_expired_locked(h)
with patch.object(w, "_beijing_now", return_value=datetime(2026, 10, 15, 15, 1, tzinfo=timezone(timedelta(hours=8)))):
    w.FloatWindow._clear_expired(h)
if not h._clear_timer.start.called:
    raise SystemExit(2)
'''
        result = subprocess.run([sys.executable, "-c", child, sf_config.CONFIG_PATH,
                                 sf_config.CONFIG_BACKUP_PATH, path],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(real_load_alerts("sh600183"))

    def test_failed_buffer_registration_keeps_original_publication_guard(self):
        from sf_quote import QuoteData
        from sf_quote_delivery import TargetTickBuffer

        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "handoff.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离的目标行情队列。
        quote = QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        open_publisher = sf_config.target_tick_publisher_open
        calls = []

        def fail_second(code):
            calls.append(code)
            return open_publisher(code) if len(calls) == 1 else None

        with mock.patch.object(sf_config, "target_tick_publisher_open", side_effect=fail_second), \
                mock.patch.object(sf_config, "target_tick_transaction") as transaction, \
                mock.patch.object(self.win, "_wake_ui"):
            transaction.return_value.__enter__.side_effect = PermissionError("denied")
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 驱动真实接收入口。
        self.assertEqual(ticks.unsaved_count(), 1)
        self.assertTrue(sf_config.target_tick_publisher_active(self.win.code),
                        "行情仍在内存时，登记交接失败不能撤销原有保护")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            recovered = ticks.take(self.win.code)
        self.assertEqual(len(recovered), 1)
        self.assertFalse(sf_config.target_tick_publisher_active(self.win.code),
                         "行情已落盘后应释放登记")

    def test_publisher_open_failure_paths_leave_no_live_marker(self):
        real_open = os.open
        with mock.patch.object(os, "open", side_effect=PermissionError("denied")):
            self.assertIsNone(sf_config.target_tick_publisher_open(self.win.code))
        with mock.patch.object(os, "open", wraps=real_open), \
                mock.patch.object(os, "write", side_effect=OSError("write failed")):
            self.assertIsNone(sf_config.target_tick_publisher_open(self.win.code))
        self.assertFalse(sf_config.target_tick_publisher_active(self.win.code))

    def test_publisher_open_write_failure_survives_a_failing_cleanup(self):
        with mock.patch.object(os, "write", side_effect=OSError("write failed")), \
                mock.patch.object(os, "unlink", side_effect=PermissionError("read-only directory")):
            self.assertIsNone(sf_config.target_tick_publisher_open(self.win.code))

    def test_registration_failure_marks_a_plain_quote_as_unprotected(self):
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "plain-guard.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        with mock.patch.object(sf_config, "target_tick_publisher_open", return_value=None), \
                mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, {"price": "101"})))  # discipline-ok(R5): 驱动真实行情入口。
        _kind, (_code, shown) = self.win.q.get_nowait()
        self.assertEqual(shown["price"], "101")
        self.assertTrue(shown._target_unprotected)  # discipline-ok(R5): 标记只能由此内部属性承载。
        self.assertFalse(ticks.has_pending(self.win.code))

    def test_lock_failure_retains_a_plain_quote_in_memory_and_displays_it(self):
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "plain-lock.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        warnings = []
        self.win.target_revision_warning.connect(lambda: warnings.append(True))
        with mock.patch.object(sf_config, "target_tick_transaction") as transaction, \
                mock.patch.object(self.win, "_wake_ui"):
            transaction.return_value.__enter__.side_effect = PermissionError("denied")
            self.win._q_put(("quote", (self.win.code, {"price": "101"})))  # discipline-ok(R5): 驱动真实行情入口。
            self.ensure_qapp().processEvents()
        _kind, (_code, shown) = self.win.q.get_nowait()
        self.assertEqual(shown["price"], "101", "锁不可用时股价仍要显示")
        self.assertEqual(ticks.unsaved_count(), 1)
        self.assertTrue(sf_config.target_tick_publisher_active(self.win.code),
                        "行情只在内存时必须有跨进程登记")
        self.assertTrue(warnings)

    def test_registration_failure_warning_is_logged_once_per_episode(self):
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "log-once.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        with mock.patch.object(sf_config, "target_tick_publisher_open", return_value=None), \
                mock.patch.object(window, "log_line") as log, \
                mock.patch.object(self.win, "_wake_ui"):
            for price in ("101", "102", "103"):
                self.win._q_put(("quote", (self.win.code, {"price": price})))  # discipline-ok(R5): 驱动真实行情入口。
        guard_lines = [call for call in log.call_args_list if "publication guard unavailable" in str(call)]
        self.assertEqual(len(guard_lines), 1, "登记长期不可用时每笔行情都写日志会挤掉 debug.log 的轮转")

    def test_registration_failure_is_counted_and_the_open_episode_is_visible(self):
        from sf_quote import QuoteData
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "count-guard.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        clean = self.win._target_tick_diagnostics()["degraded"]  # discipline-ok(R5): 诊断导出读的就是它。
        self.assertEqual(clean["counts"], {}, "没出过故障时不该有任何计数")
        with mock.patch.object(sf_config, "target_tick_publisher_open", return_value=None), \
                mock.patch.object(window, "log_line"), \
                mock.patch.object(self.win, "_wake_ui"):
            for price in ("101", "102", "103"):
                self.win._q_put(("quote", (self.win.code, {"price": price})))  # discipline-ok(R5): 驱动真实行情入口。
        during = self.win._target_tick_diagnostics()["degraded"]  # discipline-ok(R5): 诊断导出读的就是它。
        self.assertEqual(during["counts"]["unprotected_quotes"], 3,
                         "日志每次故障只记一条，次数只能靠计数事后查出")
        self.assertTrue(during["guard_unavailable_now"])
        quote = QuoteData({"code": self.win.code, "price": "104"}, "20261015100000")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 驱动真实行情入口。
        after = self.win._target_tick_diagnostics()["degraded"]  # discipline-ok(R5): 诊断导出读的就是它。
        self.assertFalse(after["guard_unavailable_now"], "登记恢复后当前状态要放下")
        self.assertEqual(after["counts"]["unprotected_quotes"], 3, "故障结束后累计数要保留")

    def test_a_broken_degraded_counter_is_logged_and_never_raises(self):
        class _Owner:
            pass

        owner = _Owner()
        owner._target_degraded_counts = "not a dict"  # discipline-ok(R5): 造一个坏掉的计数容器。
        with mock.patch.object(window, "log_swallow") as swallow:
            window._note_target_degraded(owner, "unprotected_quotes")  # discipline-ok(R5): 被测的就是这个私有辅助函数。
        swallow.assert_called_once()
        self.assertEqual(swallow.call_args.args[0], "stock_float_window.note_target_degraded",
                         "计数失败要留痕，但绝不能把异常抛进行情投递")

    def test_lock_failure_is_counted_and_visible_as_current(self):
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "count-lock.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        with mock.patch.object(sf_config, "target_tick_transaction") as transaction, \
                mock.patch.object(self.win, "_wake_ui"):
            transaction.return_value.__enter__.side_effect = PermissionError("denied")
            self.win._q_put(("quote", (self.win.code, {"price": "101"})))  # discipline-ok(R5): 驱动真实行情入口。
        got = self.win._target_tick_diagnostics()["degraded"]  # discipline-ok(R5): 诊断导出读的就是它。
        self.assertEqual(got["counts"]["lock_unavailable"], 1)
        self.assertTrue(got["lock_unavailable_now"])

    def test_panel_save_failures_are_counted_by_cause(self):
        from contextlib import contextmanager
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "count-panel.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        revision = sf_config.alerts_revision(self.win.code)
        lock, _calls = self._contended_tick_lock(busy_calls=2)
        with mock.patch.object(sf_config, "target_tick_transaction", lock), \
                mock.patch.object(window.time, "sleep"):
            self.assertTrue(self.win._panel_targets_transaction(  # discipline-ok(R5): 被测的就是面板事务入口。
                self.win.code, revision, lambda stored: True))
        self.assertEqual(self.win._target_tick_diagnostics()["degraded"]["counts"], {},  # discipline-ok(R5): 诊断导出读的就是它。
                         "重试后成功不算故障")
        stuck, _calls = self._contended_tick_lock(busy_calls=10_000)
        with mock.patch.object(sf_config, "target_tick_transaction", stuck):
            self.assertFalse(self.win._panel_targets_transaction(  # discipline-ok(R5): 被测的就是面板事务入口。
                self.win.code, revision, lambda stored: True))

        @contextmanager
        def denied(**kwargs):
            raise PermissionError("lock file not owned")
            yield

        with mock.patch.object(sf_config, "target_tick_transaction", denied):
            self.assertFalse(self.win._panel_targets_transaction(  # discipline-ok(R5): 被测的就是面板事务入口。
                self.win.code, revision, lambda stored: True))
        counts = self.win._target_tick_diagnostics()["degraded"]["counts"]  # discipline-ok(R5): 诊断导出读的就是它。
        self.assertEqual(counts, {"panel_lock_exhausted": 1, "panel_transaction_error": 1},
                         "抢锁耗尽与其他异常要分开计，两者的处置完全不同")

    def test_marker_release_failure_does_not_break_quote_delivery(self):
        from sf_quote import QuoteData
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "close-fails.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        quote = QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        with mock.patch.object(sf_config.TickPublisherToken, "close", side_effect=OSError("close failed")), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 驱动真实行情入口。
        self.assertFalse(self.win.q.empty(), "标记释放失败不能让已入队的行情变成异常")

    def _contended_tick_lock(self, busy_calls):
        """返回 (替身, 调用记录)：前 busy_calls 次取非阻塞 tick 锁抛 BlockingIOError，之后放行。"""
        from contextlib import contextmanager
        calls = []

        @contextmanager
        def lock(**kwargs):
            calls.append(kwargs)
            if len(calls) <= busy_calls:
                raise BlockingIOError("tick lock busy")
            yield

        return lock, calls

    def test_panel_transaction_retries_when_the_tick_lock_is_briefly_busy(self):
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "panel-busy.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        lock, calls = self._contended_tick_lock(busy_calls=2)
        ran = []
        sleeps = []
        revision = sf_config.alerts_revision(self.win.code)
        with mock.patch.object(sf_config, "target_tick_transaction", lock), \
                mock.patch.object(window.time, "sleep", side_effect=sleeps.append):
            ok = self.win._panel_targets_transaction(  # discipline-ok(R5): 被测的就是面板事务入口。
                self.win.code, revision, lambda stored: ran.append(stored) or True)
        self.assertTrue(ok, "生产者只占锁一瞬间时，点保存不该直接失败")
        self.assertEqual(len(ran), 1)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call == {"blocking": False} for call in calls),
                        "面板在界面线程上取锁，必须是非阻塞的，否则会卡住整个窗口")
        self.assertEqual(sleeps, [window.PANEL_TICK_LOCK_RETRY_SEC] * 2, "每次重试之间要有间隔")

    def test_panel_transaction_gives_up_when_the_tick_lock_stays_busy(self):
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "panel-stuck.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        lock, calls = self._contended_tick_lock(busy_calls=10_000)
        ran = []
        revision = sf_config.alerts_revision(self.win.code)
        with mock.patch.object(sf_config, "target_tick_transaction", lock):
            ok = self.win._panel_targets_transaction(  # discipline-ok(R5): 被测的就是面板事务入口。
                self.win.code, revision, lambda stored: ran.append(stored) or True)
        self.assertFalse(ok)
        self.assertEqual(ran, [], "拿不到锁时不能执行保存动作")
        self.assertEqual(len(calls), window.PANEL_TICK_LOCK_ATTEMPTS, "重试必须恰好用满上限，不多不少")
        self.assertGreaterEqual(window.PANEL_TICK_LOCK_ATTEMPTS, 2, "上限小于 2 等于没有重试")

    def test_panel_transaction_does_not_retry_other_lock_errors(self):
        from contextlib import contextmanager
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "panel-denied.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        calls = []

        @contextmanager
        def denied(**kwargs):
            calls.append(kwargs)
            raise PermissionError("lock file not owned")
            yield

        ran = []
        revision = sf_config.alerts_revision(self.win.code)
        with mock.patch.object(sf_config, "target_tick_transaction", denied):
            ok = self.win._panel_targets_transaction(  # discipline-ok(R5): 被测的就是面板事务入口。
                self.win.code, revision, lambda stored: ran.append(stored) or True)
        self.assertFalse(ok)
        self.assertEqual(ran, [])
        self.assertEqual(len(calls), 1, "只有\"锁被占\"值得重试，权限类错误重试没有意义")

    def test_panel_transaction_releases_the_tick_lock_when_the_config_lock_fails(self):
        from contextlib import contextmanager
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "panel-config-fail.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        events = []

        @contextmanager
        def tracked(**kwargs):
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        ran = []
        revision = sf_config.alerts_revision(self.win.code)
        with mock.patch.object(sf_config, "target_tick_transaction", tracked), \
                mock.patch.object(sf_config, "config_transaction", side_effect=TimeoutError("config busy")):
            ok = self.win._panel_targets_transaction(  # discipline-ok(R5): 被测的就是面板事务入口。
                self.win.code, revision, lambda stored: ran.append(stored) or True)
        self.assertFalse(ok)
        self.assertEqual(ran, [])
        self.assertEqual(events, ["enter", "exit"], "配置锁拿不到时，已经拿到的 tick 锁必须释放")

    def test_publisher_status_survives_an_absurd_pid_in_a_marker(self):
        directory = sf_config.target_tick_publisher_directory()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "%s.%s-1-token" % (self.win.code, "9" * 30))
        with open(path, "w", encoding="ascii"):
            pass  # 正文空着：判定只看文件名，超大进程号从名字里读出来
        self.assertEqual(sf_config.target_tick_publisher_status(self.win.code), "ACTIVE",
                         "认不出持有者的登记按“可能有人持有”处理，不能让整个状态查询抛出")

    def test_saving_targets_is_deferred_when_the_pending_check_fails(self):
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "pending-check.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        with mock.patch.object(self.win, "_targets_enabled", return_value=True), \
                mock.patch.object(ticks, "has_pending", side_effect=OSError("queue unreadable")):
            self.assertFalse(self.win._set_targets([]))  # discipline-ok(R5): 被测的就是面板保存入口。

    def test_panel_clear_that_fails_keeps_the_stored_targets(self):
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0)
        old["code"] = "sh600183"
        panel = self._open_saved_panel([old])
        with mock.patch.object(self.win, "_clear_targets", return_value=False):
            panel._clear()  # discipline-ok(R5): 模拟清空确认。
            panel._clear()  # discipline-ok(R5): 提交清空，内存清空失败。
        self.assertEqual([a["target"] for a in real_load_alerts(self.win.code)], [100.0])

    def test_registration_failure_displays_quote_but_pauses_target_reminders(self):
        import sf_quote
        from sf_quote_delivery import TargetTickBuffer
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "no-guard.sqlite"))
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离持久队列。
        self.win._alerts = [{"target": 100}]  # discipline-ok(R5): 模拟已设置目标价。
        warnings = []
        self.win.target_store_warning.connect(lambda: warnings.append("store"))
        quote = sf_quote.QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        self.track_patch(mock.patch.object(window.FloatWindow, "_is_trading_time", return_value=True))
        with mock.patch.object(sf_config, "target_tick_publisher_open", return_value=None), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(window, "_notify_deliver") as notify, \
                mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 驱动真实行情入口。
            self.ensure_qapp().processEvents()
            kind, (code, shown) = self.win.q.get_nowait()
            self.win._evaluate_received_quote(shown)  # discipline-ok(R5): 显示路径上的真实判定入口。
        self.assertEqual(kind, "quote")
        self.assertEqual(shown["price"], "101", "保护建立失败时股价仍要显示")
        self.assertFalse(ticks.has_pending(self.win.code), "没有跨进程保护时不能把行情收进目标价队列")
        notify.assert_not_called()
        self.assertTrue(warnings, "必须明确提示提醒可能遗漏")

    def test_saving_target_preserves_expired_target_with_other_instance_tick(self):
        from sf_quote import QuoteData
        from sf_quote_delivery import TargetTickBuffer

        now = datetime(2026, 10, 15, 14, 59, tzinfo=timezone(timedelta(hours=8)))
        old = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        old.update(code="sh600183", session_date="2026-10-15")
        self._open_saved_panel([old])
        path = os.path.join(self.tmp_path, "panel-pending.sqlite")
        producer = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = TargetTickBuffer(persistent_path=path)  # discipline-ok(R5): 模拟另一个实例的持久队列。
        quote = QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        quote.received_at = now
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            producer.add_ordered(self.win.code, quote)
        later = now + timedelta(minutes=2)
        new = targets.make_alert(targets.TARGET_DIR_ABOVE, 110.0, now=later)
        new["code"] = self.win.code
        revision = sf_config.alerts_revision(self.win.code)
        with mock.patch.object(window, "_beijing_now", return_value=later):
            self.win._panel_targets_transaction(  # discipline-ok(R5): 驱动面板的真实事务与保存接线。
                self.win.code, revision, lambda stored: self.win._set_targets([old, new], live=stored))  # discipline-ok(R5): 事务回调须执行真实保存。
        self.assertIn(100.0, [a["target"] for a in real_load_alerts(self.win.code)],
                      "待处理行情存在时，保存其他目标不能自动删掉原目标")

    def test_target_lock_failure_keeps_quote_visible(self):
        """目标行情锁暂时不可用时，显示队列仍必须收到行情。"""
        import queue
        from sf_quote import QuoteData
        self.win._target_ticks = window.TargetTickBuffer(  # discipline-ok(R5): 注入隔离持久队列以触发真实锁失败分支。
            persistent_path=os.path.join(self.tmp_path, "ticks.sqlite"))
        self.win.q = queue.Queue()
        quote = QuoteData({"code": self.win.code, "price": "101"}, "20261015100000")
        class DeniedLock:
            def __enter__(self):
                raise PermissionError("lock denied")

            def __exit__(self, exc_type, exc, traceback):
                return False

        with mock.patch.object(sf_config, "target_tick_transaction", return_value=DeniedLock()):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 验证锁失败后的真实行情入口。
        self.assertFalse(self.win.q.empty())
        self.assertEqual(self.win.q.get_nowait()[0], "quote")

    def test_unreadable_publisher_registry_is_unknown_and_blocks_cleanup(self):
        from sf_quote_delivery import TargetTickBuffer

        code = "sz300776"
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "registry.sqlite"))
        with mock.patch.object(sf_config.os, "listdir", side_effect=OSError("registry unavailable")):
            self.assertEqual(sf_config.target_tick_publisher_status(code), "UNKNOWN")
            self.assertTrue(ticks.has_pending(code),
                            "登记目录不可读时，自动清理必须按仍有发布者处理")

    def test_unsaved_tick_after_return_blocks_other_process_cleanup(self):
        import subprocess
        import sys
        from sf_quote import QuoteData
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore

        now = datetime(2026, 10, 15, 14, 59, tzinfo=timezone(timedelta(hours=8)))
        alert = targets.make_alert(targets.TARGET_DIR_ABOVE, 100.0, now=now)
        alert.update(code="sh600183", session_date="2026-10-15")
        self._open_saved_panel([alert])
        path = os.path.join(self.tmp_path, "unsaved-race.sqlite")
        ticks = TargetTickBuffer(persistent_path=path)
        self.win._target_ticks = ticks  # discipline-ok(R5): 注入隔离数据库。
        quote = QuoteData({"code": self.win.code, "price": "101"}, "20261015145900")
        quote.received_at = now
        with mock.patch.object(TargetTickStore, "save", side_effect=OSError("disk busy")), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True), \
                mock.patch.object(self.win, "_wake_ui"):
            self.win._q_put(("quote", (self.win.code, quote)))  # discipline-ok(R5): 真实接收入口返回后仍未落盘。
        self.assertEqual(ticks.unsaved_count(), 1)
        child = '''
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sf_config, stock_float_window as w
from sf_quote_delivery import TargetTickBuffer
sf_config.CONFIG_PATH, sf_config.CONFIG_BACKUP_PATH, db = sys.argv[1:]
h = SimpleNamespace(code="sh600183", _target_ticks=TargetTickBuffer(persistent_path=db),
                    _clear_timer=Mock(), _target_tick_publishers=0, update=Mock(),
                    _sync_target_panel=Mock(), _arm_clear_timer=Mock())
def persist(alerts, expected):
    with sf_config.expect_alerts_unchanged(h.code, expected):
        return sf_config.save_alerts(alerts, code=h.code)
h._persist_alerts = persist
h._clear_expired_locked = lambda: w.FloatWindow._clear_expired_locked(h)
with patch.object(w, "_beijing_now", return_value=datetime(2026, 10, 15, 15, 1, tzinfo=timezone(timedelta(hours=8)))):
    w.FloatWindow._clear_expired(h)
'''
        result = subprocess.run([sys.executable, "-c", child, sf_config.CONFIG_PATH,
                                 sf_config.CONFIG_BACKUP_PATH, path],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(real_load_alerts("sh600183"), "待恢复行情尚未落盘，不得清理目标价")

    def test_volatile_tick_marker_releases_once_the_tick_is_durable(self):
        """跨进程登记只护住"还没落盘"这一段，落盘后必须马上放开自动清理。"""
        from sf_quote import QuoteData
        from sf_quote_delivery import TargetTickBuffer
        from sf_target_tick_store import TargetTickStore

        code = "sz300777"
        ticks = TargetTickBuffer(persistent_path=os.path.join(self.tmp_path, "marker.sqlite"))
        quote = QuoteData({"code": code, "price": "101"}, "20261015145900")
        with mock.patch.object(TargetTickStore, "save", side_effect=OSError("disk busy")), \
                mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            self.assertEqual(len(ticks.add_ordered(code, quote)), 1)
        self.assertEqual(ticks.unsaved_count(), 1)
        self.assertTrue(sf_config.target_tick_publisher_active(code),
                        "行情仍只在内存里时，别的进程不得清理它的目标价")
        with mock.patch("sf_quote_delivery.quote_is_current", return_value=True):
            ticks.take(code)
        self.assertEqual(ticks.unsaved_count(), 0)
        self.assertFalse(sf_config.target_tick_publisher_active(code),
                         "行情落盘后登记必须放开，否则过期目标价永远不会被清理")


if __name__ == "__main__":
    import unittest
    unittest.main()
