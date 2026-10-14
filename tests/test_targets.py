# -*- coding: utf-8 -*-
"""「目标价」targets.py 纯逻辑单测（脱 Qt，时区经注入 now 验证）。"""
import datetime
import unittest

import targets
from sf_constants import TARGET_DIR_ABOVE, TARGET_DIR_BELOW

TZ = datetime.timezone(datetime.timedelta(hours=8))


def _bj(y, m, d, hh=10, mm=30, ss=0):
    return datetime.datetime(y, m, d, hh, mm, ss, tzinfo=TZ)


# R45：触发判定现在严格限定在「生效日当天 15:00:30 之前」。测试必须注入固定
# 时刻，不能依赖"今天恰好是 2026-09-07 且还没收盘"——否则明天/收盘后全组失效。
NOW = _bj(2026, 9, 7, 10, 30)


class TestValidate(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(targets.validate_alert(TARGET_DIR_ABOVE, 1500), (True, ""))
        self.assertEqual(targets.validate_alert(TARGET_DIR_BELOW, 12.5), (True, ""))
    def test_bad_direction(self):
        self.assertFalse(targets.validate_alert("up", 1500)[0])
    def test_bad_target(self):
        self.assertFalse(targets.validate_alert(TARGET_DIR_ABOVE, "x")[0])
        self.assertFalse(targets.validate_alert(TARGET_DIR_ABOVE, -1)[0])
        self.assertFalse(targets.validate_alert(TARGET_DIR_ABOVE, 0)[0])


class TestCross(unittest.TestCase):
    def test_above(self):
        self.assertTrue(targets.is_crossed(1600, 1500, TARGET_DIR_ABOVE))
        self.assertFalse(targets.is_crossed(1400, 1500, TARGET_DIR_ABOVE))
    def test_below(self):
        self.assertTrue(targets.is_crossed(1400, 1500, TARGET_DIR_BELOW))
        self.assertFalse(targets.is_crossed(1600, 1500, TARGET_DIR_BELOW))


class TestMakeAlertSessionDate(unittest.TestCase):
    def test_trading_now_same_day(self):
        a = targets.make_alert(TARGET_DIR_ABOVE, 1500, now=_bj(2026, 2, 24))
        self.assertEqual(a["session_date"], "2026-02-24")
        self.assertEqual(a["state"], targets.STATE_PENDING)
    def test_weekend_next_session(self):
        a = targets.make_alert(TARGET_DIR_ABOVE, 1500, now=_bj(2026, 2, 14))  # 周六
        self.assertEqual(a["session_date"], "2026-02-24")  # 春节后首个交易日
    def test_holiday_next_session(self):
        a = targets.make_alert(TARGET_DIR_ABOVE, 1500, now=_bj(2026, 2, 18))  # 春节休市
        self.assertEqual(a["session_date"], "2026-02-24")


class TestRecover(unittest.TestCase):
    def test_expired_discarded(self):
        alerts = [{"direction": TARGET_DIR_ABOVE, "target": 1500.0,
                   "session_date": "2026-02-24", "state": "pending", "last_side": None}]
        kept, rem = targets.recover_alerts(alerts, _bj(2026, 3, 1), 1600)
        self.assertEqual(kept, [])
        self.assertEqual(rem, [])
    def test_pending_kept(self):
        alerts = [{"direction": TARGET_DIR_ABOVE, "target": 1500.0,
                   "session_date": "2026-12-31", "state": "pending", "last_side": None}]
        kept, rem = targets.recover_alerts(alerts, _bj(2026, 9, 4), 1400)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["state"], targets.STATE_PENDING)
        self.assertEqual(rem, [])
    def test_over_line_reminder(self):
        """当天生效的目标价，启动恢复时发现已越线要补发提醒。"""
        alerts = [{"direction": TARGET_DIR_ABOVE, "target": 1500.0,
                   "session_date": "2026-09-04", "state": "pending", "last_side": None}]
        kept, rem = targets.recover_alerts(alerts, _bj(2026, 9, 4), 1600)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["state"], targets.STATE_HIT)
        self.assertEqual(len(rem), 1)
        self.assertIn("已涨到 1500.00", rem[0]["text"])
    def test_below_over_line(self):
        alerts = [{"direction": TARGET_DIR_BELOW, "target": 1400.0,
                   "session_date": "2026-09-04", "state": "pending", "last_side": None}]
        kept, rem = targets.recover_alerts(alerts, _bj(2026, 9, 4), 1300)
        self.assertEqual(kept[0]["state"], targets.STATE_HIT)
        self.assertIn("已跌到 1400.00", rem[0]["text"])

    def test_future_session_is_not_hit_on_recovery(self):
        """R48：盘后设的次日目标，重启时不得被当前价提前判成已触发。

        否则它会被当成"已触发"，到真正生效的那天反而不再提醒 ——
        用户设了目标价却永远收不到。
        """
        alerts = [{"direction": TARGET_DIR_ABOVE, "target": 1500.0,
                   "session_date": "2026-09-08", "state": targets.STATE_PENDING,
                   "last_side": None}]
        kept, rem = targets.recover_alerts(alerts, _bj(2026, 9, 7, 20, 0), 1600)
        self.assertEqual(kept[0]["state"], targets.STATE_PENDING)
        self.assertEqual(rem, [])

    def test_already_hit_does_not_remind_again(self):
        alerts = [{"direction": TARGET_DIR_ABOVE, "target": 1500.0,
                   "session_date": "2026-12-31", "state": targets.STATE_HIT,
                   "last_side": "above"}]
        kept, rem = targets.recover_alerts(alerts, _bj(2026, 9, 4), 1600)
        self.assertEqual(kept[0]["state"], targets.STATE_HIT)
        self.assertEqual(rem, [])


