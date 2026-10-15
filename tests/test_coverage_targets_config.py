# -*- coding: utf-8 -*-
"""targets / sf_config 补测：脏数据下的目标价判定与配置读写的拒绝分支。

为什么单独补这一层（本轮为扩大自动化覆盖而加）
----------------------------------------------
targets.py 与 sf_config.py 是"用户数据进出界面"的两道闸：一条脏 alert 会让
整批目标价连坐清空，一次坏写会把用户的窗口高度/背景模式改掉。它们的
**拒绝分支**（跳过哪条、退回什么、留不留日志）就是产品承诺，此前主要靠
真机走查确认。本文件把这些分支一次性钉住。

约定（AGENTS.md 第 6 节）：不写裸数字断言、不依赖"现在几点"（用 fixed_clock
或显式注入 now）、配置一律落在临时目录。
"""
import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_config             # noqa: E402
import sf_constants          # noqa: E402
import targets               # noqa: E402
from _base import SFCase     # noqa: E402

ABOVE = sf_constants.TARGET_DIR_ABOVE
BELOW = sf_constants.TARGET_DIR_BELOW


def _alert(direction=ABOVE, target=12.34, state=targets.STATE_PENDING,
           session_date="2026-02-18", **extra):
    a = {"direction": direction, "target": target, "state": state,
         "session_date": session_date, "last_side": None}
    a.update(extra)
    return a


# --------------------------------------------------------------------------
# 日期解析与时区补齐
# --------------------------------------------------------------------------

class TestSessionDateParsing(SFCase):
    def test_only_a_real_iso_date_parses(self):
        self.assertEqual(targets._parse_session_date("2026-02-18"), (2026, 2, 18))
        for bad in (None, 123, "", "2026-02", "2026-02-18-1", "abc",
                    "2026-13-01", "2026-02-30"):
            self.assertIsNone(targets._parse_session_date(bad), repr(bad))

    def test_non_numeric_segments_do_not_raise(self):
        """R33-2：脏数据抛 ValueError 会把整批目标价连坐清空。"""
        for bad in ("2026-abc-18", "2026-02-xy", "----"):
            self.assertIsNone(targets._parse_session_date(bad), bad)

    def test_clear_time_is_150030_beijing(self):
        dt = targets._clear_dt("2026-02-18")
        self.assertEqual((dt.hour, dt.minute, dt.second), (15, 0, 30))
        self.assertEqual(dt.utcoffset(), datetime.timedelta(hours=8))

    def test_naive_datetimes_are_read_as_beijing_time(self):
        naive = datetime.datetime(2026, 2, 18, 10, 0)
        aware = targets._as_beijing(naive)
        self.assertEqual(aware.utcoffset(), datetime.timedelta(hours=8))
        self.assertIsNone(targets._as_beijing(None), "None 原样返回")
        already = aware.astimezone(datetime.timezone.utc)
        self.assertIs(targets._as_beijing(already), already,
                      "已带时区的不再二次换算，否则会把时刻挪偏 8 小时")


class TestEffectWindow(SFCase):
    def test_missing_now_is_not_in_the_window(self):
        self.assertFalse(targets.in_effect_window(_alert(), None))

    def test_unparsable_session_date_never_judges(self):
        for sd in (None, "", "今天", "2026-02-30"):
            self.assertFalse(
                targets.in_effect_window(_alert(session_date=sd),
                                         datetime.datetime(2026, 2, 18, 10, 0)),
                repr(sd))

    def test_window_is_the_effective_day_until_150030(self):
        with mock.patch.object(targets, "is_trading_day_confirmed",
                               return_value=True):
            self.assertTrue(targets.in_effect_window(
                _alert(), datetime.datetime(2026, 2, 18, 14, 59)))
            self.assertFalse(targets.in_effect_window(
                _alert(), datetime.datetime(2026, 2, 18, 15, 0, 31)))
            self.assertFalse(targets.in_effect_window(
                _alert(), datetime.datetime(2026, 2, 19, 9, 30)),
                "次日行情不能触发昨天的目标")
            self.assertFalse(targets.in_effect_window(
                _alert(), datetime.datetime(2026, 2, 17, 9, 30)))

    def test_unconfirmed_calendar_holds_judgement_back(self):
        """未来月份官方日历未到时不得触发提醒（假日用收盘缓存价会误触发）。"""
        with mock.patch.object(targets, "is_trading_day_confirmed",
                               return_value=None):
            self.assertFalse(targets.in_effect_window(
                _alert(session_date="2027-01-04"),
                datetime.datetime(2027, 1, 4, 10, 0)))


