"""目标价命中事务：保存失败可重试，只有持久化成功才通知。"""
import json
import os
import tempfile
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest import mock

import sf_config
import stock_float_window as window
import targets
from tests.test_minute_recovery_latency import _Host


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
            writes.append(payload)
            if len(writes) == 1:
                raise OSError("temporary config permission failure")
            return real_write(path, payload)

        self.track_patch(mock.patch.object(window, "CONFIG_PATH", config_path, create=True))
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", config_path))
        self.track_patch(mock.patch.object(sf_config, "CONFIG_BACKUP_PATH", backup_path))
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


if __name__ == "__main__":
    import unittest
    unittest.main()