class TestEvaluateTick(unittest.TestCase):
    def _alert(self, direction, target):
        return {"direction": direction, "target": float(target),
                "session_date": "2026-09-07", "state": "pending", "last_side": None}
    def test_future_session_does_not_fire_before_effective_day(self):
        alerts = [{"direction": TARGET_DIR_ABOVE, "target": 1500.0,
                   "session_date": "2099-12-31", "state": "pending", "last_side": None}]
        out, fired = targets.evaluate_tick(alerts, 1600, now=NOW)
        self.assertEqual(fired, [])
        self.assertEqual(out[0]["state"], targets.STATE_PENDING)

    def test_fire_once(self):
        alerts = [self._alert(TARGET_DIR_ABOVE, 1500)]
        out, fired = targets.evaluate_tick(alerts, 1600, now=NOW)
        self.assertEqual(len(fired), 1)
        self.assertEqual(out[0]["state"], targets.STATE_HIT)
        out2, fired2 = targets.evaluate_tick(out, 1600, now=NOW)
        self.assertEqual(fired2, [])
    def test_no_fire(self):
        alerts = [self._alert(TARGET_DIR_ABOVE, 1500)]
        out, fired = targets.evaluate_tick(alerts, 1400, now=NOW)
        self.assertEqual(fired, [])
        self.assertEqual(out[0]["state"], targets.STATE_PENDING)