class TestRollToTradingDay(SFCase):
    def test_unparsable_date_is_returned_untouched(self):
        for sd in (None, "", "明天", 5):
            self.assertEqual(targets.roll_to_trading_day(sd), sd, repr(sd))

    def test_a_confirmed_trading_day_is_left_alone(self):
        with mock.patch.object(targets, "is_trading_day_confirmed",
                               return_value=True):
            self.assertEqual(targets.roll_to_trading_day("2026-02-18"), "2026-02-18")

    def test_holiday_rolls_forward_to_the_next_trading_day(self):
        with mock.patch.object(targets, "is_trading_day_confirmed",
                               side_effect=[False, False, True]), \
                mock.patch.object(targets, "next_trading_day",
                                  side_effect=lambda d: d + datetime.timedelta(days=1)):
            self.assertEqual(targets.roll_to_trading_day("2026-02-21"), "2026-02-23")

    def test_unconfirmed_calendar_keeps_the_original_date(self):
        """日历没补齐时不擅自顺延也不清空，等后台同步完成再判。"""
        with mock.patch.object(targets, "is_trading_day_confirmed",
                               return_value=None):
            self.assertEqual(targets.roll_to_trading_day("2027-01-01"), "2027-01-01")

    def test_roll_is_bounded_so_a_broken_calendar_cannot_loop(self):
        with mock.patch.object(targets, "is_trading_day_confirmed",
                               return_value=False), \
                mock.patch.object(targets, "next_trading_day",
                                  side_effect=lambda d: d + datetime.timedelta(days=1)) as nxt:
            self.assertEqual(targets.roll_to_trading_day("2026-02-21", max_roll=5),
                             "2026-02-21")
        self.assertEqual(nxt.call_count, 5, "最多顺延 max_roll 次，不能无限递推")

    def test_batch_roll_passes_non_dict_entries_through(self):
        alerts = [_alert(session_date="2026-02-21"), "垃圾条目", None]
        with mock.patch.object(targets, "roll_to_trading_day",
                               return_value="2026-02-23"):
            out, changed = targets.roll_alerts_to_trading_day(alerts)
        self.assertTrue(changed)
        self.assertEqual(out[1], "垃圾条目")
        self.assertIsNone(out[2])
        self.assertEqual(out[0]["session_date"], "2026-02-23")


# --------------------------------------------------------------------------
# 入参校验 / 距离计算
# --------------------------------------------------------------------------

