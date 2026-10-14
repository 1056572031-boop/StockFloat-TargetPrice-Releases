# -*- coding: utf-8 -*-
"""v1.2.95 第7轮走查：目标价状态机不变量（随机压测，固定种子可复现）。

本轮的目标是对目标价做一次"深化"复核：不再只看单点用例，而是用随机价格
序列去撞状态机，检查它会不会被撞出漏洞。结论是**未发现违规**，故把这些
不变量固化下来，防止以后被改坏。

锁死四条不变量：
  1. 已触发（hit）永不回退为待触发（pending）—— 否则会重复提醒。
  2. 同一价位连续两拍不得重复触发。
  3. 面板保存时的按槽位合并，必须保住后台已触发状态
     （否则用户设了目标、触发后一保存就又变成待触发）。
  4. 槽位等价性：10 / 10.00 / "10.0" 属同一槽位，不该被当成两条。
"""
import random
import unittest
from datetime import datetime, timedelta, timezone

import targets as T

_BJ = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 8, 10, 30, tzinfo=_BJ)   # 周二 交易时段


def _mk(direction, target, state=None):
    a = T.make_alert(direction, target, now=NOW)
    if state is not None:
        a["state"] = state
    return a


def _key(a):
    return (a["direction"], round(float(a["target"]), 6))


class TestStateMachineInvariants(unittest.TestCase):

    def test_hit_never_reverts_and_no_repeat_fire(self):
        rnd = random.Random(20260908)
        for trial in range(200):
            alerts = [_mk(rnd.choice([T.TARGET_DIR_ABOVE, T.TARGET_DIR_BELOW]),
                          round(rnd.uniform(8, 12), 2))
                      for _ in range(rnd.randint(1, 4))]
            alerts = T.roll_alerts_to_trading_day(alerts)[0]
            cur, hit_keys = list(alerts), set()
            for step in range(30):
                price = None if rnd.random() < 0.05 else round(rnd.uniform(7, 13), 2)
                cur, _fired = T.evaluate_tick(cur, price, now=NOW)
                for a in cur:
                    k = _key(a)
                    if k in hit_keys:
                        self.assertEqual(
                            a.get("state"), T.STATE_HIT,
                            "第 %d 轮第 %d 步：已触发的 %r 被回退成待触发，"
                            "会造成重复提醒" % (trial, step, k))
                    if a.get("state") == T.STATE_HIT:
                        hit_keys.add(k)
                # 同一价格再走一拍：不得再次触发
                again, refire = T.evaluate_tick(cur, price, now=NOW)
                self.assertEqual(refire, [],
                                 "第 %d 轮第 %d 步：同一价位重复触发 %r"
                                 % (trial, step, [(f["direction"], f["target"])
                                                  for f in refire]))
                cur = again

    def test_merge_preserves_hit_state(self):
        rnd = random.Random(20260907)
        for trial in range(300):
            live = []
            for _ in range(rnd.randint(1, 4)):
                a = _mk(rnd.choice([T.TARGET_DIR_ABOVE, T.TARGET_DIR_BELOW]),
                        round(rnd.uniform(8, 12), 2),
                        state=T.STATE_HIT if rnd.random() < 0.5 else T.STATE_PENDING)
                live.append(a)
            # 面板快照：同一批槽位，但状态一律 pending（模拟面板没跟上后台）
            desired = [dict(a, state=T.STATE_PENDING) for a in live]
            merged = T.merge_alerts(desired, live, now=NOW)
            hit_keys = {_key(a) for a in live if a["state"] == T.STATE_HIT}
            for a in merged:
                if _key(a) in hit_keys:
                    self.assertEqual(
                        a.get("state"), T.STATE_HIT,
                        "第 %d 轮：已触发的 %r 被面板快照打回待触发"
                        % (trial, _key(a)))

    def test_slot_equivalence_across_numeric_forms(self):
        a_int = _mk(T.TARGET_DIR_ABOVE, 10)
        a_float = _mk(T.TARGET_DIR_ABOVE, 10.00)
        a_str = _mk(T.TARGET_DIR_ABOVE, float("10.0"))
        self.assertTrue(T._same_slot(a_int, a_float))
        self.assertTrue(T._same_slot(a_int, a_str))
        # 不同数值表示仍应按同一槽位保住 hit
        merged = T.merge_alerts([dict(a_float, state=T.STATE_PENDING)],
                                [dict(a_int, state=T.STATE_HIT)], now=NOW)
        self.assertEqual(len(merged), 1, "同一槽位不应变成两条")
        self.assertEqual(merged[0]["state"], T.STATE_HIT)


class TestDistanceGuards(unittest.TestCase):
    """主窗摘要的百分比不得出现 nan% / inf%。"""

    def test_bad_price_yields_none(self):
        for bad in (None, 0, -1, float("nan"), float("inf"), "abc", object()):
            self.assertIsNone(T.distance_pct(bad, 10.0),
                              "现价 %r 应返回 None（只显示价位）" % (bad,))

    def test_nearest_pending_skips_unreliable(self):
        alerts = [_mk(T.TARGET_DIR_ABOVE, 11.0), _mk(T.TARGET_DIR_BELOW, 9.0)]
        for bad in (None, 0, float("nan")):
            self.assertIsNone(T.nearest_pending(alerts, bad),
                              "现价不可信时不应挑出任何目标")

    def test_label_falls_back_to_price_only(self):
        txt = T.build_target_label_text([_mk(T.TARGET_DIR_ABOVE, 11.0)], float("nan"))
        self.assertNotIn("nan", txt.lower())
        self.assertNotIn("inf", txt.lower())


if __name__ == "__main__":
    unittest.main()