class TestEffectWindow(unittest.TestCase):
    """R45：触发只在「生效日当天 15:00:30 之前」有效，前后都不判定。"""

    @staticmethod
    def _a(direction, target, sd):
        return {"direction": direction, "target": float(target),
                "session_date": sd, "state": targets.STATE_PENDING,
                "last_side": None}

    def test_past_session_never_fires_on_stale_price(self):
        """清空定时器漏触发/休眠跨过 15:00:30 后，盘后缓存价不得补发提醒。"""
        a = [self._a(TARGET_DIR_BELOW, 10.0, "2026-09-03")]
        _, fired = targets.evaluate_tick(a, 1.0, now=_bj(2026, 9, 7, 22, 0))
        self.assertEqual(fired, [], "已过期生效日不得再触发")

    def test_future_session_does_not_fire(self):
        a = [self._a(TARGET_DIR_BELOW, 10.0, "2026-09-08")]
        _, fired = targets.evaluate_tick(a, 1.0, now=_bj(2026, 9, 7, 22, 0))
        self.assertEqual(fired, [], "次日生效目标不得提前触发")

    def test_same_day_before_clear_fires(self):
        a = [self._a(TARGET_DIR_ABOVE, 10.0, "2026-09-07")]
        _, fired = targets.evaluate_tick(a, 11.0, now=_bj(2026, 9, 7, 14, 59))
        self.assertEqual(len(fired), 1)

    def test_same_day_at_150030_does_not_fire(self):
        a = [self._a(TARGET_DIR_ABOVE, 10.0, "2026-09-07")]
        _, fired = targets.evaluate_tick(a, 11.0, now=_bj(2026, 9, 7, 15, 0, 30))
        self.assertEqual(fired, [])

    def test_150029_still_same_session_date(self):
        """15:00:29 新建目标仍属当天，与 15:00:30 清空点同源不再有空档。"""
        self.assertEqual(targets._session_date_for(_bj(2026, 9, 7, 15, 0, 29)),
                         "2026-09-07")

    def test_150030_rolls_to_next_trading_day(self):
        self.assertEqual(targets._session_date_for(_bj(2026, 9, 7, 15, 0, 30)),
                         "2026-09-08")

    def test_missing_or_bad_session_date_never_fires(self):
        for sd in (None, "", "abc", "2026/09/07"):
            a = [self._a(TARGET_DIR_ABOVE, 10.0, sd)]
            _, fired = targets.evaluate_tick(a, 11.0, now=NOW)
            self.assertEqual(fired, [], "session_date=%r 不应触发" % (sd,))


class TestMsUntilClear(unittest.TestCase):
    def test_future_positive(self):
        ms = targets.ms_until_clear("2026-12-31", now=_bj(2026, 9, 4))
        self.assertGreater(ms, 0)
    def test_past_zero(self):
        ms = targets.ms_until_clear("2026-02-24", now=_bj(2026, 3, 1))
        self.assertEqual(ms, 0)


class TestMergeAlerts(unittest.TestCase):
    """merge_alerts（v1.2.77 自审修复 D7/D16/D17 的地基）。"""
    def _a(self, direction, target, state="pending", sd="2026-12-31", last_side=None):
        return {"direction": direction, "target": float(target),
                "session_date": sd, "state": state, "last_side": last_side}

    def test_preserves_hit_state(self):
        """面板打开期间已触发：保存不得回退成 pending（否则下个 tick 重复提醒）。"""
        live = [self._a(TARGET_DIR_ABOVE, 1600, state=targets.STATE_HIT,
                        last_side="ABOVE")]
        desired = [self._a(TARGET_DIR_ABOVE, 1600)]          # 面板快照仍是 pending
        merged = targets.merge_alerts(desired, live)
        self.assertEqual(merged[0]["state"], targets.STATE_HIT)
        self.assertEqual(merged[0]["last_side"], "ABOVE")

    def test_unchanged_equals_live_skips_write(self):
        """期望与实时完全一致 → 返回与 live 相等，调用方可跳过落盘。"""
        live = [self._a(TARGET_DIR_ABOVE, 1600, state=targets.STATE_HIT,
                        last_side="ABOVE")]
        desired = [self._a(TARGET_DIR_ABOVE, 1600)]
        self.assertEqual(targets.merge_alerts(desired, live), live)

    def test_changed_target_uses_fresh_entry(self):
        """改了价位 → 采用新条目（新生效日），且不继承已触发状态。"""
        live = [self._a(TARGET_DIR_ABOVE, 1600, state=targets.STATE_HIT)]
        desired = [self._a(TARGET_DIR_ABOVE, 1700, sd="2027-01-04")]
        merged = targets.merge_alerts(desired, live)
        self.assertEqual(merged[0]["target"], 1700.0)
        self.assertEqual(merged[0]["state"], targets.STATE_PENDING)
        self.assertEqual(merged[0]["session_date"], "2027-01-04")

    def test_new_direction_appended(self):
        live = [self._a(TARGET_DIR_ABOVE, 1600, state=targets.STATE_HIT)]
        desired = [self._a(TARGET_DIR_ABOVE, 1600),
                   self._a(TARGET_DIR_BELOW, 1400)]
        merged = targets.merge_alerts(desired, live)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["state"], targets.STATE_HIT)
        self.assertEqual(merged[1]["state"], targets.STATE_PENDING)

    def test_end_to_end_apply_after_hit_never_refires(self):
        """端到端（D16 防回归）：面板开着期间已触发 → 点立即生效 → 再 tick 不得重复提醒。"""
        live = [self._a(TARGET_DIR_ABOVE, 1600, sd="2026-09-07")]
        snapshot = [dict(a) for a in live]              # 面板打开时的快照
        live, fired = targets.evaluate_tick(live, 1650, now=NOW)  # 面板开着，行情触发
        self.assertEqual(len(fired), 1)
        # 用户点「立即生效」：面板提交按快照构造的期望值（此时仍是 pending）
        desired = [targets.make_alert(a["direction"], a["target"]) for a in snapshot]
        merged = targets.merge_alerts(desired, live)
        self.assertEqual(merged[0]["state"], targets.STATE_HIT)
        _, fired_again = targets.evaluate_tick(merged, 1650, now=NOW)
        self.assertEqual(fired_again, [], "已触发的目标价在保存后不得重复提醒")

    def test_empty_and_none_safe(self):
        self.assertEqual(targets.merge_alerts([], []), [])
        self.assertEqual(targets.merge_alerts(None, None), [])
        self.assertEqual(targets.merge_alerts([self._a(TARGET_DIR_ABOVE, 1)], None)[0]["target"], 1.0)