class TestValidation(SFCase):
    def test_direction_and_target_must_both_be_sane(self):
        ok, why = targets.validate_alert(ABOVE, "12.5")
        self.assertTrue(ok, why)
        self.assertFalse(targets.validate_alert("up", 1)[0])
        self.assertEqual(targets.validate_alert(ABOVE, "abc")[1],
                         "target 不是合法数字")
        self.assertEqual(targets.validate_alert(ABOVE, None)[1],
                         "target 不是合法数字")

    def test_infinite_targets_are_rejected_before_the_zero_check(self):
        """inf 过不了 isfinite，却能满足 "> 0" —— 顺序错了就会收下一条永远触发不了的目标。"""
        for bad in (float("inf"), float("-inf"), float("nan")):
            ok, why = targets.validate_alert(ABOVE, bad)
            self.assertFalse(ok, bad)
            self.assertEqual(why, "target 不是有限数字")

    def test_non_positive_targets_are_rejected(self):
        self.assertEqual(targets.validate_alert(ABOVE, 0)[1],
                         "target 必须大于 0")
        self.assertEqual(targets.validate_alert(BELOW, -1)[1],
                         "target 必须大于 0")

    def test_distance_pct_refuses_every_untrusted_shape(self):
        """价格不可信时只显价位不显百分比，避免界面出现 "+inf%" / "nan%"。"""
        for p, t in ((0, 10), (10, 0), (None, 10), (10, None), ("x", 10),
                     (float("nan"), 10), (10, float("inf"))):
            self.assertIsNone(targets.distance_pct(p, t), (p, t))

    def test_distance_pct_sign_is_how_much_more_it_must_move(self):
        self.assertAlmostEqual(targets.distance_pct(10.0, 11.0), 10.0)
        self.assertAlmostEqual(targets.distance_pct(10.0, 9.0), -10.0)

    def test_extreme_magnitudes_are_reported_as_untrusted(self):
        """p 极小、t 极大时会算出 inf，同样按不可信处理。"""
        self.assertIsNone(targets.distance_pct(5e-324, 1e308))

    def test_nearest_pending_skips_junk_and_hit_entries(self):
        alerts = ["not-a-dict", None, _alert(target=99, state=targets.STATE_HIT),
                  _alert(target=10.5), _alert(target=11)]
        best = targets.nearest_pending(alerts, 10.0)
        self.assertEqual(best["target"], 10.5)
        self.assertIsNone(targets.nearest_pending(alerts, 0),
                         "现价不可信时不挑「最近的一条」")

    def test_same_slot_requires_both_direction_and_price(self):
        self.assertTrue(targets._same_slot(_alert(target=10), _alert(target="10.0")))
        self.assertFalse(targets._same_slot(_alert(direction=ABOVE),
                                            _alert(direction=BELOW)))
        self.assertFalse(targets._same_slot(_alert(target=10), _alert(target=11)))
        self.assertFalse(targets._same_slot(None, _alert()))
        self.assertFalse(targets._same_slot(_alert(), "x"))

    def test_same_slot_survives_unusable_targets(self):
        for junk in (None, "", "abc", {}):
            self.assertFalse(targets._same_slot(_alert(target=junk), _alert()),
                             repr(junk))

    def test_merging_drops_duplicate_slots_so_one_crossing_fires_once(self):
        """R38：同一目标价存两份 → 越线时连发两条一模一样的通知。"""
        desired = [_alert(target=10.0), _alert(target=10.0), _alert(target=12.0)]
        merged = targets.merge_alerts(desired, [], now=datetime.datetime(
            2026, 2, 18, 10, 0))
        self.assertEqual(sorted(a["target"] for a in merged), [10.0, 12.0])

    def test_merge_skips_non_dict_wishes(self):
        merged = targets.merge_alerts([_alert(), "junk", None], [], now=None)
        self.assertEqual([a["target"] for a in merged], [12.34])

    def test_same_slot_keeps_the_live_entry_so_a_hit_is_not_reset(self):
        """面板打开期间某条已触发：保存不得把它退回 pending，否则下个 tick 重复提醒。"""
        live = [_alert(target=10.0, state=targets.STATE_HIT,
                       session_date="2026-02-18")]
        desired = [_alert(target=10.0, state=targets.STATE_PENDING)]
        merged = targets.merge_alerts(desired, live,
                                      now=datetime.datetime(2026, 2, 18, 11, 0))
        self.assertEqual(merged[0]["state"], targets.STATE_HIT)
        with mock.patch.object(targets, "is_trading_day_confirmed", return_value=True):
            self.assertEqual(merged[0]["session_date"], "2026-02-18")

    def test_expired_live_candidate_is_not_dragged_back_in(self):
        """v1.3.42 第4轮走查：live 同槽位全部过期时必须改用期望条目本身。

        否则"面板打开期间后台跨过 15:00:30"这条真实路径会让用户点保存后
        界面提示成功、目标价却被随后的 drop_expired 整条清掉。
        """
        live = [_alert(target=10.0, session_date="2026-02-18")]
        desired = [_alert(target=10.0, session_date="2026-02-19")]
        merged = targets.merge_alerts(desired, live,
                                      now=datetime.datetime(2026, 2, 19, 9, 30))
        self.assertEqual(merged[0]["session_date"], "2026-02-19",
                         "拖回过期条目 = 保存成功但目标价当场消失")

    def test_junk_in_the_live_list_is_filtered_out(self):
        live = ["junk", None, _alert(target=10.0)]
        merged = targets.merge_alerts([_alert(target=10.0)], live, now=None)
        self.assertEqual(len(merged), 1)


