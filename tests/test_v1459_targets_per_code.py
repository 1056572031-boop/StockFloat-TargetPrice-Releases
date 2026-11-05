# -*- coding: utf-8 -*-
"""v1.4.59（S4 彻底解）：目标价按 code 存档，切股不再是删除。

30 轮走查 S4 的原始事实：`apply_stock` 无条件 `_clear_targets()`，上一只股票
一条条手录的目标价整片消失，无确认、无撤销；而同一个面板点「清空」反而有两次确认。
报告给了两个改法：加确认框（治标）与按 code 存档（彻底解）。用户拍板走彻底解，
所以这里守的是三件事：
  · 存储里别的股票的条目不许被这次写入带走；
  · 面板/内存只显示当前这只的条目；
  · 旧配置（条目没有 code）在升级后一条都不许看不见。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_config                                          # noqa: E402
import stock_float_window as m                            # noqa: E402

try:
    from _base import SFCase
except ImportError:                                        # pragma: no cover
    from tests._base import SFCase


def _alert(code, target=70.0, day="2099-12-31", direction="above"):
    return {"code": code, "target": target, "direction": direction,
            "session_date": day, "state": "pending"}


class StoreIsPerCode(SFCase):
    """sf_config 这一层：写入只替换当前股票的条目。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="sf_alerts_store_")
        self.cfg = os.path.join(self.tmp, "config.json")
        self.bak = os.path.join(self.tmp, "config.backup.json")
        for p in (patch.object(sf_config, "CONFIG_PATH", self.cfg),
                  patch.object(sf_config, "CONFIG_BACKUP_PATH", self.bak),
                  patch.object(sf_config, "log_line", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    def _raw(self):
        with open(self.cfg, encoding="utf-8") as f:
            return json.load(f)["alerts"]

    def test_switching_saves_do_not_evict_other_codes(self):
        self.assertTrue(sf_config.save_alerts([_alert("sh600519", 70.0)],
                                              code="sh600519"))
        self.assertTrue(sf_config.save_alerts([_alert("sz000001", 11.0)],
                                              code="sz000001"))
        stored = self._raw()
        self.assertEqual(sorted(str(a["code"]) for a in stored),
                         ["sh600519", "sz000001"],
                         "后一次写入把前一只股票的条目带走了 —— 就是 S4 那个丢数据")

    def test_load_filters_by_code(self):
        sf_config.save_alerts([_alert("sh600519", 70.0)], code="sh600519")
        sf_config.save_alerts([_alert("sz000001", 11.0)], code="sz000001")
        self.assertEqual([a["code"] for a in sf_config.load_alerts("sh600519")],
                         ["sh600519"])
        self.assertEqual([a["code"] for a in sf_config.load_alerts("sz000001")],
                         ["sz000001"])
        self.assertEqual(len(sf_config.load_alerts()), 2, "不传 code 仍读全量（诊断包用）")

    def test_clear_only_touches_the_current_code(self):
        """面板「清空」= 只清这一只；别只股票的条目必须原样留着。"""
        sf_config.save_alerts([_alert("sh600519", 70.0)], code="sh600519")
        sf_config.save_alerts([], code="sz000001")
        self.assertEqual([a["code"] for a in self._raw()], ["sh600519"])

    def test_legacy_entries_without_code_are_never_hidden(self):
        """升级安全：旧配置里的条目没有 code，绝不能因为"对不上"而被藏起来。"""
        legacy = _alert("", 70.0)
        legacy.pop("code")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "alerts": [legacy]}, f)
        self.assertEqual(len(sf_config.load_alerts("sh600519")), 1,
                         "无主条目对任何股票都可见（宁可多显示，不许看不见）")

    def test_save_stamps_the_code_so_a_later_switch_can_tell_them_apart(self):
        """不补 code 的话：这批条目下次切到别的股票时被当成"别人的"清掉 —— 换个地方丢数据。"""
        sf_config.save_alerts([_alert("", 70.0)], code="sh600519")
        self.assertEqual(self._raw()[0]["code"], "sh600519")
        sf_config.save_alerts([_alert("sz000001", 11.0)], code="sz000001")
        self.assertEqual(len(sf_config.load_alerts("sh600519")), 1,
                         "补过 code 之后，切到别只股票再切回来才接得上")

    def test_prune_drops_dormant_never_the_active_stock(self):
        big = [_alert("sh6000%02d" % i, 10.0 + i) for i in range(80)]
        for a in big:
            sf_config.save_alerts([a], code=a["code"])       # 攒出大量休眠条目
        active = [_alert("sh600519", 70.0) for _ in range(3)]
        with patch.object(sf_config, "log_line") as lg:
            self.assertTrue(sf_config.save_alerts(active, code="sh600519"))
        mine = sf_config.load_alerts("sh600519")
        self.assertEqual(len(mine), 3, "当前股票的条目永远不因上限被剪")
        self.assertTrue(any("休眠" in str(c.args[0]) for c in lg.call_args_list),
                        "剪了休眠条目必须留一条日志，否则「条目怎么少了」无从回答")
        self.assertLessEqual(len(sf_config.load_alerts()),
                             sf_config.ALERT_STORE_MAX_TOTAL + 3)


    def test_prune_removes_the_oldest_written_stock_not_the_newest(self):
        """2026-11-02 走查 F-B-01：存储顺序是「刚写入的排最前」，剪枝必须从尾部（最早写入）下手。

        旧写法取 dormant_codes[0]，剪掉的恰是刚切走的那只，注释与日志却都写着“保留最近 12 只”。
        """
        for i in range(16):
            code = "sh6000%02d" % i
            sf_config.save_alerts([_alert(code, 10.0 + i)], code=code)
        kept = {str(a["code"]) for a in self._raw()}
        self.assertEqual(len(kept), sf_config.ALERT_STORE_MAX_CODES + 1,
                         "休眠股票留满上限，再加上当前这一只")
        self.assertIn("sh600015", kept, "刚写入的那只（当前股票）不许被剪")
        self.assertIn("sh600014", kept, "刚切走的那只是最近写入的休眠股票，不许先被剪")
        self.assertNotIn("sh600000", kept, "最早写入的那只才是被剪对象")

    def test_prune_drops_expired_dormant_before_any_live_one(self):
        """2026-11-02 走查 F-B-01：别的股票的过期条目没有任何清理路径，攒满 12 只后会把
        今天刚设的有效目标价挤掉。超限时先丢已过期的休眠条目，有效的最后才动。"""
        for i in range(sf_config.ALERT_STORE_MAX_CODES):
            code = "sh6001%02d" % i
            sf_config.save_alerts([_alert(code, 10.0 + i, day="2020-01-02")], code=code)
        sf_config.save_alerts([_alert("sh600519", 1600.0)], code="sh600519")
        sf_config.save_alerts([_alert("sz000001", 11.0)], code="sz000001")
        mine = sf_config.load_alerts("sh600519")
        self.assertEqual([a["target"] for a in mine], [1600.0],
                         "切走后，今天刚设的有效目标价必须还在，被丢的应是早已过期的旧条目")

    def test_invalid_dormant_entry_is_not_considered_expired(self):
        self.assertFalse(sf_config._session_is_past(None))  # discipline-ok(R5): 非法输入需保守处理而不能当成过期。

    def test_prune_does_not_drop_at_the_close_boundary(self):
        import targets
        from datetime import datetime, timedelta, timezone
        clear_at = datetime(2026, 11, 2, 15, 0, 30, tzinfo=timezone(timedelta(hours=8)))
        boundary = _alert("sh600399", day="2026-11-02")
        with patch.object(targets, "is_trading_day_confirmed", return_value=True), \
             patch.object(targets, "_clear_dt", return_value=clear_at), \
             patch.object(targets, "_beijing_now", return_value=clear_at + timedelta(seconds=119)):
            self.assertFalse(sf_config._droppable_expired([boundary]),  # discipline-ok(R5): 余量边界只能在判定函数上以注入时钟精确卡点，公开保存路径无法固定到秒
                             "收盘切点后的安全余量内不能把可能仍在途的目标价剪掉")
        with patch.object(targets, "is_trading_day_confirmed", return_value=True), \
             patch.object(targets, "_clear_dt", return_value=clear_at), \
             patch.object(targets, "_beijing_now", return_value=clear_at + timedelta(seconds=120)):
            self.assertTrue(sf_config._droppable_expired([boundary]))  # discipline-ok(R5): 同上，余量刚好走完的另一侧边界

    def test_prune_keeps_unconfirmed_recent_session(self):
        import targets
        for i in range(sf_config.ALERT_STORE_MAX_CODES + 1):
            code = "sh6002%02d" % i
            day = "2020-01-02" if i == sf_config.ALERT_STORE_MAX_CODES else "2099-12-31"
            with patch.object(targets, "is_trading_day_confirmed", return_value=None):
                self.assertTrue(sf_config.save_alerts([_alert(code, day=day)], code=code))
        with patch.object(targets, "is_trading_day_confirmed", return_value=None):
            self.assertTrue(sf_config.save_alerts([_alert("sz000001")], code="sz000001"))
        self.assertTrue(sf_config.load_alerts("sh600212"))

    def test_prune_uses_beijing_close_not_local_date(self):
        from datetime import datetime, timedelta, timezone
        import targets
        now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        for i in range(sf_config.ALERT_STORE_MAX_CODES + 1):
            code = "sh6003%02d" % i
            day = "2026-11-02" if i == sf_config.ALERT_STORE_MAX_CODES else "2099-12-31"
            with patch.object(targets, "is_trading_day_confirmed", return_value=True), \
                 patch.object(targets, "_beijing_now", return_value=now), \
                 patch.object(sf_config.time, "strftime", return_value="2026-11-03"):
                self.assertTrue(sf_config.save_alerts([_alert(code, day=day)], code=code))
        with patch.object(targets, "is_trading_day_confirmed", return_value=True), \
             patch.object(targets, "_beijing_now", return_value=now), \
             patch.object(sf_config.time, "strftime", return_value="2026-11-03"):
            self.assertTrue(sf_config.save_alerts([_alert("sz000001")], code="sz000001"))
        self.assertTrue(sf_config.load_alerts("sh600312"))
    def test_prune_preserves_holiday_target_rolled_to_future_session(self):
        from datetime import datetime, timedelta, timezone
        import targets
        now = datetime(2026, 10, 2, 16, tzinfo=timezone(timedelta(hours=8)))
        holiday = _alert("sh600001", day="2026-10-01")
        expired = _alert("sh600002", day="2026-09-30")
        with patch.object(sf_config, "ALERT_STORE_MAX_CODES", 1), \
             patch.object(targets, "_beijing_now", return_value=now), \
             patch.object(targets, "is_trading_day_confirmed",
                          side_effect=lambda day: day.isoformat() != "2026-10-01"), \
             patch.object(targets, "next_trading_day",
                          return_value=datetime(2026, 10, 5).date()):
            self.assertTrue(sf_config.save_alerts([expired], code="sh600002"))
            self.assertTrue(sf_config.save_alerts([holiday], code="sh600001"))
            self.assertTrue(sf_config.save_alerts([_alert("sz000001")], code="sz000001"))
        self.assertEqual(sf_config.load_alerts("sh600001"), [holiday])
        self.assertEqual(sf_config.load_alerts("sh600002"), [])

    def _durable_env(self):
        # 持久化模式是生产默认；测试默认关闭它，这里显式打开才是真实路径。
        return patch.dict(os.environ, {"SF_TARGET_TICKS_PATH": os.path.join(self.tmp, "ticks.sqlite")})

    def test_durable_mode_drops_expired_dormant_instead_of_refusing_new_stock(self):
        """复核 B1：持久化开启时超限分支只会拒绝、不剪枝，别的股票早已过期的条目又没有任何
        清理路径，于是设过 13 只股票后，第 14 只的目标价永远保存失败（界面还说是磁盘/权限问题）。
        已确认过期的休眠条目没有保留价值，超限时必须先丢它们。"""
        with self._durable_env():
            for i in range(sf_config.ALERT_STORE_MAX_CODES + 1):
                code = "sh6004%02d" % i
                self.assertTrue(sf_config.save_alerts([_alert(code, 10.0 + i, day="2020-01-02")],
                                                      code=code))
            self.assertTrue(sf_config.save_alerts([_alert("sh600519", 1600.0)], code="sh600519"),
                            "只有过期条目占着名额时，新股票的目标价必须能保存")
        self.assertEqual([a["target"] for a in sf_config.load_alerts("sh600519")], [1600.0])
        self.assertEqual(sf_config.load_alerts("sh600400"), [], "过期的休眠条目应已被清掉")

    def test_durable_mode_still_refuses_when_live_dormant_entries_fill_the_store(self):
        """有效（未过期）的休眠条目不能为了腾位置被丢：持久化模式下它们可能还有在途行情。"""
        with self._durable_env():
            for i in range(sf_config.ALERT_STORE_MAX_CODES + 1):
                code = "sh6005%02d" % i
                self.assertTrue(sf_config.save_alerts([_alert(code, 10.0 + i)], code=code))
            self.assertFalse(sf_config.save_alerts([_alert("sh600519", 1600.0)], code="sh600519"))
            self.assertEqual(sf_config.last_alert_refusal(), "store_limit",
                             "拒绝的真实原因必须能被界面读到，不能让用户去查磁盘和权限")
        self.assertTrue(sf_config.load_alerts("sh600500"), "被拒绝时已有的有效条目一条都不许少")

    def test_refusal_reason_is_cleared_by_the_next_save(self):
        with self._durable_env():
            for i in range(sf_config.ALERT_STORE_MAX_CODES + 1):
                code = "sh6006%02d" % i
                sf_config.save_alerts([_alert(code, 10.0 + i)], code=code)
            self.assertFalse(sf_config.save_alerts([_alert("sh600519", 1600.0)], code="sh600519"))
            self.assertTrue(sf_config.save_alerts([_alert("sh600600", 99.0)], code="sh600600"))
            self.assertEqual(sf_config.last_alert_refusal(), "", "一次成功的保存必须清掉上次的拒绝原因")

    def test_prune_report_uses_the_same_expiry_decision_as_retention(self):
        first = _alert("sh600001")
        oldest = _alert("sh600002")
        with patch.object(sf_config, "ALERT_STORE_MAX_CODES", 1), \
             patch.object(sf_config, "_session_is_past", side_effect=[False, False, True, False]), \
             patch.object(sf_config, "_record_store_prune") as record:
            kept, dropped = sf_config._prune_store([], [first, oldest])  # discipline-ok(R5): 注入判定变化以覆盖裁剪事务。
        self.assertEqual(kept, [first])
        self.assertEqual(dropped, 1)
        self.assertEqual(record.call_args.args, (1, ["sh600002"]))