class TestSessionDateTradingDay(unittest.TestCase):
    """v1.2.80：生效日判定不得把开盘前/午休误推到下一交易日。

    背景：原实现只问"现在是否连续竞价时段"，于是午休 12:00 设目标价会被算到
    明天生效 —— 当天下午不生效、当日 15:00:30 也不会清空。
    """
    def test_pre_open_is_same_day(self):
        self.assertEqual(targets._session_date_for(_bj(2026, 9, 7, 8, 0)), "2026-09-07")

    def test_lunch_break_is_same_day(self):
        self.assertEqual(targets._session_date_for(_bj(2026, 9, 7, 12, 0)), "2026-09-07")

    def test_trading_hours_is_same_day(self):
        self.assertEqual(targets._session_date_for(_bj(2026, 9, 7, 10, 30)), "2026-09-07")

    def test_after_close_is_next_trading_day(self):
        self.assertEqual(targets._session_date_for(_bj(2026, 9, 7, 15, 1)), "2026-09-08")

    def test_weekend_is_next_trading_day(self):
        self.assertEqual(targets._session_date_for(_bj(2026, 9, 12, 10, 0)), "2026-09-14")

    def test_holiday_is_next_trading_day(self):
        self.assertEqual(targets._session_date_for(_bj(2026, 2, 18, 10, 0)), "2026-02-24")


class TestMergeDedupe(unittest.TestCase):
    """R38：期望里出现重复槽位时只保留一条，否则越线会连发两条通知。"""

    @staticmethod
    def _a(direction, target, sd="2026-12-31", state="pending"):
        return {"direction": direction, "target": float(target),
                "session_date": sd, "state": state, "last_side": None}

    def test_duplicate_slots_collapse(self):
        live = [self._a(TARGET_DIR_ABOVE, 1600, sd="D1")]
        desired = [self._a(TARGET_DIR_ABOVE, 1600, sd="D2"),
                   self._a(TARGET_DIR_ABOVE, 1600, sd="D3")]
        self.assertEqual(len(targets.merge_alerts(desired, live)), 1)

    def test_two_directions_still_both_kept(self):
        live = [self._a(TARGET_DIR_ABOVE, 1600)]
        desired = [self._a(TARGET_DIR_ABOVE, 1600),
                   self._a(TARGET_DIR_BELOW, 1400)]
        self.assertEqual(len(targets.merge_alerts(desired, live)), 2)

    def test_dedupe_keeps_first_live_match(self):
        live = [self._a(TARGET_DIR_ABOVE, 1600, sd="D1", state="hit")]
        desired = [self._a(TARGET_DIR_ABOVE, 1600)] * 3
        out = targets.merge_alerts(desired, live)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["session_date"], "D1")
        self.assertEqual(out[0]["state"], "hit")