# --------------------------------------------------------------------------
# 主窗右上角单行摘要
# --------------------------------------------------------------------------

class TestTargetLabel(SFCase):
    def test_no_targets_shows_the_persistent_entry_hint(self):
        for empty in (None, [], [None, "junk"]):
            self.assertEqual(targets.build_target_label_text(empty, 10.0), "设目标")

    def test_a_hit_target_keeps_its_arrow_and_price(self):
        self.assertEqual(
            targets.build_target_label_text([_alert(target=11.0, state=targets.STATE_HIT)],
                                            10.0),
            "已触发 ↑11.00")
        self.assertEqual(
            targets.build_target_label_text(
                [_alert(direction=BELOW, target=9.0, state=targets.STATE_HIT)], 10.0),
            "已触发 ↓9.00")

    def test_pending_target_shows_the_distance_to_it(self):
        self.assertEqual(
            targets.build_target_label_text([_alert(target=11.0)], 10.0),
            "↑11.00 +10.0%")

    def test_untrusted_price_degrades_to_price_only(self):
        """现价不可信 → 只显价位，不显百分比。"""
        self.assertEqual(
            targets.build_target_label_text([_alert(target=11.0)], 0), "↑11.00")
        self.assertEqual(
            targets.build_target_label_text([_alert(target=11.0)], None), "↑11.00")

    def test_a_hit_entry_with_a_broken_target_still_renders(self):
        for junk in (None, "abc", {}):
            self.assertEqual(
                targets.build_target_label_text(
                    [_alert(target=junk, state=targets.STATE_HIT)], 10.0),
                "已触发 ↑0.00", repr(junk))

    def test_pending_but_unmeasurable_entries_fall_back_to_the_first(self):
        """没有可信 pending（全是脏方向/脏价）时退回第一条，不给空白。"""
        self.assertEqual(
            targets.build_target_label_text([_alert(direction="sideways",
                                                   target="abc")], 10.0),
            "↓0.00")


# --------------------------------------------------------------------------
# sf_config：读盘容错
# --------------------------------------------------------------------------

class ConfigCase(SFCase):
    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp()
        self.cfg = os.path.join(self.d, "config.json")
        self.bak = self.cfg + ".bak"
        self.lq = os.path.join(self.d, "last_quote.json")
        self._saved = (sf_config.CONFIG_PATH, sf_config.CONFIG_BACKUP_PATH,
                       os.environ.get("SF_LAST_QUOTE_PATH"))
        sf_config.CONFIG_PATH = self.cfg
        sf_config.CONFIG_BACKUP_PATH = self.bak
        os.environ["SF_LAST_QUOTE_PATH"] = self.lq

    def tearDown(self):
        (sf_config.CONFIG_PATH, sf_config.CONFIG_BACKUP_PATH, lp) = self._saved
        if lp is None:
            os.environ.pop("SF_LAST_QUOTE_PATH", None)
        else:
            os.environ["SF_LAST_QUOTE_PATH"] = lp
        super().tearDown()

    def _write_cfg(self, obj, path=None):
        with open(path or self.cfg, "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj,
                                                                ensure_ascii=False))

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _read_cfg(self):
        return self._read(self.cfg)

    def _write_alerts(self, alerts):
        self._write_cfg({"code": "sh600519", "x": 10, "y": 10, "h": 200,
                         "bg": "glass", "version": sf_config.CONFIG_SCHEMA_VERSION,
                         "alerts": alerts})


