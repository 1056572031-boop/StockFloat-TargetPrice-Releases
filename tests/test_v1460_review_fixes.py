# -*- coding: utf-8 -*-
"""v1.4.60（全方位复核 14 条）：这轮每一条都要有能失败的判据。

来源是 2026-10-10 那一轮只读复核的报告编号，逐条对着修：
  F1 无 code 的旧目标价条目必须归属 config 顶层那只股票（不这么改 = 切股串味后被盖章，
     原股目标价永久丢失，而对外说明的第一句就是"切换股票后原股目标价保留"）；
  F5 主源坏、备源兜住时的提示必须按**时间**节流（按次数取模 = 一天 1800 条 WARNING，
     把 debug.log 的 7 天轮转整个挤掉，恰好造成本轮想消灭的"事后查不到"）；
  F7 存储超上限剪掉的休眠条目必须在诊断包里问得出来（界面不加弹窗，见 v1.4.52 口径）。
锁屏停摄那一条（F2）在 tests/test_face_guard.py 里补，因为它用的是守卫那套真夹具。

写法遵守 AGENTS §6：不判版本号、不读源码做字符串断言、私有成员走 discipline-ok 标注。
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sf_config                                       # noqa: E402
import sf_quote                                        # noqa: E402

try:
    from _base import SFCase                          # noqa: E402
except ImportError:                                    # pragma: no cover
    from tests._base import SFCase                    # noqa: E402


def _legacy(target):
    """旧版本（≤1.4.58）写出来的条目形状：根本没有 code 这个键。"""
    return {"target": float(target), "direction": "below",
            "session_date": "2099-12-31", "state": "pending"}


class LegacyEntriesBelongToTheConfigStock(SFCase):
    """F1：无主条目的归属是 config 顶层那只股票，不是"任何一只"。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="sf_v1460_store_")
        self.cfg = os.path.join(self.tmp, "config.json")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        for p in (patch.object(sf_config, "CONFIG_PATH", self.cfg),
                  patch.object(sf_config, "CONFIG_BACKUP_PATH",
                               os.path.join(self.tmp, "config.backup.json")),
                  patch.object(sf_config, "log_line", lambda *a, **k: None)):
            self.track_patch(p)          # track_patch 自己会 start，别重复
        # 剪枝现场是进程内的，用例之间必须互不串味
        self.addCleanup(setattr, sf_config, "_last_store_prune", {})

    def _seed(self, payload):
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _raw_alerts(self):
        with open(self.cfg, encoding="utf-8") as f:
            return json.load(f)["alerts"]

    def test_owner_known_legacy_entries_are_not_shown_for_another_stock(self):
        """茅台（顶层 code）下的旧条目，切到平安银行时不该再出现在它名下。"""
        self._seed({"version": 1, "code": "sh600519",
                    "alerts": [_legacy(1600.0), _legacy(1601.0), _legacy(1602.0)]})
        self.assertEqual(len(sf_config.load_alerts("sh600519")), 3,
                         "归属股票自己必须看得见，这才是「升级一条都不少」的本意")
        self.assertEqual(sf_config.load_alerts("sz000001"), [],
                         "别的股票读到了茅台的旧条目 —— 面板会把它当自己的显示，"
                         "并且下一次写入就盖章成自己的 code（F1 丢数据的起点）")

    def test_saving_under_another_stock_cannot_rebind_them(self):
        """F1 的完整丢数据路径：在 B 下读到你(B)→存一条→切回 A 一条不剩。"""
        self._seed({"version": 1, "code": "sh600519",
                    "alerts": [_legacy(1600.0), _legacy(1601.0), _legacy(1602.0)]})
        # 用户切到平安银行并设一条自己的目标价。修复前这一步会把茅台那 3 条
        # 一起带走（它们此刻还"可见"），存储里 4 条全变成 sz000001。
        self.assertTrue(sf_config.save_alerts([_legacy(11.5)], code="sz000001"))
        back = sf_config.load_alerts("sh600519")
        self.assertEqual(len(back), 3,
                         "切回茅台必须还能看见自己那 3 条 —— 现在它们不见了")
        self.assertEqual(sorted(str(a.get("code") or "") for a in self._raw_alerts()),
                         ["sh600519", "sh600519", "sh600519", "sz000001"],
                         "茅台的旧条目必须原样留在存储里；顺手补上归属的 code，"
                         "下次就不用再猜（不补 = 换个地方丢数据）")

    def test_owner_unknown_stays_conservative(self):
        """顶层也没有 code（手工编辑过的配置/首装）时退回"对谁都可见"：
        宁可多显示一条，不许把用户已有的目标价藏起来（v1.4.59 那条保守口径）。"""
        self._seed({"version": 1, "alerts": [_legacy(70.0)]})
        self.assertEqual(len(sf_config.load_alerts("sz000001")), 1)
        self.assertEqual(len(sf_config.load_alerts("sh600519")), 1)

    def test_entries_with_code_still_filter_by_code(self):
        """1.4.59 的分桶口径不能被这次改动带坏。"""
        sf_config.save_alerts([_legacy(70.0)], code="sh600519")
        sf_config.save_alerts([_legacy(11.0)], code="sz000001")
        self.assertEqual([str(a["code"]) for a in sf_config.load_alerts("sh600519")],
                         ["sh600519"])
        self.assertEqual(len(sf_config.load_alerts()), 2, "不传 code 仍读全量（诊断包用）")