class TestNotificationTextFormat(unittest.TestCase):
    """R41：通知正文不得被极端数值撑成几百字符。"""

    def test_normal_price_keeps_two_decimals(self):
        self.assertEqual(targets._fmt_target(1600), "1600.00")
        self.assertEqual(targets._fmt_target(0.01), "0.01")

    def test_three_decimal_notification_format(self):
        self.assertEqual(targets._fmt_target(4.621, decimals=3), "4.621")
        alert = [targets.make_alert(TARGET_DIR_ABOVE, 4.621, now=NOW)]
        _out, fired = targets.evaluate_tick(alert, 4.621, now=NOW, decimals=3)
        self.assertIn("4.621", fired[0]["text"])
        _kept, reminders = targets.recover_alerts(alert, NOW, 4.621, decimals=3)
        self.assertIn("4.621", reminders[0]["text"])

    def test_extreme_price_uses_scientific(self):
        s = targets._fmt_target(1e308)
        self.assertLessEqual(len(s), 20)
        self.assertIn("1e+308", s)

    def test_extreme_below_target_notification_is_short(self):
        """「跌到 1e308」会立刻触发 —— 正文不能是 300 多位数字。"""
        a = [{"direction": TARGET_DIR_BELOW, "target": 1e308,
              "session_date": "2026-09-07", "state": targets.STATE_PENDING}]
        _, fired = targets.evaluate_tick(a, 10.0, now=NOW)
        self.assertEqual(len(fired), 1)
        self.assertLessEqual(len(fired[0]["text"]), 32)

    def test_non_numeric_target_does_not_raise(self):
        self.assertEqual(targets._fmt_target("abc"), "abc")
        self.assertEqual(targets._fmt_target(None), "None")


class TestDropExpired(unittest.TestCase):
    """R39-1：已过清空时刻的条目必须被剔除，否则会挂到重启（铃铛一直亮）。"""

    @staticmethod
    def _a(sd, state="pending"):
        return {"direction": TARGET_DIR_ABOVE, "target": 1600.0,
                "session_date": sd, "state": state}

    def test_past_session_dropped(self):
        out = targets.drop_expired([self._a("2026-09-04")], _bj(2026, 9, 5, 9, 0))
        self.assertEqual(out, [])

    def test_future_session_kept(self):
        out = targets.drop_expired([self._a("2026-12-31")], _bj(2026, 9, 5, 9, 0))
        self.assertEqual(len(out), 1)

    def test_same_day_before_clear_kept(self):
        out = targets.drop_expired([self._a("2026-09-07")], _bj(2026, 9, 7, 10, 0))
        self.assertEqual(len(out), 1)

    def test_unparsable_session_dropped(self):
        out = targets.drop_expired([{"direction": TARGET_DIR_ABOVE,
                                     "target": 1.0, "session_date": "x"}],
                                   _bj(2026, 9, 7, 10, 0))
        self.assertEqual(out, [])

    def test_junk_entries_dropped(self):
        out = targets.drop_expired(["junk", None, self._a("2026-12-31")],
                                   _bj(2026, 9, 7, 10, 0))
        self.assertEqual(len(out), 1)

    def test_empty_is_noop(self):
        self.assertEqual(targets.drop_expired([], _bj(2026, 9, 7)), [])
        self.assertEqual(targets.drop_expired(None, _bj(2026, 9, 7)), [])