class TestLoadAlerts(ConfigCase):
    def test_missing_file_yields_no_targets(self):
        self.assertEqual(sf_config.load_alerts(), [])

    def test_missing_or_non_list_alerts_field_is_treated_as_empty(self):
        self._write_cfg({"code": "sh600519"})
        self.assertEqual(sf_config.load_alerts(), [])
        self._write_cfg({"alerts": {"direction": ABOVE}})
        self.assertEqual(sf_config.load_alerts(), [])

    def test_backup_snapshot_rescues_a_corrupt_main_file(self):
        """主配置解析失败时回落备份：否则用户会以为是自己没设过目标价。"""
        self._write_alerts([_alert()])
        sf_config.save_alerts(sf_config.load_alerts())
        good = self._read_cfg()
        with open(self.bak, "w", encoding="utf-8") as f:
            json.dump(good, f, ensure_ascii=False)
        self._write_cfg("\ufeff{半截 JSON")
        with mock.patch.object(sf_config, "log_line") as lg:
            out = sf_config.load_alerts()
        self.assertEqual([a["target"] for a in out], [12.34])
        self.assertTrue(any("从备份快照读取" in c.args[0] for c in lg.call_args_list))

    def test_junk_entries_are_skipped_one_by_one_with_a_log(self):
        self._write_alerts(["垃圾", 42,
                           {"direction": "sideways", "target": 10.0,
                            "session_date": "2026-02-18"},
                           {"direction": ABOVE, "target": "abc",
                            "session_date": "2026-02-18"},
                           {"direction": ABOVE, "target": True,
                            "session_date": "2026-02-18"},
                           {"direction": ABOVE, "target": 0,
                            "session_date": "2026-02-18"},
                           {"direction": ABOVE, "target": float("nan"),
                            "session_date": "2026-02-18"},
                           {"direction": ABOVE, "target": 11.0, "session_date": "  "}])
        with mock.patch.object(sf_config, "log_line") as lg:
            out = sf_config.load_alerts()
        self.assertEqual(out, [], "每一条脏数据都得被点名跳过：%s" % out)
        msgs = [c.args[0] for c in lg.call_args_list]
        self.assertGreaterEqual(len(msgs), 7)

    def test_numeric_strings_are_normalised_to_float(self):
        """手工编辑/其它版本写入的字符串数字要能用，且类型归一后回写。"""
        self._write_alerts([{"direction": ABOVE, "target": " 12.50 ",
                            "session_date": "2026-02-18"}])
        out = sf_config.load_alerts()
        self.assertEqual(out[0]["target"], 12.5)
        self.assertIsInstance(out[0]["target"], float)

    def test_int_targets_are_accepted_and_returned_as_float(self):
        self._write_alerts([_alert(target=18)])
        self.assertEqual(sf_config.load_alerts()[0]["target"], 18.0)