class StorePruneIsAudibleOutsideTheLog(SFCase):
    """F7：上限剪枝的现场必须有一个不依赖 debug.log 的读取口。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="sf_v1460_prune_")
        self.cfg = os.path.join(self.tmp, "config.json")
        for p in (patch.object(sf_config, "CONFIG_PATH", self.cfg),
                  patch.object(sf_config, "CONFIG_BACKUP_PATH",
                               os.path.join(self.tmp, "config.backup.json")),
                  patch.object(sf_config, "log_line", lambda *a, **k: None)):
            self.track_patch(p)          # track_patch 自己会 start，别重复
        sf_config._last_store_prune = {}   # discipline-ok(R5): 剪枝现场没有对外写入口，用例之间必须能清干净
        self.addCleanup(setattr, sf_config, "_last_store_prune", {})
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_report_names_dropped_count_and_the_victims(self):
        self.assertEqual(sf_config.store_prune_report(), {},
                         "没剪过就是空 —— 诊断包里不许出现编造的剪枝记录")
        big = [{"code": "sh600%02d" % i, "target": 10.0 + i, "direction": "below",
                "session_date": "2099-12-31", "state": "pending"} for i in range(40)]
        for a in big:
            sf_config.save_alerts([a], code=a["code"])      # 攒出大量休眠条目
        active = [{"code": "sh600519", "target": 70.0, "direction": "above",
                   "session_date": "2099-12-31", "state": "pending"}]
        sf_config.save_alerts(active, code="sh600519")
        report = sf_config.store_prune_report()
        self.assertGreater(report.get("dropped", 0), 0,
                           "超过 12 只就必须剪，而剪了要能被问出来")
        self.assertTrue(report.get("at"), "剪枝时间必须写清，否则与日志无异")
        self.assertTrue(report.get("pruned_codes"), "被剪掉的是哪几只股票要说得出")
        self.assertLessEqual(len(report["pruned_codes"]), sf_config.ALERT_STORE_MAX_CODES)
        self.assertEqual(len(sf_config.load_alerts("sh600519")), 1,
                         "当前股票的条目永远不因上限被剪")


class DegradedSourceNoticeIsThrottledByTime(SFCase):
    """F5：连续跑备源的提示按时间节流，不再每 20 次一条。"""

    def setUp(self):
        super().setUp()
        self.clock = [1000.0]
        fake_time = types.SimpleNamespace(monotonic=lambda: self.clock[0])
        self.logs = []
        for p in (patch.object(sf_quote, "time", fake_time),
                  patch.object(sf_quote, "log_line",
                               lambda msg, level="INFO": self.logs.append(msg))):
            self.track_patch(p)          # track_patch 自己会 start，别重复
        sf_quote._reset_degraded_source()   # discipline-ok(R5): 降级计数与节流手表只有这个复位口
        self.addCleanup(sf_quote._reset_degraded_source)   # discipline-ok(R5): 同上，没有公开复位入口

    def _fire(self, times, gap=1.0):
        for _ in range(times):
            self.clock[0] += gap
            sf_quote._note_degraded_source("sh600519", RuntimeError("down"))   # discipline-ok(R5): 被测的就是这个记录入口，产品侧只有它

    def test_first_notice_always_written(self):
        self._fire(1)
        self.assertEqual(len(self.logs), 1, "第一次降级必须立刻说")

    def test_a_full_trading_day_writes_minutes_not_thousands(self):
        # 修复前：DEGRADED_SOURCE_ALERT_AFTER=20 且盘中 0.1s 节拍 → 每 2 秒一条，
        # 4 小时约 7200 条；现在按 60 秒一条，4 小时约 240 条以内。
        self._fire(240, gap=60.0)         # 名义上跑了 4 小时
        self.assertLessEqual(len(self.logs), 241)
        self._fire(120, gap=1.0)         # 再挤进 120 次（每秒一次）
        self.assertLessEqual(len(self.logs), 243,
                             "按次数取模会把一天挤满 WARNING —— 判据必须是时间")
        self.assertGreaterEqual(sf_quote.degraded_source_streak(), 360,
                                "累计次数照常可查（诊断包靠它，不能被节流一起吞掉）")

    def test_recovery_resets_the_clock(self):
        self._fire(1)
        self._fire(5, gap=10.0)          # 60 秒内不再重复
        self.assertEqual(len(self.logs), 1)
        sf_quote._reset_degraded_source()   # discipline-ok(R5): 主源恢复的复位口只有它
        self._fire(1)
        self.assertEqual(len(self.logs), 2, "恢复之后再降级，首条要立刻说")


if __name__ == "__main__":
    unittest.main()