class TestUntrustedPrice(unittest.TestCase):
    """R33-1：现价不可信（0 / None / NaN / inf）时绝不能把「跌到 X」判成已触发。

    冷启动行情未到时 _current_price() 返回 0.0，0 <= 目标价 会满足「跌到」条件，
    于是一开机就误弹提醒 + 误开彩虹高亮。没有可信价格就不下结论。
    """
    def _alert(self):
        return [{"direction": TARGET_DIR_BELOW, "target": 10.0,
                 "session_date": "2026-09-07", "state": targets.STATE_PENDING,
                 "last_side": None}]

    def test_recover_with_zero_price_does_not_fire(self):
        kept, rem = targets.recover_alerts(self._alert(), _bj(2026, 9, 7, 9, 0), 0.0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["state"], targets.STATE_PENDING)
        self.assertIsNone(kept[0]["last_side"])
        self.assertEqual(rem, [])

    def test_recover_with_none_price_does_not_fire(self):
        kept, rem = targets.recover_alerts(self._alert(), _bj(2026, 9, 7, 9, 0), None)
        self.assertEqual(kept[0]["state"], targets.STATE_PENDING)
        self.assertEqual(rem, [])

    def test_recover_with_nan_price_does_not_fire(self):
        kept, rem = targets.recover_alerts(self._alert(), _bj(2026, 9, 7, 9, 0), float("nan"))
        self.assertEqual(kept[0]["state"], targets.STATE_PENDING)
        self.assertEqual(rem, [])

    def test_recover_with_real_price_still_fires(self):
        kept, rem = targets.recover_alerts(self._alert(), _bj(2026, 9, 7, 9, 0), 9.5)
        self.assertEqual(kept[0]["state"], targets.STATE_HIT)
        self.assertEqual(len(rem), 1)

    def test_tick_with_zero_price_does_not_fire(self):
        out, fired = targets.evaluate_tick(self._alert(), 0.0, now=NOW)
        self.assertEqual(fired, [])
        self.assertEqual(out[0]["state"], targets.STATE_PENDING)

    def test_tick_with_real_price_still_fires(self):
        _, fired = targets.evaluate_tick(self._alert(), 9.5, now=NOW)
        self.assertEqual(len(fired), 1)

    def test_above_direction_unaffected_by_zero(self):
        """「涨到」方向在 price=0 时本就不会触发，不得因为加固而改成会触发。"""
        a = [{"direction": TARGET_DIR_ABOVE, "target": 10.0,
              "session_date": "2026-09-07", "state": targets.STATE_PENDING}]
        _, fired = targets.evaluate_tick(a, 0.0, now=NOW)
        self.assertEqual(fired, [])


class TestCorruptAlerts(unittest.TestCase):
    """R33-2/3：脏数据逐条跳过，不得抛异常、不得把整批目标价连坐清空。"""
    NOW = _bj(2026, 9, 7, 9, 0)

    def _good(self, target=12.0):
        return {"direction": TARGET_DIR_ABOVE, "target": target,
                "session_date": "2026-09-07", "state": targets.STATE_PENDING}

    def test_mixed_corrupt_entries_keep_the_valid_one(self):
        bad = [
            {"direction": TARGET_DIR_ABOVE, "target": 12.0, "session_date": "2026/09/07"},
            {"direction": TARGET_DIR_ABOVE, "session_date": "2026-09-07"},          # 缺 target
            {"direction": "sideways", "target": 1.0, "session_date": "2026-09-07"},  # 非法方向
            {"direction": TARGET_DIR_ABOVE, "target": -5.0, "session_date": "2026-09-07"},
            {"direction": TARGET_DIR_ABOVE, "target": 12.0, "session_date": "2026-02-30"},
            {"direction": TARGET_DIR_ABOVE, "target": float("nan"), "session_date": "2026-09-07"},
            "not a dict", None,
            self._good(),
        ]
        kept, rem = targets.recover_alerts(bad, self.NOW, 11.0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["target"], 12.0)
        self.assertEqual(rem, [])

    def test_evaluate_tick_drops_junk_entries(self):
        """非 dict 条目直接丢弃（顺带自愈）；结构不完整的 dict 原样透传不误判。"""
        out, fired = targets.evaluate_tick(
            [self._good(), "junk", None, {"direction": "x", "target": 1.0}],
            13.0, now=self.NOW)
        self.assertEqual(len(fired), 1)
        self.assertEqual(out[0]["state"], targets.STATE_HIT)
        self.assertNotIn("junk", out)
        self.assertNotIn(None, out)
        self.assertEqual(len(out), 2)

    def test_expired_returns_true_on_unparsable_date(self):
        self.assertTrue(targets.expired({"session_date": "abc"}, self.NOW))
        self.assertTrue(targets.expired({}, self.NOW))
        self.assertTrue(targets.expired(None, self.NOW))
        self.assertFalse(targets.expired(self._good(), self.NOW))

    def test_ms_until_clear_zero_on_unparsable_date(self):
        self.assertEqual(targets.ms_until_clear("abc", now=self.NOW), 0)
        self.assertEqual(targets.ms_until_clear(None, now=self.NOW), 0)
        self.assertGreater(targets.ms_until_clear("2026-12-31", now=self.NOW), 0)

    def test_clear_dt_returns_none_on_unparsable_date(self):
        self.assertIsNone(targets._clear_dt("2026/09/07"))
        self.assertIsNone(targets._clear_dt("2026-02-30"))
        self.assertIsNone(targets._clear_dt(None))
        self.assertIsNotNone(targets._clear_dt("2026-09-07"))