class TestSaveConfig(ConfigCase):
    def test_unknown_keys_survive_a_save(self):
        """以旧配置为底稿整体更新：只搬已知字段的旧实现每次保存都在删数据。"""
        self._write_cfg({"code": "sh600519", "x": 1, "y": 2, "h": 200,
                         "bg": "glass", "version": sf_config.CONFIG_SCHEMA_VERSION,
                         "my_future_key": {"a": 1}, "alerts": [_alert()]})
        self.assertTrue(sf_config.save_config("sz000001", 3, 4, 210, "glass"))
        d = self._read_cfg()
        self.assertEqual(d["my_future_key"], {"a": 1})
        self.assertEqual([a["target"] for a in d["alerts"]], [12.34])
        self.assertEqual(d["code"], "sz000001")

    def test_a_future_schema_version_is_never_overwritten(self):
        """旧版程序不得把新版写过的配置降级覆盖。"""
        self._write_cfg({"code": "sh600519", "version": sf_config.CONFIG_SCHEMA_VERSION + 5})
        with mock.patch.object(sf_config, "log_line") as lg:
            self.assertFalse(sf_config.save_config("sz000001", 1, 1, 200, "glass"))
        self.assertIn("future schema version", lg.call_args.args[0])
        d = self._read_cfg()
        self.assertEqual(d["code"], "sh600519", "拒绝写入 = 原样保留")

    def test_removed_keys_are_cleaned_up_on_any_save(self):
        self._write_cfg({"code": "sh600519", "version": 1,
                         "target_flash": {"old": True}})
        sf_config.save_config("sh600519", 1, 1, 200, "glass")
        self.assertNotIn("target_flash", self._read_cfg())

    def test_non_default_background_mode_is_loud(self):
        """背景模式界面没有切换入口，出现非默认值说明配置被异常写入过。"""
        self._write_cfg({"code": "sh600519", "version": 1})
        with mock.patch.object(sf_config, "log_line") as lg:
            sf_config.save_config("sh600519", 1, 1, 200, "translucent")
        msgs = [c.args[0] for c in lg.call_args_list]
        self.assertTrue(any("non-default bg" in m for m in msgs), msgs)

    def test_a_failed_write_leaves_no_temporary_file(self):
        self._write_cfg({"code": "sh600519", "version": 1})
        real_replace = os.replace

        def boom(a, b):
            raise OSError("disk full")

        with mock.patch("os.replace", side_effect=boom), \
                mock.patch.object(sf_config, "log_line"):
            self.assertFalse(sf_config.save_config("sh600519", 1, 1, 200, "glass"))
        self.assertFalse(os.path.exists(self.cfg + ".tmp"),
                         "半截临时文件留在盘上，下次保存会拿它当底稿")
        with mock.patch("os.replace", side_effect=boom):
            pass
        self.assertTrue(callable(real_replace))


class TestAtomicWriteAndSnapshots(ConfigCase):
    def test_atomic_write_leaves_no_temp_on_failure(self):
        target = os.path.join(self.d, "out.json")
        with mock.patch("os.replace", side_effect=OSError("no space left")):
            with self.assertRaises(OSError):
                sf_config._atomic_write_json(target, {"a": 1})
        self.assertFalse(os.path.exists(target + ".tmp"))
        self.assertFalse(os.path.exists(target))

    def test_successful_write_is_owner_only(self):
        import stat
        target = os.path.join(self.d, "out.json")
        sf_config._atomic_write_json(target, {"a": 1})
        self.assertEqual(json.load(open(target, encoding="utf-8")) if False else self._read(target), {"a": 1})
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600,
                         "配置里有自定义接口 URL，可能带凭据")

    def test_last_quote_snapshot_failure_is_reported_as_false(self):
        """快照写失败只记 WARNING：它是可再生缓存，不影响行情主流程。"""
        with mock.patch.object(sf_config, "_atomic_write_json",
                               side_effect=OSError("ro")):
            with mock.patch.object(sf_config, "log_line") as lg:
                self.assertFalse(sf_config.save_last_quote("sh600519",
                                                           {"price": 1}, [], "2026-02-18"))
        self.assertIn("save_last_quote FAILED", lg.call_args.args[0])
        self.assertEqual(lg.call_args.args[1] if len(lg.call_args.args) > 1
                         else lg.call_args.kwargs.get("level"), "WARNING")

    def test_snapshot_round_trip(self):
        self.assertTrue(sf_config.save_last_quote(
            "sh600519", {"price": 1600.5}, [("0931", 1600.0)], "2026-02-18"))
        got = sf_config.load_last_quote()
        self.assertEqual(got["code"], "sh600519")
        self.assertEqual(got["quote"]["price"], 1600.5)

    def test_snapshot_without_a_quote_dict_is_rejected(self):
        with open(self.lq, "w", encoding="utf-8") as f:
            json.dump({"code": "sh600519"}, f)
        self.assertIsNone(sf_config.load_last_quote(),
                          "结构不符要退化成占位符，而不是让首屏渲染炸掉")
        with open(self.lq, "w", encoding="utf-8") as f:
            f.write("[1,2,3]")
        self.assertIsNone(sf_config.load_last_quote())