class WindowAdoptsTargetsPerCode(SFCase):
    """主窗这一侧：切股之后内存与面板显示的是新股票的条目，切回来还在。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="sf_alerts_win_")
        self.cfg = os.path.join(self.tmp, "config.json")
        self.bak = os.path.join(self.tmp, "config.backup.json")
        for p in (
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 224, "glass")),
            patch.object(m, "save_config", lambda *a, **k: True),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m, "CONFIG_PATH", self.cfg, create=True),
            patch.object(sf_config, "CONFIG_PATH", self.cfg),
            patch.object(sf_config, "CONFIG_BACKUP_PATH", self.bak),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_force_fetch", lambda self: None),
            patch.object(m.FloatWindow, "_reassert_top", lambda self: None),
            patch.object(m.FloatWindow, "_sync_glass_frame", lambda self, **k: None),
            patch.object(m.FloatWindow, "_request_notify_auth", lambda self: None),
        ):
            self.track_patch(p)
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        self.win = m.FloatWindow()
        self.track_widget(self.win)

    def _seed_legacy_store(self):
        """按 ≤1.4.58 写出来的形状铺一份存储：顶层有 code，条目里**没有** code。"""
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "code": "sh600519",
                       "alerts": [{"target": 1600.0 + i, "direction": "below",
                                   "session_date": "2099-12-31", "state": "pending"}
                                  for i in range(3)]}, f)

    def test_switch_keeps_the_other_stocks_targets_and_restores_them(self):
        self.win.code = "sh600519"
        # 为什么要看内部状态：切股之后「窗内显示的是不是新股票的条目」没有别的公开出口，
        # 而 _persist_alerts 本身就是这轮要测的写入入口（面板侧另有 target_panel 用例）。
        self.win._alerts = [_alert("sh600519", 70.0)]  # discipline-ok(R5): 被测的内存态条目清单
        self.assertTrue(self.win._persist_alerts(self.win._alerts))  # discipline-ok(R5): 写入入口本身
        # 切走：不许删掉上面那条
        self.assertTrue(self.win.apply_stock("sz000001"))
        self.assertEqual(self.win._alerts, [], "新股票自己没有目标价，就该是空的")  # discipline-ok(R5): 同上
        self.assertEqual(len(sf_config.load_alerts("sh600519")), 1,
                         "切股把上一只的目标价删了 —— S4 的原始症状")
        # 切回来：接得上
        self.assertTrue(self.win.apply_stock("sh600519"))
        self.assertEqual([a["target"] for a in self.win._alerts], [70.0],  # discipline-ok(R5): 同上
                         "切回来必须看得见自己设过的那条")

    def test_upgrade_legacy_entries_never_move_to_another_stock(self):
        """v1.4.60（复核 F1）：升级带来的"没有 code 的旧条目"属于顶层那只股票。

        这条走的是用户的真实动作序列，因为 sf_config 层那几条测不到它：
          升级后茅台存储里 3 条无主目标价 → 切到平安银行（面板上居然显示成它的）
          → 在平安银行下设 1 条 → 切回茅台，3 条一条不剩（它们被盖章成了平安银行的，
          期间还会拿平安银行的股价去判茅台的价位、发茅台的提醒）。
        对外说明的第一句就是"切换股票后原股目标价保留"，所以这一条是必须修。
        """
        self._seed_legacy_store()
        self.win.code = "sh600519"
        self.win._adopt_targets_for("sh600519")   # discipline-ok(R5): 这是本轮被测的接管入口本身，没有别的公开路径能触发它
        self.assertEqual([a["target"] for a in self.win._alerts],          # discipline-ok(R5): 被测的内存态条目清单
                         [1600.0, 1601.0, 1602.0], "升级后茅台自己的 3 条必须看得见")
        self.assertTrue(self.win.apply_stock("sz000001"))
        self.assertEqual(self.win._alerts, [],                              # discipline-ok(R5): 同上
                         "平安银行读到了茅台的旧条目 —— 面板会把它们显示成自己的")
        # 用户在平安银行下设一条：这一步在修复前会把茅台那 3 条一起改写成平安银行的
        self.assertTrue(self.win._persist_alerts(                           # discipline-ok(R5): 写入入口本身
            [_alert("sz000001", 11.5)]))
        self.assertTrue(self.win.apply_stock("sh600519"))
        self.assertEqual([a["target"] for a in self.win._alerts],           # discipline-ok(R5): 被测的内存态条目清单
                         [1600.0, 1601.0, 1602.0],
                         "切回茅台必须还看得见自己那 3 条（修复后它们归属茅台，不是没主）")

    def test_disabled_menu_items_use_the_single_gray_token(self):
        """v1.4.60（复核 F6）：菜单里"点不动的那几行"要跟着提亮后的灰走。

        本轮把次要文字灰从 #6b7280（对纯黑只有 4.34:1，低于小字 4.5:1）提亮到
        FG_DIM_HEX，唯独右键菜单的 :disabled 还手写旧值 —— 用户看得见的"菜单里那几行
        淡字难读"就没被修掉。判据取 Qt 真正生效的样式表文本（公开属性 styleSheet()），
        不读源码：改天有人把颜色挪地方也算通过，写回裸 hex 才红。
        """
        from PyQt6.QtGui import QContextMenuEvent
        from PyQt6.QtWidgets import QMenu
        from PyQt6.QtCore import QPoint
        import sf_constants
        with patch.object(QMenu, "popup", lambda *a, **k: None):
            self.win.contextMenuEvent(QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, QPoint(10, 10), QPoint(10, 10)))
        menu = self.win._ctx_menu            # discipline-ok(R5): popup() 被桩掉，菜单对象只挂在这个私有引用上（存量用例同口径）
        css = menu.styleSheet()
        self.assertIn("QMenu::item:disabled", css, "样式表结构变了，这条判据要跟着改")
        self.assertNotIn("#6b7280", css,
                         "禁用项还写着被判定不达标的旧灰 —— 本轮提亮漏了最后一处")
        self.assertIn(sf_constants.FG_DIM_HEX, css, "次要文字灰只有一个来源 sf_constants")

    def test_apply_stock_no_longer_asks_or_clears(self):
        """确认框撤了：没有东西被删，弹框只是噪音（本 app 不许加解释性弹窗）。"""
        self.win.code = "sh600519"
        self.win._alerts = [_alert("sh600519", 70.0)]  # discipline-ok(R5): 被测的内存态条目清单
        self.assertTrue(self.win._persist_alerts(self.win._alerts))  # discipline-ok(R5): 写入入口本身
        import sf_system_alert as _sa
        with patch.object(_sa, "show_alert") as alert_mod:
            self.assertTrue(self.win.apply_stock("sz000001"))
        self.assertEqual(alert_mod.show_alert.call_count, 0)
        self.assertEqual(len(sf_config.load_alerts("sh600519")), 1)


if __name__ == "__main__":
    unittest.main()