class TestDistancePct(unittest.TestCase):
    """P0-1/P1-2：距目标价差% 计算（价格不可信返回 None）。"""
    def test_above_positive(self):
        self.assertAlmostEqual(targets.distance_pct(1500, 1600), 100 * 100 / 1500, places=6)
    def test_below_negative(self):
        self.assertAlmostEqual(targets.distance_pct(1500, 1400), -100 * 100 / 1500, places=6)
    def test_zero_price_none(self):
        self.assertIsNone(targets.distance_pct(0, 1600))
    def test_none_price_none(self):
        self.assertIsNone(targets.distance_pct(None, 1600))
    def test_zero_target_none(self):
        self.assertIsNone(targets.distance_pct(1500, 0))


class TestNearestPending(unittest.TestCase):
    """P0-1：主窗单行摘要挑最近的 pending 目标价。"""
    @staticmethod
    def _a(direction, target, state="pending"):
        return {"direction": direction, "target": float(target),
                "session_date": "2026-12-31", "state": state}

    def test_picks_nearest(self):
        alerts = [self._a(TARGET_DIR_ABOVE, 1600), self._a(TARGET_DIR_ABOVE, 1700)]
        near = targets.nearest_pending(alerts, 1580)
        self.assertEqual(near["target"], 1600.0)

    def test_skips_hit(self):
        alerts = [self._a(TARGET_DIR_ABOVE, 1600, state="hit"),
                  self._a(TARGET_DIR_ABOVE, 1700)]
        near = targets.nearest_pending(alerts, 1580)
        self.assertEqual(near["target"], 1700.0)

    def test_no_price_returns_none(self):
        alerts = [self._a(TARGET_DIR_ABOVE, 1600)]
        self.assertIsNone(targets.nearest_pending(alerts, None))

    def test_empty_returns_none(self):
        self.assertIsNone(targets.nearest_pending([], 1500))


class TestTargetLabelText(unittest.TestCase):
    """P0-1/P0-2/P1-1：主窗右上角标签文本。"""
    @staticmethod
    def _a(direction, target, state="pending"):
        return {"direction": direction, "target": float(target),
                "session_date": "2026-12-31", "state": state}

    def test_none_shows_entry(self):
        self.assertEqual(targets.build_target_label_text([], 1500), "设目标")

    def test_pending_shows_pct(self):
        txt = targets.build_target_label_text([self._a(TARGET_DIR_ABOVE, 1600)], 1500)
        self.assertEqual(txt, "↑1600.00 +6.7%")

    def test_fired_shows_marker(self):
        txt = targets.build_target_label_text(
            [self._a(TARGET_DIR_ABOVE, 1600, state="hit")], 1700)
        self.assertIn("已触发", txt)
        self.assertIn("1600.00", txt)

    def test_pending_no_price_no_pct(self):
        txt = targets.build_target_label_text([self._a(TARGET_DIR_ABOVE, 1600)], None)
        self.assertEqual(txt, "↑1600.00")


class TestSessionDateBoundary(unittest.TestCase):
    """R45：生效日判定与清空时刻 15:00:30 同源，消除 30 秒空档。"""

    def test_just_before_clear_still_same_day(self):
        self.assertEqual(targets._session_date_for(_bj(2026, 9, 7, 15, 0, 29)),
                         "2026-09-07")

    def test_at_clear_point_rolls_to_next_trading_day(self):
        from sf_calendar import next_trading_day
        import datetime as _dt
        got = targets._session_date_for(_bj(2026, 9, 7, 15, 0, 30))
        self.assertNotEqual(got, "2026-09-07")
        self.assertEqual(got,
                         next_trading_day(_dt.date(2026, 9, 7)).strftime("%Y-%m-%d"))

    def test_morning_and_lunch_are_same_day(self):
        for hh, mm in ((8, 0), (9, 15), (11, 35), (13, 5)):
            self.assertEqual(
                targets._session_date_for(_bj(2026, 9, 7, hh, mm)),
                "2026-09-07", "时刻 %02d:%02d 应属当天" % (hh, mm))