class TestConfigVersionFallback(ConfigCase):
    def test_non_numeric_version_falls_back_to_the_current_schema(self):
        self._write_cfg({"code": "sh600519", "x": 1, "y": 1,
                         "h": sf_config.MIN_H,
                         "bg": "glass", "version": "v1"})
        with mock.patch.object(sf_config, "_migrate_legacy_config"):
            code, x, y, h, bg = sf_config.load_config()
        self.assertEqual(code, "sh600519")
        self.assertEqual(bg, "glass")

    def test_load_alerts_ignores_a_version_it_cannot_parse(self):
        """顶层是数组的配置（历史半截产物）不能完全让目标价读取崩掉。"""
        self._write_cfg([1, 2, 3])
        with mock.patch.object(sf_config, "log_line"):
            self.assertEqual(sf_config.load_alerts(), [])


class TestRedaction(ConfigCase):
    """诊断包外发前的脱敏：宁可多遮，也不能回吐疑似凭据。"""

    def test_url_query_secrets_are_masked_and_structure_kept(self):
        out = sf_config._redact_url("https://api.test/q?token=abc&secid=1.600519")
        self.assertIn("https://api.test/" + sf_config._REDACTED, out)
        self.assertNotIn("abc", out)
        self.assertNotIn("secid=", out)

    def test_basic_auth_in_the_netloc_is_masked(self):
        out = sf_config._redact_url("https://user:pw@api.test/q")
        self.assertNotIn("user", out)
        self.assertNotIn("pw", out)

    def test_template_urls_without_query_are_left_readable(self):
        raw = "https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid=%s&x=1"
        self.assertIn("%s", sf_config._redact_url(raw),
                      "urlencode 会把 %s 变成 %25s，把排障信息本身改坏")

    def test_non_urls_pass_through(self):
        self.assertEqual(sf_config._redact_url("/just/a/path"), "/just/a/path")

    def test_unparseable_url_is_fully_masked(self):
        with mock.patch("urllib.parse.urlsplit", side_effect=ValueError("bad")):
            self.assertEqual(sf_config._redact_url("https://x/\u200b?token=a"),
                             sf_config._REDACTED)

    def test_home_directory_username_is_hidden(self):
        out = sf_config._redact_home_path(
            "/Users/example/Library/Application Support/x")
        self.assertNotIn("moriarty", out)
        self.assertIn("/Users/<USER>/", out, "路径结构要保留，排障才知道读的哪个文件")

    def test_home_redaction_passes_through_non_strings(self):
        for obj in (None, 5, {"a": 1}):
            self.assertIs(sf_config._redact_home_path(obj), obj)

    def test_log_redaction_of_non_text_returns_empty(self):
        """非字符串不能原样回吐 —— 调用方会把对象 repr 进诊断包。"""
        self.assertEqual(sf_config.redact_log_text(None), "")
        self.assertEqual(sf_config.redact_log_text(b"token=abc"), "")

    def test_log_line_secrets_are_masked(self):
        out = sf_config.redact_log_text(
            "GET https://api.test/q?token=SECRET 失败")
        self.assertNotIn("SECRET", out)


if __name__ == "__main__":
    unittest.main()
