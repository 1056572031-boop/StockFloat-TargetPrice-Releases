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