class TestMergeHitSlotChange(unittest.TestCase):
    """已触发条目被改价时必须作为新目标重新生效，而不是沿用旧 hit 状态。"""

    @staticmethod
    def _a(direction, target, state="pending", sd="2026-12-31"):
        return {"direction": direction, "target": float(target),
                "session_date": sd, "state": state, "last_side": None}

    def test_changing_target_creates_fresh_pending(self):
        live = [self._a(TARGET_DIR_ABOVE, 1600, state=targets.STATE_HIT,
                        sd="2026-09-07")]
        desired = [self._a(TARGET_DIR_ABOVE, 1650, sd="2026-09-07")]
        out = targets.merge_alerts(desired, live)
        self.assertEqual(out[0]["target"], 1650.0)
        self.assertEqual(out[0]["state"], targets.STATE_PENDING,
                         "改价后是新目标，不得沿用旧条目的已触发状态")

    def test_unchanged_hit_stays_hit(self):
        live = [self._a(TARGET_DIR_ABOVE, 1600, state=targets.STATE_HIT,
                        sd="2026-09-07")]
        desired = [self._a(TARGET_DIR_ABOVE, 1600)]
        out = targets.merge_alerts(desired, live)
        self.assertEqual(out[0]["state"], targets.STATE_HIT)

    def test_expired_live_copy_not_preferred(self):
        """R46：live 里同槽位有已过期副本时，合并要挑未过期的那条。"""
        now = _bj(2026, 9, 7, 10, 0)
        live = [self._a(TARGET_DIR_ABOVE, 1600, sd="2020-01-01"),      # 已过期
                self._a(TARGET_DIR_ABOVE, 1600, sd="2099-12-31")]      # 有效
        desired = [self._a(TARGET_DIR_ABOVE, 1600)]
        out = targets.merge_alerts(desired, live, now=now)
        self.assertEqual(out[0]["session_date"], "2099-12-31",
                         "取到过期副本会被 drop_expired 整条丢掉，目标凭空消失")


class TestFutureCalendarSafety(unittest.TestCase):
    """未来官方日历未同步前，目标价不得误触发或被错误清空。"""

    def test_unconfirmed_future_date_is_neither_effective_nor_expired(self):
        alert = {"direction": TARGET_DIR_ABOVE, "target": 110.0,
                 "session_date": "2027-01-01", "state": "pending", "last_side": None}
        now = _bj(2027, 1, 1, 14, 0)
        import unittest.mock as mock
        with mock.patch.object(targets, "is_trading_day_confirmed", return_value=None):
            self.assertFalse(targets.in_effect_window(alert, now))
            self.assertFalse(targets.expired(alert, _bj(2027, 1, 1, 16, 0)))


class TestRollToTradingDay(unittest.TestCase):
    """R47：生效日落在休市日时顺延，避免目标价整天静默不触发。"""

    def test_trading_day_unchanged(self):
        self.assertEqual(targets.roll_to_trading_day("2026-09-07"), "2026-09-07")

    def test_weekend_rolls_to_monday(self):
        self.assertEqual(targets.roll_to_trading_day("2026-09-12"), "2026-09-14")

    def test_holiday_rolls_forward(self):
        self.assertEqual(targets.roll_to_trading_day("2026-02-18"), "2026-02-24")

    def test_unparsable_returns_original(self):
        self.assertEqual(targets.roll_to_trading_day("abc"), "abc")
        self.assertIsNone(targets.roll_to_trading_day(None))

    def test_batch_reports_change(self):
        out, changed = targets.roll_alerts_to_trading_day(
            [{"session_date": "2026-09-07"}, {"session_date": "2026-09-12"}])
        self.assertTrue(changed)
        self.assertEqual(out[1]["session_date"], "2026-09-14")

    def test_batch_no_change_when_all_trading_days(self):
        out, changed = targets.roll_alerts_to_trading_day(
            [{"session_date": "2026-09-07"}])
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
